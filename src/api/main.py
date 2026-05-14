"""
Intelligence Dashboard API.

Lifespan: initialises asyncpg pool + Redis on startup, tears them down on shutdown.
Serves templates/dashboard.html at GET / and exposes JSON endpoints + a live WebSocket.
"""

import asyncio
import copy
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import redis.asyncio as redis_async
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from src.api import queries
from src.api.terminal_snapshot import build_terminal_snapshot, load_recent_log_entries
from src.api.ws_bridge import WSBridge
from src.config import settings
from src.control.killswitch import get_killswitch
from src.control.runtime_config import (
    ALLOWED_KEYS as RUNTIME_CONFIG_ALLOWED_KEYS,
)
from src.control.runtime_config import (
    BOUNDS as RUNTIME_CONFIG_BOUNDS,
)
from src.control.runtime_config import (
    get_runtime_config,
    init_runtime_config,
)
from src.engine.neural_readiness import ReadinessInputs, build_neural_readiness_snapshot
from src.engine.readiness_persistence import (
    load_recent_persisted_transitions,
    persist_readiness_snapshot,
)
from src.logging_setup import configure_logging
from src.monitoring.metrics import export_latest as export_metrics_latest
from src.registry.falcon_client import FalconClient

# ---------------------------------------------------------------------------
# Module-level singletons (populated in lifespan)
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None
_redis: redis_async.Redis | None = None
_bridge = WSBridge()

# Falcon probe state — updated at most once per 60 seconds
_falcon_probe: dict = {"ok": False, "last_checked": 0.0, "error": None}
_falcon_probe_task: asyncio.Task | None = None
_health_cache: dict = {"data": None, "last_checked": 0.0}
_health_lock = asyncio.Lock()
_live_snapshot_cache: dict = {"data": None, "last_built": 0.0}
_live_snapshot_lock = asyncio.Lock()

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "templates" / "dashboard.html"
TEMPLATE_V2_PATH = Path(__file__).parent.parent.parent / "templates" / "dashboard_v2.html"
STATIC_DIR = Path(__file__).parent.parent.parent / "static"
STATS_PUSH_INTERVAL_S = 1.0  # how often to push live stats over WebSocket
HEALTH_CACHE_TTL_S = 5.0
LIVE_SNAPSHOT_TTL_S = 5.0
TERMINAL_SNAPSHOT_TTL_S = 5.0
# Background rebuilder cadence. Each cycle calls _get_terminal_snapshot(force=True)
# so the cache stays warm. Aligned with V1 client poll interval (5s) — by the
# time the V1 client polls, a fresh snapshot is already in cache.
SNAPSHOT_REBUILDER_INTERVAL_S = 5.0
# If the background rebuilder hasn't produced a fresh snapshot within this
# many seconds, the snapshot endpoint logs a warning and falls back to a
# synchronous rebuild (slow but correct). 30s = 6x the normal cadence —
# allows for one or two transient stalls without flipping to fallback.
SNAPSHOT_STALENESS_WARN_S = 30.0
LOG_PATHS = [
    Path("/tmp/polymarket-bot-observer.log"),
    Path(__file__).parent.parent.parent / "orchestrate.log",
]
_terminal_snapshot_cache: dict = {"data": None, "last_built": 0.0}
_terminal_snapshot_lock = asyncio.Lock()
_api_started_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _redis
    # Configure loguru first so every log line during boot uses the env-driven
    # level/format/file sink rather than loguru's default DEBUG-stderr.
    log_level = configure_logging()
    logger.info(f"Dashboard API booting (log_level={log_level})")
    created_pool = False
    created_redis = False
    if _pool is None:
        # Pool sized for the new asyncio.gather() in queries.overview()
        # (11 parallel sub-queries) + concurrent V1+V2 clients. The
        # observed saturation at the old max=10 happened with 6 active
        # queries from a single live-summary rebuild — bumping to 20
        # gives ~2x headroom.
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=4,
            max_size=20,
        )
        created_pool = True
        # CRITICAL: also expose the pool to src.database.connection so that
        # killswitch / mempool wallet_index / any other module calling
        # `from src.database.connection import get_db` sees a live pool.
        # Without this hop, those modules see `_pool=None` and raise
        # "DB pool not initialized. Call initialize_pool() first." —
        # which is the root cause of the `infra_failure:RuntimeError`
        # killswitch state observed in production.
        import src.database.connection as _db_conn
        _db_conn._pool = _pool
    if _redis is None:
        _redis = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
        created_redis = True
    _bridge.attach_redis(_redis)
    # Bind redis to the global killswitch service so writes propagate to all
    # workers via the shared Redis cache.
    get_killswitch(redis_client=_redis)
    # Initialise runtime config (Risk & Config Option 2: mutable params).
    init_runtime_config(redis_client=_redis)
    # Phase 2 Task D: push-invalidate local cache on runtime_config:changed.
    # The API publishes here from `set_overrides`; subscribing here as
    # well means a curl-driven override picked up by another API replica
    # invalidates this one immediately rather than 30s later.
    try:
        await get_runtime_config().start_pubsub()
    except Exception as exc:
        logger.warning(f"runtime_config pub/sub init failed: {exc}")
    await _bridge.start()
    _schedule_falcon_probe()
    push_task = asyncio.create_task(_stats_push_loop())
    # Background snapshot rebuilder — keeps `/api/v1/live-summary` cache
    # always-warm so cold start is 0ms instead of 30s. See _snapshot_rebuilder_loop
    # for design rationale.
    snapshot_task = asyncio.create_task(_snapshot_rebuilder_loop(), name="snapshot-rebuilder")
    logger.info("Dashboard API started")
    yield
    push_task.cancel()
    snapshot_task.cancel()
    try:
        await snapshot_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await get_runtime_config().stop_pubsub()
    except Exception:
        pass
    await _bridge.stop()
    if created_pool and _pool:
        await _pool.close()
        _pool = None
        # Unbind from src.database.connection so a future restart can
        # cleanly re-init.
        import src.database.connection as _db_conn
        _db_conn._pool = None
    if created_redis and _redis:
        await _redis.aclose()
        _redis = None
    logger.info("Dashboard API stopped")


app = FastAPI(title="Polymarket Intelligence Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _conn():
    """Acquire a connection from the pool."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool.acquire()


async def _probe_falcon() -> bool:
    """Probe Falcon API with a minimal query. Result cached for 60s."""
    import ssl

    import aiohttp

    global _falcon_probe
    now = time.monotonic()
    if now - _falcon_probe["last_checked"] < 60.0:
        return _falcon_probe["ok"]
    if not settings.FALCON_API_KEY:
        _falcon_probe = {
            "ok": False,
            "last_checked": now,
            "error": "FALCON_API_KEY missing from runtime environment",
        }
        return False
    try:
        # Build an SSL context that falls back to certifi if the system store fails
        try:
            import certifi

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = ssl.create_default_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        client = FalconClient(redis_client=_redis)
        client._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {settings.FALCON_API_KEY}"},
            connector=connector,
        )
        try:
            await asyncio.wait_for(
                client.query(
                    581,
                    {"proxy_wallet": "0xabc", "window_days": "7"},
                    limit=1,
                ),
                timeout=5.0,
            )
        finally:
            await client._session.close()
        _falcon_probe = {"ok": True, "last_checked": now, "error": None}
        logger.debug("Falcon probe: OK")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        _falcon_probe = {"ok": False, "last_checked": now, "error": err[:150]}
        logger.warning(f"Falcon probe failed: {err}")
    return _falcon_probe["ok"]


def _schedule_falcon_probe() -> None:
    global _falcon_probe_task
    now = time.monotonic()
    if now - _falcon_probe["last_checked"] < 60.0:
        return
    if _falcon_probe_task is not None and not _falcon_probe_task.done():
        return
    try:
        _falcon_probe_task = asyncio.create_task(_probe_falcon())
    except RuntimeError:
        # No running loop yet (for example during import-time tests)
        _falcon_probe_task = None


async def _health_checks(force: bool = False) -> dict:
    now = time.monotonic()
    cached = _health_cache.get("data")
    if (
        not force
        and cached is not None
        and now - float(_health_cache.get("last_checked", 0.0) or 0.0) < HEALTH_CACHE_TTL_S
    ):
        _schedule_falcon_probe()
        return copy.deepcopy(cached)

    async with _health_lock:
        now = time.monotonic()
        cached = _health_cache.get("data")
        if (
            not force
            and cached is not None
            and now - float(_health_cache.get("last_checked", 0.0) or 0.0) < HEALTH_CACHE_TTL_S
        ):
            _schedule_falcon_probe()
            return copy.deepcopy(cached)

        db_ok = False
        redis_ok = False
        last_trade_age_s: float | None = None
        websocket_connected = False
        last_message_age_s: float | None = None
        book_age_p95_s: float | None = None
        fee_snapshot_coverage_pct: float | None = None
        token_map_coverage_pct: float | None = None
        rejected_signals_1h: dict[str, int] = {}
        paper_rejections_1h: dict[str, int] = {}
        fee_snapshot_coverage_source: str | None = None
        data_accumulation_counts: dict[str, int] = {}
        pipeline_stage_health: dict = {}
        ws_msgs_last_minute: int = 0

        try:
            async with _pool.acquire() as conn:
                last = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(time))) FROM trades_observed"
                )
                db_quality = await _db_data_quality_snapshot(conn)
                try:
                    pipeline_stage_health = await _db_pipeline_stage_health_snapshot(conn)
                except Exception as exc:
                    logger.warning(f"Pipeline stage health check failed: {exc}")
            db_ok = True
            last_trade_age_s = float(last) if last is not None else None
            data_accumulation_counts = db_quality.get("counts", {})
        except Exception as e:
            logger.warning(f"DB health check failed: {e}")
            db_quality = {}

        try:
            await _redis.ping()
            redis_ok = True
            last_message_ts = await _redis.get("ws:market:last_message_ts")
            if last_message_ts is not None:
                last_message_age_s = max(0.0, time.time() - float(last_message_ts))
                websocket_connected = last_message_age_s <= 30.0
            # Real WS throughput — read the previous minute bucket (the
            # current one is still being written, so it under-counts).
            try:
                prev_minute = int(time.time() // 60) - 1
                ws_msgs = await _redis.get(f"ws:msgs:minute:{prev_minute}")
                ws_msgs_last_minute = int(ws_msgs) if ws_msgs is not None else 0
            except Exception:
                ws_msgs_last_minute = 0
            book_age = await _redis.get("metrics:book_age_p95_s")
            fee_coverage = await _redis.get("metrics:fee_snapshot_coverage_pct")
            token_coverage = await _redis.get("metrics:token_map_coverage_pct")
            rejected = await _redis.hgetall("signals:rejected:1h")
            paper_rejected = await _redis.hgetall("paper:rejections:1h")
            book_age_p95_s = float(book_age) if book_age is not None else None
            fee_snapshot_coverage_pct = float(fee_coverage) if fee_coverage is not None else None
            token_map_coverage_pct = float(token_coverage) if token_coverage is not None else None
            rejected_signals_1h = {
                str(reason): int(count) for reason, count in dict(rejected).items()
            }
            paper_rejections_1h = {
                str(reason): int(count) for reason, count in dict(paper_rejected).items()
            }
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")

        if db_quality:
            if fee_snapshot_coverage_pct is None:
                fee_snapshot_coverage_pct = db_quality.get("fee_snapshot_coverage_pct")
                fee_snapshot_coverage_source = db_quality.get("fee_snapshot_coverage_source")
            else:
                fee_snapshot_coverage_source = "redis"
            if token_map_coverage_pct is None:
                token_map_coverage_pct = db_quality.get("token_map_coverage_pct")

        pipeline_stage_health["signal_rejections_1h"] = rejected_signals_1h
        pipeline_stage_health["paper_rejections_1h"] = paper_rejections_1h
        pipeline_stage_health["stage_status"] = {
            "book_capture": (
                "healthy"
                if book_age_p95_s is not None
                and int(pipeline_stage_health.get("book_quality_snapshots_5m") or 0) > 0
                else "blocked"
            ),
            "readiness_persistence": (
                "active"
                if int(pipeline_stage_health.get("market_belief_states") or 0) > 0
                else "empty"
            ),
            "signal_gate": "active" if rejected_signals_1h else "idle",
            "paper_execution": "active" if paper_rejections_1h else "idle",
        }

        _schedule_falcon_probe()
        data = {
            "db": db_ok,
            "redis": redis_ok,
            "falcon": bool(_falcon_probe.get("ok", False)),
            "falcon_error": _falcon_probe.get("error"),
            "websocket": websocket_connected,
            "websocket_connected": websocket_connected,
            "last_message_age_s": last_message_age_s,
            "ws_messages_last_minute": ws_msgs_last_minute,
            "book_age_p95_s": book_age_p95_s,
            "fee_snapshot_coverage_pct": fee_snapshot_coverage_pct,
            "fee_snapshot_coverage_source": fee_snapshot_coverage_source,
            "token_map_coverage_pct": token_map_coverage_pct,
            "data_accumulation_counts": data_accumulation_counts,
            "rejected_signals_1h": rejected_signals_1h,
            "paper_rejections_1h": paper_rejections_1h,
            "pipeline_stage_health": pipeline_stage_health,
            "last_trade_age_s": last_trade_age_s,
        }
        _health_cache["data"] = copy.deepcopy(data)
        _health_cache["last_checked"] = now
        return data


async def _db_data_quality_snapshot(conn) -> dict:
    row = await conn.fetchrow(
        """
        WITH market_counts AS (
            SELECT
                COUNT(*) AS total_markets,
                COUNT(*) FILTER (
                    WHERE NULLIF(token_yes, '') IS NOT NULL
                      AND NULLIF(token_no, '') IS NOT NULL
                ) AS token_mapped_markets,
                (
                    COUNT(token_yes) FILTER (WHERE NULLIF(token_yes, '') IS NOT NULL)
                    + COUNT(token_no) FILTER (WHERE NULLIF(token_no, '') IS NOT NULL)
                ) AS mapped_tokens,
                COUNT(*) FILTER (WHERE fee_rate_pct IS NOT NULL) AS legacy_fee_markets
            FROM markets
        ),
        fee_snapshot_counts AS (
            SELECT COUNT(DISTINCT (market_id, token_id)) AS fee_snapshot_tokens
            FROM fee_snapshots
        )
        SELECT
            market_counts.total_markets,
            market_counts.token_mapped_markets,
            market_counts.mapped_tokens,
            market_counts.legacy_fee_markets,
            fee_snapshot_counts.fee_snapshot_tokens
        FROM market_counts, fee_snapshot_counts
        """
    )
    if not row:
        return {}

    def _int(name: str) -> int:
        try:
            return int(row[name] or 0)
        except Exception:
            return 0

    total_markets = _int("total_markets")
    token_mapped_markets = _int("token_mapped_markets")
    mapped_tokens = _int("mapped_tokens")
    legacy_fee_markets = _int("legacy_fee_markets")
    fee_snapshot_tokens = _int("fee_snapshot_tokens")
    token_coverage = (
        round(token_mapped_markets / total_markets * 100, 2) if total_markets else None
    )
    if fee_snapshot_tokens > 0 and mapped_tokens > 0:
        fee_coverage = round(min(fee_snapshot_tokens / mapped_tokens * 100, 100.0), 2)
        fee_source = "fee_snapshots"
    else:
        fee_coverage = round(legacy_fee_markets / total_markets * 100, 2) if total_markets else None
        fee_source = "markets.fee_rate_pct" if fee_coverage is not None else None

    return {
        "token_map_coverage_pct": token_coverage,
        "fee_snapshot_coverage_pct": fee_coverage,
        "fee_snapshot_coverage_source": fee_source,
        "counts": {
            "total_markets": total_markets,
            "token_mapped_markets": token_mapped_markets,
            "mapped_tokens": mapped_tokens,
            "legacy_fee_markets": legacy_fee_markets,
            "fee_snapshot_tokens": fee_snapshot_tokens,
        },
    }


async def _db_pipeline_stage_health_snapshot(conn) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM market_belief_states) AS market_belief_states,
            (SELECT COUNT(*) FROM decision_state_transitions
             WHERE created_at >= NOW() - INTERVAL '1 hour') AS decision_transitions_1h,
            (SELECT COUNT(*) FROM book_quality_snapshots
             WHERE observed_at >= NOW() - INTERVAL '5 minutes') AS book_quality_snapshots_5m,
            (SELECT EXTRACT(EPOCH FROM (NOW() - MAX(observed_at)))
             FROM book_quality_snapshots) AS last_book_snapshot_age_s,
            (SELECT COUNT(*) FROM signal_audits
             WHERE created_at >= NOW() - INTERVAL '1 hour') AS signal_audits_1h
        """
    )

    def _int(name: str) -> int:
        try:
            return int(row[name] or 0)
        except Exception:
            return 0

    def _float(name: str) -> float | None:
        try:
            value = row[name]
            return float(value) if value is not None else None
        except Exception:
            return None

    return {
        "market_belief_states": _int("market_belief_states"),
        "decision_transitions_1h": _int("decision_transitions_1h"),
        "book_quality_snapshots_5m": _int("book_quality_snapshots_5m"),
        "last_book_snapshot_age_s": _float("last_book_snapshot_age_s"),
        "signal_audits_1h": _int("signal_audits_1h"),
    }


