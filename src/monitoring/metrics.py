"""
Monitoring — structured health checks, logging helpers, and the Prometheus
metrics contract for Phase 1.

The Prometheus block at the top of this file is the SINGLE SOURCE OF TRUTH for
metric names and labels. Phase 1 Task O (trade observer hot path) and Phase 1
Task F (Falcon backfill parallelisation) import from here:

    from src.monitoring.metrics import (
        trades_ingested_total,
        trade_ingestion_latency_seconds,
        falcon_calls_total,
        ...
    )

DO NOT rename a metric or relabel without coordinating with the consumers and
updating ``docs/audit/phase1/M_metrics_foundation.md``. The default Prometheus
``REGISTRY`` is used intentionally — multiprocess-aware collection is an
explicit non-goal for Phase 1 (see audit docs).
"""

from datetime import datetime, timezone

from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

from src.database.connection import get_db

# ---------------------------------------------------------------------------
# Prometheus metrics contract (Phase 1 Task M)
# ---------------------------------------------------------------------------
# All metrics are prefixed ``polybot_`` so a shared Prometheus instance can host
# multiple services without collisions. Histogram buckets were tuned from the
# audit (docs/audit/04_perf_hotpaths.md) — they cover the realistic tail of
# each path without wasting cardinality on the head.

