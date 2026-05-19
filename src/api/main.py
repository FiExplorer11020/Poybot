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
# TEMPLATE_V2_PATH removed 2026-05-17 — V2 dashboard deleted (R6-R13
# features now exposed via the V1 LAB tab; see static/dashboard/dashboard-tabs.jsx
# LabGates component + /api/lab/gates endpoint).
STATIC_DIR = Path(__file__).parent.parent.parent / "static"

# ---------------------------------------------------------------------------
# Pre-computed snapshot — Redis-backed (Phase: Precomputed Snapshot, 2026-05-17)
# ---------------------------------------------------------------------------
# The maintenance container (scripts/maintenance_loop.py +
# src/api/snapshot_builder.py) composes the live-summary JSON sequentially
# every ~30s and writes it to Redis. The API endpoint just serves the
# cached value, eliminating pool DB contention from user requests.
#
# These constants are duplicated in src/api/snapshot_builder.py so the
# maintenance container can import them without depending on this module.
# When snapshot_builder.py ships, this file should import from there
# rather than redefining the literals.
SNAPSHOT_REDIS_KEY = "snapshot:live_summary"
SNAPSHOT_BUILT_AT_KEY = "snapshot:live_summary:built_at"
# Skeleton served when Redis has no snapshot yet (cold start / maintenance
# container down). Matches the shape consumed by static/dashboard/api-client.js
# closely enough for the dashboard to render shells without exploding.
_SKELETON = {
    "clock": {"updated_at": None, "warming_up": True},
    "meta": {},
    "bot": {"status": "warming_up"},
    "stats": {},
    "positions": {"open": [], "closed": [], "stats": {}},
    "wallet_graph": {"nodes": [], "edges": [], "stats": {}},
}
STATS_PUSH_INTERVAL_S = 1.0  # how often to push live stats over WebSocket
HEALTH_CACHE_TTL_S = 5.0
LIVE_SNAPSHOT_TTL_S = 5.0
TERMINAL_SNAPSHOT_TTL_S = 5.0
# Background rebuilder cadence. Each cycle calls _get_terminal_snapshot(force=True)
# so the cache stays warm. Aligned with V1 client poll interval (5s) — by the
# time the V1 client polls, a fresh snapshot is already in cache.
SNAPSHOT_REBUILDER_INTERVAL_S = 10.0
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
        # Pool sized for the asyncio.gather() in _get_terminal_snapshot
        # (17 parallel sub-queries) + concurrent V1+V2 clients + observer
        # sharing the same pool. Sized from settings.DB_POOL_MAX (default
        # 25 after the May 17 V1 audit Phase 3 bump) so the limit can be
        # tuned via env without a code change. min_size kept at 4 to avoid
        # cold-start penalty on the rebuild loop.
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=max(4, settings.DB_POOL_MIN),
            max_size=settings.DB_POOL_MAX,
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

        # B7+B8 fix (2026-05-19): when either the DB acquire or the
        # pipeline-stage snapshot raises, we must surface "unknown" in
        # the downstream stage_status rather than silently report
        # "blocked" / "empty" (which the dashboard renders as RED — the
        # operator then chases a phantom failure that's really a DB
        # timeout). Track the failure explicitly so the stage_status
        # block below can fall back to "unknown".
        pipeline_health_failed = False
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
                    pipeline_health_failed = True
                    pipeline_stage_health = {}
            db_ok = True
            last_trade_age_s = float(last) if last is not None else None
            data_accumulation_counts = db_quality.get("counts", {})
        except Exception as e:
            logger.warning(f"DB health check failed: {e}")
            db_quality = {}
            pipeline_health_failed = True

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
        # B7+B8 fix (2026-05-19): differentiate "unknown" (DB / snapshot
        # query failed) from "blocked" / "empty" (DB returned but the
        # counters were zero). Without this, a DB timeout silently
        # presents as a hard failure on the dashboard.
        if pipeline_health_failed:
            book_capture_status = "unknown"
            readiness_status = "unknown"
        else:
            book_capture_status = (
                "healthy"
                if book_age_p95_s is not None
                and int(pipeline_stage_health.get("book_quality_snapshots_5m") or 0) > 0
                else "blocked"
            )
            readiness_status = (
                "active"
                if int(pipeline_stage_health.get("market_belief_states") or 0) > 0
                else "empty"
            )
        pipeline_stage_health["stage_status"] = {
            "book_capture": book_capture_status,
            "readiness_persistence": readiness_status,
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
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.overview(conn, redis_client=_redis)
    return await _cached_helper("overview", _build)


# ------------------------------------------------------------------------- #
# In-process TTL cache for slow helpers                                      #
# ------------------------------------------------------------------------- #
# The terminal snapshot rebuild calls 17 helpers in parallel via gather().
# The slowest dominate `last_duration_ms`. Profiling on prod (V1 audit
# Phase 3) measured:
#   - queries.data_quality       : 15.7s  ← dominant
#   - queries.ml_summary         :  8.0s
#   - queries.activation_queue   :  4.7s  (used by neural-readiness)
#
# These compute operational stats that change slowly. Caching their
# results for 30-60s collapses snapshot rebuild duration from ~25s to
# ~2s while keeping the data fresh enough for a dashboard. The cache
# is a plain `dict` keyed by helper name — no Redis round-trip
# because the snapshot rebuilder lives in the same process.
_HELPER_CACHE: dict = {}  # key -> {"data": ..., "fetched_at_mono": float}
_HELPER_CACHE_TTLS = {
    # 2026-05-18 (A6 perf pass): bumped TTLs for slow helpers. Rebuild
    # can take 20-40s under parallel pool pressure; if TTL < rebuild
    # time, the cache "expires before it's written" → perpetual
    # cold-start loop. Rule of thumb: TTL >= max(rebuild × 5, 600s) for
    # helpers whose rebuild has been observed > 60s. _cached_helper now
    # records rebuild durations and emits a structured warning when a
    # rebuild took more than half of its TTL — surfaces the next round
    # of TTL tuning candidates without a separate profiling pass.
    "data_quality": 600.0,   # 20-30s rebuild on prod (15.7s baseline) → 20x margin
    "ml_summary":   600.0,   # 8s rebuild → keep parity with data_quality
    "ml_diagnostics": 120.0,
    "activation":   180.0,
    "system":       120.0,   # 20s query, bump to 120 to stay cached
    "alpha_extras": 600.0,   # 60s+ rebuild → 10x margin (was 180)
    "wallet_graph": 120.0,   # 15s query, comfortable margin
    "rejections":   15.0,
    "equity_curve_v2": 30.0,
    "market_scanner": 30.0,
    "neural_readiness": 10.0,  # /api/neural-readiness compose health+activation+risk+ml
    # Phase 1 V2 audit — slow endpoints with high poll frequency.
    # NOTE on TTLs: a TTL shorter than the rebuild duration means the
    # cache "expires before it is written" — every poll triggers a
    # fresh rebuild. Inspector takes 5-11s to build, so a 3s TTL was
    # useless. We size TTL ≥ 3× max rebuild duration.
    "decisions_200": 10.0,       # /api/decisions?limit=200 (~500ms build), V2 polls 5s
    "wallet_universe_500": 30.0, # /api/wallet/universe?limit=500 (~500ms build), V2 polls 30s
    "leaders_200": 30.0,         # /api/leaders?limit=200 (~1.5s build), V2 polls 30s
    "inspector_snapshot": 30.0,  # /api/inspector/snapshot (~5-11s build), V2 polls 3s
    # Phase 2 V2 audit — OPS endpoints filling the R6/R7 gaps:
    "ops_fee_snapshots": 30.0,
    "ops_chain_sync": 10.0,
    "ops_rpc_health": 15.0,
    "ops_mempool_wallet_index": 15.0,
    # PLAN-UIA-001 (2026-05-18) — mission-alignment additions. Recon
    # summary changes at most every 5 min (cron pre-warm); pillars are
    # boolean health checks, no point polling faster than the dashboard
    # refresh.
    "recon_summary_30d": 30.0,
    "pillars_status": 30.0,
    # Dashboard hot-path (229s rebuild fix, V1 audit Phase 3, May 17 session).
    # These helpers used to bypass the cache entirely; every parallel
    # gather() in _get_terminal_snapshot re-queried Postgres. Short TTLs
    # (5-10s) keep the data dashboard-fresh while collapsing the pool
    # contention seen at SNAPSHOT_REBUILDER_INTERVAL_S=5s.
    "overview": 5.0,
    "recent_trades": 5.0,
    "positions": 5.0,
    "decisions": 5.0,
    "decisions_stats": 5.0,
    "risk": 10.0,
    "activation": 10.0,
}


async def _safe_query(query_fn, default=None):
    """Execute a query function with its own pool connection,
    swallowing exceptions and returning the default on error.

    Used by parallel `asyncio.gather()` sites where a single failing
    sub-query should NOT crash the composite endpoint. Mirrors the
    legacy try/except pattern used before parallelization.
    """
    try:
        async with _pool.acquire() as conn:
            return await query_fn(conn)
    except Exception as exc:
        logger.warning(f"_safe_query for {query_fn.__name__} failed: {exc}")
        return default


# Telemetry: track per-key rebuild durations so we can flag cold-start
# loops (rebuild took more than half of its TTL → cache expires before
# it's even written for the next caller). Updated by _cached_helper on
# every miss; read-only metric, no eviction.
_HELPER_REBUILD_STATS: dict[str, dict] = {}
# Cooldown so the cold-start warning doesn't spam: log at most once per
# key per cooldown window. Keyed by helper name → monotonic timestamp.
_HELPER_COLDSTART_WARN_AT: dict[str, float] = {}
_HELPER_COLDSTART_WARN_COOLDOWN_S = 300.0


async def _cached_helper(key: str, builder):
    """Wrap an async builder fn with an in-process TTL cache.

    The cache is keyed by `key` (string identifier of the helper).
    TTL comes from `_HELPER_CACHE_TTLS[key]`. If the cached value is
    fresh enough, return it directly. Otherwise call `builder()`,
    update the cache, and return the new value.

    Concurrent calls for the same key while the value is being
    rebuilt will both hit the builder. That's fine for our use case
    (the rebuilder loop is single-threaded). For multi-threaded
    callers add an asyncio.Lock per key if needed.

    Telemetry (added 2026-05-18, A6 perf pass): on every cache miss we
    record the rebuild duration in `_HELPER_REBUILD_STATS[key]` and, if
    the rebuild took more than half of the configured TTL, emit a
    structured WARNING ("cache_ttl_too_short ..."). That's the cold-start
    loop fingerprint: TTL < rebuild_s × 2 means the cache effectively
    expires before the next caller can hit it, so every poll re-runs
    the slow query. The warning is throttled per key
    (_HELPER_COLDSTART_WARN_COOLDOWN_S) to keep the log readable.
    """
    ttl = _HELPER_CACHE_TTLS.get(key, 30.0)
    now = time.monotonic()
    cached = _HELPER_CACHE.get(key)
    if cached and (now - cached["fetched_at_mono"]) < ttl:
        return cached["data"]

    rebuild_start = time.monotonic()
    data = await builder()
    rebuild_end = time.monotonic()
    rebuild_s = rebuild_end - rebuild_start

    # Update rolling per-key telemetry (max + EWMA).
    stats = _HELPER_REBUILD_STATS.setdefault(
        key, {"last_s": 0.0, "max_s": 0.0, "ewma_s": 0.0, "n": 0}
    )
    stats["last_s"] = rebuild_s
    stats["max_s"] = max(stats["max_s"], rebuild_s)
    # EWMA with λ=0.5 — fast convergence on rebuild-time changes.
    stats["ewma_s"] = (
        rebuild_s if stats["n"] == 0 else 0.5 * stats["ewma_s"] + 0.5 * rebuild_s
    )
    stats["n"] += 1

    _HELPER_CACHE[key] = {"data": data, "fetched_at_mono": rebuild_end}

    # Cold-start loop guard: warn when the rebuild took more than half
    # of the TTL. We use `rebuild_s * 2 > ttl` rather than
    # `rebuild_s > ttl / 2` to make the threshold relationship explicit
    # in code review.
    if rebuild_s * 2 > ttl:
        last_warn = _HELPER_COLDSTART_WARN_AT.get(key, 0.0)
        if (now - last_warn) > _HELPER_COLDSTART_WARN_COOLDOWN_S:
            _HELPER_COLDSTART_WARN_AT[key] = now
            logger.warning(
                "cache_ttl_too_short namespace={key} rebuild_s={rebuild_s:.2f} "
                "ttl_s={ttl:.1f} max_s={max_s:.2f} ewma_s={ewma_s:.2f} "
                "suggested_ttl_s={suggest:.0f}",
                key=key,
                rebuild_s=rebuild_s,
                ttl=ttl,
                max_s=stats["max_s"],
                ewma_s=stats["ewma_s"],
                suggest=max(stats["max_s"] * 5.0, 600.0),
            )

    return data


async def _fetch_ml_snapshot() -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.ml_summary(conn)
    return await _cached_helper("ml_summary", _build)


async def _fetch_system_snapshot() -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            # Batch 2 fix #2: pass redis_client so system_status can
            # populate the canonical bot_status/ws_status block.
            return await queries.system_status(conn, redis_client=_redis)
    return await _cached_helper("system", _build)


async def _fetch_positions_snapshot() -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.positions(conn)
    return await _cached_helper("positions", _build)


async def _fetch_positions_live_snapshot() -> list[dict]:
    # NOT cached — live prices read from Redis on every call; the snapshot
    # rebuilder loop already gates the cadence.
    async with _pool.acquire() as conn:
        return await queries.open_positions_with_prices(conn, _redis)


async def _fetch_decisions_snapshot(limit: int = 60) -> list[dict]:
    # The cache key is fixed (no `limit` in the key) — every dashboard caller
    # uses the default `limit=60`. Other limits go through /api/decisions
    # which has its own dedicated `decisions_<N>` cache entries.
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.decisions(conn, limit=limit, offset=0)
    if limit != 60:
        # Bypass cache for non-default limits to avoid leaking stale rows
        # for callers that ask for a different page size.
        return await _build()
    return await _cached_helper("decisions", _build)


async def _fetch_decisions_stats_snapshot(window_hours: int = 24) -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.decisions_stats(conn, window_hours=window_hours)
    if window_hours != 24:
        # Bypass cache for non-default windows (only callsite uses 24h).
        return await _build()
    return await _cached_helper("decisions_stats", _build)


async def _fetch_risk_snapshot() -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.risk(conn)
    return await _cached_helper("risk", _build)


async def _fetch_activation_snapshot() -> list[dict]:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.activation_queue(conn)
    return await _cached_helper("activation", _build)


async def _fetch_data_quality_snapshot() -> dict:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.data_quality(conn, redis_client=_redis)
    return await _cached_helper("data_quality", _build)


async def _fetch_market_scanner_rows(limit: int = 60) -> list[dict]:
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.market_scanner_rows(conn, limit=limit)
    return await _cached_helper("market_scanner", _build)


async def _fetch_recent_observed_trades(limit: int = 60) -> list[dict]:
    # Cached briefly (5s) — V1 client expects fresh trade tape but the
    # snapshot rebuilder runs every 10s, so caching one cycle is enough
    # to avoid double-querying when the bootstrap fallback fires in the
    # same window. WS push remains the source of truth for low-latency
    # updates on the client.
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.recent_observed_trades(conn, limit=limit)
    if limit != 60:
        # Bypass cache for non-default limits.
        return await _build()
    return await _cached_helper("recent_trades", _build)


async def _fetch_alpha_extras() -> dict:
    """ALPHA TERMINAL v2 — 24h timeline + Next Signal ETA + ML totals."""
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.alpha_extras(conn)
    return await _cached_helper("alpha_extras", _build)


async def _fetch_wallet_graph() -> dict:
    """WALLET GRAPH — nodes + edges for force-directed visualisation."""
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.wallet_graph(conn)
    return await _cached_helper("wallet_graph", _build)


async def _fetch_rejections_breakdown() -> dict:
    """ML PROGRESSION — last-hour SKIP reasons grouped."""
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.decision_rejections_breakdown(conn, hours=1)
    return await _cached_helper("rejections", _build)


async def _fetch_equity_curve_v2() -> dict:
    """LIVE PORTFOLIO — equity series + by-leader / by-strategy breakdown."""
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.equity_curve(conn)
    return await _cached_helper("equity_curve_v2", _build)


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
    # If cache is None and we're not the rebuilder, do NOT block on lock —
    # return an empty skeleton so the dashboard renders shells while the
    # background rebuilder warms the cache.
    if not force and cached is None:
        return {
            "clock": {"updated_at": datetime.now(timezone.utc).isoformat(),
                      "warming_up": True},
            "meta": {},
            "bot": {"status": "starting"},
            "stats": {},
            "positions": {"open": [], "closed": [], "stats": {}},
            "wallet_graph": {"nodes": [], "edges": [], "stats": {}},
        }

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
        # 2026-05-17: generous per-fetcher timeout (30s). Cold-start needs
        # the slow queries to actually COMPLETE so the cache fills; user
        # endpoint returns skeleton (non-blocking) while rebuilder warms.
        # Heavy fetchers get 45s. Once cache is warm, _cached_helper returns
        # in <1ms regardless.
        async def _tof(coro, name, timeout=30.0):
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"snapshot fetcher '{name}' TIMEOUT >{timeout}s")
                return TimeoutError(name)
            except Exception as e:
                logger.warning(f"snapshot fetcher '{name}' ERROR: {e}")
                return e
        # Heavy fetchers get 8s; the rest get 5s.
        results = await asyncio.gather(
            _tof(_fetch_overview_snapshot(), "overview"),
            _tof(_fetch_ml_snapshot(), "ml"),
            _tof(_fetch_system_snapshot(), "system"),
            _tof(_fetch_positions_live_snapshot(), "positions_live"),
            _tof(_fetch_positions_snapshot(), "positions"),
            _tof(_fetch_decisions_snapshot(), "decisions"),
            _tof(_fetch_decisions_stats_snapshot(), "decision_stats"),
            _tof(_fetch_risk_snapshot(), "risk"),
            _tof(_fetch_activation_snapshot(), "activation", timeout=45.0),
            _tof(_fetch_data_quality_snapshot(), "data_quality", timeout=45.0),
            _tof(_health_checks(), "health"),
            _tof(_fetch_market_scanner_rows(), "market_rows", timeout=45.0),
            _tof(_fetch_recent_observed_trades(), "observed_trades"),
            _tof(_fetch_alpha_extras(), "alpha_extras", timeout=45.0),
            _tof(_fetch_wallet_graph(), "wallet_graph", timeout=45.0),
            _tof(_fetch_rejections_breakdown(), "rejections"),
            _tof(_fetch_equity_curve_v2(), "equity_curve"),
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
        # PLAN-UIA-001: surface execution_mode in `runtime` (passed to bot block).
        if runtime_overrides:
            mode = runtime_overrides.get("trading_mode") or runtime_overrides.get("TRADING_MODE")
            if mode:
                runtime["execution_mode"] = mode

        # PLAN-UIA-001: fetch reconciliation summary + pillars status in
        # parallel with the existing snapshot work. Both are cached at
        # the helper layer; the work here is just an async dispatch.
        async def _build_recon():
            async with _pool.acquire() as conn:
                return await _recon_q.reconciliation_summary(conn, window_days=30)

        async def _build_pillars():
            async with _pool.acquire() as conn:
                return await _pillars_q.pillars_status(conn, redis_client=_redis)

        try:
            recon_for_snapshot, pillars_for_snapshot = await asyncio.gather(
                _cached_helper("recon_summary_30d", _build_recon),
                _cached_helper("pillars_status", _build_pillars),
                return_exceptions=True,
            )
            if isinstance(recon_for_snapshot, Exception):
                logger.debug(f"snapshot recon fetch failed: {recon_for_snapshot}")
                recon_for_snapshot = None
            if isinstance(pillars_for_snapshot, Exception):
                logger.debug(f"snapshot pillars fetch failed: {pillars_for_snapshot}")
                pillars_for_snapshot = None
        except Exception as exc:
            logger.debug(f"snapshot recon+pillars gather failed: {exc}")
            recon_for_snapshot = None
            pillars_for_snapshot = None

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
            reconciliation=recon_for_snapshot,
            health_pillars=pillars_for_snapshot,
        )
        build_ms = round((time.perf_counter() - build_started) * 1000, 2)
        snapshot.setdefault("bot", {})["cycle_latency_ms"] = build_ms

        _terminal_snapshot_cache["data"] = copy.deepcopy(snapshot)
        _terminal_snapshot_cache["last_built"] = now
        return snapshot