async def _fetch_overview_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.overview(conn, redis_client=_redis)


async def _fetch_ml_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.ml_summary(conn)


async def _fetch_system_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.system_status(conn)


async def _fetch_positions_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.positions(conn)


async def _fetch_positions_live_snapshot() -> list[dict]:
    async with _pool.acquire() as conn:
        return await queries.open_positions_with_prices(conn, _redis)


async def _fetch_decisions_snapshot(limit: int = 60) -> list[dict]:
    async with _pool.acquire() as conn:
        return await queries.decisions(conn, limit=limit, offset=0)


async def _fetch_decisions_stats_snapshot(window_hours: int = 24) -> dict:
    async with _pool.acquire() as conn:
        return await queries.decisions_stats(conn, window_hours=window_hours)


async def _fetch_risk_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.risk(conn)


async def _fetch_activation_snapshot() -> list[dict]:
    async with _pool.acquire() as conn:
        return await queries.activation_queue(conn)


async def _fetch_data_quality_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.data_quality(conn, redis_client=_redis)


async def _fetch_market_scanner_rows(limit: int = 60) -> list[dict]:
    async with _pool.acquire() as conn:
        return await queries.market_scanner_rows(conn, limit=limit)


async def _fetch_recent_observed_trades(limit: int = 60) -> list[dict]:
    async with _pool.acquire() as conn:
        return await queries.recent_observed_trades(conn, limit=limit)


async def _fetch_alpha_extras() -> dict:
    """ALPHA TERMINAL v2 — 24h timeline + Next Signal ETA + ML totals."""
    async with _pool.acquire() as conn:
        return await queries.alpha_extras(conn)


async def _fetch_wallet_graph() -> dict:
    """WALLET GRAPH — nodes + edges for force-directed visualisation."""
    async with _pool.acquire() as conn:
        return await queries.wallet_graph(conn)


async def _fetch_rejections_breakdown() -> dict:
    """ML PROGRESSION — last-hour SKIP reasons grouped."""
    async with _pool.acquire() as conn:
        return await queries.decision_rejections_breakdown(conn, hours=1)


async def _fetch_equity_curve_v2() -> dict:
    """LIVE PORTFOLIO — equity series + by-leader / by-strategy breakdown."""
    async with _pool.acquire() as conn:
        return await queries.equity_curve(conn)


async def _get_live_snapshot(force: bool = False) -> dict:
    now = time.monotonic()
    cached = _live_snapshot_cache.get("data")
    if (
        not force
        and cached is not None
        and now - float(_live_snapshot_cache.get("last_built", 0.0) or 0.0) < LIVE_SNAPSHOT_TTL_S
    ):
        return copy.deepcopy(cached)

    async with _live_snapshot_lock:
        now = time.monotonic()
        cached = _live_snapshot_cache.get("data")
        if (
            not force
            and cached is not None
            and now - float(_live_snapshot_cache.get("last_built", 0.0) or 0.0)
            < LIVE_SNAPSHOT_TTL_S
        ):
            return copy.deepcopy(cached)

        overview_data, ml_data, health_data = await asyncio.gather(
            _fetch_overview_snapshot(),
            _fetch_ml_snapshot(),
            _health_checks(),
        )
        snapshot = dict(overview_data)
        snapshot["ml"] = ml_data
        snapshot["health"] = health_data
        _live_snapshot_cache["data"] = copy.deepcopy(snapshot)
        _live_snapshot_cache["last_built"] = now
        return snapshot


async def _get_terminal_snapshot(force: bool = False) -> dict:
    """Read the cached terminal snapshot, or rebuild if needed.

    DESIGN POST-PHASE-2 (background worker introduction):
    The background `_snapshot_rebuilder_loop` holds the lock for the
    entire ~30s rebuild duration. The previous double-checked-locking
    pattern made every reader wait on the lock — so readers timed out
    even though cache was present. The new design separates concerns:

      * Readers (`force=False`): NEVER block. If cache is present
        (even stale), return it. The background worker will refresh it.
      * Background worker (`force=True`): builds with the lock so two
        rebuilders can't run in parallel.
      * Cold start fallback (`force=False`, no cache): if no cache
        exists at all, block on the lock to build the first snapshot
        synchronously — but only on the very first request after
        process start.

    This means a reader may see snapshot up to ~30s stale during a
    slow rebuild — much better than blocking 30s.
    """
    now = time.monotonic()
    cached = _terminal_snapshot_cache.get("data")
    last_built = float(_terminal_snapshot_cache.get("last_built", 0.0) or 0.0)

    # Readers always return whatever cache exists — fresh or stale.
    if not force and cached is not None:
        return copy.deepcopy(cached)

    # No cache + not forced: cold start, must build synchronously.
    # `force=True` (background worker): also reach the rebuild path.
    async with _terminal_snapshot_lock:
        now = time.monotonic()
        cached = _terminal_snapshot_cache.get("data")
        # Double-checked: another rebuilder may have just finished
        # while we were waiting on the lock.
        if (
            not force
            and cached is not None
            and now - float(_terminal_snapshot_cache.get("last_built", 0.0) or 0.0)
            < TERMINAL_SNAPSHOT_TTL_S
        ):
            return copy.deepcopy(cached)

        build_started = time.perf_counter()
        results = await asyncio.gather(
            _fetch_overview_snapshot(),
            _fetch_ml_snapshot(),
            _fetch_system_snapshot(),
            _fetch_positions_live_snapshot(),
            _fetch_positions_snapshot(),
            _fetch_decisions_snapshot(),
            _fetch_decisions_stats_snapshot(),
            _fetch_risk_snapshot(),
            _fetch_activation_snapshot(),
            _fetch_data_quality_snapshot(),
            _health_checks(),
            _fetch_market_scanner_rows(),
            _fetch_recent_observed_trades(),
            _fetch_alpha_extras(),
            _fetch_wallet_graph(),
            _fetch_rejections_breakdown(),
            _fetch_equity_curve_v2(),
            return_exceptions=True,
        )
        (
            overview_data,
            ml_data,
            system_data,
            positions_live_data,
            positions_data,
            decisions_data,
            decision_stats_data,
            risk_data,
            activation_data,
            data_quality_data,
            health_data,
            market_rows,
            observed_trades,
            alpha_extras_data,
            wallet_graph_data,
            rejections_data,
            equity_curve_data,
        ) = results

        defaults = (
            {},
            {},
            {},
            [],
            {"open": [], "closed": [], "stats": {}},
            [],
            {"totals": {}},
            {},
            [],
            {},
            {},
            [],
            [],
            {"timeline": [], "follow_ready": [], "totals": {}},
            {"nodes": [], "edges": [], "stats": {}},
            {"total": 0, "breakdown": []},
            {"series": [], "by_leader": [], "by_strategy": []},
        )
        names = (
            "overview",
            "ml",
            "system",
            "positions_live",
            "positions",
            "decisions",
            "decision_stats",
            "risk",
            "activation",
            "data_quality",
            "health",
            "market_rows",
            "observed_trades",
            "alpha_extras",
            "wallet_graph",
            "rejections",
            "equity_curve",
        )
        normalized: list = []
        for name, value, default in zip(
            names,
            (
                overview_data,
                ml_data,
                system_data,
                positions_live_data,
                positions_data,
                decisions_data,
                decision_stats_data,
                risk_data,
                activation_data,
                data_quality_data,
                health_data,
                market_rows,
                observed_trades,
                alpha_extras_data,
                wallet_graph_data,
                rejections_data,
                equity_curve_data,
            ),
            defaults,
        ):
            if isinstance(value, Exception):
                logger.warning(f"Terminal snapshot section failed: {name}: {value}")
                normalized.append(copy.deepcopy(default))
            else:
                normalized.append(value)

        (
            overview_data,
            ml_data,
            system_data,
            positions_live_data,
            positions_data,
            decisions_data,
            decision_stats_data,
            risk_data,
            activation_data,
            data_quality_data,
            health_data,
            market_rows,
            observed_trades,
            alpha_extras_data,
            wallet_graph_data,
            rejections_data,
            equity_curve_data,
        ) = normalized
        readiness_data = build_neural_readiness_snapshot(
            ReadinessInputs(
                health=health_data,
                activation=activation_data,
                risk=risk_data,
                ml=ml_data,
            )
        )
        runtime = {
            "started_at": _api_started_at.isoformat(),
            "uptime_seconds": int((datetime.now(timezone.utc) - _api_started_at).total_seconds()),
            "cycle_latency_ms": 0.0,
            "last_command_at": None,
            # Killswitch + risk config writes go through real endpoints
            # (api_control_killswitch, api_risk_update). The dashboard uses
            # these flags to gate the editable / disabled state of its inputs.
            "control_available": True,
            "config_mutable": True,
        }
        # Load the current effective runtime config so the snapshot exposes the
        # actual live values (defaults merged with persisted Redis overrides).
        try:
            runtime_overrides = await get_runtime_config().effective()
        except Exception as exc:
            logger.warning(f"runtime_config load failed: {exc}")
            runtime_overrides = None
        logs = load_recent_log_entries(LOG_PATHS, limit=120)
        snapshot = build_terminal_snapshot(
            overview=overview_data,
            ml=ml_data,
            system=system_data,
            positions_live=positions_live_data,
            positions=positions_data,
            decisions=decisions_data,
            decision_stats=decision_stats_data,
            risk=risk_data,
            readiness=readiness_data,
            data_quality=data_quality_data,
            health=health_data,
            market_rows=market_rows,
            observed_trades=observed_trades,
            alpha_extras=alpha_extras_data,
            wallet_graph=wallet_graph_data,
            rejections=rejections_data,
            equity_curve=equity_curve_data,
            runtime=runtime,
            logs=logs,
            runtime_overrides=runtime_overrides,
        )
        build_ms = round((time.perf_counter() - build_started) * 1000, 2)
        snapshot.setdefault("bot", {})["cycle_latency_ms"] = build_ms

        _terminal_snapshot_cache["data"] = copy.deepcopy(snapshot)
        _terminal_snapshot_cache["last_built"] = now
        return snapshot


async def _stats_push_loop() -> None:
    """Push fresh overview stats to all connected WS clients every STATS_PUSH_INTERVAL_S."""
    while True:
        await asyncio.sleep(STATS_PUSH_INTERVAL_S)
        if not _bridge.has_connections:
            continue
        try:
            data = await _get_terminal_snapshot()
            await _bridge.broadcast({"type": "tick", "payload": data})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Stats push loop error: {exc}")


# Stats for `/api/snapshot/health` — telemetry of the background rebuilder.
# Updated by `_snapshot_rebuilder_loop` each cycle so operators can see
# whether the snapshot pipeline is keeping up.
_snapshot_rebuilder_stats: dict = {
    "last_completed_at": None,      # monotonic time of last successful rebuild
    "last_duration_ms": None,        # how long the last rebuild took
    "consecutive_failures": 0,       # incremented on exception, reset on success
    "total_rebuilds": 0,             # cumulative successful rebuilds
    "total_failures": 0,             # cumulative failed rebuilds
    "last_error": None,              # last exception repr (for /api/snapshot/health)
}


