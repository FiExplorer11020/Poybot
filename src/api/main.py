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
LIVE_SNAPSHOT_TTL_S = 1.0
TERMINAL_SNAPSHOT_TTL_S = 1.0
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
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
        )
        created_pool = True
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
    logger.info("Dashboard API started")
    yield
    push_task.cancel()
    try:
        await get_runtime_config().stop_pubsub()
    except Exception:
        pass
    await _bridge.stop()
    if created_pool and _pool:
        await _pool.close()
        _pool = None
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
    now = time.monotonic()
    cached = _terminal_snapshot_cache.get("data")
    if (
        not force
        and cached is not None
        and now - float(_terminal_snapshot_cache.get("last_built", 0.0) or 0.0)
        < TERMINAL_SNAPSHOT_TTL_S
    ):
        return copy.deepcopy(cached)

    async with _terminal_snapshot_lock:
        now = time.monotonic()
        cached = _terminal_snapshot_cache.get("data")
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return HTMLResponse(content=TEMPLATE_PATH.read_text())


@app.get("/api/overview")
async def api_overview():
    snapshot = await _get_live_snapshot()
    data = {key: value for key, value in snapshot.items() if key != "ml"}
    return data


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
    snapshot = await _get_live_snapshot()
    return snapshot.get("ml", {})


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


# --- Overview --------------------------------------------------------------


@app.get("/api/overview/timeline")
async def api_overview_timeline():
    """Last 5 'what changed' events for the OVERVIEW timeline panel.

    Operator wire-up: the calibration daemon + control endpoints already
    log these; this endpoint joins them into a single feed. Returns an
    empty list until that join lands.
    """
    return {"events": []}


@app.get("/api/calibration/summary")
async def api_calibration_summary():
    """R13 high-level rollup for the OVERVIEW Mirror bento card."""
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
    return {"intents": [], "nonce_chains": []}


@app.get("/api/mempool/pool")
async def api_mempool_pool():
    return {"entries": [], "miss_reasons_last_hour": {}}


@app.get("/api/mempool/decisions")
async def api_mempool_decisions(filter: str = "all"):  # noqa: A002
    return {"decisions": [], "filter": filter}


# --- Microscope (R11) ------------------------------------------------------


@app.get("/api/microscope/summary")
async def api_microscope_summary():
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
    return {"events": []}


@app.get("/api/microscope/microstructure")
async def api_microscope_microstructure(limit: int = 50):
    return {"rows": [], "limit": limit}


@app.get("/api/microscope/signatures")
async def api_microscope_signatures(limit: int = 100):
    return {"signatures": [], "limit": limit}


# --- Periphery (R12 + R10 instruments) -------------------------------------


@app.get("/api/periphery/summary")
async def api_periphery_summary():
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
    return {"signals": []}


@app.get("/api/periphery/crossmarket/status")
async def api_periphery_crossmarket_status():
    return {
        "kalshi":    {"reachable": False, "latency_p50_ms": None, "api_calls_24h": 0, "positions_observed": 0},
        "manifold":  {"reachable": False, "latency_p50_ms": None, "api_calls_24h": 0, "positions_observed": 0},
        "predictit": {"reachable": False, "latency_p50_ms": None, "api_calls_24h": 0, "positions_observed": 0},
    }


@app.get("/api/periphery/crossmarket/operators")
async def api_periphery_crossmarket_operators():
    return {"operators": []}


@app.post("/api/periphery/crossmarket/confirm/{op_id}")
async def api_periphery_crossmarket_confirm(op_id: int):
    """Operator confirms an auto-suggested cross-market resolution.

    Wires to src.cross_market.wallet_resolver.WalletResolver.confirm_match.
    Currently a no-op stub.
    """
    return {"ok": True, "operator_id": op_id}


# --- Intelligence (R8 Lens, R9 Web, R10 Causal) ---------------------------


@app.get("/api/intelligence/lens/distribution")
async def api_intelligence_lens_distribution():
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
    return {"wallets": []}


@app.get("/api/intelligence/web/summary")
async def api_intelligence_web_summary():
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
    return {"events": []}


# --- Wallet Lab (R6 universe + augmented profile) -------------------------


@app.get("/api/wallet/universe")
async def api_wallet_universe(limit: int = 200):
    """R6 wallet_universe browser. Joins on depth_tier + strategy_class
    where available."""
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
    return {"wallet": wallet, "probs": {}, "last_trained_at": None, "drift_score": None}


@app.get("/api/wallet/{wallet}/microstructure")
async def api_wallet_microstructure(wallet: str):
    """R11 per-wallet microstructure signature."""
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
    return {"series": [], "days": days}


@app.get("/api/calibration/drift")
async def api_calibration_drift():
    """Per-model drift gauges."""
    return {"models": []}


@app.get("/api/calibration/disabled")
async def api_calibration_disabled():
    """List of currently disabled models."""
    return {"rows": []}


@app.post("/api/calibration/disable/{model}")
async def api_calibration_disable(model: str):
    return {"ok": True, "model": model, "auto_or_manual": "manual"}


@app.post("/api/calibration/enable/{model}")
async def api_calibration_enable(model: str):
    return {"ok": True, "model": model}


# --- Research (R13 § 3.5) -------------------------------------------------


@app.post("/api/research/notebook/{notebook_id}/run")
async def api_research_notebook_run(notebook_id: str):
    """Kicks a `jupyter nbconvert --execute` for the notebook.

    Currently a no-op stub. Operator must implement the run dispatcher.
    """
    return {"ok": True, "notebook_id": notebook_id, "queued": False}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await _bridge.handle(ws)