async def _stats_push_loop() -> None:
    """Push fresh snapshot to all connected WS clients every STATS_PUSH_INTERVAL_S.

    Now Redis-backed: reads the same payload the V1 endpoint serves, so
    WS subscribers and HTTP pollers see byte-identical snapshots. No DB
    pool contention — just a single GET against Redis.

    If the snapshot key is absent (cold start) or Redis raises, we skip
    the tick rather than broadcasting noise. The next cycle retries.
    """
    while True:
        await asyncio.sleep(STATS_PUSH_INTERVAL_S)
        if not _bridge.has_connections:
            continue
        try:
            raw = await _redis.get(SNAPSHOT_REDIS_KEY)
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                data = json.loads(raw)
            except (ValueError, TypeError) as exc:
                logger.warning(f"Stats push loop: bad snapshot JSON: {exc}")
                continue
            # The Redis-stored payload is the inner snapshot dict; we wrap
            # it in {"data": ...} only at the HTTP layer. For the WS tick,
            # we keep the legacy shape (broadcast payload is the snapshot
            # itself, not wrapped in {"data": ...}).
            payload = data.get("data", data) if isinstance(data, dict) else data
            await _bridge.broadcast({"type": "tick", "payload": payload})
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
    """No-op stub — snapshot composition now lives in the maintenance container.

    NOTE (2026-05-17, precomputed-snapshot refactor):
    The in-process rebuilder is intentionally disabled. The snapshot is
    now composed by `scripts/maintenance_loop.py` via
    `src/api/snapshot_builder.py`, written to Redis under
    `SNAPSHOT_REDIS_KEY`, and served by `/api/v1/live-summary` with a
    single Redis GET. This eliminates the 17-way `asyncio.gather()` that
    used to saturate the DB pool from user requests.

    The function body is preserved so:
      * External imports (`from src.api.main import _snapshot_rebuilder_loop`)
        keep working without an ImportError.
      * The task spawned by `lifespan` (kept for now to avoid a config
        ripple) loops harmlessly instead of crashing.
      * A future fallback mode could re-enable the in-process path by
        flipping a feature flag without restoring the implementation.

    If you ever need the old behaviour, restore from git history
    (commit prior to 2026-05-17 precomputed-snapshot refactor).
    """
    while True:
        try:
            await asyncio.sleep(60)
            logger.debug("rebuilder loop is no-op (replaced by maintenance)")
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
                -- Batch 2 fix #4: filter for the canonical action set
                -- (both legacy lower-case and uppercase) so the counter
                -- matches what reconciliation reports. Excluding NULL
                -- actions defends against an old schema row leaking
                -- through and inflating the count.
                (SELECT COUNT(*)::int FROM decision_log
                 WHERE time >= NOW() - INTERVAL '24 hours'
                   AND action IS NOT NULL
                   AND LOWER(action) IN
                       ('follow','fade','skip','open','close','reduce','volume_anticipation'))            AS decisions_24h,
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
    # Process uptime — B3v2 fix (2026-05-19): routed through the shared
    # `src.control.uptime.get_bot_uptime_seconds` helper so the live API
    # path and the maintenance snapshot builder return the same value.
    # The helper prefers `bot:engine:started_at` Redis (canonical, set by
    # the engine container) and falls back to `_api_started_at` (API
    # module load time) when Redis is unavailable.
    try:
        from src.control.uptime import get_bot_uptime_seconds

        uptime_seconds = await get_bot_uptime_seconds(
            _redis, fallback_started_at=_api_started_at
        )
    except Exception as exc:
        logger.warning(f"uptime: helper raised: {exc}")
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
async def api_leaders(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
):
    """List leaders with profile + follower counts.

    Paginated to keep payload sane (was 961 KB / 1560 rows for the
    full set). Default `limit=200` covers the Wallet Scanner first
    paint comfortably. Frontends needing more can request additional
    pages via offset.

    Cached 30s on the default V2 query (limit=200, offset=0). Other
    pages bypass cache for accurate cursor traversal.
    """
    if limit == 200 and offset == 0:
        async def _build():
            async with _pool.acquire() as conn:
                return await queries.leaders(conn, limit=200, offset=0)
        return await _cached_helper("leaders_200", _build)
    async with _pool.acquire() as conn:
        return await queries.leaders(conn, limit=limit, offset=offset)


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
    """High-signal ML pipeline indicators for tracking development.

    Cached 30s — this is an aggregate over `leader_profiles` (730 rows
    with JSONB parsing) which takes ~8s on first call. Refreshed in
    the background by the snapshot rebuilder.
    """
    async def _build():
        async with _pool.acquire() as conn:
            return await queries.ml_diagnostics(conn)
    return await _cached_helper("ml_diagnostics", _build)


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
    """Recent decisions feed. Cached 5s when paginated default params.

    The V2 ExecutionDecisions sub-tab polls this every 5s with
    limit=200, generating a 407 KB payload per poll. The cache lets
    multiple subscribers share one DB roundtrip per 5s window.
    Custom (limit/offset) params still bypass the cache so drill-down
    pagination stays accurate.
    """
    # Only cache the "default" V2 query (limit=200, offset=0) — other
    # combos are likely drill-downs that need accurate cursors.
    if limit == 200 and offset == 0:
        async def _build():
            async with _pool.acquire() as conn:
                return await queries.decisions(conn, limit=200, offset=0)
        return await _cached_helper("decisions_200", _build)
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
        # Batch 2 fix #4 — filter for the canonical action set so the
        # KPI matches the reconciliation source of truth instead of
        # drifting to 0 when the engine emits only legacy values.
        try:
            decisions_24h = await conn.fetchval(
                "SELECT COUNT(*)::int FROM decision_log "
                "WHERE time >= NOW() - INTERVAL '24 hours' "
                "  AND action IS NOT NULL "
                "  AND LOWER(action) IN "
                "      ('follow','fade','skip','open','close','reduce','volume_anticipation')"
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
    """Neural readiness composite — cached 10s.

    Composes health checks + activation queue + risk + ml_summary.
    The underlying queries.activation_queue is ~5s; with the new
    cache layer + parallel fetch, this endpoint drops to ~50ms warm.
    """
    return await _cached_helper("neural_readiness", _neural_readiness_build)


async def _neural_readiness_build() -> dict:
    """Uncached neural-readiness builder (called by the cache wrapper)."""
    # Run the 3 independent queries in parallel for ~3x speedup.
    health, activation, risk, ml = await asyncio.gather(
        _health_checks(),
        _safe_query(queries.activation_queue, default=[]),
        _safe_query(queries.risk, default={"open_count": 0, "drawdown_pct": 0.0}),
        _safe_query(queries.ml_summary, default={}),
    )
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
        # Batch 2 fix #2: redis_client passed so the canonical bot_status
        # / ws_status block is computed alongside leaders/graph.
        data = await queries.system_status(conn, redis_client=_redis)
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
    """Silent-rot detector + market enrichment gaps.

    Cached 60s — the underlying SQL has a heavy LEFT JOIN
    trades_observed × markets that scanned 7 days of partitions
    (~15s on prod). The output rarely changes minute-to-minute,
    so a 60s TTL is comfortable for operators.
    """
    return await _fetch_data_quality_snapshot()


@app.get("/api/v1/live-summary")
async def api_live_summary_v1(request: Request, response: Response):
    """Redis-backed snapshot endpoint.

    The snapshot is composed by the maintenance container (see
    scripts/maintenance_loop.py + src/api/snapshot_builder.py) and
    stored as JSON in Redis. This endpoint just serves the cached
    value with <10ms latency, eliminating pool DB contention from
    user requests.

    Behaviour:
      * Redis present + populated → 200 with payload + ETag.
      * Redis present, key missing (cold start, maintenance down for >120s)
        → 503 with skeleton + warming_up flag so the dashboard renders
        shells instead of blank.
      * Redis unavailable (raises) → 503 with skeleton + error flag.
      * `If-None-Match` matches our ETag → 304 (zero-body response).
      * Built-at age > 60s → adds `X-Snapshot-Stale-Age` header so the
        dashboard can display a "data refresh paused" indicator.
    """
    try:
        raw_bytes = await _redis.get(SNAPSHOT_REDIS_KEY)
        built_at_raw = await _redis.get(SNAPSHOT_BUILT_AT_KEY)
    except Exception as exc:
        logger.warning(f"snapshot redis read failed: {exc}")
        response.status_code = 503
        return {"data": _SKELETON, "warming_up": True, "error": "redis_unavailable"}

    if raw_bytes is None:
        response.status_code = 503
        return {"data": _SKELETON, "warming_up": True}

    # Compute age + stale warning
    try:
        built_at = float(built_at_raw) if built_at_raw else 0.0
    except Exception:
        built_at = 0.0
    age_s = max(0.0, time.time() - built_at) if built_at else None

    # ETag = hash of raw bytes (already serialized by the builder, so
    # repeated calls yield identical hashes — no JSON re-encoding round-trip).
    if isinstance(raw_bytes, bytes):
        raw = raw_bytes.decode("utf-8")
    else:
        raw = raw_bytes
    etag = '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] + '"'

    headers = {"ETag": etag, "Cache-Control": "private, no-cache, must-revalidate"}
    if age_s is not None and age_s > 60.0:
        headers["X-Snapshot-Stale-Age"] = str(round(age_s, 1))

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    return Response(content=raw, media_type="application/json", headers=headers)


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


# ---------------------------------------------------------------------------
# OPS endpoints — fill the 4 R6/R7 visibility gaps identified in the V2
# audit. These surface internal pipeline state (fee_snapshots freshness,
# onchain verifier cursor, RPC provider liveness, mempool bloom filter
# size) so the V2 OPERATIONS tab can show real status instead of just
# "running" / "gated" heuristics.
# ---------------------------------------------------------------------------


@app.get("/api/ops/fee-snapshots/status")
async def api_ops_fee_snapshots_status():
    """Fee-snapshot pipeline freshness.

    The economic gate in src/economics/gates.py rejects every
    FOLLOW/FADE decision unless a fresh (< 24h) fee_snapshot exists
    for the (market_id, token_id). This endpoint surfaces whether
    the snapshot pipeline is keeping up.

    Returns: counts per source, latest captured_at, age in hours.
    Cached 30s — fee_snapshots only get rebuilt every few hours.
    """
    async def _build():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int                                   AS total,
                    COUNT(DISTINCT market_id)::int                  AS unique_markets,
                    COUNT(DISTINCT (market_id, token_id))::int      AS unique_tokens,
                    MAX(captured_at)                                AS latest_captured_at,
                    EXTRACT(EPOCH FROM (NOW() - MAX(captured_at)))::int AS age_seconds
                FROM fee_snapshots
                """
            )
            sources = await conn.fetch(
                """
                SELECT source, COUNT(*)::int AS n, MAX(captured_at) AS latest
                FROM fee_snapshots GROUP BY source ORDER BY n DESC
                """
            )
        age_s = int(row["age_seconds"] or 0) if row else 0
        # Stale = older than the gate's max_fee_age (24h)
        stale = age_s > 24 * 3600
        return {
            "total": int(row["total"] or 0) if row else 0,
            "unique_markets": int(row["unique_markets"] or 0) if row else 0,
            "unique_tokens": int(row["unique_tokens"] or 0) if row else 0,
            "latest_captured_at": (
                row["latest_captured_at"].isoformat()
                if row and row["latest_captured_at"] else None
            ),
            "age_seconds": age_s,
            "age_hours": round(age_s / 3600, 2),
            "stale": stale,
            "by_source": [
                {
                    "source": r["source"],
                    "count": int(r["n"] or 0),
                    "latest": r["latest"].isoformat() if r["latest"] else None,
                }
                for r in sources
            ],
        }
    return await _cached_helper("ops_fee_snapshots", _build)


@app.get("/api/ops/chain-sync")
async def api_ops_chain_sync():
    """Onchain verifier (R6) cursor + lag.

    The `chain_sync_state` singleton tracks where the verifier mode
    onchain daemon is in the Polygon chain. We expose:
      - last_processed_block
      - last_updated_at + age
      - blocks_behind_at_write
      - extra metadata (decoded event types, etc.)
    """
    async def _build():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_processed_block, last_updated_at,
                       blocks_behind_at_write, metadata
                FROM chain_sync_state
                WHERE id = 'singleton'
                """
            )
        if row is None:
            return {"present": False, "message": "no chain_sync_state row yet — onchain verifier may not have produced a cursor"}
        age_s = (
            int((datetime.now(timezone.utc) - row["last_updated_at"]).total_seconds())
            if row["last_updated_at"] else None
        )
        return {
            "present": True,
            "last_processed_block": int(row["last_processed_block"] or 0),
            "last_updated_at": (
                row["last_updated_at"].isoformat() if row["last_updated_at"] else None
            ),
            "age_seconds": age_s,
            "stale": (age_s or 0) > 300,  # > 5min idle = suspicious
            "blocks_behind_at_write": int(row["blocks_behind_at_write"] or 0)
                if row["blocks_behind_at_write"] is not None else None,
            "metadata": dict(row["metadata"] or {}),
        }
    return await _cached_helper("ops_chain_sync", _build)