async def _snapshot_rebuilder_loop() -> None:
    """Background loop that keeps `_terminal_snapshot_cache` warm.

    DESIGN RATIONALE
    ----------------
    Before this loop existed, `_get_terminal_snapshot()` was invoked
    lazily on every incoming request whose cache had expired. With a
    TTL of 1s (5s post-Phase-1) and a rebuild cost of ~15-30s, this
    meant:
      * Every V1 client polling /api/v1/live-summary triggered a
        rebuild if the cache had expired between polls.
      * Concurrent rebuilds were serialised by `_terminal_snapshot_lock`
        so the second client waited for the first to finish (15-30s).
      * Cold start (first request after restart) was always ~30s.
    By moving rebuild to a single background task running every
    SNAPSHOT_REBUILDER_INTERVAL_S, we get:
      * 1 rebuild per period — predictable DB load.
      * Cold start ≈ 0ms (request reads cache).
      * No request thread ever waits on rebuild (cache always fresh
        or, in the worst case, slightly stale within the TTL window).
      * Backpressure isolated to one task; clients never feel it.

    FAILURE HANDLING
    ----------------
    If a rebuild fails (exception inside `_get_terminal_snapshot`), we
    record it in stats and continue the loop. The cache keeps the
    last-known-good value so the endpoint serves stale data rather
    than 500s. After SNAPSHOT_STALENESS_WARN_S of staleness,
    `/api/snapshot/health` flips to warning and the next request that
    hits `/api/v1/live-summary` may trigger a synchronous fallback
    rebuild (still capped by the lock).
    """
    logger.info(
        f"Snapshot rebuilder loop starting "
        f"(interval={SNAPSHOT_REBUILDER_INTERVAL_S}s, "
        f"staleness_warn={SNAPSHOT_STALENESS_WARN_S}s)"
    )
    while True:
        try:
            t_start = time.perf_counter()
            await _get_terminal_snapshot(force=True)
            duration_ms = round((time.perf_counter() - t_start) * 1000, 2)
            _snapshot_rebuilder_stats["last_completed_at"] = time.monotonic()
            _snapshot_rebuilder_stats["last_duration_ms"] = duration_ms
            _snapshot_rebuilder_stats["consecutive_failures"] = 0
            _snapshot_rebuilder_stats["total_rebuilds"] += 1
            _snapshot_rebuilder_stats["last_error"] = None
        except asyncio.CancelledError:
            logger.info("Snapshot rebuilder loop cancelled")
            break
        except Exception as exc:  # pragma: no cover — defensive top-level
            _snapshot_rebuilder_stats["consecutive_failures"] += 1
            _snapshot_rebuilder_stats["total_failures"] += 1
            _snapshot_rebuilder_stats["last_error"] = repr(exc)
            logger.warning(
                f"Snapshot rebuilder failed (consecutive={_snapshot_rebuilder_stats['consecutive_failures']}): {exc}"
            )
        # Sleep regardless — even on failure, don't tight-loop the DB.
        try:
            await asyncio.sleep(SNAPSHOT_REBUILDER_INTERVAL_S)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return HTMLResponse(content=TEMPLATE_PATH.read_text())


def _v2_fmt_uptime(seconds: int | float | None) -> str:
    """Format uptime seconds as e.g. '2d 4h' / '6h 32m' / '12m' / '45s'.

    Mirrors the V1 dashboard's `fmtAge()` helper so the V2 sidebar's
    `bot.uptime_human` rendering is consistent across both UIs.
    """
    s = int(seconds or 0)
    if s <= 0:
        return "—"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


@app.get("/api/overview")
async def api_overview():
    """V2 OVERVIEW + sidebar data source — LEAN VARIANT.

    The V2 dashboard polls this every 3s and (until we add a global
    cache layer in `useApi`) every sub-tab navigation triggers a
    fresh fetch. The endpoint MUST respond in <500ms.

    Implementation history:
    - v1 (commit edd49ee): called `_get_terminal_snapshot()` → 14
      parallel queries → 30s timeouts under load.
    - v2 (commit 8c58fd6): replaced with light parallel COUNT batch
      BUT still called `_get_live_snapshot()` for legacy_keys —
      which transitively triggers `queries.overview()` (a heavy
      activity_feed JOIN + follower_map CTE). Measured 25s/call
      in production under polling load.
    - v3 (this): drop `_get_live_snapshot()` entirely. The legacy
      portfolio fields (capital, equity_curve, total_pnl, etc.) are
      not consumed by V2 — V1 reads them via /api/v1/live-summary.
      The endpoint is now pure V2-shape: 1 SQL batch + 1 Redis GET
      + module-time arithmetic. Target: <100ms warm.
    """
    # --- Single parallel batch for all V2 counts ----------------------- #
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*)::int FROM decision_log
                 WHERE time >= NOW() - INTERVAL '24 hours')                                              AS decisions_24h,
                (SELECT COUNT(*)::int FROM paper_trades WHERE status = 'open')                            AS positions_open,
                (SELECT COUNT(*)::int FROM microstructure_features
                 WHERE bucket_ts >= NOW() - INTERVAL '5 minutes')                                         AS microstructure_recent,
                (SELECT COUNT(*)::int FROM wallet_universe)                                               AS wallet_universe_n,
                (SELECT COUNT(*)::int FROM social_signals
                 WHERE posted_at >= NOW() - INTERVAL '24 hours')                                          AS social_recent,
                (SELECT COUNT(*)::int FROM cross_market_positions)                                        AS crossmarket_recent,
                (SELECT COUNT(*)::int FROM chain_sync_state)                                              AS onchain_recent,
                (SELECT COUNT(*)::int FROM markets WHERE active = TRUE)                                   AS total_markets,
                (SELECT COUNT(DISTINCT (market_id, token_id))::int FROM book_quality_snapshots
                 WHERE observed_at >= NOW() - INTERVAL '15 seconds')                                      AS live_markets,
                (SELECT COALESCE(SUM(pnl_usdc), 0)::float FROM paper_trades WHERE status = 'closed')      AS net_pnl,
                (SELECT
                    COUNT(*) FILTER (WHERE pnl_usdc > 0)::float / NULLIF(COUNT(*), 0)
                 FROM paper_trades WHERE status = 'closed')                                               AS win_rate
            """
        )
    row = dict(row) if row else {}

    # --- Layer statuses ------------------------------------------------- #
    layers = {
        "onchain":     "running" if (row.get("onchain_recent") or 0) > 0       else "gated",
        "cold_tier":   "running" if (row.get("wallet_universe_n") or 0) > 0    else "gated",
        "book_l3":     "running" if (row.get("microstructure_recent") or 0) > 0 else "gated",
        "social":      "running" if (row.get("social_recent") or 0) > 0        else "off",
        "crossmarket": "running" if (row.get("crossmarket_recent") or 0) > 0   else "off",
    }

    # --- Coverage ------------------------------------------------------- #
    live = int(row.get("live_markets") or 0)
    total = int(row.get("total_markets") or 0)
    coverage_pct = round((live / total * 100), 2) if total > 0 else None

    # --- Bot block (light — no terminal snapshot needed) ---------------- #
    health = await _health_checks()
    # Process uptime — computed from module load time (same source the
    # terminal snapshot uses, just inlined to avoid the snapshot cost).
    try:
        uptime_seconds = int((datetime.now(timezone.utc) - _api_started_at).total_seconds())
    except Exception:
        uptime_seconds = 0
    # `_health_checks()` exposes booleans under keys 'db' + 'redis'
    # (not 'database'). Treat the bot as 'running' if both backends
    # respond AND the WS hasn't gone silent (last message < 60s ago).
    db_ok = bool(health.get("db"))
    redis_ok = bool(health.get("redis"))
    ws_lag_s = float(health.get("last_message_age_s") or 0.0)
    ws_healthy = ws_lag_s < 60.0
    bot_status = "running" if (db_ok and redis_ok and ws_healthy) else "stopped"
    bot_block = {
        "status": bot_status,
        "execution_enabled": False,  # paper-only — flipped by ops via killswitch
        "uptime_seconds": uptime_seconds,
        "uptime_human": _v2_fmt_uptime(uptime_seconds),
        "latency_ms": round(ws_lag_s * 1000, 2),
        "killswitch_active": False,
        "paper_only": True,
    }
    # Killswitch via Redis (single GET — fast).
    try:
        if _redis is not None:
            ks = await _redis.get("polymarket:killswitch")
            bot_block["killswitch_active"] = bool(ks and str(ks).strip().lower() not in ("", "0", "false", "off"))
    except Exception:
        pass

    # --- Stats block (V2 shape) ----------------------------------------- #
    stats = {
        "net_pnl":       float(row.get("net_pnl") or 0.0),
        "total_pnl":     float(row.get("net_pnl") or 0.0),
        "win_rate":      float(row.get("win_rate") or 0.0) if row.get("win_rate") is not None else 0.0,
        "positions_open": int(row.get("positions_open") or 0),
        "max_positions": int(getattr(settings, "MAX_CONCURRENT_POSITIONS", 10) or 10),
        "decisions_24h": int(row.get("decisions_24h") or 0),
        "active_markets": live,
    }

    # --- Ingestion block ------------------------------------------------ #
    ingestion = {
        "live_markets": live,
        "total_markets": total,
    }

    return {
        "bot": bot_block,
        "stats": stats,
        "ingestion": ingestion,
        "layers": layers,
        "coverage_pct": coverage_pct,
    }


@app.get("/api/leaders")
async def api_leaders():
    async with _pool.acquire() as conn:
        return await queries.leaders(conn)


@app.get("/api/leaders/{wallet}")
async def api_leader_detail(wallet: str):
    async with _pool.acquire() as conn:
        detail = await queries.leader_detail(conn, wallet)
    if detail is None:
        raise HTTPException(status_code=404, detail="Leader not found")
    return detail


@app.get("/api/wallet/{wallet}/markets")
async def api_wallet_markets(wallet: str, window_days: int = 30, limit: int = 20):
    """Per-wallet market drilldown — list of markets the wallet has traded
    in the last N days, with category, volume, PnL, and a category breakdown."""
    async with _pool.acquire() as conn:
        return await queries.wallet_markets(conn, wallet, window_days=window_days, limit=limit)


@app.get("/api/ml/diagnostics")
async def api_ml_diagnostics():
    """High-signal ML pipeline indicators for tracking development."""
    async with _pool.acquire() as conn:
        return await queries.ml_diagnostics(conn)


@app.get("/api/data-quality/markets")
async def api_data_quality_markets(issue: str, limit: int = 100):
    """Drill-down list of markets / leaders affected by a specific DQ issue.

    issue ∈ {unmapped_tokens, expired_still_active, orphan_market_ids,
             stale_leaders, stale_profiles}
    """
    async with _pool.acquire() as conn:
        return await queries.data_quality_markets(conn, issue=issue, limit=limit)


@app.get("/api/wallet/{wallet}/profile")
async def api_wallet_profile(wallet: str):
    """Full per-wallet profile (categories, accuracy, sizing, edges, decisions)."""
    async with _pool.acquire() as conn:
        result = await queries.wallet_profile(conn, wallet)
    if result is None:
        raise HTTPException(status_code=404, detail="Wallet profile not found")
    return result


@app.get("/api/decision/{decision_id}")
async def api_decision_detail(decision_id: int):
    """Full reasoning panel for a single decision_log row."""
    async with _pool.acquire() as conn:
        result = await queries.decision_detail(conn, decision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return result


@app.get("/api/risk/history")
async def api_risk_history(limit: int = 50):
    """Recent runtime config changes for the Risk cockpit audit panel."""
    async with _pool.acquire() as conn:
        return await queries.risk_history(conn, limit=limit)


@app.get("/api/positions")
async def api_positions():
    async with _pool.acquire() as conn:
        return await queries.positions(conn)


@app.get("/api/decisions")
async def api_decisions(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    async with _pool.acquire() as conn:
        return await queries.decisions(conn, limit=limit, offset=offset)


@app.get("/api/decisions/stats")
async def api_decisions_stats(
    window_hours: int = Query(default=24, ge=1, le=720),
):
    async with _pool.acquire() as conn:
        return await queries.decisions_stats(conn, window_hours=window_hours)


@app.get("/api/risk")
async def api_risk():
    async with _pool.acquire() as conn:
        return await queries.risk(conn)


@app.get("/api/ml")
async def api_ml():
    """V2 INTELLIGENCE / Maturity sub-tab data source.

    The legacy shape (returned by `_aggregate_ml_profiles`) exposed
    `leaders_with_process`, `phase2_leaders`, `phase3_leaders`, etc.
    V2's `IntelligenceMaturity` component instead reads `total_profiles`,
    `maturity_pct`, `phase_distribution.{p1,p2,p3}`, `decisions_24h`,
    and a 24h hourly `trajectory.{trades,resolved,edges}` series.

    The legacy fields are NOT included here — V1 reads them via
    /api/v1/live-summary → snapshot.ml. Calling `_get_live_snapshot()`
    from this endpoint cascades into `queries.overview()` which is
    slow (activity_feed JOIN + follower_map CTE) and was killing
    V2 performance.
    """
    base: dict = {}

    async with _pool.acquire() as conn:
        # Total profiles + mean maturity (V2 KPI: TOTAL PROFILES + MATURITY).
        try:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS total_profiles,
                    COALESCE(AVG(profile_maturity), 0)::float AS avg_maturity
                FROM leader_profiles
                """
            )
            total_profiles = int(row["total_profiles"] or 0)
            maturity_pct = float(row["avg_maturity"] or 0.0)
        except Exception:
            total_profiles = 0
            maturity_pct = 0.0

        # Phase distribution across all profiles (V2 KPI: PHASE 1/2/3).
        try:
            phase_rows = await conn.fetch(
                """
                SELECT
                    COALESCE(error_model_phase, 1)::int AS phase,
                    COUNT(*)::int AS n
                FROM leader_profiles
                GROUP BY 1
                """
            )
            phase_distribution = {"p1": 0, "p2": 0, "p3": 0}
            for r in phase_rows:
                p = int(r["phase"])
                if p <= 1:
                    phase_distribution["p1"] += int(r["n"])
                elif p == 2:
                    phase_distribution["p2"] += int(r["n"])
                else:
                    phase_distribution["p3"] += int(r["n"])
        except Exception:
            phase_distribution = {"p1": 0, "p2": 0, "p3": 0}

        # Decisions in last 24h (V2 KPI: DECISIONS 24H).
        try:
            decisions_24h = await conn.fetchval(
                "SELECT COUNT(*)::int FROM decision_log "
                "WHERE time >= NOW() - INTERVAL '24 hours'"
            )
        except Exception:
            decisions_24h = 0

        # 24h hourly trajectory (V2 LEARNING TRAJECTORY chart).
        # Three series: new trades observed, positions resolved, new
        # edges confirmed — each as a 24-element array of hourly counts.
        try:
            traj_rows = await conn.fetch(
                """
                WITH hours AS (
                    SELECT generate_series(0, 23) AS h
                ),
                bucket_trades AS (
                    SELECT
                        EXTRACT(HOUR FROM (NOW() - time))::int AS hours_ago,
                        COUNT(*)::int AS n
                    FROM trades_observed
                    WHERE time >= NOW() - INTERVAL '24 hours'
                    GROUP BY 1
                ),
                bucket_resolved AS (
                    SELECT
                        EXTRACT(HOUR FROM (NOW() - close_time))::int AS hours_ago,
                        COUNT(*)::int AS n
                    FROM positions_reconstructed
                    WHERE close_time >= NOW() - INTERVAL '24 hours'
                      AND close_time IS NOT NULL
                    GROUP BY 1
                ),
                bucket_edges AS (
                    SELECT
                        EXTRACT(HOUR FROM (NOW() - last_observed))::int AS hours_ago,
                        COUNT(*)::int AS n
                    FROM follower_edges
                    WHERE last_observed >= NOW() - INTERVAL '24 hours'
                      AND follow_probability > 0.6
                      AND co_occurrences >= 5
                    GROUP BY 1
                )
                SELECT
                    hours.h AS h,
                    COALESCE(bucket_trades.n, 0)   AS trades,
                    COALESCE(bucket_resolved.n, 0) AS resolved,
                    COALESCE(bucket_edges.n, 0)    AS edges
                FROM hours
                LEFT JOIN bucket_trades   ON bucket_trades.hours_ago   = hours.h
                LEFT JOIN bucket_resolved ON bucket_resolved.hours_ago = hours.h
                LEFT JOIN bucket_edges    ON bucket_edges.hours_ago    = hours.h
                ORDER BY hours.h DESC
                """
            )
            # The query returns hours_ago=0 first (now) ... 23 (oldest).
            # The chart expects ascending time → reverse.
            traj_rows = list(reversed(traj_rows))
            trajectory = {
                "trades":   [int(r["trades"])   for r in traj_rows],
                "resolved": [int(r["resolved"]) for r in traj_rows],
                "edges":    [int(r["edges"])    for r in traj_rows],
            }
        except Exception as exc:
            logger.warning(f"api_ml trajectory query failed: {exc}")
            trajectory = {"trades": [], "resolved": [], "edges": []}

        # Lens trained flag — does a strategy classifier model file exist
        # AND has it produced any non-uniform prediction recently?
        try:
            distinct_classes = await conn.fetchval(
                "SELECT COUNT(DISTINCT primary_strategy)::int "
                "FROM leader_strategy_history "
                "WHERE classified_at >= NOW() - INTERVAL '24 hours'"
            )
            lens_trained = int(distinct_classes or 0) >= 2
        except Exception:
            lens_trained = False

    return {
        **base,                                  # legacy fields (leaders_with_process, phase2_leaders, etc.)
        # V2 IntelligenceMaturity expects these names:
        "total_profiles": total_profiles,
        "maturity_pct": maturity_pct,
        "phase_distribution": phase_distribution,
        "decisions_24h": int(decisions_24h or 0),
        "trajectory": trajectory,
        "lens_trained": lens_trained,
    }


