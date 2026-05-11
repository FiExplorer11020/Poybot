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

# === Phase 3 Task B — Smart Falcon Client ===
# All additive; existing scrapers see no breaking changes. The legacy
# `falcon_calls_total{agent,result}` and `falcon_call_latency_seconds{agent}`
# above remain instrumented exactly as before; the Phase 3 client extends
# their wiring with the new key-pool and coalescing metrics below.
falcon_keys_in_pool = Gauge(
    "polybot_falcon_keys_in_pool",
    "Number of Falcon API keys configured in the pool",
)
falcon_tokens_available = Gauge(
    "polybot_falcon_tokens_available",
    "Tokens currently available in the per-key bucket",
    ["key_index"],
)
falcon_rate_limit_hits_total = Counter(
    "polybot_falcon_rate_limit_hits_total",
    "HTTP 429 responses from Falcon (triggers adaptive backoff)",
    ["key_index"],
)
falcon_coalesced_calls_total = Counter(
    "polybot_falcon_coalesced_calls_total",
    "Calls deduplicated by in-flight coalescing (waiter joined an existing request)",
    ["agent"],
)
falcon_conditional_get_savings_total = Counter(
    "polybot_falcon_conditional_get_savings_total",
    "304 Not-Modified responses (revalidated, cached payload reused)",
    ["agent"],
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

# === Phase 3 Round 1 — Redis Streams (Agent C) ===
# Owned by ``src/control/redis_streams.py``. Closes audit Section 6's
# "no durability, no trace context, no idempotency token, no
# consumer-group semantics" finding for the trades:observed → engine
# pipeline. The Streams primitives replace pub/sub's "messages
# published during the disconnect window are LOST" gap with a server-
# side cursor (consumer group + XACK + XCLAIM). The metrics here are
# the dashboard's window onto producer health, consumer throughput,
# pending depth (= backpressure), and deadletter outflow.
stream_published_total = Counter(
    "polybot_stream_published_total",
    "Entries published to a Redis Stream",
    ["stream"],
)
stream_consumed_total = Counter(
    "polybot_stream_consumed_total",
    "Entries successfully consumed (XACKed)",
    ["stream", "group"],
)
stream_pending_entries = Gauge(
    "polybot_stream_pending_entries",
    "XPENDING count per group",
    ["stream", "group"],
)
stream_dead_letters_total = Counter(
    "polybot_stream_dead_letters_total",
    "Entries that exhausted retries and went to the deadletter stream",
    ["stream", "group"],
)
stream_handler_latency_seconds = Histogram(
    "polybot_stream_handler_latency_seconds",
    "Stream consumer handler processing time",
    ["stream", "group"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
stream_reconnects_total = Counter(
    "polybot_stream_reconnects_total",
    "Producer/consumer reconnect events",
    ["component", "stream"],  # component: producer|consumer
)

# === Phase 3 Round 1 — Data Continuity Backbone (Agent A) ===
# These metrics measure the new continuity primitives: cursor-driven REST
# polling, event-driven Falcon refresh, the WS freshness watchdog, and
# the Falcon daily budget guard. The user-facing problem they exist to
# prove fixed: "10-30 min pauses between continuous data gathering".
# Histogram buckets are tuned to the 5 s REST cadence + the 30 min
# FALCON_REFRESH_INTERVAL_S floor so the tail of each path is visible
# without burning cardinality.
polling_cursor_lag_seconds = Histogram(
    "polybot_polling_cursor_lag_seconds",
    "Seconds between persisted cursor and now at poll start (per source)",
    ["source"],  # api_wallet|api_market|api_global
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0),
)
ws_channel_stale_total = Counter(
    "polybot_ws_channel_stale_total",
    "WS channels detected as stale by the freshness watchdog",
    ["channel"],  # market|book|price_change|trade
)
ws_backfill_hours_used = Histogram(
    "polybot_ws_backfill_hours_used",
    "Hours of history requested on WS reconnect backfill",
    buckets=(0.1, 0.5, 1.0, 2.0, 6.0, 12.0, 24.0),
)
event_driven_refreshes_total = Counter(
    "polybot_event_driven_refreshes_total",
    "Event-driven leader refresh invocations",
    # reason: ws_unknown_wallet|user_command|watchdog|trade_observer
    # result: refreshed|skipped_recent|budget_exhausted|coalesced|error
    ["reason", "result"],
)
falcon_daily_budget_remaining = Gauge(
    "polybot_falcon_daily_budget_remaining",
    "Calls remaining in today's Falcon event-driven refresh budget",
)

# === Phase 3 Round 2 — Point-in-time feature store (Agent Y) ===
# Owned by ``src/profiler/feature_store.py``. The feature store closes
# audit MG-3 / §3.1 (training leakage via AS-OF-NOW reads of
# `markets.liquidity_score`). These three metrics expose:
#   1. Lookup outcome (asof_hit | fallback_live | miss) so dashboards
#      can track how often the training path is forced to fall back
#      to the live `markets` row for pre-dual-write legacy positions.
#      `fallback_live` trending down over time is the success signal.
#   2. Batch size distribution — the typical training pass batches
#      hundreds to thousands of (market_id, asof) tuples through a
#      single LATERAL JOIN, so the histogram exposes the N+1
#      avoidance guarantee at a glance.
#   3. Lookup latency, bucketed by batch size (so single-row hot-path
#      reads and 5k-row training reads don't share a histogram).
feature_store_lookups_total = Counter(
    "polybot_feature_store_lookups_total",
    "market_features_history lookups",
    ["table", "result"],  # result: asof_hit|fallback_live|miss
)
feature_store_batch_size = Histogram(
    "polybot_feature_store_batch_size",
    "Number of (market_id, asof) tuples per batched lookup",
    buckets=(1, 10, 100, 1000, 10000),
)
feature_store_lookup_latency_seconds = Histogram(
    "polybot_feature_store_lookup_latency_seconds",
    "Lookup latency",
    ["batch_size_bucket"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)

# === Phase 3 Round 1 — Ingest Health Watchdog (Agent D) ===
# Owned by ``src/monitoring/ingest_health.py``. The IngestHealthMonitor
# tracks freshness of every ingestion source (WS, REST data-api, Falcon
# agents, Redis pub/sub) via per-source heartbeats. A background tick loop
# computes "seconds since last event" on every scrape; when the gap
# exceeds the source-specific threshold a recovery callback fires (with
# cooldown) and the gauges/counters below light up. Prometheus alert
# rules in ``docs/monitoring/alerts.yml`` reference these names verbatim.
#
# Severity convention: warning = >threshold, critical = >2 × threshold.
ingest_seconds_since_last_event = Gauge(
    "polybot_ingest_seconds_since_last_event",
    "Seconds since last activity per ingestion source",
    ["source"],
)
ingest_gaps_total = Counter(
    "polybot_ingest_gaps_total",
    "Detected ingestion gaps (threshold crossings)",
    ["source", "severity"],  # severity: warning|critical
)
ingest_recovery_attempts_total = Counter(
    "polybot_ingest_recovery_attempts_total",
    "Auto-recovery callback invocations",
    ["source", "result"],  # result: triggered|skipped_cooldown|failed
)
ingest_recovery_success_total = Counter(
    "polybot_ingest_recovery_success_total",
    "Recoveries confirmed (heartbeat returned after gap)",
    ["source"],
)
ingest_threshold_breaches_active = Gauge(
    "polybot_ingest_threshold_breaches_active",
    "Currently-active gap states (1=active, 0=closed)",
    ["source"],
)

# === Phase 3 Round 2 — Order-book imbalance feature pipeline (Agent Z) ===
# Owned by ``src/observer/orderbook_observer.py`` (rollup loop) and
# ``src/profiler/feature_store.py`` (read path). Closes the "highest-ROI
# new data source" recommendation in docs/audit/05_ml_pipeline.md
# summary. The rollup runs every 60 s with a 70 s lookback. The `source`
# label on the ingest counter lets us tell apart live WS-driven writes
# vs operator-driven backfill once `scripts/orderbook_backfill.py`
# lands. The `result` label on the rollup counter distinguishes a normal
# run (`ok`), a window with zero raw snapshots (`empty` — common during
# quiet hours), and a DB / parse error (`error` — should be rare; see
# the rollup loop's broad except handler).
orderbook_snapshots_ingested_total = Counter(
    "polybot_orderbook_snapshots_ingested_total",
    "Raw book_quality_snapshots rows written",
    ["source"],  # ws|backfill
)
orderbook_rollup_runs_total = Counter(
    "polybot_orderbook_rollup_runs_total",
    "Per-minute rollup invocations",
    ["result"],  # ok|empty|error
)
orderbook_rollup_rows_per_run = Histogram(
    "polybot_orderbook_rollup_rows_per_run",
    "Markets x tokens with non-zero snapshots per rollup",
    buckets=(0, 5, 25, 100, 500, 2000),
)
orderbook_features_lookup_total = Counter(
    "polybot_orderbook_features_lookup_total",
    "Feature store orderbook lookups",
    ["result"],  # hit|stale|miss
)

# === Phase 3 Round 2 — Bivariate Hawkes fitter (Agent X) ===
# Owned by ``src/graph/hawkes_fitter.py``. The legacy fitter was univariate
# and silently confirmed every clustered retail trader as a follower (see
# docs/audit/05_ml_pipeline.md § MG-5). The new fitter is bivariate
# leader→follower with closed-form MLE; these three metrics let the
# dashboard see (a) how many fits land on each solver path, (b) wall-time
# per fit (the cold path's budget is ~10 min for the whole nightly), and
# (c) the distribution of α/μ ratios — a sanity check that the gate
# threshold (>1.0 = confirmed) is hitting meaningful tails, not 100% of
# edges (the symptom of the old bug).
hawkes_fits_total = Counter(
    "polybot_hawkes_fits_total",
    "Bivariate Hawkes fits performed",
    ["result"],  # converged|fallback_nelder_mead|degenerate|failed
)
hawkes_fit_duration_seconds = Histogram(
    "polybot_hawkes_fit_duration_seconds",
    "Wall time per Hawkes fit",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
)
hawkes_alpha_mu_ratio = Histogram(
    "polybot_hawkes_alpha_mu_ratio",
    "Distribution of fitted alpha/mu ratios - sanity check on follower-edge quality",
    buckets=(0.0, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0),
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