@app.get("/api/ops/rpc-health")
async def api_ops_rpc_health():
    """RPC provider liveness (alchemy / quicknode / local_erigon).

    Reads the most recent `rpc_health_history` row per provider with
    a quick aggregate of success rate over the last hour. Surfaces:
      - per-provider availability + circuit state
      - latency_ms (latest)
      - success rate 1h
    """
    async def _build():
        async with _pool.acquire() as conn:
            providers = await conn.fetch(
                """
                SELECT DISTINCT ON (provider)
                    provider, observed_at, available,
                    latency_ms, circuit_state, detail
                FROM rpc_health_history
                ORDER BY provider, observed_at DESC
                """
            )
            rates = await conn.fetch(
                """
                SELECT provider,
                       COUNT(*) FILTER (WHERE available)::float / NULLIF(COUNT(*), 0) AS success_rate_1h,
                       COUNT(*)::int AS samples_1h,
                       AVG(latency_ms)::float AS avg_latency_ms_1h
                FROM rpc_health_history
                WHERE observed_at >= NOW() - INTERVAL '1 hour'
                GROUP BY provider
                """
            )
            rates_map = {
                r["provider"]: {
                    "success_rate_1h": float(r["success_rate_1h"] or 0),
                    "samples_1h": int(r["samples_1h"] or 0),
                    "avg_latency_ms_1h": float(r["avg_latency_ms_1h"] or 0),
                }
                for r in rates
            }
        out = []
        for p in providers:
            stat = rates_map.get(p["provider"], {})
            out.append({
                "provider": p["provider"],
                "latest_observed_at": (
                    p["observed_at"].isoformat() if p["observed_at"] else None
                ),
                "available": bool(p["available"]),
                "latency_ms": int(p["latency_ms"] or 0) if p["latency_ms"] is not None else None,
                "circuit_state": p["circuit_state"],
                "success_rate_1h": stat.get("success_rate_1h"),
                "samples_1h": stat.get("samples_1h", 0),
                "avg_latency_ms_1h": stat.get("avg_latency_ms_1h"),
                "detail": dict(p["detail"] or {}),
            })
        return {"providers": out, "total_providers": len(out)}
    return await _cached_helper("ops_rpc_health", _build)