@app.get("/api/neural-readiness")
async def api_neural_readiness():
    health = await _health_checks()
    async with _pool.acquire() as conn:
        try:
            activation = await queries.activation_queue(conn)
        except Exception as exc:
            logger.warning(f"Neural readiness activation snapshot failed: {exc}")
            activation = []
        try:
            risk = await queries.risk(conn)
        except Exception as exc:
            logger.warning(f"Neural readiness risk snapshot failed: {exc}")
            risk = {"open_count": 0, "drawdown_pct": 0.0}
        try:
            ml = await queries.ml_summary(conn)
        except Exception as exc:
            logger.warning(f"Neural readiness ML snapshot failed: {exc}")
            ml = {}
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health=health,
            activation=activation,
            risk=risk,
            ml=ml,
        )
    )
    async with _pool.acquire() as conn:
        try:
            persistence = await persist_readiness_snapshot(
                conn,
                snapshot,
                trigger_event_type="api_neural_readiness",
                trigger_event_ref={
                    "updated_at": snapshot.get("global", {}).get("updated_at"),
                    "blockers": snapshot.get("global", {}).get("blockers", []),
                },
            )
            snapshot.setdefault("global", {})["persistence"] = {
                "ok": True,
                **persistence,
            }
            persisted_transitions = await load_recent_persisted_transitions(conn, limit=8)
            if persisted_transitions:
                snapshot["transitions"] = persisted_transitions
        except Exception as exc:
            logger.warning(f"Neural readiness persistence failed: {exc}")
            snapshot.setdefault("global", {})["persistence"] = {
                "ok": False,
                "error": str(exc),
            }
    return snapshot


@app.get("/api/system")
async def api_system():
    async with _pool.acquire() as conn:
        data = await queries.system_status(conn)
    data["health"] = await _health_checks()
    return data


@app.get("/api/activation")
async def api_activation():
    async with _pool.acquire() as conn:
        return await queries.activation_queue(conn)


@app.get("/api/positions/live")
async def api_positions_live():
    async with _pool.acquire() as conn:
        return await queries.open_positions_with_prices(conn, _redis)


@app.get("/api/graph/top-edges")
async def api_graph_top_edges(limit: int = Query(default=30, ge=1, le=200)):
    async with _pool.acquire() as conn:
        return await queries.graph_top_edges(conn, limit=limit)


@app.get("/api/profiler/health")
async def api_profiler_health():
    async with _pool.acquire() as conn:
        return await queries.profiler_health(conn)


@app.get("/api/data-quality")
async def api_data_quality():
    async with _pool.acquire() as conn:
        return await queries.data_quality(conn, redis_client=_redis)


@app.get("/api/v1/live-summary")
async def api_live_summary_v1(request: Request, response: Response):
    """Full snapshot endpoint with conditional-GET (ETag / If-None-Match).

    The dashboard polls this every 5 s. Hashing the serialized snapshot and
    returning 304 Not Modified when nothing changed cuts the wire payload
    (~50–200 KB) and JSON parse time on the client to near-zero on idle ticks.
    The snapshot itself is already cached in-process for ≤ 1 s
    (_get_terminal_snapshot), so this is a pure additive optimisation.
    """
    snap = await _get_terminal_snapshot()
    payload = json.dumps({"data": snap}, default=str, separators=(",", ":"))
    etag = '"' + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] + '"'

    inm = request.headers.get("if-none-match")
    if inm and inm == etag:
        response.status_code = 304
        response.headers["ETag"] = etag
        # 304 must have an empty body — FastAPI's Response handles that.
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    return Response(content=payload, media_type="application/json", headers={"ETag": etag})


@app.get("/api/snapshot/health")
async def api_snapshot_health():
    """Telemetry for the background snapshot rebuilder.

    Exposes how the cache-keeping loop is doing. Useful for the
    dashboard's BOT HEALTH tab and for external monitoring (alerting
    if `staleness_s` exceeds the warn threshold for several poll
    cycles). All times are seconds, all counters are cumulative
    since process start.
    """
    last_completed_at = _snapshot_rebuilder_stats.get("last_completed_at")
    if last_completed_at is None:
        staleness_s = None
        status = "warming_up"
    else:
        staleness_s = round(time.monotonic() - last_completed_at, 2)
        status = (
            "ok"
            if staleness_s <= SNAPSHOT_STALENESS_WARN_S
            else "degraded"
        )
    cache_present = _terminal_snapshot_cache.get("data") is not None
    return {
        "status": status,
        "staleness_s": staleness_s,
        "cache_present": cache_present,
        "last_duration_ms": _snapshot_rebuilder_stats.get("last_duration_ms"),
        "consecutive_failures": _snapshot_rebuilder_stats.get("consecutive_failures", 0),
        "total_rebuilds": _snapshot_rebuilder_stats.get("total_rebuilds", 0),
        "total_failures": _snapshot_rebuilder_stats.get("total_failures", 0),
        "last_error": _snapshot_rebuilder_stats.get("last_error"),
        "rebuilder_interval_s": SNAPSHOT_REBUILDER_INTERVAL_S,
        "staleness_warn_s": SNAPSHOT_STALENESS_WARN_S,
    }


@app.get("/api/inspector/snapshot")
async def api_inspector_snapshot(limit: int = Query(80, ge=10, le=500)):
    """Pipeline observability snapshot for the INSPECTOR dashboard tab."""
    async with _pool.acquire() as conn:
        return await queries.inspector_snapshot(conn, redis_client=_redis, limit=limit)


# ---------------------------------------------------------------------------
# Runtime control: killswitch + execution mode
# ---------------------------------------------------------------------------


class _KillswitchFlip(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=512)
    actor: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Health & liveness probes
#
# `/healthz` is a CHEAP liveness probe — no external I/O. Cloud orchestrators,
# Docker HEALTHCHECK, and watchdogs hit this very frequently and must not be
# coupled to the database or Redis (otherwise a transient DB blip would cause
# the whole container to be killed).
#
# `/health` is a readiness probe — verifies the upstream dependencies (DB +
# Redis) are reachable. Returns HTTP 503 when the API process is alive but not
# ready to serve traffic. Used by load balancers to drain traffic during a
# partial outage.
#
# Aliased without leading slash and at /api/health for callers that already
# scrape under /api/.
# ---------------------------------------------------------------------------
async def _liveness_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "service": "polymarket-bot-api",
        "started_at": _api_started_at.isoformat(),
        "uptime_s": round((now - _api_started_at).total_seconds(), 1),
        "now": now.isoformat(),
    }


async def _readiness_payload() -> tuple[dict, int]:
    """Light DB + Redis probe. Returns (payload, http_status_code)."""
    db_ok = False
    redis_ok = False
    db_err: str | None = None
    redis_err: str | None = None

    if _pool is not None:
        try:
            async with _pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
            db_ok = True
        except Exception as exc:  # asyncio.TimeoutError, asyncpg errors, etc.
            db_err = f"{type(exc).__name__}: {exc}"
    else:
        db_err = "pool_not_initialized"

    if _redis is not None:
        try:
            pong = await asyncio.wait_for(_redis.ping(), timeout=1.0)
            redis_ok = bool(pong)
            if not redis_ok:
                redis_err = "ping_returned_falsey"
        except Exception as exc:
            redis_err = f"{type(exc).__name__}: {exc}"
    else:
        redis_err = "redis_not_initialized"

    payload = await _liveness_payload()
    payload["status"] = "ok" if (db_ok and redis_ok) else "degraded"
    payload["checks"] = {
        "db": {"ok": db_ok, "error": db_err},
        "redis": {"ok": redis_ok, "error": redis_err},
    }
    code = 200 if (db_ok and redis_ok) else 503
    return payload, code


@app.get("/healthz")
async def healthz():
    """Liveness probe — process alive, event loop responsive. No external I/O."""
    return await _liveness_payload()


@app.get("/health")
@app.get("/api/health")
async def health():
    """Readiness probe — DB + Redis reachable. 200 if ready, 503 if degraded."""
    payload, code = await _readiness_payload()
    return JSONResponse(payload, status_code=code)


# Phase 1 Task M — Prometheus scrape endpoint. Single-process default REGISTRY;
# the trade observer (Task O) and Falcon backfill (Task F) emit metrics into it
# from the same process. No auth in Phase 1 (LAN-only scrape).
# TODO(Phase 2): add bearer-token auth + per-IP rate-limit before exposing this
# beyond the prod LAN. Tracked in docs/audit/phase1/M_metrics_foundation.md.
@app.get("/metrics")
async def metrics():
    payload, content_type = export_metrics_latest()
    return Response(content=payload, media_type=content_type)


@app.get("/api/control/state")
async def api_control_state():
    """Returns the current killswitch state (cached read)."""
    state = await get_killswitch().get_state()
    return state.to_dict()


@app.post("/api/control/killswitch")
async def api_control_killswitch(payload: _KillswitchFlip):
    """
    Master execution switch. When False, neither paper nor real trades execute.
    """
    state = await get_killswitch().set_execution_enabled(
        payload.enabled,
        reason=payload.reason,
        actor=payload.actor or "api",
    )
    return state.to_dict()


class _RiskConfigUpdate(BaseModel):
    edits: dict = Field(default_factory=dict)
    actor: str | None = "dashboard"


@app.get("/api/risk/config")
async def api_risk_config():
    """Return the current effective runtime config (defaults + overrides)."""
    cfg = await get_runtime_config().effective()
    return {
        "config": cfg,
        "allowed_keys": RUNTIME_CONFIG_ALLOWED_KEYS,
        "bounds": {k: list(v) for k, v in RUNTIME_CONFIG_BOUNDS.items()},
    }


