"""
Intelligence Dashboard API.

Lifespan: initialises asyncpg pool + Redis on startup, tears them down on shutdown.
Serves templates/dashboard.html at GET / and exposes JSON endpoints + a live WebSocket.
"""

import asyncio
import copy
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import redis.asyncio as redis_async
from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import HTMLResponse
from loguru import logger

from src.api import queries
from src.api.ws_bridge import WSBridge
from src.config import settings
from src.engine.neural_readiness import ReadinessInputs, build_neural_readiness_snapshot
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
STATS_PUSH_INTERVAL_S = 1.0  # how often to push live stats over WebSocket
HEALTH_CACHE_TTL_S = 5.0
LIVE_SNAPSHOT_TTL_S = 1.0


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _redis
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
    await _bridge.start()
    _schedule_falcon_probe()
    push_task = asyncio.create_task(_stats_push_loop())
    logger.info("Dashboard API started")
    yield
    push_task.cancel()
    await _bridge.stop()
    if created_pool and _pool:
        await _pool.close()
        _pool = None
    if created_redis and _redis:
        await _redis.aclose()
        _redis = None
    logger.info("Dashboard API stopped")


app = FastAPI(title="Polymarket Intelligence Dashboard", lifespan=lifespan)


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
        fee_snapshot_coverage_source: str | None = None
        data_accumulation_counts: dict[str, int] = {}

        try:
            async with _pool.acquire() as conn:
                last = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(time))) FROM trades_observed"
                )
                db_quality = await _db_data_quality_snapshot(conn)
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
            book_age = await _redis.get("metrics:book_age_p95_s")
            fee_coverage = await _redis.get("metrics:fee_snapshot_coverage_pct")
            token_coverage = await _redis.get("metrics:token_map_coverage_pct")
            rejected = await _redis.hgetall("signals:rejected:1h")
            book_age_p95_s = float(book_age) if book_age is not None else None
            fee_snapshot_coverage_pct = float(fee_coverage) if fee_coverage is not None else None
            token_map_coverage_pct = float(token_coverage) if token_coverage is not None else None
            rejected_signals_1h = {
                str(reason): int(count) for reason, count in dict(rejected).items()
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

        _schedule_falcon_probe()
        data = {
            "db": db_ok,
            "redis": redis_ok,
            "falcon": bool(_falcon_probe.get("ok", False)),
            "falcon_error": _falcon_probe.get("error"),
            "websocket": websocket_connected,
            "websocket_connected": websocket_connected,
            "last_message_age_s": last_message_age_s,
            "book_age_p95_s": book_age_p95_s,
            "fee_snapshot_coverage_pct": fee_snapshot_coverage_pct,
            "fee_snapshot_coverage_source": fee_snapshot_coverage_source,
            "token_map_coverage_pct": token_map_coverage_pct,
            "data_accumulation_counts": data_accumulation_counts,
            "rejected_signals_1h": rejected_signals_1h,
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


async def _fetch_overview_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.overview(conn)


async def _fetch_ml_snapshot() -> dict:
    async with _pool.acquire() as conn:
        return await queries.ml_summary(conn)


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


async def _stats_push_loop() -> None:
    """Push fresh overview stats to all connected WS clients every STATS_PUSH_INTERVAL_S."""
    while True:
        await asyncio.sleep(STATS_PUSH_INTERVAL_S)
        if not _bridge.has_connections:
            continue
        try:
            data = await _get_live_snapshot()
            await _bridge.broadcast({"type": "stats", "data": data})
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
    return build_neural_readiness_snapshot(
        ReadinessInputs(
            health=health,
            activation=activation,
            risk=risk,
            ml=ml,
        )
    )


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


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await _bridge.handle(ws)