@app.get("/api/mempool/wallet-index")
async def api_mempool_wallet_index():
    """R7 mempool bloom-filter inspector.

    The mempool daemon maintains a Bloom filter of watched wallets
    (see src/mempool/wallet_index.py). We can't introspect the
    in-memory filter from a different process, but we CAN compute
    the universe size (what the filter SHOULD have) by counting
    the same wallets the daemon refreshes from.

    Returns: target size + last refresh hint via Redis key if set.
    """
    async def _build():
        async with _pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM wallet_universe"
            )
        # The wallet_index daemon may publish freshness via Redis.
        # We expose what's there — None if it doesn't.
        last_refresh = None
        wallet_count = None
        try:
            if _redis is not None:
                lr = await _redis.get("mempool:wallet_index:last_refresh_at")
                wc = await _redis.get("mempool:wallet_index:wallet_count")
                last_refresh = lr if lr else None
                wallet_count = int(wc) if wc and str(wc).isdigit() else None
        except Exception:
            pass
        target_size = int(n or 0)
        return {
            "target_size": target_size,
            "wallet_count_in_filter": wallet_count,
            "last_refresh_at": last_refresh,
            "gap": (
                (target_size - wallet_count)
                if wallet_count is not None else None
            ),
        }
    return await _cached_helper("ops_mempool_wallet_index", _build)