@app.post("/api/risk/update")
async def api_risk_update(payload: _RiskConfigUpdate):
    """Apply runtime config edits.

    Only keys in ``RUNTIME_CONFIG_ALLOWED_KEYS`` are accepted. Each value
    is validated against ``RUNTIME_CONFIG_BOUNDS``. Returns the merged
    effective config so the dashboard can refresh its display without
    waiting for the next snapshot push.
    """
    # Snapshot the *previous* effective config so we can diff and persist an
    # audit trail of which keys actually changed.
    previous = await get_runtime_config().effective()
    try:
        merged = await get_runtime_config().set_overrides(
            payload.edits or {},
            actor=payload.actor or "dashboard",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Drop the in-memory cached snapshot so the next /api/v1/live-summary
    # call surfaces the new values immediately.
    _terminal_snapshot_cache["data"] = None
    _terminal_snapshot_cache["last_built"] = 0.0

    # Append history rows — best-effort, never blocks the response.
    try:
        async with _pool.acquire() as conn:
            for k in (payload.edits or {}):
                old_v = previous.get(k)
                new_v = merged.get(k)
                if old_v == new_v:
                    continue  # no real change — skip noise
                await queries.log_risk_change(
                    conn, k, old_v, new_v,
                    actor=payload.actor or "dashboard",
                    source="dashboard",
                )
    except Exception as exc:
        logger.debug(f"risk_history logging failed: {exc}")

    return {"config": merged}


@app.post("/api/control/real_execution")
async def api_control_real_execution(payload: _KillswitchFlip):
    """
    Real-trading switch. Independent of the master switch — paper always shadows
    when execution is enabled. To run real trades you need BOTH execution_enabled
    AND real_execution_enabled set to True.
    """
    state = await get_killswitch().set_real_execution_enabled(
        payload.enabled,
        reason=payload.reason,
        actor=payload.actor or "api",
    )
    return state.to_dict()


# ---------------------------------------------------------------------------
# Dashboard v2 — refonded UI (R6-R13 surface)
#
# Lives at /v2 alongside the existing dashboard at /. Migration strategy
# per docs/UI_REDESIGN_PHASE3.md § 9 — both UIs run side-by-side for one
# release cycle, then v1 is retired. The 22 stub endpoints below return
# minimal placeholder shapes (empty arrays / null) so the v2 components
# render their graceful empty states until the underlying data layer is
# wired by the operator.
# ---------------------------------------------------------------------------


@app.get("/v2", response_class=HTMLResponse)
async def root_v2():
    if not TEMPLATE_V2_PATH.exists():
        raise HTTPException(status_code=404, detail="dashboard_v2.html not found")
    return HTMLResponse(content=TEMPLATE_V2_PATH.read_text())


# ---------------------------------------------------------------------------
# Shared helpers for the v2 endpoints
# ---------------------------------------------------------------------------

# Models that the confidence engine treats as core / never auto-disabled.
# Imported lazily inside the endpoints so test collection without the
# calibration package still works.
_V2_PROTECTED_MODELS = {"follow_confidence"}

# Strategy taxonomy mirrors strategy_labels CHECK constraint (migration 026).
_V2_STRATEGY_CLASSES = (
    "directional", "momentum", "contrarian",
    "arb_2way", "arb_3way", "market_maker",
    "structural_bot", "info_leak", "social_driven",
)


def _v2_fmt_time(ts) -> str:
    """Format a TIMESTAMPTZ for the v2 timeline / DataTable LAST SEEN column."""
    if ts is None:
        return ""
    try:
        return ts.strftime("%H:%M:%S")
    except Exception:
        return str(ts)


def _v2_fmt_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except Exception:
        return str(ts)


def _v2_loss_column_for(model: str) -> str:
    """Pick the loss column to plot per model (spec § 3.2)."""
    if model == "follow_confidence":
        return "brier_score"
    if model == "strategy_class":
        return "log_loss"
    if model in ("volume_forecast", "causal_ate"):
        return "mape"
    return "brier_score"


async def _v2_runtime_gate_flags() -> dict[str, bool]:
    """Read the three R8/R9/R10 boolean gates from runtime_config (fail-soft)."""
    out = {
        "strategy_class_enabled": False,
        "volume_forecast_enabled": False,
        "causal_enabled": False,
    }
    try:
        cfg = get_runtime_config()
        snap = await cfg.effective()
        out["strategy_class_enabled"] = bool(snap.get("strategy_conditional_confidence_enabled", False))
        out["volume_forecast_enabled"] = bool(snap.get("volume_anticipation_enabled", False))
        out["causal_enabled"] = bool(snap.get("causal_gating_enabled", False))
    except Exception as exc:
        logger.warning(f"_v2_runtime_gate_flags failed: {exc}")
    return out


# --- Overview --------------------------------------------------------------


@app.get("/api/overview/timeline")
async def api_overview_timeline():
    """Last 5 'what changed' events for the OVERVIEW timeline panel.

    Builds a union of three sources:
      * decision_log last 24h (action='follow'|'fade')
      * model_disable_state recent disables
      * mempool_observations recent decoded intents
    """
    events: list[dict] = []
    try:
        async with _pool.acquire() as conn:
            decisions = await conn.fetch(
                """
                SELECT id, time, leader_wallet, market_id, action
                FROM decision_log
                WHERE time >= NOW() - INTERVAL '24 hours'
                  AND action IN ('follow', 'fade')
                ORDER BY time DESC
                LIMIT 5
                """
            )
            disables = await conn.fetch(
                """
                SELECT model, disabled_at, disabled_reason, auto_or_manual
                FROM model_disable_state
                WHERE disabled_at IS NOT NULL
                ORDER BY disabled_at DESC
                LIMIT 5
                """
            )
            intents = await conn.fetch(
                """
                SELECT intent_id, intent_received_at, wallet_address, fire_result
                FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '24 hours'
                ORDER BY intent_received_at DESC
                LIMIT 5
                """
            )
        for row in decisions:
            events.append({
                "_sort": row["time"],
                "time": _v2_fmt_time(row["time"]),
                "severity": "ok" if row["action"] == "follow" else "warn",
                "message": f"DECISION {row['action'].upper()} · leader {row['leader_wallet'][:10]}…",
                "deepLink": {"id": "execution", "subTab": "decisions"},
            })
        for row in disables:
            kind = (row["auto_or_manual"] or "auto").upper()
            events.append({
                "_sort": row["disabled_at"],
                "time": _v2_fmt_time(row["disabled_at"]),
                "severity": "err",
                "message": f"MODEL DISABLED ({kind}) · {row['model']} — {row['disabled_reason'] or 'no reason'}",
                "deepLink": {"id": "operations", "subTab": "calibration"},
            })
        for row in intents:
            severity = "info"
            if row["fire_result"] == "filled":
                severity = "ok"
            elif row["fire_result"] in ("risk_blocked", "killswitch_off"):
                severity = "warn"
            events.append({
                "_sort": row["intent_received_at"],
                "time": _v2_fmt_time(row["intent_received_at"]),
                "severity": severity,
                "message": f"INTENT {row['fire_result'] or 'pending'} · wallet {row['wallet_address'][:10]}…",
                "deepLink": {"id": "mempool", "subTab": "live"},
            })
        # Sort newest first and keep top 5 across all three streams.
        events.sort(key=lambda e: e.get("_sort") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        events = [{k: v for k, v in e.items() if k != "_sort"} for e in events[:5]]
        return {"events": events}
    except Exception as exc:
        logger.warning(f"api_overview_timeline failed: {exc}")
        return {"events": []}


@app.get("/api/calibration/summary")
async def api_calibration_summary():
    """R13 high-level rollup for the OVERVIEW Mirror bento card."""
    try:
        async with _pool.acquire() as conn:
            disable_rows = await conn.fetch(
                """
                SELECT auto_or_manual, COUNT(*)::int AS n
                FROM model_disable_state
                WHERE is_disabled = TRUE
                GROUP BY auto_or_manual
                """
            )
            auto_n = 0
            manual_n = 0
            for row in disable_rows:
                if row["auto_or_manual"] == "manual":
                    manual_n = int(row["n"])
                else:
                    auto_n = int(row["n"])
            drift_alerts_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM model_drift_streak
                WHERE last_breach_at >= CURRENT_DATE - INTERVAL '1 day'
                """
            ) or 0
            last_batch_at = await conn.fetchval(
                "SELECT MAX(measured_at) FROM calibration_loss_history"
            )
            predictions_logged_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM decision_predictions
                WHERE predicted_at >= NOW() - INTERVAL '24 hours'
                """
            ) or 0
        gates = await _v2_runtime_gate_flags()
        return {
            "disabled_count": auto_n + manual_n,
            "auto_disabled_count": auto_n,
            "manual_disabled_count": manual_n,
            "drift_alerts_24h": int(drift_alerts_24h),
            "last_batch_at": _v2_fmt_iso(last_batch_at),
            "predictions_logged_24h": int(predictions_logged_24h),
            **gates,
        }
    except Exception as exc:
        logger.warning(f"api_calibration_summary failed: {exc}")
        return {
            "disabled_count": 0,
            "auto_disabled_count": 0,
            "manual_disabled_count": 0,
            "drift_alerts_24h": 0,
            "last_batch_at": None,
            "predictions_logged_24h": 0,
            "strategy_class_enabled": False,
            "volume_forecast_enabled": False,
            "causal_enabled": False,
        }


# --- Mempool (R7) ----------------------------------------------------------


@app.get("/api/mempool/summary")
async def api_mempool_summary():
    """Mempool watcher rollup.

    Pool inventory + freshness + intent->fire latency live in-memory in the
    R7 daemon. Operator wire-up: surface those via a Redis health key
    (``polybot:prefill_pool:snapshot``) so the API can read them without a
    direct RPC into the daemon process. Until that lands we report 0 with
    a known capacity so the UI shows ``0/40`` instead of ``—``.
    """
    try:
        async with _pool.acquire() as conn:
            intents_15m = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '15 minutes'
                """
            ) or 0
            intents_60m = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '60 minutes'
                """
            ) or 0
            decode_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (
                         WHERE fire_result IS NOT NULL
                         AND fire_result <> 'killswitch_off'
                       )::int AS decoded
                FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '60 minutes'
                """
            )
            total = int(decode_row["total"] or 0) if decode_row else 0
            decoded = int(decode_row["decoded"] or 0) if decode_row else 0
            decode_hit_rate = (decoded / total) if total > 0 else 0.0
            fires_row = await conn.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE fire_result = 'shadow')::int AS shadow,
                  COUNT(*) FILTER (WHERE fire_result = 'filled')::int AS live
                FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '24 hours'
                """
            )
            shadow_24 = int(fires_row["shadow"] or 0) if fires_row else 0
            live_24 = int(fires_row["live"] or 0) if fires_row else 0
            nonce_chains = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM (
                    SELECT wallet_address, nonce
                    FROM mempool_observations
                    WHERE intent_received_at >= NOW() - INTERVAL '60 minutes'
                      AND replaces_tx_hash IS NOT NULL
                    GROUP BY wallet_address, nonce
                ) sub
                """
            ) or 0
        # Daemon liveness: read the Redis heartbeat key the R7 watcher
        # writes. Absent / stale = "off".
        connected = False
        if _redis is not None:
            try:
                hb = await _redis.get("polybot:mempool:heartbeat")
                connected = hb is not None
            except Exception:
                connected = False
        return {
            "connected": connected,
            "intents_per_min": round((intents_15m / 15.0), 2) if intents_15m else 0,
            "intents_per_hour": int(intents_60m),
            "decode_hit_rate": float(decode_hit_rate),
            # NOTE: pool inventory + freshness + intent->fire timing live
            # in-memory in the R7 daemon. Operator must expose via Redis
            # snapshot key (polybot:prefill_pool:snapshot) before these
            # populate. Returning known shape for now.
            "pool_size": 0,
            "pool_capacity": 40,
            "pool_freshness_pct": 0.0,
            "intent_to_fire_p50_ms": None,
            "shadow_fires_24h": shadow_24,
            "live_fires_24h": live_24,
            "active_nonce_chains": int(nonce_chains),
        }
    except Exception as exc:
        logger.warning(f"api_mempool_summary failed: {exc}")
        return {
            "connected": False,
            "intents_per_min": 0,
            "intents_per_hour": 0,
            "decode_hit_rate": 0.0,
            "pool_size": 0,
            "pool_capacity": 40,
            "pool_freshness_pct": 0.0,
            "intent_to_fire_p50_ms": None,
            "shadow_fires_24h": 0,
            "live_fires_24h": 0,
            "active_nonce_chains": 0,
        }


@app.get("/api/mempool/live")
async def api_mempool_live():
    """Last 15 minutes of intents + active nonce replacement chains."""
    try:
        async with _pool.acquire() as conn:
            intent_rows = await conn.fetch(
                """
                SELECT mo.intent_id::text AS intent_id,
                       mo.wallet_address AS wallet,
                       mo.side,
                       mo.size_usdc,
                       NULL::numeric AS price,
                       mo.market_id,
                       m.question AS market_title,
                       mo.intent_received_at,
                       EXTRACT(EPOCH FROM (NOW() - mo.intent_received_at))::int AS age_s,
                       (mo.fire_result IS NOT NULL
                        AND mo.fire_result NOT IN ('killswitch_off'))::bool AS decoded,
                       mo.fire_result AS skip_reason
                FROM mempool_observations mo
                LEFT JOIN markets m ON m.market_id = mo.market_id
                WHERE mo.intent_received_at >= NOW() - INTERVAL '15 minutes'
                ORDER BY mo.intent_received_at DESC
                LIMIT 100
                """
            )
            chain_rows = await conn.fetch(
                """
                SELECT wallet_address AS wallet,
                       nonce,
                       COUNT(*)::int AS chain_len,
                       MAX(fire_result) AS state
                FROM mempool_observations
                WHERE intent_received_at >= NOW() - INTERVAL '60 minutes'
                GROUP BY wallet_address, nonce
                HAVING COUNT(*) > 1
                ORDER BY chain_len DESC
                LIMIT 50
                """
            )
        intents = [
            {
                "intent_id": r["intent_id"],
                "wallet": r["wallet"],
                "side": r["side"],
                "size_usdc": float(r["size_usdc"]) if r["size_usdc"] is not None else None,
                "price": float(r["price"]) if r["price"] is not None else None,
                "market_id": r["market_id"],
                "market_title": r["market_title"] or r["market_id"],
                "age_s": int(r["age_s"] or 0),
                "decoded": bool(r["decoded"]),
                "skip_reason": r["skip_reason"],
            }
            for r in intent_rows
        ]
        nonce_chains = [
            {
                "wallet": r["wallet"],
                "nonce": int(r["nonce"]),
                "chain_summary": f"{r['chain_len']} txs",
                "state": r["state"] or "pending",
            }
            for r in chain_rows
        ]
        return {"intents": intents, "nonce_chains": nonce_chains}
    except Exception as exc:
        logger.warning(f"api_mempool_live failed: {exc}")
        return {"intents": [], "nonce_chains": []}


@app.get("/api/mempool/pool")
async def api_mempool_pool():
    """Pre-signed pool inventory.

    Pool state is in-memory in the R7 daemon (see
    src/execution/prefill/pool.py) — not in the DB. Operator wire-up:
    have the daemon publish a snapshot to the Redis key
    ``polybot:prefill_pool:snapshot`` on each rotation and read it here.
    Until that's wired, return the known empty shape so the UI shows
    "Pool empty" instead of crashing.
    """
    try:
        entries: list[dict] = []
        miss_reasons: dict[str, int] = {}
        if _redis is not None:
            try:
                raw = await _redis.get("polybot:prefill_pool:snapshot")
                if raw:
                    snap = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    entries = list(snap.get("entries", []))[:200]
                    miss_reasons = dict(snap.get("miss_reasons_last_hour", {}))
            except Exception:
                entries = []
                miss_reasons = {}
        return {"entries": entries, "miss_reasons_last_hour": miss_reasons}
    except Exception as exc:
        logger.warning(f"api_mempool_pool failed: {exc}")
        return {"entries": [], "miss_reasons_last_hour": {}}


@app.get("/api/mempool/decisions")
async def api_mempool_decisions(filter: str = "all"):  # noqa: A002
    """IntentRouter outcome feed from mempool_observations.

    The router writes the result vocabulary
    (filled/pool_miss/risk_blocked/killswitch_off/shadow/cooldown/
    confidence_skip/size_cap) onto mempool_observations.fire_result;
    we surface it directly so the v2 MempoolDecisions table can
    filter by it.
    """
    try:
        async with _pool.acquire() as conn:
            if filter and filter != "all":
                rows = await conn.fetch(
                    """
                    SELECT mo.intent_id::text AS decision_id,
                           mo.intent_received_at AS time,
                           mo.wallet_address AS wallet,
                           mo.market_id,
                           m.question AS market_title,
                           mo.fire_result AS result,
                           mo.tx_hash AS detail
                    FROM mempool_observations mo
                    LEFT JOIN markets m ON m.market_id = mo.market_id
                    WHERE mo.intent_received_at >= NOW() - INTERVAL '24 hours'
                      AND mo.fire_result = $1
                    ORDER BY mo.intent_received_at DESC
                    LIMIT 200
                    """,
                    filter,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT mo.intent_id::text AS decision_id,
                           mo.intent_received_at AS time,
                           mo.wallet_address AS wallet,
                           mo.market_id,
                           m.question AS market_title,
                           mo.fire_result AS result,
                           mo.tx_hash AS detail
                    FROM mempool_observations mo
                    LEFT JOIN markets m ON m.market_id = mo.market_id
                    WHERE mo.intent_received_at >= NOW() - INTERVAL '24 hours'
                    ORDER BY mo.intent_received_at DESC
                    LIMIT 200
                    """
                )
        decisions = [
            {
                "decision_id": r["decision_id"],
                "time": _v2_fmt_time(r["time"]),
                "wallet": r["wallet"],
                "market": r["market_title"] or r["market_id"],
                "result": r["result"] or "pending",
                "detail": (r["detail"] or "")[:16] if r["detail"] else "",
            }
            for r in rows
        ]
        return {"decisions": decisions, "filter": filter}
    except Exception as exc:
        logger.warning(f"api_mempool_decisions failed: {exc}")
        return {"decisions": [], "filter": filter}


