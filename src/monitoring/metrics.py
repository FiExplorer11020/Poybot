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

# === Round 6 (The Spine) — Multi-RPC abstraction (src/rpc/) ===
# Owned by ``src/rpc/client.py``. Every JSON-RPC call goes through one of
# the providers in the pool; the labels let the dashboard tell "the local
# Erigon is degraded" from "Alchemy is the slow one". Bucket choices mirror
# the falcon_call_latency_seconds histogram so RPC and Falcon side-by-side
# comparisons share a granularity. See docs/ROUND_6_THE_SPINE.md § 3.2.
rpc_calls_total = Counter(
    "polybot_rpc_calls_total",
    "Total Polygon RPC calls",
    # method: eth_subscribe|eth_call|eth_getLogs|eth_getBlockByNumber|...
    # result: ok|empty|rate_limited|error|timeout|circuit_open
    ["provider", "method", "result"],
)
rpc_latency_seconds = Histogram(
    "polybot_rpc_latency_seconds",
    "RPC call latency",
    ["provider", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
rpc_circuit_breaker_open = Gauge(
    "polybot_rpc_circuit_breaker_open",
    "1 iff the per-provider circuit breaker is currently OPEN or HALF_OPEN",
    ["provider"],
)
rpc_fallback_total = Counter(
    "polybot_rpc_fallback_total",
    "Provider fall-throughs (priority N skipped, used priority M instead)",
    ["from_provider", "to_provider"],
)
rpc_coalesced_calls_total = Counter(
    "polybot_rpc_coalesced_calls_total",
    "RPC calls deduplicated by in-flight coalescing",
    ["provider", "method"],
)

# === Round 6 — On-chain CLOB ingestion (src/onchain/) ===
# Owned by ``src/onchain/clob_listener.py``. The ingestion-latency
# histogram is the headline acceptance criterion from § 6
# ("p95 chain_ingestion_latency_seconds < 4.0"); buckets reach 30 s so a
# tail spike is visible without being clipped.
chain_blocks_processed_total = Counter(
    "polybot_chain_blocks_processed_total",
    "Polygon blocks the CLOB listener has decoded events from",
)
chain_blocks_behind = Gauge(
    "polybot_chain_blocks_behind",
    "Chain head minus our last_processed_block (lag in blocks)",
)
chain_events_decoded_total = Counter(
    "polybot_chain_events_decoded_total",
    "Successfully decoded CLOB events",
    ["event_type"],  # OrderFilled|OrdersMatched|OrderCancelled|FeeRateUpdated|...
)
chain_events_failed_decode_total = Counter(
    "polybot_chain_events_failed_decode_total",
    "CLOB log events that failed to decode",
    # reason: abi_mismatch|topic_unknown|asset_id_unmapped|exception
    ["event_type", "reason"],
)
chain_ingestion_latency_seconds = Histogram(
    "polybot_chain_ingestion_latency_seconds",
    "Block timestamp → our chain:trades:stream publish time",
    buckets=(0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
)

# === Round 6 — Wallet universe (src/crawler/) ===
# Owned by ``src/crawler/universe.py`` (size + tier_count) and
# ``src/crawler/depth_tiers.py`` (promotions). The promotions counter is
# how we see the adaptive-depth review actually doing work each night.
wallet_universe_size = Gauge(
    "polybot_wallet_universe_size",
    "Total wallets in wallet_universe (all tiers combined)",
)
wallet_universe_tier_count = Gauge(
    "polybot_wallet_universe_tier_count",
    "Wallets per depth_tier (0=FULL, 1=PERIODIC, 2=LIGHT)",
    ["tier"],
)
wallet_universe_promotions_total = Counter(
    "polybot_wallet_universe_promotions_total",
    "Nightly tier transitions",
    ["from_tier", "to_tier"],
)

# === Round 6 — Cold storage exporter (src/cold_storage/) ===
# Owned by ``src/cold_storage/exporter.py``. One scrape after the nightly
# export reveals how much data moved off the hot tier; ``bytes_total``
# trending up lets ops budget the Hetzner Storage Box capacity.
cold_export_rows_total = Counter(
    "polybot_cold_export_rows_total",
    "Rows written to Parquet across all nightly exports",
    ["table"],
)
cold_export_bytes_total = Counter(
    "polybot_cold_export_bytes_total",
    "Bytes written to Parquet across all nightly exports",
)
cold_export_duration_seconds = Histogram(
    "polybot_cold_export_duration_seconds",
    "Wall time per nightly per-table export",
    ["table"],
    buckets=(1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0),
)

# === Round 6 — Cross-source coverage reconciler (src/monitoring/coverage_reconciler.py) ===
# The headline R6 deliverable: ``coverage_ratio{source} < 0.95`` is the
# alert that catches data-acquisition holes BEFORE the operator notices.
coverage_ratio = Gauge(
    "polybot_coverage_ratio",
    "Trades seen by source / trades seen on-chain (5-minute window)",
    ["source"],  # onchain|websocket|api_market|api_wallet|falcon_556
)
coverage_disagreement_total = Counter(
    "polybot_coverage_disagreement_total",
    "Trades observed by primary source but missing from missed_by source",
    ["primary", "missed_by"],
)

# === Round 6 — Ingestion daemon supervision (src/ingestion_daemon/) ===
# Owned by ``src/ingestion_daemon/supervisor.py``. The ``up`` gauge feeds
# the dashboard's "Bot Health" row; restart_total catches crash-loops
# without grepping journalctl.
ingestion_daemon_up = Gauge(
    "polybot_ingestion_daemon_up",
    "1 iff the named ingestion daemon is currently active per systemd/PID-file",
    ["service"],  # engine|observer|onchain|crawler|falcon-refresher|api
)
ingestion_daemon_restarts_total = Counter(
    "polybot_ingestion_daemon_restarts_total",
    "NRestarts reported by systemd for each ingestion daemon",
    ["service"],
)
ingestion_daemon_memory_bytes = Gauge(
    "polybot_ingestion_daemon_memory_bytes",
    "Current RSS reported by systemd's MemoryCurrent for each daemon",
    ["service"],
)

# === Round 7 (The Front Door) — Mempool watcher (src/mempool/) ===
# Owned by src.mempool.{node_client,tx_decoder,event_emitter}. Five
# metrics cover the firehose: how many tx Erigon hands us, how many we
# successfully decode, how many target a watched wallet, and the
# distribution of replacement-chain depths. The latency budget itself
# is measured by intent_router_latency_seconds further down; these are
# the pure-ingestion volume / quality signals. See
# docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 5.
mempool_subscriptions_active = Gauge(
    "polybot_mempool_subscriptions_active",
    "1 iff the named mempool provider has an active eth_subscribe socket",
    ["provider"],  # local_erigon|alchemy|quicknode (the RPC pool labels)
)
mempool_tx_received_total = Counter(
    "polybot_mempool_tx_received_total",
    "Pending tx the mempool subscription handed to the decoder",
    ["source"],  # erigon|fallback (paid provider without filter support)
)
mempool_tx_decoded_total = Counter(
    "polybot_mempool_tx_decoded_total",
    "Outcome of every decode attempt on a received mempool tx",
    ["result"],  # decoded|not_clob|decode_failed
)
mempool_wallet_matches_total = Counter(
    "polybot_mempool_wallet_matches_total",
    "Mempool tx whose from-address matched the WatchedWalletIndex",
)
mempool_replacement_chain_length = Histogram(
    "polybot_mempool_replacement_chain_length",
    "Distribution of (wallet,nonce) replacement-chain lengths at confirmation",
    # Most chains are length 1 (no replacement). Long chains hint at
    # gas-war / deception behavior — fingerprint for the R8 strategy
    # classifier.
    buckets=(1, 2, 3, 5, 8, 13, 21, 34, 55),
)

# === Round 7 — Pre-signed order pool (src/execution/prefill/pool.py) ===
# Owned by PreSignedPool. The size gauge feeds the dashboard's "are we
# warm?" panel; the misses counter is the headline acceptance metric
# (acceptance criteria in R7 § 6 want filled > pool_miss in steady
# state). signing_seconds tracks the background latency budget so a
# regression in py-clob-client signing surfaces immediately.
prefill_pool_size = Gauge(
    "polybot_prefill_pool_size",
    "Pre-signed orders currently warehoused, keyed by market and direction",
    ["market", "direction"],  # direction: buy|sell
)
prefill_pool_misses_total = Counter(
    "polybot_prefill_pool_misses_total",
    "Pool lookups that found no matching pre-signed order",
    # reason: no_signature|below_min_bucket|signature_expired|all_in_flight
    ["reason"],
)
prefill_pool_signing_seconds = Histogram(
    "polybot_prefill_pool_signing_seconds",
    "Wall time per pre-sign call (background warm path)",
    # Typical signing is 30–80 ms; tail rare > 250 ms.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# === Round 7 — Intent router (src/execution/prefill/intent_router.py) ===
# Owned by IntentRouter. The decision counter explains every branch of
# the R7 § 3.6 decision tree (killswitch / confidence / size / cooldown
# / pool_miss / filled). The latency histogram is the headline R7
# acceptance metric: p50 < 250 ms, p99 < 3 s.
intent_router_decisions_total = Counter(
    "polybot_intent_router_decisions_total",
    "IntentRouter decision outcomes per consumed mempool intent",
    # result: filled|pool_miss|killswitch_off|risk_blocked|cooldown|
    #         confidence_skip|size_cap|shadow|error
    ["result"],
)
intent_router_latency_seconds = Histogram(
    "polybot_intent_router_latency_seconds",
    "intent_received -> fire_complete wall time (R7 § 6 acceptance gate)",
    # Tight low-end buckets — the whole point of R7 is sub-second
    # latency. Buckets per the architect spec (25 ms ... 5 s).
    buckets=(0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# === Round 7 — End-to-end latency + shadow-vs-live calibration ===
# intent_to_confirm_seconds is the §6 acceptance gate
# ("p50 ≤ Polygon's 2 s block time"), measured fire → chain confirm
# (reconciler updates mempool_observations.confirmed_at). The
# shadow_vs_live_pnl_diff gauge surfaces the running drift between the
# paper-traded shadow path and what the live path would have produced;
# operators watch this during the 30-day soak.
mempool_intent_to_confirm_seconds = Histogram(
    "polybot_mempool_intent_to_confirm_seconds",
    "fire_complete -> chain confirmation wall time",
    # Polygon block time is ~2 s; tail ~10 s on network congestion.
    buckets=(0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
)
mempool_shadow_vs_live_pnl_diff_usdc = Gauge(
    "polybot_mempool_shadow_vs_live_pnl_diff_usdc",
    "Running PnL diff: live-would-have - shadow-actual, USDC. "
    "During the 30-day soak the IntentRouter logs both; the diff "
    "tells operators whether shipping live is justified.",
    # Unlabelled — single global signal for the dashboard
    # comparison panel.
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