@app.get("/api/inspector/snapshot")
async def api_inspector_snapshot(limit: int = Query(80, ge=10, le=500)):
    """Pipeline observability snapshot for the INSPECTOR dashboard tab.

    Cached 3s — the underlying queries (with parallel asyncio.gather
    inside queries.inspector_snapshot) still take 3-5s due to the
    LEFT JOIN markets × trades_observed. Caching at 3s aligns with
    the V2 ExecutionInspector poll interval and gives near-instant
    response on warm cache.
    """
    if limit == 80:
        async def _build():
            async with _pool.acquire() as conn:
                return await queries.inspector_snapshot(conn, redis_client=_redis, limit=80)
        return await _cached_helper("inspector_snapshot", _build)
    async with _pool.acquire() as conn:
        return await queries.inspector_snapshot(conn, redis_client=_redis, limit=limit)


# ---------------------------------------------------------------------------
# PLAN-UIA-001 — Paper-trade reconciliation endpoints (mission alignment).
#
# /api/inspector/reconciliation        → summary for the recon panel
# /api/inspector/reconciliation/trades → per-trade drift drill-down
# /api/inspector/reconciliation/run    → operator-triggered fresh run
#
# Backed by paper_close_divergences (migration 051) + paper_trades.
# See `src/api/reconciliation_queries.py` for the verdict thresholds.
# ---------------------------------------------------------------------------
from src.api import reconciliation_queries as _recon_q
from src.api import pillars_queries as _pillars_q