# --- Microscope (R11) ------------------------------------------------------


@app.get("/api/microscope/summary")
async def api_microscope_summary():
    """R11 microstructure rollup."""
    try:
        async with _pool.acquire() as conn:
            events_per_sec = await conn.fetchval(
                """
                SELECT COALESCE(COUNT(*)/60.0, 0)::float FROM clob_book_events
                WHERE event_time >= NOW() - INTERVAL '1 minute'
                """
            ) or 0.0
            ms_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(iceberg_orders_count), 0)::int AS iceberg_n,
                       COALESCE(SUM(spoof_orders_count),   0)::int AS spoof_n,
                       AVG(ofi_mean)::float                      AS ofi_mean
                FROM microstructure_features
                WHERE bucket_ts >= NOW() - INTERVAL '1 hour'
                """
            )
            iceberg_n = int(ms_row["iceberg_n"] or 0) if ms_row else 0
            spoof_n = int(ms_row["spoof_n"] or 0) if ms_row else 0
            ofi_mean = float(ms_row["ofi_mean"]) if ms_row and ms_row["ofi_mean"] is not None else None
            # Storage from pg_total_relation_size — fail-soft on unknown table.
            storage_gb = None
            try:
                size_bytes = await conn.fetchval(
                    "SELECT pg_total_relation_size('clob_book_events')::bigint"
                )
                if size_bytes:
                    storage_gb = float(size_bytes) / 1e9
            except Exception:
                storage_gb = None
        return {
            "events_per_sec": float(events_per_sec),
            # NOTE: queue depth + dropped counter are in-memory in the R11
            # ingestion daemon. Operator must expose via Redis or
            # Prometheus gauge. Returning safe placeholders.
            "queue_depth": 0,
            "queue_capacity": 50_000,
            "dropped_24h": 0,
            "iceberg_per_hour": iceberg_n,
            "spoof_per_hour": spoof_n,
            "ofi_mean": ofi_mean,
            "place_to_fill_p50_ms": None,
            "storage_gb": storage_gb,
        }
    except Exception as exc:
        logger.warning(f"api_microscope_summary failed: {exc}")
        return {
            "events_per_sec": 0,
            "queue_depth": 0,
            "queue_capacity": 50_000,
            "dropped_24h": 0,
            "iceberg_per_hour": 0,
            "spoof_per_hour": 0,
            "ofi_mean": None,
            "place_to_fill_p50_ms": None,
            "storage_gb": None,
        }


@app.get("/api/microscope/firehose")
async def api_microscope_firehose():
    """L3 book event firehose — last 5 minutes."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT cbe.event_id::text AS event_id,
                       cbe.event_time,
                       EXTRACT(EPOCH FROM (NOW() - cbe.event_time))::int AS age_s,
                       cbe.market_id,
                       m.question AS market_title,
                       cbe.event_type,
                       cbe.side,
                       cbe.size_delta,
                       cbe.wallet_address AS wallet
                FROM clob_book_events cbe
                LEFT JOIN markets m ON m.market_id = cbe.market_id
                WHERE cbe.event_time >= NOW() - INTERVAL '5 minutes'
                ORDER BY cbe.event_time DESC
                LIMIT 200
                """
            )
        events = [
            {
                "event_id": r["event_id"],
                "event_time": _v2_fmt_iso(r["event_time"]),
                "age_s": int(r["age_s"] or 0),
                "market_id": r["market_id"],
                "market_title": r["market_title"] or r["market_id"],
                "event_type": r["event_type"],
                "side": r["side"],
                "size_delta": float(r["size_delta"]) if r["size_delta"] is not None else None,
                "wallet": r["wallet"],
            }
            for r in rows
        ]
        return {"events": events}
    except Exception as exc:
        logger.warning(f"api_microscope_firehose failed: {exc}")
        return {"events": []}


@app.get("/api/microscope/microstructure")
async def api_microscope_microstructure(limit: int = 50):
    """Per-market microstructure rollup."""
    try:
        limit = max(1, min(int(limit), 500))
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT mf.market_id,
                       mf.token_id,
                       m.question AS market_title,
                       mf.bucket_ts,
                       mf.iceberg_orders_count,
                       mf.spoof_orders_count,
                       mf.ofi_mean,
                       mf.ofi_max,
                       mf.ofi_min,
                       mf.ofi_std
                FROM microstructure_features mf
                LEFT JOIN markets m ON m.market_id = mf.market_id
                ORDER BY mf.bucket_ts DESC
                LIMIT $1
                """,
                limit,
            )
        out = [
            {
                "market_id": r["market_id"],
                "token_id": r["token_id"],
                "market_title": r["market_title"] or r["market_id"],
                "bucket_ts": _v2_fmt_iso(r["bucket_ts"]),
                "iceberg_orders_count": int(r["iceberg_orders_count"] or 0),
                "spoof_orders_count": int(r["spoof_orders_count"] or 0),
                "ofi_mean": float(r["ofi_mean"]) if r["ofi_mean"] is not None else None,
                "ofi_max": float(r["ofi_max"]) if r["ofi_max"] is not None else None,
                "ofi_min": float(r["ofi_min"]) if r["ofi_min"] is not None else None,
                "ofi_std": float(r["ofi_std"]) if r["ofi_std"] is not None else None,
            }
            for r in rows
        ]
        return {"rows": out, "limit": limit}
    except Exception as exc:
        logger.warning(f"api_microscope_microstructure failed: {exc}")
        return {"rows": [], "limit": limit}


@app.get("/api/microscope/signatures")
async def api_microscope_signatures(limit: int = 100):
    """Per-wallet 30-day microstructure signatures (tier-0/1)."""
    try:
        limit = max(1, min(int(limit), 500))
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT wms.wallet_address,
                       wms.rollup_at,
                       wms.cancel_to_fill_ratio_30d,
                       wms.iceberg_score_30d,
                       wms.spoof_score_30d,
                       wms.place_to_fill_seconds_p50,
                       wms.place_to_fill_seconds_p99,
                       wms.n_orders_30d,
                       wms.n_fills_30d,
                       wu.depth_tier
                FROM wallet_microstructure_signature wms
                LEFT JOIN wallet_universe wu
                       ON wu.wallet_address = wms.wallet_address
                ORDER BY wms.rollup_at DESC
                LIMIT $1
                """,
                limit,
            )
        out = []
        for r in rows:
            out.append({
                "wallet_address": r["wallet_address"],
                "rollup_at": _v2_fmt_iso(r["rollup_at"]),
                "cancel_to_fill_ratio_30d": float(r["cancel_to_fill_ratio_30d"]) if r["cancel_to_fill_ratio_30d"] is not None else None,
                "iceberg_score_30d": float(r["iceberg_score_30d"]) if r["iceberg_score_30d"] is not None else None,
                "spoof_score_30d": float(r["spoof_score_30d"]) if r["spoof_score_30d"] is not None else None,
                "place_to_fill_seconds_p50": float(r["place_to_fill_seconds_p50"]) if r["place_to_fill_seconds_p50"] is not None else None,
                "place_to_fill_seconds_p99": float(r["place_to_fill_seconds_p99"]) if r["place_to_fill_seconds_p99"] is not None else None,
                "n_orders_30d": int(r["n_orders_30d"]) if r["n_orders_30d"] is not None else None,
                "n_fills_30d": int(r["n_fills_30d"]) if r["n_fills_30d"] is not None else None,
                "depth_tier": int(r["depth_tier"]) if r["depth_tier"] is not None else None,
            })
        return {"signatures": out, "limit": limit}
    except Exception as exc:
        logger.warning(f"api_microscope_signatures failed: {exc}")
        return {"signatures": [], "limit": limit}


# --- Periphery (R12 + R10 instruments) -------------------------------------


@app.get("/api/periphery/summary")
async def api_periphery_summary():
    """R12 social + cross-market + R10 instrument event rollup."""
    try:
        async with _pool.acquire() as conn:
            social_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE intent = 'entry_signal')::int AS entry_n,
                       COUNT(*) FILTER (WHERE intent = 'exit_signal')::int  AS exit_n
                FROM social_signals
                WHERE posted_at >= NOW() - INTERVAL '24 hours'
                """
            )
            tweets_24h = int(social_row["total"] or 0) if social_row else 0
            entry_n = int(social_row["entry_n"] or 0) if social_row else 0
            exit_n = int(social_row["exit_n"] or 0) if social_row else 0
            entry_pct = (entry_n / tweets_24h) if tweets_24h > 0 else 0.0
            exit_pct = (exit_n / tweets_24h) if tweets_24h > 0 else 0.0
            ops_row = await conn.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE confidence >= 0.8)::int AS resolved_n,
                  COUNT(*) FILTER (WHERE confidence < 0.8)::int  AS pending_n
                FROM cross_market_operators
                """
            )
            resolved = int(ops_row["resolved_n"] or 0) if ops_row else 0
            pending = int(ops_row["pending_n"] or 0) if ops_row else 0
            instrument_events_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM instrumental_events
                WHERE event_time >= NOW() - INTERVAL '24 hours'
                """
            ) or 0
        # X quota lives in the social daemon's runtime state. Read from
        # Redis if available; default to 1.0 (no signal of exhaustion).
        x_quota_pct = 1.0
        if _redis is not None:
            try:
                raw = await _redis.get("polybot:social:x:quota_remaining")
                if raw is not None:
                    x_quota_pct = float(raw)
            except Exception:
                x_quota_pct = 1.0
        return {
            "tweets_24h": tweets_24h,
            "entry_pct": float(entry_pct),
            "exit_pct": float(exit_pct),
            "x_quota_pct": float(x_quota_pct),
            "operators_resolved": resolved,
            "operators_pending": pending,
            "instrument_events_24h": int(instrument_events_24h),
        }
    except Exception as exc:
        logger.warning(f"api_periphery_summary failed: {exc}")
        return {
            "tweets_24h": 0,
            "entry_pct": 0.0,
            "exit_pct": 0.0,
            "x_quota_pct": 1.0,
            "operators_resolved": 0,
            "operators_pending": 0,
            "instrument_events_24h": 0,
        }


@app.get("/api/periphery/social/feed")
async def api_periphery_social_feed():
    """Per-author social signal stream (last 24h)."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT signal_id, source, author_handle, resolved_wallet,
                       posted_at, text, intent, intent_confidence,
                       parsed_market, parsed_direction,
                       EXTRACT(EPOCH FROM (NOW() - posted_at))::int AS age_s
                FROM social_signals
                ORDER BY posted_at DESC
                LIMIT 200
                """
            )
        signals = [
            {
                "signal_id": int(r["signal_id"]),
                "source": r["source"],
                "author_handle": r["author_handle"],
                "resolved_wallet": r["resolved_wallet"],
                "posted_at": _v2_fmt_iso(r["posted_at"]),
                "text": r["text"],
                "intent": r["intent"],
                "intent_confidence": float(r["intent_confidence"]) if r["intent_confidence"] is not None else None,
                "parsed_market": r["parsed_market"],
                "parsed_direction": r["parsed_direction"],
                "age_s": int(r["age_s"] or 0),
            }
            for r in rows
        ]
        return {"signals": signals}
    except Exception as exc:
        logger.warning(f"api_periphery_social_feed failed: {exc}")
        return {"signals": []}


@app.get("/api/periphery/crossmarket/status")
async def api_periphery_crossmarket_status():
    """Cross-venue daemon liveness.

    No DB table — daemons publish health to Redis keys
    ``polybot:crossmarket:{venue}:health`` (JSON). Operator must wire the
    R12 R&D daemons to publish there. Until that lands every venue
    reports reachable=False.
    """
    out = {
        venue: {"reachable": False, "latency_p50_ms": None, "api_calls_24h": 0, "positions_observed": 0}
        for venue in ("kalshi", "manifold", "predictit")
    }
    if _redis is None:
        return out
    try:
        for venue in out:
            try:
                raw = await _redis.get(f"polybot:crossmarket:{venue}:health")
                if raw:
                    payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    out[venue] = {
                        "reachable": bool(payload.get("reachable", False)),
                        "latency_p50_ms": payload.get("latency_p50_ms"),
                        "api_calls_24h": int(payload.get("api_calls_24h", 0) or 0),
                        "positions_observed": int(payload.get("positions_observed", 0) or 0),
                    }
            except Exception:
                continue
    except Exception as exc:
        logger.warning(f"api_periphery_crossmarket_status failed: {exc}")
    return out