# === Trade observer hot path (consumed by Phase 1 Task O) ===
trades_ingested_total = Counter(
    "polybot_trades_ingested_total",
    "Total trades ingested",
    ["source", "result"],  # source: ws|rest|backfill   result: inserted|deduped|failed
)
trade_ingestion_latency_seconds = Histogram(
    "polybot_trade_ingestion_latency_seconds",
    "Latency from event arrival to DB commit",
    ["source"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
ws_disconnects_total = Counter(
    "polybot_ws_disconnects_total",
    "WebSocket disconnect events",
    ["reason"],  # ping_timeout|server_close|exception|reconnect
)
db_write_batch_size = Histogram(
    "polybot_db_write_batch_size",
    "Rows per executemany flush",
    buckets=(1, 5, 10, 25, 50, 100, 200, 500, 1000),
)
db_write_latency_seconds = Histogram(
    "polybot_db_write_latency_seconds",
    "Latency of an executemany batch flush",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
observer_queue_depth = Gauge(
    "polybot_observer_queue_depth",
    "Current depth of the trade-write queue",
)
observer_queue_drops_total = Counter(
    "polybot_observer_queue_drops_total",
    "Trades dropped due to backpressure (queue full)",
    ["reason"],  # queue_full|shutdown
)

# === Falcon API (consumed by Phase 1 Task F) ===
falcon_calls_total = Counter(
    "polybot_falcon_calls_total",
    "Total Falcon API calls",
    ["agent", "result"],  # agent: 574|575|...   result: ok|empty|rate_limited|error|timeout
)
falcon_call_latency_seconds = Histogram(
    "polybot_falcon_call_latency_seconds",
    "Falcon call latency",
    ["agent"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
falcon_concurrency = Gauge(
    "polybot_falcon_concurrency",
    "Current concurrent in-flight Falcon calls",
)

# === Redis pub/sub (any path that publishes can use this) ===
redis_publishes_total = Counter(
    "polybot_redis_publishes_total",
    "Redis pub/sub publishes",
    ["channel", "result"],  # result: ok|error
)

# === Killswitch (Phase 0 wired the strict path; we measure consultation rate) ===
killswitch_strict_path_total = Counter(
    "polybot_killswitch_strict_path_total",
    "Strict-path killswitch consultations (cache-bypass)",
    ["result"],  # enabled|disabled|error
)

# === Position Tracker persistence (Phase 2 Task C) ===
# These three are emitted from src/observer/position_tracker.py. The gauge
# is `set()` after every OPEN / CLOSE; the warm-start counter increments
# once at boot per row loaded from `position_tracker_state`; the eviction
# counter fires when MAX_OPEN_POSITIONS_TRACKED is hit and we drop the
# oldest open by open_time.
position_tracker_open_count = Gauge(
    "polybot_position_tracker_open_count",
    "Current number of positions tracked as OPEN",
)
position_tracker_warm_start_loaded_total = Counter(
    "polybot_position_tracker_warm_start_loaded_total",
    "Positions loaded into PositionTracker on warm-start",
)
position_tracker_evictions_total = Counter(
    "polybot_position_tracker_evictions_total",
    "Positions evicted from PositionTracker due to MAX_OPEN_POSITIONS_TRACKED",
)

# === Redis pub/sub subscribers (Phase 2 Task D — audit F-04) ===
# Owned by ``src/control/redis_pubsub.py``. Every subscriber site that
# previously shared the project-wide Redis client and re-iterated
# silently on disconnect is now wrapped in a Subscriber that bumps
# these counters on reconnect / message / handler-error.
redis_subscribers_active = Gauge(
    "polybot_redis_subscribers_active",
    "Currently-running Subscriber instances",
)
redis_subscriber_reconnects_total = Counter(
    "polybot_redis_subscriber_reconnects_total",
    "Subscriber reconnect events",
    ["subscriber", "reason"],  # reason: timeout|conn_error|other
)
redis_subscriber_messages_total = Counter(
    "polybot_redis_subscriber_messages_total",
    "Messages received by a subscriber",
    ["subscriber", "channel"],
)
redis_subscriber_handler_errors_total = Counter(
    "polybot_redis_subscriber_handler_errors_total",
    "Handler-raised exceptions inside a subscriber",
    ["subscriber", "channel"],
)


# ---------------------------------------------------------------------------
# Build info — best-effort. Surfaces version + git short-SHA on /metrics so a
# scrape can correlate metrics with a deploy. Failure here must NEVER break
# import (the rest of the contract is the actual deliverable).
# ---------------------------------------------------------------------------
build_info = Info("polybot_build", "Build / version metadata for the running process")


def _read_version_from_pyproject() -> str:
    """Best-effort version read from pyproject.toml. Returns 'unknown' on error."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("polymarket-bot")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    # Fallback: parse pyproject.toml directly (covers editable / non-installed
    # contexts the test suite sometimes runs in).
    try:
        from pathlib import Path

        try:
            import tomllib  # py311+
        except ImportError:  # pragma: no cover — py310 fallback
            import tomli as tomllib  # type: ignore[import-not-found, no-redef]

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
            return str(data.get("project", {}).get("version", "unknown"))
    except Exception:
        pass
    return "unknown"


def _read_git_short_sha() -> str:
    """Best-effort git short-SHA. Returns 'unknown' if not in a git tree."""
    try:
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


try:
    build_info.info(
        {
            "version": _read_version_from_pyproject(),
            "git_sha": _read_git_short_sha(),
        }
    )
except Exception:  # pragma: no cover — registry collisions on hot reload, etc.
    pass


def export_latest() -> tuple[bytes, str]:
    """Return (payload, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# Existing health-check helpers (used by scripts/health_check.py).
# Do not remove without updating callers.
# ---------------------------------------------------------------------------


async def check_db_connectivity() -> bool:
    try:
        async with get_db() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"DB connectivity failed: {e}")
        return False


async def check_redis_connectivity(redis_client) -> bool:
    try:
        await redis_client.ping()
        return True
    except Exception as e:
        logger.error(f"Redis connectivity failed: {e}")
        return False


async def get_latest_trade_age(max_age_s: int = 300) -> tuple[bool, int]:
    """Returns (is_fresh, age_seconds). Fresh = within max_age_s."""
    try:
        async with get_db() as conn:
            row = await conn.fetchrow("SELECT MAX(time) AS latest FROM trades_observed")
            if not row or not row["latest"]:
                return False, -1
            age = int((datetime.now(tz=timezone.utc) - row["latest"]).total_seconds())
            return age < max_age_s, age
    except Exception:
        return False, -1


async def get_leader_registry_stats() -> dict:
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE on_watchlist AND NOT excluded) AS active,
                       COUNT(*) FILTER (WHERE on_watchlist OR excluded) AS total,
                       MIN(last_refresh) AS oldest_refresh
                FROM leaders
                """
            )
            return dict(row) if row else {}
    except Exception:
        return {}


async def get_paper_trading_summary() -> dict:
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE status='open') AS open_count,
                       COUNT(*) FILTER (WHERE status='closed') AS closed_count,
                       COALESCE(SUM(pnl_usdc) FILTER (WHERE status='closed'), 0) AS total_pnl,
                       COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END)
                           FILTER (WHERE status='closed'), 0) AS wins
                FROM paper_trades
                """
            )
            return dict(row) if row else {}
    except Exception:
        return {}