@app.get("/api/inspector/reconciliation")
async def api_inspector_reconciliation(window_days: int = Query(30, ge=1, le=365)):
    """Paper-trade reconciliation summary for the Inspector recon panel."""
    if window_days == 30:
        async def _build():
            async with _pool.acquire() as conn:
                return await _recon_q.reconciliation_summary(conn, window_days=30)
        return await _cached_helper("recon_summary_30d", _build)
    async with _pool.acquire() as conn:
        return await _recon_q.reconciliation_summary(conn, window_days=window_days)


@app.get("/api/inspector/reconciliation/trades")
async def api_inspector_reconciliation_trades(
    classification: str | None = Query(
        None,
        pattern="^(ok|drift|phantom|premature|all)$",
    ),
    limit: int = Query(50, ge=1, le=500),
):
    """Per-trade drift breakdown for the drill-down modal."""
    eff_class = None if classification in (None, "all") else classification
    async with _pool.acquire() as conn:
        rows = await _recon_q.reconciliation_drift_trades(
            conn, classification=eff_class, limit=limit
        )
    return {"trades": rows, "classification": classification or "all", "limit": limit}


class _ReconRunRequest(BaseModel):
    window_days: int = Field(default=30, ge=1, le=365)


@app.post("/api/inspector/reconciliation/run")
async def api_inspector_reconciliation_run(req: _ReconRunRequest | None = None):
    """Operator-triggered reconciliation. Non-blocking — sets a Redis
    key the engine's scheduler polls. The dashboard's "↻ Run now"
    button calls this; UI then re-fetches the summary 30-90s later."""
    window = (req or _ReconRunRequest()).window_days
    async with _pool.acquire() as conn:
        return await _recon_q.reconciliation_trigger_run(conn, _redis, window_days=window)


@app.get("/api/health/pillars")
async def api_health_pillars():
    """5-pillar health gauge for the Bot Health tab.

    Aggregate of oracle / reconciliation / backfill / spread_gates /
    audit_log. Cached 30s — pillars don't change faster than that.
    """
    async def _build():
        async with _pool.acquire() as conn:
            return await _pillars_q.pillars_status(conn, redis_client=_redis)
    return await _cached_helper("pillars_status", _build)


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
# PLAN-UIA-001 — EMERGENCY HALT.
#
# Distinct from /api/control/killswitch which ONLY gates new trades.
# Halt = killswitch off + force-close all open paper positions.
# Force-close is fanned out via Redis pubsub `control:halt` so the
# engine container (which owns the PaperTrader instance) actually does
# the closing — the API process only owns the DB pool.
# ---------------------------------------------------------------------------
class _HaltRequest(BaseModel):
    reason: str | None = Field(default="emergency_halt", max_length=512)
    actor: str | None = Field(default=None, max_length=64)