@app.get("/api/periphery/crossmarket/operators")
async def api_periphery_crossmarket_operators():
    """Cross-market operator resolution table."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT operator_id, polymarket_wallet, kalshi_account,
                       manifold_handle, predictit_account, x_handle,
                       resolution_source, confidence, resolved_at, notes
                FROM cross_market_operators
                ORDER BY confidence DESC, resolved_at DESC
                LIMIT 100
                """
            )
        ops = []
        for r in rows:
            conf = float(r["confidence"]) if r["confidence"] is not None else 0.0
            ops.append({
                "operator_id": int(r["operator_id"]),
                "polymarket_wallet": r["polymarket_wallet"],
                "kalshi_account": r["kalshi_account"],
                "manifold_handle": r["manifold_handle"],
                "predictit_account": r["predictit_account"],
                "x_handle": r["x_handle"],
                "resolution_source": r["resolution_source"],
                "confidence": conf,
                "resolved_at": _v2_fmt_iso(r["resolved_at"]),
                "notes": r["notes"],
                # Pending review when confidence is below the operator
                # gate. Mirrors the cross_market_operators column header
                # comment in migration 036 (fingerprint rows < threshold).
                "is_pending_review": bool(conf < 0.8),
            })
        return {"operators": ops}
    except Exception as exc:
        logger.warning(f"api_periphery_crossmarket_operators failed: {exc}")
        return {"operators": []}


@app.post("/api/periphery/crossmarket/confirm/{op_id}")
async def api_periphery_crossmarket_confirm(op_id: int):
    """Operator confirms an auto-suggested cross-market resolution.

    Promotes a low-confidence fingerprint match to confirmed by setting
    confidence to 1.0. The dashboard polls
    ``/api/periphery/crossmarket/operators`` which keys
    ``is_pending_review`` off confidence < 0.8.
    """
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE cross_market_operators
                   SET confidence = 1.0
                 WHERE operator_id = $1
                """,
                int(op_id),
            )
        ok = False
        try:
            ok = int(str(result).split()[-1]) > 0
        except Exception:
            ok = True
        return {"ok": ok, "operator_id": int(op_id)}
    except Exception as exc:
        logger.warning(f"api_periphery_crossmarket_confirm failed: {exc}")
        return {"ok": False, "operator_id": int(op_id)}


# --- Intelligence (R8 Lens, R9 Web, R10 Causal) ---------------------------


@app.get("/api/intelligence/lens/distribution")
async def api_intelligence_lens_distribution():
    """R8 strategy distribution + drift heatmap."""
    try:
        async with _pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(DISTINCT wallet_address)::int FROM leader_strategy_history"
            ) or 0
            trained_classes = await conn.fetchval(
                "SELECT COUNT(DISTINCT primary_strategy)::int FROM strategy_labels"
            )
            drift_alerts_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM model_drift_streak
                WHERE model = 'strategy_class'
                  AND last_breach_at >= CURRENT_DATE - INTERVAL '1 day'
                """
            ) or 0
            by_class_rows = await conn.fetch(
                """
                SELECT primary_strategy, COUNT(*)::int AS n
                FROM (
                  SELECT DISTINCT ON (wallet_address)
                         wallet_address, primary_strategy
                  FROM leader_strategy_history
                  ORDER BY wallet_address, classified_at DESC
                ) sub
                GROUP BY primary_strategy
                """
            )
            # Drift heatmap: per-(wallet, day) drift score for top-20
            # most-active wallets over the last 7 days.
            drift_rows = await conn.fetch(
                """
                WITH top_wallets AS (
                  SELECT wallet_address
                  FROM leader_strategy_history
                  WHERE classified_at >= NOW() - INTERVAL '7 days'
                  GROUP BY wallet_address
                  ORDER BY COUNT(*) DESC
                  LIMIT 20
                )
                SELECT lsh.wallet_address,
                       DATE_TRUNC('day', lsh.classified_at) AS day,
                       AVG(COALESCE(lsh.drift_js_divergence, 0))::float AS val
                FROM leader_strategy_history lsh
                JOIN top_wallets tw ON tw.wallet_address = lsh.wallet_address
                WHERE lsh.classified_at >= NOW() - INTERVAL '7 days'
                GROUP BY lsh.wallet_address, DATE_TRUNC('day', lsh.classified_at)
                ORDER BY lsh.wallet_address, day
                """
            )
        by_class: dict[str, int] = {row["primary_strategy"]: int(row["n"]) for row in by_class_rows}

        # Build the wallet × day matrix.
        wallets_seen: list[str] = []
        days_seen: list[str] = []
        cell_map: dict[tuple[str, str], float] = {}
        for r in drift_rows:
            w = r["wallet_address"]
            d = r["day"].strftime("%a") if r["day"] else ""
            if w not in wallets_seen:
                wallets_seen.append(w)
            if d and d not in days_seen:
                days_seen.append(d)
            cell_map[(w, d)] = float(r["val"] or 0.0)
        # Force 7-day column order Mon..Sun for stable heatmap.
        day_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        cols = [d for d in day_order if d in days_seen] or days_seen
        matrix: list[list[float]] = []
        for w in wallets_seen:
            matrix.append([cell_map.get((w, d), 0.0) for d in cols])

        return {
            "total": int(total),
            "trained_classes": int(trained_classes) if trained_classes is not None else None,
            "cohens_kappa": None,  # Optional helper not yet implemented.
            "drift_alerts_24h": int(drift_alerts_24h),
            "by_class": by_class,
            "drift_rows": [{"wallet": w} for w in wallets_seen],
            "drift_cols": cols,
            "drift_values": matrix,
        }
    except Exception as exc:
        logger.warning(f"api_intelligence_lens_distribution failed: {exc}")
        return {
            "total": 0,
            "trained_classes": None,
            "cohens_kappa": None,
            "drift_alerts_24h": 0,
            "by_class": {},
            "drift_rows": [],
            "drift_cols": [],
            "drift_values": [],
        }


@app.get("/api/intelligence/lens/labels/pending")
async def api_intelligence_lens_labels_pending():
    """High-confidence classifier outputs awaiting a hand label."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (lsh.wallet_address)
                       lsh.wallet_address AS wallet,
                       lsh.primary_strategy AS suggested,
                       lsh.confidence::float AS confidence
                FROM leader_strategy_history lsh
                LEFT JOIN strategy_labels sl
                       ON sl.wallet_address = lsh.wallet_address
                WHERE sl.label_id IS NULL
                  AND lsh.confidence > 0.7
                ORDER BY lsh.wallet_address, lsh.confidence DESC
                LIMIT 50
                """
            )
        wallets = [
            {
                "wallet": r["wallet"],
                "suggested": r["suggested"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            }
            for r in rows
        ]
        return {"wallets": wallets}
    except Exception as exc:
        logger.warning(f"api_intelligence_lens_labels_pending failed: {exc}")
        return {"wallets": []}


@app.get("/api/intelligence/web/summary")
async def api_intelligence_web_summary():
    """R9 multivariate Hawkes + Kalman summary."""
    try:
        async with _pool.acquire() as conn:
            active_fits = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT leader_wallet)::int
                FROM multivariate_hawkes_fits
                WHERE fit_at >= NOW() - INTERVAL '7 days'
                """
            ) or 0
            # Count accepted couplings across all recent fits — sum the
            # number of TRUE values in accepted_couplings_json.
            accepted_couplings_raw = await conn.fetch(
                """
                SELECT accepted_couplings_json
                FROM multivariate_hawkes_fits
                WHERE fit_at >= NOW() - INTERVAL '7 days'
                  AND accepted_couplings_json IS NOT NULL
                """
            )
            accepted = 0
            for r in accepted_couplings_raw:
                payload = r["accepted_couplings_json"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                if isinstance(payload, dict):
                    accepted += sum(1 for v in payload.values() if bool(v))
            kalman_updates_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM follower_pool_state_history
                WHERE snapshot_at >= NOW() - INTERVAL '24 hours'
                """
            ) or 0
            # Latest converged multivariate fit — render its α matrix.
            latest_fit = await conn.fetchrow(
                """
                SELECT leader_wallet, alpha_matrix_json, pool_classes
                FROM multivariate_hawkes_fits
                WHERE convergence = 'converged'
                ORDER BY fit_at DESC
                LIMIT 1
                """
            )
            alpha = {"row_labels": [], "col_labels": [], "matrix": []}
            kalman_rows: list[dict] = []
            if latest_fit:
                pool_classes = (latest_fit["pool_classes"] or "").split(",")
                pool_classes = [p.strip() for p in pool_classes if p.strip()]
                labels = ["leader"] + pool_classes
                alpha_payload = latest_fit["alpha_matrix_json"]
                if isinstance(alpha_payload, str):
                    try:
                        alpha_payload = json.loads(alpha_payload)
                    except Exception:
                        alpha_payload = {}
                if not isinstance(alpha_payload, dict):
                    alpha_payload = {}
                # alpha_matrix_json keyed by "(i,j)" → float.
                n = len(labels)
                matrix = [[0.0] * n for _ in range(n)]
                for key, val in alpha_payload.items():
                    try:
                        i, j = key.strip("()").split(",")
                        ii, jj = int(i), int(j)
                        if 0 <= ii < n and 0 <= jj < n:
                            matrix[ii][jj] = float(val)
                    except Exception:
                        continue
                alpha = {"row_labels": labels, "col_labels": labels, "matrix": matrix}
                kalman_rows_raw = await conn.fetch(
                    """
                    SELECT pool_class AS pool,
                           pool_size_usdc,
                           recent_response_pct,
                           decay_rate,
                           n_observations
                    FROM follower_pool_state
                    WHERE leader_wallet = $1
                    ORDER BY updated_at DESC
                    LIMIT 4
                    """,
                    latest_fit["leader_wallet"],
                )
                for kr in kalman_rows_raw:
                    kalman_rows.append({
                        "pool": kr["pool"],
                        "pool_size_usdc": float(kr["pool_size_usdc"]) if kr["pool_size_usdc"] is not None else None,
                        "recent_response_pct": float(kr["recent_response_pct"]) if kr["recent_response_pct"] is not None else None,
                        "decay_rate": float(kr["decay_rate"]) if kr["decay_rate"] is not None else None,
                        "n_observations": int(kr["n_observations"] or 0),
                    })
        return {
            "active_fits": int(active_fits),
            "accepted_couplings": int(accepted),
            "kalman_updates_24h": int(kalman_updates_24h),
            # Forecasts are computed on-demand by the R9 daemon; no
            # persistence table yet (see ROUND_9_MULTIVARIATE_HAWKES.md
            # § 3.3). Surface as 0 with forecast=None.
            "forecasts_24h": 0,
            "alpha": alpha,
            "kalman": kalman_rows,
            "forecast": None,
        }
    except Exception as exc:
        logger.warning(f"api_intelligence_web_summary failed: {exc}")
        return {
            "active_fits": 0,
            "accepted_couplings": 0,
            "kalman_updates_24h": 0,
            "forecasts_24h": 0,
            "alpha": {"row_labels": [], "col_labels": [], "matrix": []},
            "kalman": [],
            "forecast": None,
        }


@app.get("/api/intelligence/causal/scatter")
async def api_intelligence_causal_scatter():
    """R10 IV vs Hawkes scatter + diagnostic histograms."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT leader_wallet, pool_class,
                       hawkes_alpha_mu_ratio, causal_ate,
                       causal_ate_ci_low, causal_ate_ci_high,
                       wu_hausman_p, first_stage_f,
                       estimated_at
                FROM causal_estimates
                WHERE estimated_at >= NOW() - INTERVAL '7 days'
                ORDER BY estimated_at DESC
                LIMIT 500
                """
            )
        points = []
        wh_vals = []
        f_vals = []
        recent_24h = 0
        wh_pass = 0
        fs_pass = 0
        disagree = 0
        now_aware = datetime.now(timezone.utc)
        for r in rows:
            est_at = r["estimated_at"]
            if est_at and (now_aware - est_at).total_seconds() <= 86400:
                recent_24h += 1
            ratio = float(r["hawkes_alpha_mu_ratio"]) if r["hawkes_alpha_mu_ratio"] is not None else None
            ate = float(r["causal_ate"]) if r["causal_ate"] is not None else None
            ate_hi = float(r["causal_ate_ci_high"]) if r["causal_ate_ci_high"] is not None else None
            wh_p = float(r["wu_hausman_p"]) if r["wu_hausman_p"] is not None else None
            fs_f = float(r["first_stage_f"]) if r["first_stage_f"] is not None else None
            if wh_p is not None:
                wh_vals.append(wh_p)
                if wh_p < 0.05:
                    wh_pass += 1
            if fs_f is not None:
                f_vals.append(fs_f)
                if fs_f > 10:
                    fs_pass += 1
            if ratio is not None and ate_hi is not None and ratio > 1.0 and ate_hi < 0.1:
                disagree += 1
            points.append({
                "leader_wallet": r["leader_wallet"],
                "pool_class": r["pool_class"],
                "hawkes_alpha_mu_ratio": ratio,
                "causal_ate": ate,
                "causal_ate_ci_low": float(r["causal_ate_ci_low"]) if r["causal_ate_ci_low"] is not None else None,
                "causal_ate_ci_high": ate_hi,
                "wu_hausman_p": wh_p,
                "first_stage_f": fs_f,
            })
        total = len(rows)
        wh_pass_rate = (wh_pass / total) if total > 0 else 0.0
        fs_pass_rate = (fs_pass / total) if total > 0 else 0.0
        disagreement_pct = (disagree / total) if total > 0 else 0.0
        # 10-bin histograms.
        def _hist(values, lo, hi, bins=10):
            out = [0] * bins
            if not values:
                return out
            width = (hi - lo) / bins
            for v in values:
                if v is None:
                    continue
                idx = int((v - lo) / width) if width > 0 else 0
                if idx < 0:
                    idx = 0
                if idx >= bins:
                    idx = bins - 1
                out[idx] += 1
            return out
        wu_hausman_histogram = _hist(wh_vals, 0.0, 1.0, 10)
        # First-stage F can grow large — bucket by log scale up to F=100.
        f_log = [v for v in f_vals if v is not None and v > 0]
        first_stage_f_histogram = _hist(f_log, 0.0, 100.0, 10)
        return {
            "estimates_24h": recent_24h,
            "wu_hausman_pass_rate": float(wh_pass_rate),
            "first_stage_pass_rate": float(fs_pass_rate),
            "disagreement_pct": float(disagreement_pct),
            "points": points,
            "wu_hausman_histogram": wu_hausman_histogram,
            "first_stage_f_histogram": first_stage_f_histogram,
        }
    except Exception as exc:
        logger.warning(f"api_intelligence_causal_scatter failed: {exc}")
        return {
            "estimates_24h": 0,
            "wu_hausman_pass_rate": 0.0,
            "first_stage_pass_rate": 0.0,
            "disagreement_pct": 0.0,
            "points": [],
            "wu_hausman_histogram": [],
            "first_stage_f_histogram": [],
        }