@app.post("/api/control/halt")
async def api_control_halt(req: _HaltRequest | None = None):
    """EMERGENCY HALT — flip killswitch off AND force-close all open paper trades.

    The killswitch flip is synchronous (DB write + Redis cache).
    The force-close is published on Redis channel `control:halt` and
    consumed by the running PaperTrader instance (engine container).
    """
    req = req or _HaltRequest()
    reason = req.reason or "emergency_halt"
    actor = req.actor or "operator"

    # 1. Flip master killswitch off.
    state = await get_killswitch().set_execution_enabled(
        False,
        reason=f"halt:{reason}",
        actor=actor,
    )

    # 2. Publish force-close request. Best-effort — never raise out.
    published = False
    if _redis is not None:
        try:
            await _redis.publish(
                "control:halt",
                json.dumps(
                    {
                        "reason": reason,
                        "actor": actor,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
            published = True
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning(f"halt: redis publish failed: {exc}")

    return {
        "killswitched": True,
        "halt_published": published,
        "reason": reason,
        "actor": actor,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "killswitch_state": state.to_dict(),
    }


@app.get("/api/lab/gates")
async def api_lab_gates():
    """LAB cockpit — aggregate the 4 V2 runtime gate states + shadow-mode
    daemon health counters.

    Returns the count of items each gated daemon produced in the last 24h
    so the operator can verify the daemon is healthy BEFORE flipping the
    gate ON. A daemon returning 0 over 24h is either disabled, crashed,
    or has no work to do — none of which warrant activating the gate.
    """
    cfg = await get_runtime_config().effective()

    async def _safe_count(sql: str) -> int | None:
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(sql)
                return int(row["n"]) if row and row["n"] is not None else 0
        except Exception as exc:
            logger.debug(f"lab_gates count failed for {sql[:60]}: {exc}")
            return None

    # SQL tables/columns verified against actual daemon write paths
    # 2026-05-17 investigation:
    #   R8: writes to BOTH strategy_labels AND leader_strategy_history;
    #       the history table is the heartbeat (one row per classification
    #       pass, classified_at column), so it's the right liveness signal.
    #   R9: writes to multivariate_hawkes_fits (NOT follower_pool_state_history,
    #       which is for a separate Kalman feature that's not yet wired).
    #   R10: reads instrumental_events but InstrumentRegistry detectors are
    #        NEVER scheduled in production → instrumental_events stays empty
    #        → causal_estimates stays empty.
    #   R7: mempool_observations is written by IntentRouter (engine container)
    #       on each leaders:intent message. In prod the leaders:intent pubsub
    #       has 0 subscribers AND the mempool stream has 0 messages → wiring
    #       gap (daemon doesn't publish OR engine doesn't subscribe).
    r8 = await _safe_count(
        "SELECT COUNT(*) AS n FROM leader_strategy_history WHERE classified_at > NOW() - INTERVAL '24 hours'"
    )
    r9 = await _safe_count(
        "SELECT COUNT(*) AS n FROM multivariate_hawkes_fits WHERE fit_at > NOW() - INTERVAL '24 hours'"
    )
    r10 = await _safe_count(
        "SELECT COUNT(*) AS n FROM causal_estimates WHERE estimated_at > NOW() - INTERVAL '24 hours'"
    )
    r7 = await _safe_count(
        "SELECT COUNT(*) AS n FROM mempool_observations WHERE intent_received_at > NOW() - INTERVAL '24 hours'"
    )
    r8_total  = await _safe_count("SELECT COUNT(*) AS n FROM leader_strategy_history")
    r9_total  = await _safe_count("SELECT COUNT(*) AS n FROM multivariate_hawkes_fits")
    r10_total = await _safe_count("SELECT COUNT(*) AS n FROM causal_estimates")
    r7_total  = await _safe_count("SELECT COUNT(*) AS n FROM mempool_observations")

    # A12 — last_output timestamp per daemon. Lets the UI render
    # "warming up — last output 2 days ago" instead of just "—" when
    # the gate is ON but the daemon is quiet. Best-effort: NULL fields
    # in the DB simply propagate as None (frontend ?? '—').
    async def _safe_iso(sql: str) -> str | None:
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(sql)
                ts = row[0] if row else None
                return ts.isoformat() if ts is not None else None
        except Exception as exc:
            logger.debug(f"lab_gates ts failed for {sql[:60]}: {exc}")
            return None

    r8_last  = await _safe_iso("SELECT MAX(classified_at) FROM leader_strategy_history")
    r9_last  = await _safe_iso("SELECT MAX(fit_at) FROM multivariate_hawkes_fits")
    r10_last = await _safe_iso("SELECT MAX(estimated_at) FROM causal_estimates")
    r7_last  = await _safe_iso("SELECT MAX(intent_received_at) FROM mempool_observations")

    # A daemon counts as "running" if it has produced ANYTHING in its
    # table. 24h emptiness on a never-empty table = quiet but alive.
    daemons = sum(1 for v in (r8_total, r9_total, r10_total, r7_total) if v is not None and v > 0)

    # A12 — gate state as booleans (raw config values are floats from
    # runtime_config). Surfaced inside each rN block so the UI doesn't
    # have to cross-reference gates{} with rN_*.
    r8_enabled  = bool(cfg.get("strategy_conditional_confidence_enabled", False))
    r9_enabled  = bool(cfg.get("volume_anticipation_enabled", False))
    r10_enabled = bool(cfg.get("causal_gating_enabled", False))
    r7_enabled  = bool(cfg.get("prefill_live_enabled", False))

    return {
        "gates": {
            "strategy_conditional_confidence_enabled": r8_enabled,
            "volume_anticipation_enabled":             r9_enabled,
            "causal_gating_enabled":                   r10_enabled,
            "prefill_live_enabled":                    r7_enabled,
        },
        # Legacy flat fields (kept for backward-compat with the existing
        # LabGates KPI strip + daemonHealth helper).
        "r8_classifications_24h": r8,
        "r9_forecasts_24h":       r9,
        "r10_estimates_24h":      r10,
        "r7_intents_24h":         r7,
        "r8_classifications_total": r8_total,
        "r9_forecasts_total":       r9_total,
        "r10_estimates_total":      r10_total,
        "r7_intents_total":         r7_total,
        "daemons_running":          daemons,
        # A12 — structured per-daemon block. Frontend reads `daemons.r7`,
        # etc. so it can render a single helper instead of switching on
        # tableKey. Each block is self-describing: enabled, 24h count,
        # lifetime total, last output timestamp. A None field means the
        # query failed (renders as "—" with no further interpretation).
        "daemons": {
            "r7":  {"enabled": r7_enabled,  "count_24h": r7,  "lifetime": r7_total,
                    "last_output_ts": r7_last,  "metric": "intents"},
            "r8":  {"enabled": r8_enabled,  "count_24h": r8,  "lifetime": r8_total,
                    "last_output_ts": r8_last,  "metric": "classifications"},
            "r9":  {"enabled": r9_enabled,  "count_24h": r9,  "lifetime": r9_total,
                    "last_output_ts": r9_last,  "metric": "forecasts"},
            "r10": {"enabled": r10_enabled, "count_24h": r10, "lifetime": r10_total,
                    "last_output_ts": r10_last, "metric": "estimates"},
        },
    }


# ---------------------------------------------------------------------------
# Dashboard v2 — REMOVED 2026-05-17.
#
# The /v2 route and dashboard_v2.html template were deleted as part of
# the V1-as-source-of-truth strategy. R6-R13 features are now surfaced
# via the V1 LAB tab (see static/dashboard/dashboard-tabs.jsx
# `LabGates` component + /api/lab/gates endpoint). Anyone hitting /v2
# will get a 404 from FastAPI's default handler.
#
# The placeholder /api/overview/, /api/intelligence/*, /api/mempool/*,
# /api/microscope/*, /api/periphery/*, /api/execution/*, /api/wallet/*,
# /api/calibration/*, /api/research/* endpoints below are kept INACTIVE
# (no V2 client calls them) but left in place — they make zero runtime
# impact when not called and may be repurposed by future LAB-tab
# extensions. To purge them entirely, delete from here to the
# `# /ws/live` block.
# ---------------------------------------------------------------------------


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
    where available.

    Cached 30s when called with the default V2 limit (500) so the
    Wallet Lab → Universe sub-tab + the V2 sidebar both share one
    DB roundtrip per 30s window.
    """
    if limit == 500:
        return await _cached_helper(
            "wallet_universe_500",
            lambda: _api_wallet_universe_uncached(500),
        )
    return await _api_wallet_universe_uncached(limit)


async def _api_wallet_universe_uncached(limit: int = 200):
    try:
        limit = max(1, min(int(limit), 1000))
        async with _pool.acquire() as conn:
            stats_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int                                  AS total,
                    COUNT(*) FILTER (WHERE depth_tier = 0)::int    AS tier_0,
                    COUNT(*) FILTER (WHERE depth_tier = 1)::int    AS tier_1,
                    COUNT(*) FILTER (WHERE depth_tier = 2)::int    AS tier_2,
                    MAX(last_active)                               AS last_crawl_at,
                    -- Activity context — V2 audit found "23k wallets but
                    -- 0 trade signal" was a UX trap because Tier 2 are
                    -- depth crawl results, not active leaders. Surfacing
                    -- the trade-side counts makes the dashboard tell the
                    -- truth: only N wallets actually trade in 24h.
                    (SELECT COUNT(DISTINCT wallet_address)::int FROM trades_observed
                     WHERE time >= NOW() - INTERVAL '24 hours')   AS active_traders_24h,
                    (SELECT COUNT(DISTINCT wallet_address)::int FROM trades_observed
                     WHERE time >= NOW() - INTERVAL '24 hours'
                       AND is_leader = TRUE)                       AS active_leaders_24h,
                    (SELECT COUNT(*)::int FROM leaders
                     WHERE on_watchlist = TRUE AND excluded = FALSE) AS leaders_on_watchlist
                FROM wallet_universe
                """
            )
            total = int(stats_row["total"] or 0) if stats_row else 0
            tier_0 = int(stats_row["tier_0"] or 0) if stats_row else 0
            tier_1 = int(stats_row["tier_1"] or 0) if stats_row else 0
            tier_2 = int(stats_row["tier_2"] or 0) if stats_row else 0
            last_crawl_at = stats_row["last_crawl_at"] if stats_row else None
            active_traders_24h = int(stats_row["active_traders_24h"] or 0) if stats_row else 0
            active_leaders_24h = int(stats_row["active_leaders_24h"] or 0) if stats_row else 0
            leaders_on_watchlist = int(stats_row["leaders_on_watchlist"] or 0) if stats_row else 0
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
            "active_traders_24h": active_traders_24h,
            "active_leaders_24h": active_leaders_24h,
            "leaders_on_watchlist": leaders_on_watchlist,
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
            "active_traders_24h": 0,
            "active_leaders_24h": 0,
            "leaders_on_watchlist": 0,
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


# ---------------------------------------------------------------------------
# LIVE PORTFOLIO DASHBOARD (May 17, 2026)
# Five endpoints powering the redesigned terminal-style Live Portfolio page.
# Spec: docs/autonomous_session_2026_05_17_strategy/03_UI_REDESIGN_PROFESSIONAL.md
#
# Performance budget: each endpoint must return JSON in <500ms at current
# scale. KPI / pipeline_status are designed for a 5s poll (<100ms).
# ---------------------------------------------------------------------------


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a tz-aware datetime, or None.

    Accepts the trailing 'Z' shorthand. Returns None on parse failure so
    the caller can fall back to a default range (rather than 422-ing the
    whole request).
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PortfolioBar(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    pnl_realized: float
    trades_closed: int = 0
    n_samples: int = 0


class PortfolioTimeseriesResponse(BaseModel):
    timeframe: str
    bucket_seconds: int
    bars: list[PortfolioBar] = Field(default_factory=list)
    from_: str = Field(alias="from")
    to: str

    class Config:
        populate_by_name = True


@app.get("/api/portfolio/timeseries")
async def api_portfolio_timeseries(
    timeframe: str = Query(default="1h"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    """OHLCV-style equity buckets for the dashboard chart.

    Query params:
      - timeframe : 1m, 5m, 15m, 1h, 4h, 1d, 1w (whitelist)
      - from      : ISO-8601 timestamp (inclusive lower bound)
      - to        : ISO-8601 timestamp (exclusive upper bound)

    Default lookback is timeframe-dependent (e.g. 1m → 6h, 1h → 7d, 1w → 365d).
    """
    try:
        async with _pool.acquire() as conn:
            return await queries.portfolio_timeseries(
                conn,
                timeframe=timeframe,
                from_ts=_parse_iso_datetime(from_),
                to_ts=_parse_iso_datetime(to),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/portfolio/trades")
async def api_portfolio_trades(
    limit: int = Query(default=50, ge=1, le=500),
    order: str = Query(default="closed_desc"),
    status: str = Query(default="all"),
):
    """Recent paper_trades joined with markets, optionally with live bid/ask.

    Query params:
      - limit  : 1..500
      - order  : closed_desc | closed_asc | opened_desc | opened_asc |
                 pnl_desc | pnl_asc
      - status : closed | open | all
    """
    allowed_orders = {
        "closed_desc", "closed_asc",
        "opened_desc", "opened_asc",
        "pnl_desc", "pnl_asc",
    }
    if order not in allowed_orders:
        raise HTTPException(
            status_code=400,
            detail=f"invalid order '{order}'; must be one of {sorted(allowed_orders)}",
        )
    allowed_status = {"closed", "open", "all"}
    if status not in allowed_status:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status '{status}'; must be one of {sorted(allowed_status)}",
        )
    async with _pool.acquire() as conn:
        return await queries.portfolio_trades(
            conn,
            redis_client=_redis,
            limit=limit,
            order=order,
            status=status,
        )


@app.get("/api/portfolio/allocation")
async def api_portfolio_allocation(
    as_of: str | None = Query(default=None),
):
    """Current capital allocation by category / leader (top 5 + other) / strategy.

    Query params:
      - as_of : ISO-8601 timestamp or 'now' (default). Reserved for future
                point-in-time allocation; currently always returns the live
                view of open paper_trades.
    """
    as_of_dt = None if (as_of in (None, "now")) else _parse_iso_datetime(as_of)
    async with _pool.acquire() as conn:
        return await queries.portfolio_allocation(conn, as_of=as_of_dt)


@app.get("/api/portfolio/kpis")
async def api_portfolio_kpis():
    """Top-line dashboard KPIs (capital / drawdown / daily PnL / win-rate / streak).

    Designed for a 5s poll cadence — p50 target <100ms. Single payload so the
    frontend can update all tiles from one request.
    """
    async with _pool.acquire() as conn:
        return await queries.portfolio_kpis(conn, redis_client=_redis)


@app.get("/api/portfolio/pipeline_status")
async def api_portfolio_pipeline_status():
    """Bot pipeline health bar for the dashboard header.

    Composes: bot_status, ws_status, ingestion_lag_s, ingestion_count_24h,
    exec_mode, killswitch_active, redis_ok, db_ok, last_decision_at,
    last_trade_at. Gracefully degrades on Redis unreachable (returns
    redis_ok=False rather than 500-ing).
    """
    async with _pool.acquire() as conn:
        return await queries.portfolio_pipeline_status(conn, redis_client=_redis)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await _bridge.handle(ws)