@app.get("/api/intelligence/causal/instruments")
async def api_intelligence_causal_instruments():
    """R10 instrumental events for the last 24h."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_type, event_time
                FROM instrumental_events
                WHERE event_time >= NOW() - INTERVAL '24 hours'
                ORDER BY event_time DESC
                LIMIT 500
                """
            )
        events = [
            {"type": r["event_type"], "time": _v2_fmt_iso(r["event_time"])}
            for r in rows
        ]
        return {"events": events}
    except Exception as exc:
        logger.warning(f"api_intelligence_causal_instruments failed: {exc}")
        return {"events": []}


# --- Wallet Lab (R6 universe + augmented profile) -------------------------


@app.get("/api/wallet/universe")
async def api_wallet_universe(limit: int = 200):
    """R6 wallet_universe browser. Joins on depth_tier + strategy_class
    where available."""
    try:
        limit = max(1, min(int(limit), 1000))
        async with _pool.acquire() as conn:
            stats_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE depth_tier = 0)::int AS tier_0,
                       COUNT(*) FILTER (WHERE depth_tier = 1)::int AS tier_1,
                       COUNT(*) FILTER (WHERE depth_tier = 2)::int AS tier_2,
                       MAX(last_active) AS last_crawl_at
                FROM wallet_universe
                """
            )
            total = int(stats_row["total"] or 0) if stats_row else 0
            tier_0 = int(stats_row["tier_0"] or 0) if stats_row else 0
            tier_1 = int(stats_row["tier_1"] or 0) if stats_row else 0
            tier_2 = int(stats_row["tier_2"] or 0) if stats_row else 0
            last_crawl_at = stats_row["last_crawl_at"] if stats_row else None
            rows = await conn.fetch(
                """
                SELECT wu.wallet_address,
                       wu.depth_tier,
                       wu.total_trades_ever AS trades_30d,
                       wu.total_volume_usdc_ever AS volume_30d_usdc,
                       wu.last_active AS last_seen,
                       latest.primary_strategy AS strategy_class
                FROM wallet_universe wu
                LEFT JOIN LATERAL (
                    SELECT primary_strategy
                    FROM leader_strategy_history lsh
                    WHERE lsh.wallet_address = wu.wallet_address
                    ORDER BY lsh.classified_at DESC
                    LIMIT 1
                ) latest ON TRUE
                ORDER BY wu.depth_tier ASC, wu.total_volume_usdc_ever DESC
                LIMIT $1
                """,
                limit,
            )
        wallets = []
        for r in rows:
            wallets.append({
                "wallet_address": r["wallet_address"],
                "depth_tier": int(r["depth_tier"]) if r["depth_tier"] is not None else None,
                "trades_30d": int(r["trades_30d"]) if r["trades_30d"] is not None else None,
                "volume_30d_usdc": float(r["volume_30d_usdc"]) if r["volume_30d_usdc"] is not None else None,
                "last_seen": _v2_fmt_iso(r["last_seen"]),
                "strategy_class": r["strategy_class"],
            })
        return {
            "total": int(total),
            "tier_0": tier_0,
            "tier_1": tier_1,
            "tier_2": tier_2,
            "last_crawl_at": _v2_fmt_iso(last_crawl_at),
            "wallets": wallets,
            "limit": limit,
        }
    except Exception as exc:
        logger.warning(f"api_wallet_universe failed: {exc}")
        return {
            "total": 0,
            "tier_0": 0,
            "tier_1": 0,
            "tier_2": 0,
            "last_crawl_at": None,
            "wallets": [],
            "limit": limit,
        }


@app.get("/api/wallet/{wallet}/strategy")
async def api_wallet_strategy(wallet: str):
    """R8 strategy fingerprint per wallet — 9-class probability vector."""
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT primary_strategy, confidence, classified_at, strategy_probs,
                       drift_js_divergence
                FROM leader_strategy_history
                WHERE wallet_address = $1
                ORDER BY classified_at DESC
                LIMIT 1
                """,
                wallet,
            )
        if not row:
            return {"wallet": wallet, "probs": {}, "last_trained_at": None, "drift_score": None}
        probs_payload = row["strategy_probs"]
        if isinstance(probs_payload, str):
            try:
                probs_payload = json.loads(probs_payload)
            except Exception:
                probs_payload = {}
        if not isinstance(probs_payload, dict):
            probs_payload = {}
        # Normalize all known classes (zeros for missing).
        probs = {cls: float(probs_payload.get(cls, 0.0) or 0.0) for cls in _V2_STRATEGY_CLASSES}
        return {
            "wallet": wallet,
            "primary_strategy": row["primary_strategy"],
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "probs": probs,
            "last_trained_at": _v2_fmt_iso(row["classified_at"]),
            "drift_score": float(row["drift_js_divergence"]) if row["drift_js_divergence"] is not None else None,
        }
    except Exception as exc:
        logger.warning(f"api_wallet_strategy failed: {exc}")
        return {"wallet": wallet, "probs": {}, "last_trained_at": None, "drift_score": None}


@app.get("/api/wallet/{wallet}/microstructure")
async def api_wallet_microstructure(wallet: str):
    """R11 per-wallet microstructure signature."""
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cancel_to_fill_ratio_30d, iceberg_score_30d, spoof_score_30d,
                       place_to_fill_seconds_p50, place_to_fill_seconds_p99,
                       n_orders_30d, n_fills_30d, rollup_at
                FROM wallet_microstructure_signature
                WHERE wallet_address = $1
                ORDER BY rollup_at DESC
                LIMIT 1
                """,
                wallet,
            )
        if not row:
            return {
                "wallet": wallet,
                "cancel_to_fill_ratio_30d": None,
                "iceberg_score_30d": None,
                "spoof_score_30d": None,
                "place_to_fill_seconds_p50": None,
                "place_to_fill_seconds_p99": None,
                "n_orders_30d": None,
                "n_fills_30d": None,
            }
        return {
            "wallet": wallet,
            "cancel_to_fill_ratio_30d": float(row["cancel_to_fill_ratio_30d"]) if row["cancel_to_fill_ratio_30d"] is not None else None,
            "iceberg_score_30d": float(row["iceberg_score_30d"]) if row["iceberg_score_30d"] is not None else None,
            "spoof_score_30d": float(row["spoof_score_30d"]) if row["spoof_score_30d"] is not None else None,
            "place_to_fill_seconds_p50": float(row["place_to_fill_seconds_p50"]) if row["place_to_fill_seconds_p50"] is not None else None,
            "place_to_fill_seconds_p99": float(row["place_to_fill_seconds_p99"]) if row["place_to_fill_seconds_p99"] is not None else None,
            "n_orders_30d": int(row["n_orders_30d"]) if row["n_orders_30d"] is not None else None,
            "n_fills_30d": int(row["n_fills_30d"]) if row["n_fills_30d"] is not None else None,
            "rollup_at": _v2_fmt_iso(row["rollup_at"]),
        }
    except Exception as exc:
        logger.warning(f"api_wallet_microstructure failed: {exc}")
        return {
            "wallet": wallet,
            "cancel_to_fill_ratio_30d": None,
            "iceberg_score_30d": None,
            "spoof_score_30d": None,
            "place_to_fill_seconds_p50": None,
            "place_to_fill_seconds_p99": None,
            "n_orders_30d": None,
            "n_fills_30d": None,
        }


# --- Execution + Operations rollups ----------------------------------------


@app.get("/api/execution/summary")
async def api_execution_summary():
    """Execution + decision rollups for the EXECUTION tab KPI strip."""
    try:
        max_positions = int(getattr(settings, "MAX_CONCURRENT_POSITIONS", 10) or 10)
        async with _pool.acquire() as conn:
            positions_open = await conn.fetchval(
                "SELECT COUNT(*)::int FROM paper_trades WHERE status = 'open'"
            ) or 0
            filled_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM decision_log
                WHERE action IN ('follow', 'fade')
                  AND time >= NOW() - INTERVAL '24 hours'
                """
            ) or 0
            shadow_24h = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM mempool_observations
                WHERE fire_result = 'shadow'
                  AND intent_received_at >= NOW() - INTERVAL '24 hours'
                """
            ) or 0
            # Decisions per hour over the last hour (rate).
            decisions_last_hour = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM decision_log
                WHERE time >= NOW() - INTERVAL '1 hour'
                """
            ) or 0
            actionable = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM decision_log
                WHERE action IN ('follow', 'fade')
                  AND time >= NOW() - INTERVAL '1 hour'
                """
            ) or 0
            net_pnl = await conn.fetchval(
                """
                SELECT COALESCE(SUM(pnl_usdc), 0)::float
                FROM paper_trades
                WHERE status = 'closed'
                """
            ) or 0.0
        return {
            "positions_open": int(positions_open),
            "max_positions": max_positions,
            "filled_24h": int(filled_24h),
            "shadow_24h": int(shadow_24h),
            "decisions_per_hour": int(decisions_last_hour),
            "actionable": int(actionable),
            "net_pnl": float(net_pnl),
        }
    except Exception as exc:
        logger.warning(f"api_execution_summary failed: {exc}")
        return {
            "positions_open": 0,
            "max_positions": 10,
            "filled_24h": 0,
            "shadow_24h": 0,
            "decisions_per_hour": 0,
            "actionable": 0,
            "net_pnl": 0.0,
        }


# --- Calibration (R13) ----------------------------------------------------


@app.get("/api/calibration/losses")
async def api_calibration_losses(days: int = 30):
    """Per-model loss trajectory for the OPERATIONS / Calibration chart."""
    try:
        days = max(1, min(int(days), 365))
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model, strategy_class, measured_at,
                       brier_score, log_loss, mape, ci_coverage
                FROM calibration_loss_history
                WHERE measured_at >= CURRENT_DATE - ($1::int || ' days')::interval
                  AND strategy_class IS NULL
                ORDER BY model, measured_at
                """,
                days,
            )
        series_map: dict[str, list[dict]] = {}
        for r in rows:
            model = r["model"]
            col = _v2_loss_column_for(model)
            val = r[col]
            if val is None:
                continue
            series_map.setdefault(model, []).append({
                "day": r["measured_at"].isoformat() if r["measured_at"] else None,
                "loss": float(val),
            })
        series = [{"model": m, "points": pts} for m, pts in series_map.items()]
        return {"series": series, "days": days}
    except Exception as exc:
        logger.warning(f"api_calibration_losses failed: {exc}")
        return {"series": [], "days": days}


@app.get("/api/calibration/drift")
async def api_calibration_drift():
    """Per-model drift gauges with today's z-score + protected/disabled flags."""
    try:
        async with _pool.acquire() as conn:
            # Union of every model the system has ever seen.
            model_rows = await conn.fetch(
                """
                SELECT model FROM model_drift_streak
                UNION
                SELECT model FROM model_disable_state
                UNION
                SELECT DISTINCT model FROM calibration_loss_history WHERE strategy_class IS NULL
                """
            )
            models = sorted({r["model"] for r in model_rows if r["model"]})
            disabled_rows = await conn.fetch(
                "SELECT model, is_disabled FROM model_disable_state"
            )
            disabled_map = {r["model"]: bool(r["is_disabled"]) for r in disabled_rows}
            out_models = []
            for model in models:
                col = _v2_loss_column_for(model)
                stats = await conn.fetchrow(
                    f"""
                    SELECT AVG({col})::float AS mean,
                           STDDEV_POP({col})::float AS std,
                           (
                             SELECT {col}::float FROM calibration_loss_history
                             WHERE model = $1 AND strategy_class IS NULL
                             ORDER BY measured_at DESC LIMIT 1
                           ) AS today
                    FROM calibration_loss_history
                    WHERE model = $1 AND strategy_class IS NULL
                      AND measured_at >= CURRENT_DATE - INTERVAL '30 days'
                    """,
                    model,
                )
                z_score = None
                if stats and stats["today"] is not None and stats["mean"] is not None and stats["std"]:
                    try:
                        z_score = float((stats["today"] - stats["mean"]) / stats["std"])
                    except Exception:
                        z_score = None
                out_models.append({
                    "model": model,
                    "z_score": z_score,
                    "protected": model in _V2_PROTECTED_MODELS,
                    "disabled": disabled_map.get(model, False),
                })
        return {"models": out_models}
    except Exception as exc:
        logger.warning(f"api_calibration_drift failed: {exc}")
        return {"models": []}


@app.get("/api/calibration/disabled")
async def api_calibration_disabled():
    """List of currently disabled models."""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model, disabled_at, disabled_reason, auto_or_manual
                FROM model_disable_state
                WHERE is_disabled = TRUE
                ORDER BY disabled_at DESC NULLS LAST
                LIMIT 50
                """
            )
        out = [
            {
                "model": r["model"],
                "disabled_at": _v2_fmt_iso(r["disabled_at"]),
                "disabled_reason": r["disabled_reason"],
                "auto_or_manual": r["auto_or_manual"] or "auto",
            }
            for r in rows
        ]
        return {"rows": out}
    except Exception as exc:
        logger.warning(f"api_calibration_disabled failed: {exc}")
        return {"rows": []}


@app.post("/api/calibration/disable/{model}")
async def api_calibration_disable(model: str):
    """Manually disable a model via ModelAutoDisabler."""
    try:
        from src.calibration.auto_disable import get_auto_disabler
        await get_auto_disabler().disable_model(
            model=model,
            reason="manual operator override",
            auto_or_manual="manual",
        )
        return {"ok": True, "model": model, "auto_or_manual": "manual"}
    except Exception as exc:
        logger.warning(f"api_calibration_disable failed for {model!r}: {exc}")
        return {"ok": False, "model": model, "auto_or_manual": "manual"}


@app.post("/api/calibration/enable/{model}")
async def api_calibration_enable(model: str):
    """Manually re-enable a model via ModelAutoDisabler."""
    try:
        from src.calibration.auto_disable import get_auto_disabler
        await get_auto_disabler().enable_model(model)
        return {"ok": True, "model": model}
    except Exception as exc:
        logger.warning(f"api_calibration_enable failed for {model!r}: {exc}")
        return {"ok": False, "model": model}


# --- Research (R13 § 3.5) -------------------------------------------------


@app.post("/api/research/notebook/{notebook_id}/run")
async def api_research_notebook_run(notebook_id: str):
    """Kicks a `jupyter nbconvert --execute` for the notebook.

    Currently a no-op stub. Operator must implement the run dispatcher
    (papermill subprocess in a worker, status pushed via Redis). Until
    that lands the v2 NotebookTile just shows "Run queued" optimistically
    but no execution actually happens.
    """
    return {"ok": True, "notebook_id": notebook_id, "queued": False}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await _bridge.handle(ws)
