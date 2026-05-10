# 01 — Data-Flow Inventory (Polymarket Leader Intelligence Bot)

> **Audit role**: data-flow cartographer. This document is the exhaustive inventory of every data
> source the bot reads from or writes to, with refresh cadence, idempotency and failure modes.
> No fixes are proposed here — that is a separate agent's job. Inventory only.
>
> **Repository root**: `/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot/`
> **Generated**: 2026-05-10
> **Scope**: src/{registry,observer,graph,profiler,engine,control,api,backups,monitoring,economics,backtest,execution,telegram_bot}/ + scripts/ + docs/migrations/001..010_*.sql

---

## Executive Summary (one page, ≤15 lines)

The bot ingests data from four external sources (Falcon API on `narrative.agent.heisenberg.so`, Polymarket CLOB WebSocket `wss://ws-subscriptions-clob.polymarket.com/ws/market`, Polymarket REST `data-api.polymarket.com` + `gamma-api.polymarket.com` + `clob.polymarket.com`) and persists into PostgreSQL 15 (24 tables across 10 migrations) plus Redis 7.2 (cache + pub/sub on six channels). Trade ingestion is dual-source (WS market channel publishes book/price_change/last_trade_price events but **does not carry wallet addresses** — wallet attribution comes from REST polling on `data-api` every 30s); deduplication is layered (Redis SET NX with 7-day TTL on `seen_trades:{wallet}:{market}:{day}:{hash}` plus DB UNIQUE INDEX `uq_trades_observed_natural_key` from migration 007). Decisions ride a Redis pub/sub bus (`trades:observed` → ConfidenceEngine + GraphEngine + BehaviorProfiler; `decisions` → PaperTrader; `decisions:live` → LiveTrader; `positions:closed` → BehaviorProfiler). APScheduler runs five cron/interval jobs in the engine container plus one in the backups container. **RED FLAGS**: (1) `fee_snapshots` and `signal_audits` tables (migration 003) have no INSERT in source code — they are read by `confidence_engine._build_signal_audit()` but never populated, so `evaluate_signal_gate` always sees a stale/missing snapshot; (2) `data-api.polymarket.com` calls (`_backfill_wallet_trades`, `_backfill_market_activity`) have **no rate-limit protection** — Falcon has `_throttle()` but the data-api backfill loop runs at `TRADE_OBSERVER_POLL_INTERVAL_S=30s` against every leader wallet without backoff; (3) the `markets.fee_rate_pct` legacy column is the ONLY actively-written fee source — 24% migration to `fee_snapshots` was started but never wired; (4) `decision_log` rows accumulate without retention (only `trades_observed` has a 90-day cleanup); (5) on-disk `data_cache/` parquet caches under `{dataset}/{shard}.parquet` are written by `BacktestCache.write_records` but no cleanup job exists; (6) the engine container's `refresh_markets` job exists in `src/engine/jobs/refresh_markets.py` but is **not registered** in `src/engine/main.py` — it only fires inside `scripts/run_all.py`. See section F for full red-flag list.

---

## A. PostgreSQL Tables

PostgreSQL 15, single database (typically `polymarket`). Connection pool sized via `DB_POOL_MIN=2`,
`DB_POOL_MAX=10` (`src/config.py:29-30`). All access goes through `src/database/connection.py`'s
`get_db()` async context manager. **No TimescaleDB**, no row-level partitioning. Migrations applied
sequentially by `scripts/setup_db.py`, tracked in `schema_migrations(version)`.

### A.1 `leaders`

- **Source/Sink**: PG table, primary key `wallet_address VARCHAR(100)`. Defined in `docs/migrations/001_schema.sql:5-15`.
- **Touched by migrations**: `001_schema.sql:5-15` (CREATE), no further migrations.
- **Writers**:
  - `src/registry/leader_registry.py:64-67` (`refresh_leaderboard`, `executemany` upsert of (wallet, falcon_score) for INITIAL_LEADER_COUNT entries).
  - `src/registry/leader_registry.py:70-77` (`refresh_leaderboard` flips `on_watchlist=FALSE` for previously-listed wallets that fall out of the new leaderboard).
  - `src/registry/leader_registry.py:159-169` (`enrich_leaders` stamps `excluded=TRUE, on_watchlist=FALSE, exclude_reason='falcon_no_data'` when Falcon agent 581 returns no metrics).
  - `src/registry/leader_registry.py:174-189` (`enrich_leaders` writes `wallet360_json`, `classification_json`, `excluded`, `exclude_reason`, `last_refresh`).
  - `src/profiler/behavior_profiler.py:507-514` (FK guard: `INSERT ... ON CONFLICT DO NOTHING` so the FK from `leader_profiles.wallet_address` doesn't fail when the profiler sees a wallet before the registry).
- **Readers**:
  - `src/registry/leader_registry.py:44-49,406-412,415-425` (cached counts, active leaders, leader markets).
  - `src/observer/main.py:61-69` (bootstrap: top-N watchlisted leaders for WS subscription).
  - `src/observer/trade_observer.py:1015-1023` (per-trade enrichment for pub/sub event).
  - `src/engine/confidence_engine.py:733-737,786-794` (precompute cache, readiness check JOIN).
  - `src/api/queries.py:520,747,1375-1397,1467,1859` (dashboard leaders/profile/data-quality SQL).
  - `scripts/batch_runner.py:51-55` (backfill targets).
- **Refresh cadence**: registry loop every `FALCON_REFRESH_INTERVAL_S=1800s` (`src/config.py:39`, `src/registry/leader_registry.py:491-513`); enrichment uses 24h staleness window (`src/registry/leader_registry.py:129`).
- **Volume estimate**: `INITIAL_LEADER_COUNT=200` to `MAX_LEADER_COUNT=2000` rows (`src/config.py:46-47`); ~200-2000 total. Comment at `src/observer/trade_observer.py:34-36` references "few hundred leaders".
- **Idempotency / dedup**: PRIMARY KEY on `wallet_address` (`docs/migrations/001_schema.sql:6`). All upserts use `ON CONFLICT (wallet_address) DO UPDATE`.
- **Failure mode**: Falcon API down → `_fallback_leaderboard_entries` falls back to PnL leaderboard (agent 579) preserving existing scores (`src/registry/leader_registry.py:32-42`); both down → uses cached DB count (`src/registry/leader_registry.py:44-54`). On Wallet360 quota → `enrich_leaders` skips wallet, logs at debug level (`src/registry/leader_registry.py:148-150`).

### A.2 `trades_observed`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/001_schema.sql:18-34`.
- **Touched by migrations**:
  - `001_schema.sql:18-34` (CREATE + 4 indexes: `idx_trades_wallet_time`, `idx_trades_market_time`, `idx_trades_time`, `idx_trades_leader (WHERE is_leader=TRUE)`).
  - `002_dashboard_compat.sql:5-7` (additional partial index `idx_trades_leader_wallet (wallet_address) WHERE is_leader=TRUE`).
  - `007_trades_observed_idempotency.sql:33-49` (de-dup of historical rows + UNIQUE INDEX `uq_trades_observed_natural_key (wallet_address, market_id, time, side, price, size_usdc)`).
  - `009_trades_category_denorm.sql:17-27` (ADD COLUMN `category VARCHAR(50)` nullable + partial index `idx_trades_wallet_category_time`; UPDATE backfill from `markets.category`).
- **Writers**:
  - `src/observer/trade_observer.py:942-967` (single INSERT path; `INSERT ... ON CONFLICT DO NOTHING RETURNING id`; `RETURNING id IS NULL` ⇒ DB-level dedup hit, silent no-op).
  - `src/observer/trade_observer.py:1006-1014` (UPDATE `category` after `_repair_market_from_trade_hint` infers a new value).
  - `src/registry/leader_registry.py:466-473` (UPDATE `category` from `recategorize_unknowns` once `markets.category` upgrades from `unknown`).
- **Readers**:
  - `src/observer/trade_observer.py:861-881` (`_trade_exists` natural-key probe used after Redis dedup miss).
  - `src/observer/trade_observer.py:732-779` (`_get_recent_leader_market_ids` rehydrates `_leader_condition_ids` from MAX(time) per market).
  - `src/observer/main.py:71-81` (bootstrap: distinct token_ids ordered by MAX(time) for WS subscription).
  - `src/graph/graph_engine.py:60-71` (warm-start: last 4×FOLLOWER_WINDOW_S=1200s of trades).
  - `src/graph/hawkes_fitter.py:74-92` (per-edge timestamps over `HAWKES_LOOKBACK_DAYS=30`).
  - `src/engine/confidence_engine.py:782-784,620-635` (per-wallet trade count + recent avg price).
  - `src/engine/paper_trader.py:751-760` (price fallback when Redis cache miss).
  - `src/observer/position_tracker.py:311-325` (10-trade trailing avg price for is_contrarian).
  - `src/api/queries.py:452,588,1568,1630,1690,1937,1974,2647-2671` (dashboard SQL ~30+ refs).
  - `src/api/main.py:237` (`SELECT MAX(time) FROM trades_observed` for last_trade_age_s health check).
- **Cleanup**: `scripts/batch_runner.py:127-134` `step_cleanup_old_trades` `DELETE FROM trades_observed WHERE time < NOW() - INTERVAL 'RETENTION_TRADES_DAYS=90'` daily at 03:00 UTC.
- **Refresh cadence**: Continuous on WS market events (price_change/book/trade) — only `event_type=='trade'` with `maker_address`/`taker_address` produces an INSERT (`src/observer/trade_observer.py:564-604`), which is rare on the modern CLOB feed. Most rows arrive via REST `_backfill_loop` polling every `TRADE_OBSERVER_POLL_INTERVAL_S=30s` (`src/observer/trade_observer.py:606-619`).
- **Volume estimate**: `MARKET_META_CACHE_MAXSIZE=10_000`, `LEADER_CONDITION_IDS_MAXSIZE=2_000` (cache caps from `src/observer/trade_observer.py:37-38`). Migration 007 comment cites "71k existing rows" pre-migration. With 200-500 leaders × ~30 trades/day each ≈ 6-15k rows/day.
- **Idempotency / dedup**: Two-layer:
  - Redis: `seen_trades:{wallet}:{market_id}:{YYYYMMDD}:{md5(bucket:side:price:size)[:12]}` with 7-day TTL via `SET NX EX 604800` (`src/observer/trade_observer.py:823-840`, constants `DEDUP_KEY_PREFIX='seen_trades'`, `DEDUP_TTL_S=7*86400` at L27-28).
  - DB: UNIQUE INDEX `uq_trades_observed_natural_key (wallet_address, market_id, time, side, price, size_usdc)` from migration 007.
- **Failure mode**: Redis dedup-cache flushed → DB UNIQUE constraint catches dupes silently (`src/observer/trade_observer.py:968-979`, no telemetry counter on the silent block). Insert fails altogether → `_clear_dedup_key` rolls back the Redis SETNX so retry can succeed (`src/observer/trade_observer.py:1024-1027`). data-api 4xx/5xx → request-level try/except, no backoff (`src/observer/trade_observer.py:701-702,720-722`).

### A.3 `positions_reconstructed`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/001_schema.sql:37-52`.
- **Touched by migrations**:
  - `001_schema.sql:37-56` (CREATE + 3 indexes including partial `idx_positions_open WHERE close_time IS NULL`).
  - `003_v1_economic_spine.sql:40-48` (ADD COLUMNs: size_shares, entry_fee_usdc, exit_fee_usdc, gross_pnl_usdc, net_pnl_usdc, economic_model_version, invalidated_at, invalidated_reason).
  - `009_trades_category_denorm.sql:20-21` (ADD COLUMN `category VARCHAR(50)` + UPDATE backfill).
- **Writers**:
  - `src/observer/position_tracker.py:336-342` (single INSERT path on every `_close_position`; FIFO per `(wallet, market, token)` with sell/merge/resolution close methods).
  - `src/registry/leader_registry.py:475-483` (UPDATE `category` from `recategorize_unknowns`).
- **Readers**:
  - `src/registry/leader_registry.py:415-424` (open positions for active markets).
  - `src/engine/paper_trader.py:899-913` (`_leader_exited_recently`: looks for close_time within last 5 min as paper-trade close trigger).
  - `src/engine/jobs/refresh_thresholds.py:43-47` (counts `WHERE close_time IS NOT NULL` as `resolved_total`).
  - `src/api/queries.py:776,1856-1860,2013-2030` (dashboard rolling PnL, leader profile, drilldowns).
  - `scripts/batch_runner.py:79` (`leader_profiles.positions_resolved` is read here, not `positions_reconstructed` directly).
- **Refresh cadence**: On every Redis `trades:observed` event (PositionTracker subscribes — `src/observer/position_tracker.py:60-75`); writes only when a position closes (sell ≥ entry size, merge with sibling token, or resolution triggered manually via `close_market_positions`).
- **Volume estimate**: 1 row per closed position. UNKNOWN throughput; per migration 007 historical scale ≈ 71k trades → likely thousands of positions.
- **Idempotency / dedup**: **No UNIQUE constraint**. The state machine in PositionTracker is in-memory (`_open_positions: dict[(wallet, market_id, token_id), list[OpenPosition]]` at `src/observer/position_tracker.py:42`); a process restart drops in-flight open positions, and on re-subscribe the same trade event would re-open. INVESTIGATE: there is no warm-start from DB for PositionTracker (only GraphEngine warm-starts).
- **Failure mode**: DB INSERT fails → return early, no rollback (`src/observer/position_tracker.py:365-367`). Redis pub/sub publish fails → logged at WARN, position is still in DB (`src/observer/position_tracker.py:388-391`). On process restart, in-memory open positions are lost: subsequent SELLs may not find a matching open and silently drop (`src/observer/position_tracker.py:226-227`).

### A.4 `follower_edges`

- **Source/Sink**: PG table, BIGSERIAL `id`, UNIQUE `(leader_wallet, follower_wallet)`. Defined `docs/migrations/001_schema.sql:59-77`.
- **Touched by migrations**: `001_schema.sql:59-77` only.
- **Writers**:
  - `src/graph/graph_engine.py:253-277` (UPSERT on every trade pair within `FOLLOWER_WINDOW_S=300s`).
  - `src/graph/hawkes_fitter.py:174-184` (UPDATE `hawkes_alpha_mu` after batch MLE fit).
- **Readers**:
  - `src/graph/graph_engine.py:281-305,308-323` (followers/leaders/confirmed_edges queries).
  - `src/graph/hawkes_fitter.py:152-163` (batch fetch of edges with `co_occurrences ≥ MIN_CO_OCCURRENCES`).
  - `src/profiler/behavior_profiler.py:541-551` (`_count_confirmed_followers` for maturity).
  - `src/engine/confidence_engine.py:786-789` (readiness check: confirmed followers).
  - `src/engine/jobs/refresh_thresholds.py:47-49` (counts confirmed edges for system maturity).
  - `src/api/queries.py:553,763,1411-1420,1792,1831` (dashboard graph queries).
- **Refresh cadence**: Hot path — every Redis `trades:observed` event triggers `_detect_followers` or `_detect_recent_leaders` (`src/graph/graph_engine.py:101-138`). Cold path — Hawkes batch nightly at `BATCH_HOUR_UTC=3` (`src/graph/hawkes_fitter.py:145-191`).
- **Volume estimate**: O(leaders × followers). At 200 leaders × 50 followers ≈ 10k rows. Hawkes batch reads up to `BATCH_HAWKES_LEADERS=200` edges per cycle (`src/config.py:212`).
- **Idempotency / dedup**: UNIQUE `(leader_wallet, follower_wallet)`; UPSERT via `ON CONFLICT ... DO UPDATE`.
- **Failure mode**: DB error → logged, no retry (`src/graph/graph_engine.py:278-279`). Hawkes fit fails (insufficient data, n<5) → returns None, edge unchanged (`src/graph/hawkes_fitter.py:97-98`).

### A.5 `leader_profiles`

- **Source/Sink**: PG table, PRIMARY KEY `wallet_address` REFERENCES `leaders(wallet_address)`. Defined `docs/migrations/001_schema.sql:80-89`.
- **Touched by migrations**:
  - `001_schema.sql:80-89` (CREATE).
  - `003_v1_economic_spine.sql:35-38` (ADD COLUMNs `learning_invalidated_at`, `learning_invalidated_reason`, `economic_model_version`).
- **Writers**:
  - `src/profiler/behavior_profiler.py:515-535` (UPSERT on every `on_position_closed` Redis event).
  - `src/profiler/error_model.py:430,450` (UPDATE `error_model_phase`, `error_model_blob` after re-fit).
- **Readers**:
  - `src/profiler/behavior_profiler.py:_load_profile` (read same wallet on every position closed; INVESTIGATE: line not pinpointed in our reads).
  - `src/profiler/error_model.py:_load_state` (phase + blob).
  - `src/engine/confidence_engine.py:564-572,613-618,724-737` (`_get_profile_snapshot`, profile_maturity, precompute_redis_cache).
  - `src/engine/jobs/refresh_thresholds.py:42-44` (counts profiles `WHERE positions_resolved > 0`).
  - `src/api/queries.py:366-381,753-757,1001,1859` (dashboard).
  - `scripts/batch_runner.py:78-82` (refit phase).
- **Refresh cadence**: On every `positions:closed` Redis pub/sub event (one per closed position). Phase upgrades batched daily at 03:00 UTC.
- **Volume estimate**: One row per leader (≤ MAX_LEADER_COUNT=2000). `error_model_blob BYTEA` may be ≤ 1 MB per row at phase 3 (LightGBM serialized).
- **Idempotency / dedup**: PRIMARY KEY on `wallet_address`; UPSERT via `ON CONFLICT (wallet_address) DO UPDATE`. FK to `leaders` is enforced; profiler pre-creates the leaders row to avoid violation (`src/profiler/behavior_profiler.py:507-514`).
- **Failure mode**: DB error logged, no retry (`src/profiler/behavior_profiler.py:536-537`).

### A.6 `markets`

- **Source/Sink**: PG table, PRIMARY KEY `market_id`. Defined `docs/migrations/001_schema.sql:92-104`.
- **Touched by migrations**: `001_schema.sql:92-104` only.
- **Writers**:
  - `src/observer/trade_observer.py:931-939` (stub INSERT `(market_id, question, category='unknown')` BEFORE the trade INSERT so denormalized category subquery works).
  - `src/observer/trade_observer.py:1040-1070` (UPSERT after Gamma enrichment).
  - `src/observer/trade_observer.py:1198-1222` (UPSERT in `_repair_market_from_trade_hint`).
  - `src/registry/leader_registry.py:354-378` (UPSERT in `sync_markets`, every registry cycle).
  - `src/registry/leader_registry.py:461-463` (UPDATE `category` in `recategorize_unknowns`).
- **Readers**:
  - `src/observer/main.py:82-91` (token_yes/token_no for WS subscription).
  - `src/observer/trade_observer.py:982-988` (per-trade enrichment).
  - `src/observer/position_tracker.py:438-445` (token_yes/token_no for merge detection).
  - `src/observer/position_tracker.py:411-415` (fee_rate_pct for PnL).
  - `src/engine/paper_trader.py:769-772,780-783,866-877` (fee, opposite_token, end_date for resolution).
  - `src/engine/confidence_engine.py:605-611,1006-1015` (category, liquidity, token_yes/no for signal_audit).
  - `src/api/queries.py:1948,3138-...` (dashboard market scanner, DQ drilldowns).
- **Refresh cadence**:
  - **On-demand stub**: every new `(market_id, trade)` not yet in `markets` triggers a stub INSERT (`src/observer/trade_observer.py:931-939`). Hot-path.
  - **Gamma enrichment**: same code path (line 1029-1077) calls `_fetch_market_metadata_from_gamma` when `_needs_market_enrichment(...)==True` AND `MARKET_META_TTL_S=3600` cache has expired.
  - **Registry sync**: every `FALCON_REFRESH_INTERVAL_S=1800s`, `sync_markets` queries last 7 days of distinct trade markets and upserts those whose `markets.updated_at < NOW() - 24h` OR token_yes/no/volume_24h is NULL (`src/registry/leader_registry.py:298-313`).
  - **Recategorize**: same registry cycle, `recategorize_unknowns` re-runs text inference on stuck `category='unknown'` rows.
- **Volume estimate**: ~1900 active markets at any time per `src/config.py:55` comment. Plus expired markets accumulating (no cleanup).
- **Idempotency / dedup**: PRIMARY KEY `market_id`; all writes use `ON CONFLICT DO UPDATE` or `DO NOTHING`.
- **Failure mode**: Gamma 4xx/5xx → `_fetch_market_metadata_from_gamma` returns None, market stays at stub (`src/observer/trade_observer.py:1031-1034`). Falcon agent 574 down → fallback to Gamma (`src/registry/leader_registry.py:317-325`).

### A.7 `paper_trades`

- **Source/Sink**: PG table, SERIAL `id`. Defined `docs/migrations/001_schema.sql:107-127`.
- **Touched by migrations**:
  - `001_schema.sql:107-127` (CREATE + partial index `idx_paper_open WHERE status='open'`).
  - `002_dashboard_compat.sql:9-15` (`idx_paper_market_open`, `idx_paper_opened_date`).
  - `003_v1_economic_spine.sql:14-26` (ADD COLUMNs: `strategy_track`, `economic_model_version`, `invalidated_at`, `invalidated_reason`, `size_shares`, `entry_fee_usdc`, `exit_fee_usdc`, `spread_cost_usdc`, `slippage_usdc`, `gross_pnl_usdc`, `net_pnl_usdc`, `fill_audit JSONB`).
- **Writers**:
  - `src/engine/paper_trader.py:497-521` (INSERT on every `open_trade`).
  - `src/engine/paper_trader.py:612-635` (UPDATE on every `close_trade`).
- **Readers**:
  - `src/engine/paper_trader.py:178-192` (`_reload_open_trades` on boot, `WHERE status='open'`).
  - `src/engine/paper_trader.py:807-820,838-852` (open conflict + recent re-entry checks).
  - `src/profiler/behavior_profiler.py:567-588` (closed trades for replay/decision_learning).
  - `src/api/queries.py:457,504,514,525,533,789,800,985,1109-1121,1309,1331,1538` (dashboard heavy).
- **Refresh cadence**: On every Redis `decisions` event with `action ∈ {follow, fade}` (paper open) or every monitor-loop tick (close: leader_exit, market_resolved, timeout, stop_loss, take_profit).
- **Volume estimate**: PAPER_CAPITAL_USDC=10_000, MAX_POSITION_PCT=0.02 → max 50 simultaneous full-size positions; `MAX_CONCURRENT_POSITIONS=10` runtime cap. ≤10 open at any time, indefinite history.
- **Idempotency / dedup**: SERIAL primary key. Application-level guard against same-leader/market/strategy duplicates: `_has_open_trade_conflict` (`src/engine/paper_trader.py:792-823`) + `_has_recent_reentry_conflict` (`src/engine/paper_trader.py:825-855`, cooldown=`PAPER_REENTRY_COOLDOWN_S=300`).
- **Failure mode**: INSERT/UPDATE failure → log + `return None` (`src/engine/paper_trader.py:523-525,653-655`); Redis publish failure swallowed.

### A.8 `decision_log`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/001_schema.sql:130-142`.
- **Touched by migrations**:
  - `001_schema.sql:130-145` (CREATE + indexes).
  - `002_dashboard_compat.sql:18-21` (partial `idx_decisions_outcome_null`).
  - `003_v1_economic_spine.sql:28-33` (ADD COLUMNs: `strategy_track`, `economic_model_version`, `invalidated_at`, `invalidated_reason`, `signal_audit JSONB`).
- **Writers**:
  - `src/engine/confidence_engine.py:826-843` (extended INSERT with `strategy_track`, `economic_model_version`, `signal_audit`).
  - `src/engine/confidence_engine.py:850-862` (legacy fallback INSERT if extended fails).
  - `src/engine/paper_trader.py:639-652` (UPDATE `outcome` after close, picks last NULL outcome for `(leader_wallet, market_id)`).
- **Readers**:
  - `src/api/queries.py:813,1109-1121,1237` (dashboard decision feed + reasoning).
- **Refresh cadence**: Every leader trade observed by `ConfidenceEngine.evaluate` produces a row, including `action='skip'`. Volume = leader-trade rate × every leader trade triggers one decision_log row.
- **Volume estimate**: At 200-500 leaders observed × ~5-10 trades/day each = ~1000-5000 rows/day. UNKNOWN cleanup; **no retention job**.
- **Idempotency / dedup**: BIGSERIAL pk only, no natural-key constraint. UPDATE `outcome` uses `ORDER BY time DESC LIMIT 1` (`src/engine/paper_trader.py:644-648`) — INVESTIGATE: race window between two paper trades for same (leader, market) closing within seconds could cross-attribute.
- **Failure mode**: INSERT extended fails → fallback to legacy schema; both fail → logged, no row written (`src/engine/confidence_engine.py:864-865`).

### A.9 `schema_migrations`

- **Source/Sink**: PG table, PRIMARY KEY `version`. Defined `docs/migrations/001_schema.sql:148-151`.
- **Writers**: `scripts/setup_db.py` (INVESTIGATE: file not opened in this audit; behavior inferred from migration 008 line 131 `INSERT INTO schema_migrations (version) VALUES (8) ON CONFLICT DO NOTHING`).
- **Readers**: `scripts/setup_db.py`.
- **Refresh cadence**: On `python scripts/setup_db.py` invocation.
- **Volume**: 10 rows (one per applied migration).
- **Idempotency**: PRIMARY KEY + `ON CONFLICT DO NOTHING`.

### A.10 `v1_label_invalidations`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/003_v1_economic_spine.sql:3-12`.
- **Writers**:
  - `scripts/invalidate_pre_v1_labels.py:32,61` (one-off invalidation script for paper_trades/decisions/profiles using legacy economic-model versions).
- **Readers**: None in source code paths (the columns are referenced via `valid_paper_trade_filter`/`valid_position_filter` in `src/economics/versioning.py`, which apply the `invalidated_at IS NULL AND economic_model_version='v1.0.0'` filter directly to the parent tables).
- **Refresh cadence**: Manual / on demand. UNKNOWN frequency.
- **Volume estimate**: UNKNOWN; bounded by total historical trades+decisions+profiles invalidated.
- **Idempotency**: BIGSERIAL pk; `target_table + target_id` would be a candidate natural key but no UNIQUE constraint exists. INVESTIGATE: re-running the script could double-log an invalidation.
- **Failure mode**: Script-level retry on failure; not part of the live runtime path.

### A.11 `fee_snapshots`

- **Source/Sink**: PG table, BIGSERIAL `id`, UNIQUE `(market_id, token_id, captured_at, source)`. Defined `docs/migrations/003_v1_economic_spine.sql:50-62`.
- **Writers**: **None in source code.** `src/economics/fee_snapshots.py` defines a builder `fee_snapshot_from_clob_market_info` but no caller in the runtime modules invokes a DB INSERT. INVESTIGATE: this table appears intended for `clob.polymarket.com getClobMarketInfo` capture but the integration is not wired. Backtest path at `src/backtest/normalizers.py:6` imports the builder but writes to `data_cache/`, not to PG.
- **Readers**:
  - `src/engine/confidence_engine.py:1025-1037` (`_build_signal_audit` reads `ORDER BY captured_at DESC LIMIT 1`; effectively always returns NULL → `evaluate_signal_gate` sees `has_fee_snapshot=False`).
  - `src/api/main.py:354,384` (`_db_data_quality_snapshot` counts distinct `(market_id, token_id)` for `fee_snapshot_coverage_pct`; falls back to `markets.fee_rate_pct` since the table is empty).
- **Refresh cadence**: N/A — never written.
- **Volume estimate**: 0 rows in production today (RED FLAG).
- **Idempotency**: UNIQUE constraint defined but unreachable.

### A.12 `signal_audits`

- **Source/Sink**: PG table, BIGSERIAL `id`, FK to `fee_snapshots(id)`. Defined `docs/migrations/003_v1_economic_spine.sql:64-80`.
- **Writers**: **None in source code.** RED FLAG: signal_audit data is captured into `decision_log.signal_audit JSONB` (per migration 003 line 33) and never reaches this dedicated table.
- **Readers**:
  - `src/api/main.py:414-415,437` (count of rows in last 1h for pipeline-stage health snapshot — always 0).
- **Refresh cadence**: N/A.
- **Volume estimate**: 0 rows.

### A.13 `portfolio_state`

- **Source/Sink**: PG table, singleton row id=1. Defined `docs/migrations/004_portfolio_state.sql:9-17`.
- **Writers**:
  - `src/engine/portfolio_state.py:68-92` (`save_state`, UPSERT id=1).
- **Readers**:
  - `src/engine/portfolio_state.py:38-65` (`load_state` on PaperTrader boot).
  - `src/api/queries.py:381` (dashboard).
- **Refresh cadence**: Every paper_trade open and close (`src/engine/paper_trader.py:549,729`).
- **Volume estimate**: 1 row, ever.
- **Idempotency**: PRIMARY KEY id=1 + `ON CONFLICT DO UPDATE`.
- **Failure mode**: Save failure → logged, in-memory state still authoritative until restart (`src/engine/portfolio_state.py:93-94`).

### A.14 `portfolio_equity`

- **Source/Sink**: PG table, time-series PRIMARY KEY `time TIMESTAMPTZ`. Defined `docs/migrations/004_portfolio_state.sql:21-33`.
- **Writers**:
  - `src/engine/portfolio_state.py:97-131` (`record_equity`, UPSERT on `time` collision).
- **Readers**:
  - `src/api/queries.py:416` (dashboard equity curve).
- **Refresh cadence**: Every paper_trade open/close + every monitor-loop tick (60s, `src/engine/paper_trader.py:294-306`).
- **Volume estimate**: ~1 row/min × 60×24 = 1440 rows/day. **No retention job.**
- **Idempotency**: PRIMARY KEY `time` + `ON CONFLICT (time) DO UPDATE`.

### A.15 `market_belief_states`

- **Source/Sink**: PG table, UNIQUE `(market_id, strategy_track)`. Defined `docs/migrations/005_neural_readiness.sql:10-32`.
- **Writers**:
  - `src/engine/readiness_persistence.py:95-144` (UPSERT in `persist_readiness_snapshot`).
- **Readers**:
  - `src/api/main.py:407` (count for pipeline_stage_health).
- **Refresh cadence**: Every `GET /api/neural-readiness` call (`src/api/main.py:901-952`). UNKNOWN external poll cadence; the dashboard's tick interval `STATS_PUSH_INTERVAL_S=1.0s` does NOT call `/api/neural-readiness` directly — it lives in the terminal snapshot's `_get_terminal_snapshot` indirectly via `build_neural_readiness_snapshot` (`src/api/main.py:709-716`), but that path does NOT call `persist_readiness_snapshot`. So persistence depends on dashboard users hitting `/api/neural-readiness` on demand. INVESTIGATE.
- **Volume estimate**: ≤ count(active markets) × count(strategy_tracks). With ~50 ready markets × 1 track ≈ 50 rows.
- **Idempotency**: UNIQUE `(market_id, strategy_track)` + `ON CONFLICT ... DO UPDATE`.

### A.16 `decision_state_transitions`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/005_neural_readiness.sql:37-50`.
- **Writers**:
  - `src/engine/readiness_persistence.py:150-168` (INSERT only on state change; same trigger as A.15).
- **Readers**:
  - `src/engine/readiness_persistence.py:177-186` (`load_recent_persisted_transitions`).
  - `src/api/main.py:408-410` (count `created_at >= NOW() - INTERVAL '1 hour'`).
- **Refresh cadence**: Same as A.15.
- **Volume estimate**: O(state changes / market). UNKNOWN. **No retention job.**

### A.17 `book_quality_snapshots`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/005_neural_readiness.sql:58-73`.
- **Writers**:
  - `src/observer/trade_observer.py:484-507` (`_persist_book_quality_snapshot`, called from `_record_book_metrics` on every WS `event_type='book'` message).
- **Readers**:
  - `src/api/queries.py:1679` (latest book per market in dashboard).
  - `src/api/main.py:410-412` (5-min count + age age).
- **Refresh cadence**: On every WS book event. RED FLAG: WS book channel volume can be 100s/min per market — this table can grow very fast with no retention.
- **Volume estimate**: WS messages per minute counter caps at `ws:msgs:minute:{bucket}` Redis key (TTL 180s). No volume comment in code. Estimated O(10k-100k rows/day) at full WS subscription.
- **Idempotency / dedup**: **None.** Every book event creates a new row; no UNIQUE constraint. **No retention job.**
- **Failure mode**: DB error → debug-level log, no rollback (`src/observer/trade_observer.py:508-509`).

### A.18 `system_control`

- **Source/Sink**: PG table, singleton id=1 with CHECK. Defined `docs/migrations/006_system_control.sql:17-29`.
- **Writers**:
  - `src/control/killswitch.py:178-212` (transactional UPDATE id=1 with FOR UPDATE).
  - `docs/migrations/006_system_control.sql:27-29` (seed row on migration apply).
- **Readers**:
  - `src/control/killswitch.py:296-313` (`_read_db`, fallback when Redis cache miss/expired).
  - `src/api/queries.py:?` (dashboard system status — INVESTIGATE: not pinpointed).
- **Refresh cadence**:
  - Writes: triggered by Telegram bot `/killswitch`, `/pause`, `/resume`, or `POST /api/control/killswitch` (UNKNOWN exact API endpoint), or test code.
  - Reads: every trade attempt via RiskManager + every 5 min by `killswitch_sync` job (`src/engine/jobs/killswitch_sync.py:28-43`, interval `KILLSWITCH_SYNC_INTERVAL_S=300s`).
- **Volume**: 1 row.
- **Idempotency**: CHECK `id=1` + PRIMARY KEY.
- **Failure mode**: DB unreachable → `KillswitchService.get_state()` returns `_safe_off_state` (everything off, fail-safe — `src/control/killswitch.py:99-103,355-362`).

### A.19 `system_control_audit`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/006_system_control.sql:31-42`.
- **Writers**: `src/control/killswitch.py:285-294` (one row per `field_changed` flip inside the killswitch mutation transaction).
- **Readers**: dashboard (UNKNOWN endpoint).
- **Refresh cadence**: Same as `system_control` writes.
- **Volume**: 1 row per killswitch flip.
- **Idempotency**: BIGSERIAL pk only.

### A.20 `live_trades`

- **Source/Sink**: PG table, SERIAL `id`. Defined `docs/migrations/008_live_trades.sql:40-69`.
- **Writers**:
  - `src/engine/live_trader.py:235` (INSERT on `decisions:live` event).
  - `src/engine/live_trader.py:280,347,465` (UPDATEs on fill / close / failure).
- **Readers**:
  - `src/engine/live_trader.py:138-148` (`_reload_open_trades` on boot, `WHERE status='open'`).
  - Dashboard (UNKNOWN endpoint).
- **Refresh cadence**: On every Redis `decisions:live` event (DecisionRouter publishes when `TRADING_MODE ∈ {live, dual}` AND `_passes_live_filter`). Default mode is `paper`, so this channel is silent in default config.
- **Volume estimate**: 0 rows in default `LIVE_TRADING_DRY_RUN=true` configuration.
- **Idempotency**: SERIAL pk; partial indexes on `status IN ('pending','open')` and on `clob_order_id WHERE NOT NULL`.
- **Failure mode**: `LIVE_TRADING_DRY_RUN=true` ⇒ status='shadow' rows written but no order sent (`src/engine/clob_client_wrapper.py:138-139`).

### A.21 `live_orders`

- **Source/Sink**: PG table, BIGSERIAL `id`, FK to `live_trades(id) ON DELETE CASCADE`. Defined `docs/migrations/008_live_trades.sql:100-118`.
- **Writers**:
  - `src/engine/order_manager.py:330-352` (INSERT on every CLOB place attempt).
  - `src/engine/order_manager.py:369-385` (UPDATE on order finalize).
- **Readers**: Dashboard (UNKNOWN endpoint).
- **Refresh cadence**: Same as live_trades.
- **Volume**: 1..N rows per live_trade; in dry-run all rows are `order_state='shadow'`.
- **Idempotency**: BIGSERIAL pk only.

### A.22 `risk_config_history`

- **Source/Sink**: PG table, BIGSERIAL `id`. Defined `docs/migrations/010_risk_config_history.sql:10-18`.
- **Writers**: `src/api/queries.py:3104-3117` (`log_risk_change`, called from `POST /api/risk/update`).
- **Readers**: `src/api/queries.py:3091-3097` (dashboard risk audit panel).
- **Refresh cadence**: On every dashboard `POST /api/risk/update` mutation.
- **Volume**: 1 row per key flip.
- **Idempotency**: BIGSERIAL pk only.

---

## B. Redis Keys, Channels, Caches

Redis 7.2-alpine. Single client per process (`redis_async.from_url(settings.REDIS_URL, decode_responses=True)`).
**No per-key TTL discipline** — most keys have explicit TTLs, but a few are unbounded.

### B.1 Pub/Sub Channels

| Channel | Producer (file:line) | Consumer (file:line) | Cadence | Idempotency |
|---|---|---|---|---|
| `trades:observed` | `src/observer/trade_observer.py:1119` (`_process_trade` after DB INSERT). | `src/observer/position_tracker.py:62` (PositionTracker), `src/graph/graph_engine.py:41` (GraphEngine), `src/profiler/behavior_profiler.py:123` (BehaviorProfiler trade loop), `src/engine/confidence_engine.py:92` (ConfidenceEngine — only `is_leader=True` filtered at L101). | One event per accepted trade insert. | None at message level; consumers are independent. Failure of one consumer doesn't block others (each subscribes independently). |
| `positions:closed` | `src/observer/position_tracker.py:389` (`_close_position`). | `src/profiler/behavior_profiler.py:106` (`_subscribe_positions_loop`). | One per closed position (FIFO, partial slices each emit separately). | None. |
| `decisions` | `src/engine/decision_router.py:234` (DecisionRouter, when mode∈{paper,dual}). Legacy: `src/engine/confidence_engine.py:870` (when no router injected). | `src/engine/paper_trader.py:278` (PaperTrader). | One per non-skip decision. | None at message level — PaperTrader applies own conflict checks. |
| `decisions:live` | `src/engine/decision_router.py:234` (mode∈{live,dual} + `_passes_live_filter`). | `src/engine/live_trader.py:171` (LiveTrader). | Same as `decisions`, filtered. | None. |
| `decisions:trace` | `src/engine/paper_trader.py:124` (paper rejection telemetry). | None pinpointed (likely dashboard inspector — INVESTIGATE). | Per paper-trader rejection. | None. |
| `positions:paper_opened` | `src/engine/paper_trader.py:560` | `src/telegram_bot/notifier.py:113` (subscribed to `ALL_CHANNELS`). | Per open. | None. |
| `positions:paper_closed` | `src/engine/paper_trader.py:723` | Telegram notifier. | Per close. | None. |
| `positions:live_opened` | `src/engine/live_trader.py:568` (one of two channels — INVESTIGATE which). | Telegram notifier. | Per fill (LiveTrader, when not dry_run). | None. |
| `positions:live_closed` | `src/engine/live_trader.py:568` | Telegram notifier. | Per close. | None. |
| `control:killswitch_changed` | `src/control/killswitch.py:269` (`_publish_change`). | Telegram notifier. | Per killswitch flip. | None. |
| `engine:crash` | `src/engine/main.py:42` (`_publish_crash`); `src/engine/watchdog.py:343` (`_publish_crash`). | Telegram notifier. | On unhandled exception OR component restart streak. | Watchdog throttles via `crash_published` flag (`src/engine/watchdog.py:75,292-294`). |
| `runtime_config:changed` | `src/control/runtime_config.py:183-187` (after `set_overrides`). | INVESTIGATE — RiskManager / ConfidenceEngine should subscribe but no `pubsub().subscribe('runtime_config:changed')` found. The 30s in-memory cache (`_CACHE_TTL_S` at L64) acts as the propagation mechanism instead. | Per dashboard `/api/risk/update`. | None. |
| `market:price_changes` | `src/observer/trade_observer.py:384` (on WS `event_type='price_change'`). | None pinpointed in source — likely consumed by `WSBridge` for dashboard fan-out (INVESTIGATE: `src/api/ws_bridge.py:68` calls `pubsub()` but channel set wasn't read). | High frequency on active markets. | None. |

### B.2 Key/Value Caches (with TTL)

| Key pattern | Producer | Consumer | TTL | Cadence | Notes |
|---|---|---|---|---|---|
| `falcon:{agent_id}:{md5(params)}` | `src/registry/falcon_client.py:146-148` (after every successful Falcon API call). | `src/registry/falcon_client.py:80-82` (read on every Falcon call before HTTP). | `FALCON_CACHE_TTL_S=172800` (48h, `src/config.py:40`). | Per Falcon agent invocation (584/581/556/569/574/575/568/572/579/585). | Stale fallback intent ("survive Falcon downtime") but the cache is checked BEFORE any error path, so 48h cache is the only protection. |
| `seen_trades:{wallet}:{market_id}:{YYYYMMDD}:{md5_12}` | `src/observer/trade_observer.py:823-840` (`SET NX EX 604800`). | Same path: SETNX returns None ⇒ duplicate. | 7 days (`DEDUP_TTL_S=7*86400` at L28). | Per trade attempt (WS + REST). | Bucket = floor(time, 1s). Cleared via `_clear_dedup_key` if DB INSERT fails OR DB-level UNIQUE catches it. |
| `ws:market:last_message_ts` | `src/observer/trade_observer.py:366` (every WS message). | `src/api/main.py:254`, `src/api/queries.py:2040,2671`. | 300s (`ex=300`). | Every WS frame received. | Used for `last_message_age_s` and `websocket_connected ≤ 30s` health check. |
| `ws:msgs:minute:{minute_bucket}` | `src/observer/trade_observer.py:372-373` (INCRBY + EXPIRE). | `src/api/main.py:262` (reads `prev_minute - 1`). | 180s. | Once per WS message. | Sliding-window msg/min counter for dashboard. |
| `metrics:book_age_p95_s` | `src/observer/trade_observer.py:520` (after deque-based p95 calc). | `src/api/main.py:266`. | 300s. | Per WS book event. | |
| `metrics:fee_snapshot_coverage_pct` | INVESTIGATE — read at `src/api/main.py:267` but no producer found. | API only. | UNKNOWN. | UNKNOWN. | Likely never set ⇒ falls back to DB calc (`_db_data_quality_snapshot`). |
| `metrics:token_map_coverage_pct` | INVESTIGATE — same status as above. | `src/api/main.py:268`. | UNKNOWN. | UNKNOWN. | |
| `book:last:{market_id}:{token_id}` | `src/observer/trade_observer.py:528-548` (per WS book event with bid/ask/age JSON). | `src/engine/confidence_engine.py:945` (`_load_book_snapshot` for signal_audit). | 300s. | Per WS book event. | |
| `price:{market_id}:{token_id}` | `src/observer/trade_observer.py:402-405` (per WS `price_change` event, per asset_id in changes list). | `src/engine/paper_trader.py:743` (latest price for monitor loop), `src/api/queries.py:479,1559`. | 300s. | Per WS price_change. | |
| `confidence:leader:{wallet}` | `src/engine/confidence_engine.py:766-770` (`precompute_redis_cache`, batch). | `src/engine/confidence_engine.py:545-562` (`_seed_thompson_from_cache`). | `max(3600, FALCON_CACHE_TTL_S)` = 48h. | Nightly batch (`scripts/batch_runner.py:101-111`). | |
| `control:killswitch:state` | `src/control/killswitch.py:336-340` (`_write_cache`). | `src/control/killswitch.py:319` (`_read_cache`). | `REDIS_TTL_S=2s` (`src/control/killswitch.py:35`). | On every state read miss + every mutation. | Force-refreshed every `KILLSWITCH_SYNC_INTERVAL_S=300s` by the killswitch_sync job. |
| `runtime_config:overrides` | `src/control/runtime_config.py:182` (`set_overrides`, no TTL). | `src/control/runtime_config.py:124,171` (per `_load_overrides`). | **No TTL** — persistent. | On dashboard `POST /api/risk/update`. | In-memory cache: 30s (`_CACHE_TTL_S`). |
| `heartbeat:{name}` | `src/engine/watchdog.py:88-92` (`write_heartbeat`, called by component busy loops). | `src/engine/watchdog.py:100-106` (`read_heartbeat`). | 4× component interval (default 120s). | Per component tick. | `WATCHDOG_HEARTBEAT_INTERVAL_S=30`. Cleanup via `redis_cleanup` job at 04:00 UTC removes orphans with `ttl=-1`. |
| `paper:rejections:1h` | `src/engine/paper_trader.py:118-121` (HINCRBY + EXPIRE). | `src/api/main.py:270`. | 3600s. | Per paper-trader rejection. | Hash field per reason code. |
| `signals:rejected:1h` | `src/engine/confidence_engine.py:985-986`. | `src/api/main.py:269`. | 3600s. | Per confidence-engine signal rejection. | Hash field per reason code. |
| `subscriptions:active_markets` | `src/engine/jobs/refresh_markets.py:84-86` (DELETE + SADD pipeline). | INVESTIGATE: claimed to be observer subscription source per file docstring (lines 17-21) but `src/observer/main.py` does NOT read this Redis SET. The observer pulls tokens from Gamma + DB at boot only (`_bootstrap_subscriptions`, L123-142). | No TTL — persistent SET. | Hourly (`REFRESH_MARKETS_INTERVAL_S=3600s`) IF the job is registered. | RED FLAG: job is NOT registered in `src/engine/main.py`'s scheduler — only registered inside `scripts/run_all.py` (which is the legacy single-process dev runner). |
| `trading:mode_override` | `src/telegram_bot/commands.py:227` (`/mode` command). | `src/engine/decision_router.py:150`, `src/telegram_bot/commands.py:52`. | No TTL — persistent. | On Telegram `/mode` invocation. | Master mode override; falls back to `TRADING_MODE` env. |

### B.3 In-Memory Caches (process-local, NOT Redis)

| Cache | Owner | Size cap | TTL | Eviction |
|---|---|---|---|---|
| `_market_meta_cache` | `TradeObserver` (`src/observer/trade_observer.py:316-319`). | `MARKET_META_CACHE_MAXSIZE=10_000`. | `MARKET_META_TTL_S=3600s`. | LRU on write, TTL on read. |
| `_leader_condition_ids` | `TradeObserver` (`src/observer/trade_observer.py:309-311`). | `LEADER_CONDITION_IDS_MAXSIZE=2_000`. | None — FIFO. | FIFO on overflow. Hot keys refresh on re-add. |
| `_book_age_samples` | `TradeObserver` (`src/observer/trade_observer.py:320`). | `deque(maxlen=512)`. | None. | Ring buffer. |
| `_market_trades` | `GraphEngine` (`src/graph/graph_engine.py:27`). | `defaultdict(lambda: deque(maxlen=1000))` per market. | None. | Ring buffer per market. |
| `_open_positions` | `PositionTracker` (`src/observer/position_tracker.py:42`). | UNCAPPED. | None. | RED FLAG: unbounded; lost on restart, no warm-start from DB. |
| `_market_tokens` | `PositionTracker` (`src/observer/position_tracker.py:47`). | UNCAPPED. | None. | Manual `invalidate_market_tokens`. |
| `_thompson` | `ConfidenceEngine` (`src/engine/confidence_engine.py:79`). | UNCAPPED (one entry per leader seen). | None. | Persisted to `confidence:leader:{wallet}` Redis cache via `precompute_redis_cache`. |
| `_cusum_state` | `ErrorModel` (`src/profiler/error_model.py:70`). | UNCAPPED. | None. | Implicit reset on phase downgrade. |

---

## C. External APIs

### C.1 Falcon API (Heisenberg Narrative)

- **Endpoint**: `POST https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized` (`src/config.py:36-38`).
- **Auth**: `Authorization: Bearer {FALCON_API_KEY}` header (`src/registry/falcon_client.py:48`).
- **Used by**: `src/registry/falcon_client.py` (sole client). Wrapped via `FalconClient.query(agent_id, params, limit, offset)`.
- **Agents called** (from `CLAUDE.md` § 5 + grep over source):
  - **584** (`get_leaderboard`): Falcon Score Leaderboard, called from `src/registry/leader_registry.py:31` every registry cycle, limit `INITIAL_LEADER_COUNT=200`. Window=15d, win_rate∈[0.45,0.92], min trades=30.
  - **581** (`get_wallet360`): Wallet 360 metrics, called from `src/registry/leader_registry.py:148` per stale leader (24h TTL), limit=1. Also probed from `src/api/main.py:163-171` once per 60s for health check.
  - **556** (Polymarket Trades): called from `scripts/batch_runner.py:58-62` (backfill nightly, limit=200 per wallet, condition_id="ALL"). Also legacy: `src/observer/trade_observer.py:642` (compatibility path).
  - **569**: Polymarket PnL — never called in source (declared in CLAUDE.md but no grep hits).
  - **574**: Polymarket Markets — `src/registry/leader_registry.py:318,320` (per-market lookup in `sync_markets`, limit=10).
  - **575, 568, 572, 585**: declared in CLAUDE.md, no source grep hits — UNREACHABLE.
  - **579** (`get_pnl_leaderboard`): PnL Leaderboard fallback, called from `src/registry/leader_registry.py:85` when agent 584 fails, limit=200, period=7d.
- **Refresh cadence**: rate-limited via `FALCON_MAX_REQUESTS_PER_MINUTE=60` (`src/config.py:41`, `src/registry/falcon_client.py:60-70`). Each call is throttled to ≥ 1.0s spacing.
- **Volume estimate**: per registry cycle — 1 call agent 584 + ≤300 calls agent 581 (stale-only, `src/registry/leader_registry.py:139`) + ≤300 calls agent 574 (`sync_markets` LIMIT 300, L311) = ~600 calls / 30 min = ~1200 calls/hour. Falcon RPM cap of 60 ⇒ throttled to ≤ 60/min.
- **Idempotency / dedup**: Redis cache `falcon:{agent_id}:{md5(params)}` 48h TTL (B.2). All calls go through `query()` which checks cache first.
- **Failure mode**: Per-call retry: 3 attempts with 2^attempt s backoff (`src/registry/falcon_client.py:101-141`). HTTP 400/404/422 ⇒ `FalconAPIError` immediately (no retry). HTTP 429/5xx ⇒ retry. After 3 failures ⇒ raise. Caller paths:
  - `refresh_leaderboard` ⇒ `_fallback_leaderboard_entries` (PnL leaderboard) ⇒ DB cached count.
  - `enrich_leaders` ⇒ skip cycle (`src/registry/leader_registry.py:124-127`).
  - `sync_markets` ⇒ Gamma fallback (`src/registry/leader_registry.py:322-325`).

### C.2 Polymarket CLOB WebSocket

- **Endpoint**: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (`src/observer/websocket_client.py:19`, `src/config.py:53`).
- **Used by**: `src/observer/websocket_client.py` → `src/observer/trade_observer.py:_handle_ws_message`.
- **Subscription**: `{"assets_ids": [...], "type": "market", "custom_feature_enabled": True}` (`src/observer/websocket_client.py:141-153`). Chunks of `SUBSCRIBE_CHUNK_SIZE=100`.
- **Events received** (per CLAUDE.md `src/observer/CLAUDE.md` and code):
  - `event_type='book'` ⇒ `_record_book_metrics` (Redis `book:last:*`, `metrics:book_age_p95_s`) + DB `book_quality_snapshots` INSERT (`src/observer/trade_observer.py:407-408,484-509`).
  - `event_type='price_change'` ⇒ Redis publish to `market:price_changes` + per-asset `price:{market_id}:{token_id}` cache (`src/observer/trade_observer.py:379-406`).
  - `event_type='trade'` ⇒ legacy path `_process_legacy_ws_trade` (only when wallet attribution present, rare on modern feed) → `trades_observed` INSERT (`src/observer/trade_observer.py:377-378,564-604`).
- **Refresh cadence**: continuous push. `WEBSOCKET_PING_INTERVAL_S=30s`, `WEBSOCKET_PONG_TIMEOUT_S=10s` (`src/config.py:64-65`, `src/observer/websocket_client.py:85-89`). Auto-reconnect with exponential backoff (1→60s) on disconnect.
- **Volume estimate**: WS message rate logged via `metrics:book_age_p95_s` and `ws:msgs:minute:{bucket}`. UNKNOWN absolute throughput; the `_book_age_samples deque(maxlen=512)` suggests bursts of 100s/min.
- **Idempotency / dedup**: Redis dedup at the trade-INSERT layer (B.2 `seen_trades:*`). Book snapshots have NO dedup.
- **Failure mode**: silent drop — `WebSocketException, ConnectionClosed, OSError` ⇒ exponential backoff reconnect (`src/observer/websocket_client.py:62-81`). The CLOB does NOT replay missed messages on reconnect; recovery comes from REST `data-api` polling.

### C.3 Polymarket data-api REST

- **Endpoint**: `https://data-api.polymarket.com/trades` (`src/observer/trade_observer.py:692,712`).
- **Used by**:
  - `_backfill_wallet_trades(session)`: `?user={wallet}&limit=100` per leader wallet (`src/observer/trade_observer.py:687-703`).
  - `_backfill_market_activity(session)`: `?limit={DATA_API_GLOBAL_TRADES_LIMIT=1500}` (`src/observer/trade_observer.py:705-730`).
- **Refresh cadence**: every `TRADE_OBSERVER_POLL_INTERVAL_S=30s` (`src/config.py:59`, `src/observer/trade_observer.py:606-619`). Each poll iterates `_leader_wallets` (~200) for the per-wallet endpoint (= ~200 HTTP calls / 30s = ~7 RPS) PLUS one global market call. RED FLAG: NO RATE-LIMIT ROTECTION; no throttle; no semaphore beyond what aiohttp connector enforces.
- **Volume estimate**: 200 wallets × 100 trades/call × every 30s = up to 20k trade events polled / 30s ≈ 666 events/s into the dedup pipeline. Most are duplicates.
- **Idempotency / dedup**: Redis `seen_trades:*` + DB UNIQUE INDEX (A.2).
- **Failure mode**: per-call try/except with `aiohttp.ClientTimeout(total=8)` for wallet polls and `total=10` for market polls; status != 200 ⇒ skip silently; exception ⇒ debug log (`src/observer/trade_observer.py:694-702,716-722`).

### C.4 Polymarket Gamma API REST

- **Endpoint**: `https://gamma-api.polymarket.com/markets`.
- **Used by** (multiple call sites):
  - `src/observer/main.py:46` (bootstrap WS subscription, `?active=true&closed=false&limit=50&order=volume24hr`).
  - `src/observer/trade_observer.py:1254` (`_fetch_market_metadata_from_gamma`, `?conditionId={market_id}` then `?clobTokenIds={token_id}` fallback, limit=1, on every new market or stale `market_meta_cache` miss).
  - `src/registry/leader_registry.py:390` (`_fetch_market_from_gamma`, `?conditionId={market_id}&limit=1`, fallback when Falcon agent 574 fails).
  - `src/engine/jobs/refresh_markets.py:42-46` (`?active=true&closed=false&limit=50&order=volume24hr&ascending=false`, hourly).
- **Refresh cadence**: opportunistic on cache miss + observer boot + hourly job. UNKNOWN total cadence.
- **Volume estimate**: probably 100s of calls/hour at peak (when many new market_ids land).
- **Idempotency**: response cached in `_market_meta_cache` (in-memory) with 1h TTL.
- **Failure mode**: `aiohttp.ClientTimeout(total=8 or 10)`; status != 200 ⇒ return None; exception ⇒ debug log; caller falls back to local stub (`src/observer/trade_observer.py:1265-1276`).

### C.5 Polymarket CLOB REST + py-clob-client

- **Endpoint**: `https://clob.polymarket.com` (`src/config.py:140`).
- **Used by**: `src/engine/clob_client_wrapper.py` (wrapper around `py-clob-client` SDK; sync client run via `loop.run_in_executor`). Methods declared: `get_midpoint`, `get_orderbook`, `place_limit_order`, `cancel_order`, `get_order_status`, `get_trades_for_order` (`src/engine/clob_client_wrapper.py:22-29`).
- **Refresh cadence**:
  - `LIVE_FILL_POLL_INTERVAL_S=2.0s` (`src/config.py:156`) — poll for fills on open orders.
  - `LIVE_ORDER_TIMEOUT_S=30s` — cancel/reprice timeout.
  - `LIVE_ORDER_MAX_RETRIES=3`.
- **Volume estimate**: 0 in default `LIVE_TRADING_DRY_RUN=true`. In live: ≤ MAX_CONCURRENT_POSITIONS=10 × poll rate.
- **Idempotency**: `live_orders` audit row pre-insert (`src/engine/order_manager.py:330-352`). Each retry attempt is a new row with incremented `attempt_index`.
- **Failure mode**: dry-run short-circuits to shadow rows; live errors surface via `PlaceOrderResult.error_message` + `live_orders.error_message` field; rejected status via SDK exception.

### C.6 Telegram Bot API

- **Endpoint**: Telegram cloud (`https://api.telegram.org`, via `python-telegram-bot` SDK).
- **Auth**: `TELEGRAM_BOT_TOKEN` (`src/config.py:190`).
- **Used by**: `src/telegram_bot/notifier.py` (outbound) + `src/telegram_bot/bot.py` (inbound long-polling).
- **Refresh cadence**: long-poll `TELEGRAM_POLL_TIMEOUT_S=30s`. Outbound throttled at `TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE=20` (`src/config.py:202-206`).
- **Volume estimate**: tens to hundreds of msgs/day depending on signal volume.
- **Idempotency**: leaky-bucket throttle (`src/telegram_bot/notifier.py:80,116`). Each Redis pub/sub event = at most one Telegram message (per authorized chat_id).
- **Failure mode**: silent — `TELEGRAM_ENABLED=false` or empty token ⇒ service idles.

---

## D. Local File Caches

### D.1 `data_cache/` (Backtest parquet shards)

- **Path**: `polymarket-bot/data_cache/{dataset}/{shard}.parquet` (`src/backtest/cache.py:23-43`).
- **Datasets**: `falcon_556_trades`, `falcon_568_candles`, `falcon_572_books`, `falcon_574_markets`, `manifest` (per `ls data_cache/`).
- **Existing scenario folders**: `gate_20x30_diag/`, `smoke_5x7/`.
- **Writer**: `src/backtest/cache.py:27-43` (`BacktestCache.write_records`); called from `src/backtest/data_loader.py:79-...` after Falcon fetches.
- **Reader**: `src/backtest/cache.py:46-54` (`read_records`).
- **Cadence**: on-demand (when `python scripts/backtest.py` runs).
- **Volume**: UNKNOWN — depends on backtest scenario size; CLAUDE.md notes 30 days × leaders × markets.
- **Idempotency**: `dedupe_keys` arg on write_records dedups within a shard; cross-shard dedup not enforced.
- **Failure mode**: silent overwrite of `{shard}.parquet`. **No retention/cleanup.**

### D.2 Log files

- **Path**: `LOG_FILE` env var (default empty ⇒ stderr only). On Oracle Cloud / Hetzner: `/var/log/polymarket-bot/app.log` per `src/config.py:289-295`.
- **Writer**: loguru via `src/logging_setup.py:94`.
- **Rotation**: `LOG_FILE_ROTATION="daily"`, `LOG_FILE_RETENTION="14 days"`.
- **Aux paths (read by API)**: `/tmp/polymarket-bot-observer.log`, `polymarket-bot/orchestrate.log` — `src/api/main.py:65-68`. The API tail-reads these for the dashboard log panel (`load_recent_log_entries`, `src/api/terminal_snapshot.py`).

### D.3 pg_dump scratch

- **Path**: `BACKUP_LOCAL_SCRATCH_DIR=/tmp` (`src/config.py:279`).
- **Filename**: `polymarket-{ISO8601}.dump` (`src/backups/job.py:122-123`).
- **Cleanup**: unlinked on success, kept on upload failure (`src/backups/job.py:170-177`).

---

## E. R2 / S3 Backup Paths

### E.1 Cloudflare R2 — postgres dumps

- **Endpoint**: `R2_ENDPOINT_URL` env (S3-compatible, `https://<account_id>.r2.cloudflarestorage.com`).
- **Bucket**: `R2_BUCKET="polymarket-backups"` (`src/config.py:264`).
- **Object key**: `{R2_KEY_PREFIX}{YYYY}/{MM}/{YYYY-MM-DDTHH-MM-SSZ}.dump` (`src/backups/job.py:46-51`). Default prefix `postgres/`.
- **Writer**: `src/backups/job.py:138-145` (`r2_client.put_file`, called from `run_backup_once`).
- **Reader / lister**: `src/backups/job.py:148-154` (retention sweep), `src/backups/r2_client.py:114-130` (paginated `list_objects_v2`).
- **Deleter**: `src/backups/r2_client.py:132-159` (bulk delete, capped at 1000 keys/req).
- **Cadence**: cron `BACKUP_HOUR_UTC=5` daily (`src/config.py:258`, `src/backups/main.py:84-86`). Only runs if `BACKUPS_ENABLED=true` AND all 4 R2 envs set.
- **Volume estimate**: 1 dump/day × custom-format `--compress=9`. Migration 007 cites "71k existing rows" baseline; mature DB likely 100s of MB compressed.
- **Idempotency**: timestamp-keyed object name guarantees uniqueness.
- **Retention**: GFS via `src/backups/retention.py` `classify_keys` — `BACKUP_RETENTION_DAILY=7`, `_WEEKLY=4`, `_MONTHLY=3`, `BACKUP_WEEKLY_DOW=6` (Sun) (`src/config.py:269-274`). Total ≈ 14 objects.
- **Failure mode**:
  - `pg_dump` fails ⇒ `PgDumpError`, dump unlinked, RED+log, raises out (`src/backups/dumper.py:118-127`).
  - R2 upload fails ⇒ scratch file kept for retry, raises (`src/backups/job.py:140-145`).
  - Retention sweep fails ⇒ logged WARN, doesn't fail the run (`src/backups/job.py:165-168`).
  - Wrapping `make_backup_job` swallows all exceptions and lets APScheduler retry next cycle (`src/backups/job.py:230-235`).

---

## F. Refresh-Cadence Cross-Reference

The synthesis agent consumes this table. Rows: data sources. Columns: refresh trigger / target latency / current latency-if-known / downstream consumers. "Target latency" is the design intent (from CLAUDE.md or comments); "Current latency-if-known" is what the code actually achieves where measurable.

| # | Data source | Refresh trigger | Target latency | Current latency (if known) | Downstream consumers |
|---|---|---|---|---|---|
| 1 | Falcon agent 584 (Falcon Score Leaderboard) | Registry loop, `FALCON_REFRESH_INTERVAL_S=1800s` (was 3600 per CLAUDE.md). | <1h fresh (CLAUDE.md § 9). | 30 min cycle + Redis 48h cache. | `leaders` table, `refresh_leaderboard` upsert. |
| 2 | Falcon agent 581 (Wallet 360) | `enrich_leaders` per-wallet on `last_refresh < NOW()-24h`, LIMIT 300/cycle. | 24h staleness window. | Up to ~24h + retry. | `leaders.wallet360_json`, `classification_json`. |
| 3 | Falcon agent 556 (Trades) | `scripts/batch_runner.py` nightly at 03:00 UTC, LIMIT 200/wallet. | Daily backfill. | 24h. | `trades_observed` (legacy compat). |
| 4 | Falcon agent 574 (Markets) | `sync_markets` per-market every 1800s (registry cycle), LIMIT 300/cycle. | <1h. | Up to 30 min cycle. | `markets` table. |
| 5 | Falcon agent 579 (PnL Leaderboard) | Fallback in `_fallback_leaderboard_entries` (only on agent 584 failure). | On-demand. | N/A | `leaders` upsert (preserve existing score). |
| 6 | Polymarket WS `wss://...market` (book events) | Continuous push from CLOB. | ~real-time, p95 book age <60s (per `metrics:book_age_p95_s`). | Tracked via Redis `metrics:book_age_p95_s` (300s TTL). | `book_quality_snapshots`, Redis `book:last:*`, `price:*`. |
| 7 | Polymarket WS `wss://...market` (price_change) | Continuous push. | Real-time. | UNKNOWN. | Redis `market:price_changes` channel, `price:*` cache. |
| 8 | Polymarket WS `wss://...market` (trade events with wallet) | Continuous push, rare on modern feed. | Real-time. | RARE — modern WS feed does not include wallets. | `trades_observed`, `trades:observed` pub/sub. |
| 9 | data-api.polymarket.com `/trades?user={wallet}` | `_backfill_wallet_trades` every 30s, all leaders, limit=100. | 30-60s leader-trade detection. | 30s polling cycle (no rate-limit, ~200 calls / 30s). | `trades_observed`, `trades:observed`. |
| 10 | data-api.polymarket.com `/trades?limit=1500` | `_backfill_market_activity` every 30s, single global call, filtered to recent leader markets. | 30s. | 30s. | `trades_observed`, `trades:observed`. |
| 11 | gamma-api.polymarket.com (per-market) | `_fetch_market_metadata_from_gamma` on cache miss + `MARKET_META_TTL_S=3600s`. | <1h fresh. | 1h cache window. | `markets` table. |
| 12 | gamma-api.polymarket.com (top markets bootstrap) | Observer boot (`_bootstrap_subscriptions`); also `refresh_markets` job hourly **if registered**. | Hourly. | Boot-only in production engine container (job not in main.py scheduler). | Observer WS subscription set; Redis `subscriptions:active_markets` (when job runs). |
| 13 | clob.polymarket.com (orderbook, midpoint) | LiveTrader on-demand per decision. | <1s pre-trade. | UNKNOWN. | `live_trades`, `live_orders`. |
| 14 | clob.polymarket.com (fills polling) | LiveTrader, `LIVE_FILL_POLL_INTERVAL_S=2.0s`. | 2s. | 2s. | `live_orders`, `live_trades`. |
| 15 | PG `trades_observed` cleanup | `step_cleanup_old_trades` daily 03:00 UTC, `RETENTION_TRADES_DAYS=90`. | Daily. | 90-day window. | DELETE old rows. |
| 16 | PG `decision_log` cleanup | **None in code.** | UNDEFINED. | Unbounded growth. | None. |
| 17 | PG `book_quality_snapshots` cleanup | **None in code.** | UNDEFINED. | Unbounded growth. | None. |
| 18 | PG `portfolio_equity` cleanup | **None in code.** | UNDEFINED. | ~1440 rows/day, ~525k rows/yr. | None. |
| 19 | PG `decision_state_transitions` cleanup | **None in code.** | UNDEFINED. | Unbounded growth (one row per state change per market). | None. |
| 20 | Hawkes batch | APScheduler cron `BATCH_HOUR_UTC=3` daily (`src/engine/main.py:136-140`). | Daily. | Daily. | `follower_edges.hawkes_alpha_mu`. |
| 21 | Error model phase upgrade | Same nightly batch, `step_refit_error_models`. | Daily for phase 2; weekly for phase 3 per CLAUDE.md (but actual code refits both nightly). | Daily. | `leader_profiles.error_model_blob`, `error_model_phase`. |
| 22 | Behavioral profile update | Real-time on `positions:closed` Redis event. | <1s. | Hot path, O(1) per event. | `leader_profiles.profile_json`. |
| 23 | Confidence cache precompute | Nightly batch. | Daily. | Daily. | `confidence:leader:{wallet}` Redis 48h TTL. |
| 24 | Adaptive thresholds | APScheduler interval 300s (`src/engine/main.py:160-164`). | 5 min. | 5 min. | `EFFECTIVE_THRESHOLDS` module-level dict (in-process only — doesn't propagate across containers; each container's scheduler refreshes its own copy). |
| 25 | Killswitch state | Reads: 2s Redis cache; force-refresh every `KILLSWITCH_SYNC_INTERVAL_S=300s`. Writes: synchronous on dashboard/Telegram action. | Reads <2s; writes immediate. | 5 min worst-case for stale Redis cache (per killswitch_sync purpose). | `RiskManager.check_can_trade`, `LiveTrader`, dashboard. |
| 26 | Runtime config (Risk knobs) | Reads: 30s in-process cache. Writes: synchronous on dashboard `POST /api/risk/update`. | Reads <30s; writes immediate. | 30s for propagation across containers (since pub/sub is published but no subscribers found — INVESTIGATE). | `RiskManager`, `ConfidenceEngine`, `PaperTrader`. |
| 27 | Watchdog heartbeats | Components write per busy-loop tick (varies). Watchdog reads every 30s. | <30s freshness; 120s timeout. | 30s scan + 120s timeout. | Restart of frozen components; `engine:crash` channel. |
| 28 | Redis cleanup | Daily cron `REDIS_CLEANUP_HOUR_UTC=4`. | Daily. | Daily. | Orphan heartbeat keys (`heartbeat:*` with TTL=-1). |
| 29 | R2 backup upload | Daily cron `BACKUP_HOUR_UTC=5`. | Daily. | Daily, only if `BACKUPS_ENABLED=true`. | R2 `polymarket-backups/postgres/`. |
| 30 | Engine WebSocket bridge fan-out | `STATS_PUSH_INTERVAL_S=1.0s` push of terminal snapshot to WS clients. | 1s. | 1s. | Dashboard `/ws/live`. |
| 31 | Terminal snapshot cache | TTL 1.0s in process (`TERMINAL_SNAPSHOT_TTL_S`). | 1s. | 1s. | Dashboard. |
| 32 | Live snapshot cache | TTL 1.0s (`LIVE_SNAPSHOT_TTL_S`). | 1s. | 1s. | `/api/overview`, `/api/ml`. |
| 33 | Health check cache | TTL 5.0s (`HEALTH_CACHE_TTL_S`). | 5s. | 5s. | `/api/health`, snapshot composer. |
| 34 | Falcon health probe | TTL 60s in process; only refreshed when terminal snapshot is built. | 60s. | 60s. | Health check `falcon` flag. |
| 35 | `data_cache/` parquet caches | On `python scripts/backtest.py` run only. | On-demand. | N/A. | Backtest engine. |
| 36 | `paper:rejections:1h`, `signals:rejected:1h` Redis hashes | HINCRBY on each rejection + EXPIRE 3600s. | <1s. | TTL refreshed on every increment ⇒ effective 1h sliding. | Dashboard health. |
| 37 | `ws:msgs:minute:{bucket}` | INCRBY per WS msg + EXPIRE 180s. | <1s. | Real-time. | Dashboard `ws_messages_last_minute`. |
| 38 | `subscriptions:active_markets` | hourly job — only registered in legacy `scripts/run_all.py`, NOT engine main.py. | Hourly. | NEVER refreshed in current production engine container ⇒ Redis SET stays whatever bootstrap last wrote. | Documented as observer-side subscription source; observer never reads it. RED FLAG. |
| 39 | PostgreSQL connection pool warmup | On every `*/main.py` boot (`initialize_pool`). | <5s startup. | UNKNOWN. | All async DB ops. |
| 40 | Telegram inbound long-poll | `TELEGRAM_POLL_TIMEOUT_S=30s`. | 30s. | 30s. | `/status`, `/pnl`, `/positions`, `/mode`, `/killswitch`, `/pause`, `/resume` commands. |

---

## G. Red Flags / Inventory Gaps (no fixes — observation only)

These are documented here so the synthesis agent can prioritize. **Not** prescriptions.

1. **`fee_snapshots` table never written** (A.11). Read by `confidence_engine._build_signal_audit`, used for `evaluate_signal_gate` decisions; always returns NULL ⇒ `has_fee_snapshot=False`. The `markets.fee_rate_pct` legacy column is the only fee source actually populated (via Gamma `makerBaseFee`). Migration 003 introduces the table; no production code path INSERTs.
2. **`signal_audits` table never written** (A.12). Migration 003 creates it; the audit data is captured into `decision_log.signal_audit JSONB` instead. The dashboard counts rows in `signal_audits` for the last hour ⇒ always 0.
3. **`subscriptions:active_markets` Redis SET never read** (B.2 last row, F#38). Hourly `refresh_markets` job exists at `src/engine/jobs/refresh_markets.py` but is NOT registered in `src/engine/main.py`'s scheduler. Even if registered, no observer reads the SET — observer pulls subscriptions from Gamma + DB at boot only.
4. **`data-api.polymarket.com` polling has no rate-limit protection.** `_backfill_wallet_trades` makes ~200 sequential HTTP calls per 30s cycle (one per leader wallet), no semaphore, no backoff, no Redis cache. Falcon has `_throttle()` at 60 RPM; data-api has nothing.
5. **`PositionTracker` in-memory state is unbounded and never warm-started from DB.** `_open_positions` dict grows without cap; on engine restart, all in-flight open positions are lost ⇒ subsequent SELLs are dropped (`_handle_sell` returns when key missing).
6. **No retention on `decision_log`, `book_quality_snapshots`, `portfolio_equity`, `decision_state_transitions`, `live_orders`, `system_control_audit`, `risk_config_history`, `signal_audits`, `fee_snapshots`** (F#16-19). Only `trades_observed` has cleanup. `book_quality_snapshots` is the highest growth-rate table.
7. **`runtime_config:changed` Redis pub/sub channel published but no subscriber.** Per `src/control/runtime_config.py:183-187` the writer publishes; no `pubsub().subscribe('runtime_config:changed')` found in source. Propagation relies entirely on the 30s in-process cache TTL ⇒ engine container can read stale risk thresholds for up to 30s after dashboard write.
8. **`positions_reconstructed` has no UNIQUE constraint.** A double-close (e.g. due to retry after partial failure) would create duplicate rows. PnL aggregations would silently double-count.
9. **`decision_log.outcome` UPDATE uses `ORDER BY time DESC LIMIT 1`** to attribute close to the last decision for `(leader_wallet, market_id)`. Race window if two paper trades open back-to-back for same (leader, market).
10. **No DB-level cleanup for `paper_trades`, `live_trades`, `positions_reconstructed`, `leader_profiles`** — these are intentionally retained for ML re-fits, but the absence of per-row size limits or partition rotation on `leader_profiles.error_model_blob BYTEA` could grow without bound (LightGBM phase 3 blobs can be MBs).
11. **`book_quality_snapshots.gap_detected` column always FALSE** (`src/observer/trade_observer.py:503`). Either dead column or unimplemented detection.
12. **Per-market `fee_rate_pct` written from Gamma `makerBaseFee`** which Polymarket docs describe as MAKER fee — but `paper_trader.calculate_polymarket_fee` calls it with `liquidity_role=LiquidityRole.TAKER` (`src/engine/paper_trader.py:488`, `src/observer/position_tracker.py:284`). INVESTIGATE: this may be a fee-source mismatch.
13. **`data_cache/` parquet files have no rotation/cleanup** (D.1). Backtest scenarios accumulate.
14. **`market_belief_states` and `decision_state_transitions` only persist when `/api/neural-readiness` is hit** (A.15). The terminal snapshot path builds the same readiness data but doesn't persist. So persistence is effectively user-driven, not autonomous.
15. **`schema_migrations` table referenced but `scripts/setup_db.py` not opened in this audit.** Behavior inferred from migration 008 line 131. INVESTIGATE if not all migrations track their version.
16. **`v1_label_invalidations` has no UNIQUE constraint on `(target_table, target_id)`.** Re-running `scripts/invalidate_pre_v1_labels.py` could double-log.
17. **The legacy `src/observer/trade_observer.py:_backfill_from_falcon` path** is kept "for older Falcon trade fixtures" but is never called in production code; only test fixtures hit it.
18. **`refresh_thresholds` is a per-process module-level dict** (`EFFECTIVE_THRESHOLDS` in `src/config.py:380`). Each container (engine, api, observer if it imported it) has its own copy; only the engine refreshes via APScheduler. The API container reads stale values forever unless the FastAPI lifespan hooks call `refresh_effective_thresholds`. INVESTIGATE.
19. **`engine:crash` channel is fire-and-forget**: if Telegram is disabled (default `TELEGRAM_ENABLED=false`), crash signals are lost.
20. **`sync_markets` LIMIT 300/cycle** combined with 1800s cycle ⇒ at 1900 active markets, the queue can take ~3 hours to drain when there's a backlog of stub `markets` rows.

---

## H. Per-Module Cross-Reference Index

For the synthesis agent's convenience, lookup of "what does module X read/write":

### `src/registry/`
- Reads: Falcon (584/581/574/579), `leaders` (cached count), `trades_observed` (sync_markets seed), `markets` (sync target).
- Writes: `leaders` (insert/update + flag-flip), `markets` (upsert in sync + recategorize), `trades_observed.category`, `positions_reconstructed.category` (recategorize backfill).
- Redis: `falcon:{agent_id}:{params_hash}` 48h TTL.

### `src/observer/`
- Reads: WS market channel, data-api `/trades`, gamma-api `/markets`, Falcon (legacy compat), `trades_observed` (rehydrate), `markets` (token/category lookup), `leaders` (bootstrap).
- Writes: `trades_observed`, `markets` (stub + Gamma upsert + repair), `book_quality_snapshots`, `positions_reconstructed` (via PositionTracker), Redis pub/sub `trades:observed`+`positions:closed`+`market:price_changes`, Redis caches `book:last:*`+`price:*`+`metrics:*`+`ws:*`+`seen_trades:*`.

### `src/graph/`
- Reads: `trades_observed` (warm-start + Hawkes lookback), `follower_edges` (existing state).
- Writes: `follower_edges` (Beta-Binomial upsert + Hawkes alpha_mu update).
- Redis: subscribe `trades:observed`.

### `src/profiler/`
- Reads: `leader_profiles` (current state), `paper_trades` (replay), `markets` (category/liquidity).
- Writes: `leaders` (FK guard insert), `leader_profiles` (profile_json + error model state).
- Redis: subscribe `positions:closed` and `trades:observed`.

### `src/engine/`
- Reads: `leaders`, `leader_profiles`, `follower_edges`, `markets`, `trades_observed`, `positions_reconstructed`, `paper_trades`, `live_trades`, `fee_snapshots` (always empty), `system_control`, `portfolio_state`, `book_quality_snapshots` indirectly via Redis.
- Writes: `decision_log`, `paper_trades`, `live_trades`, `live_orders`, `portfolio_state`, `portfolio_equity`, `market_belief_states` (via api), `decision_state_transitions` (via api).
- Redis: pub/sub `decisions`, `decisions:live`, `decisions:trace`, `positions:paper_opened/closed`, `positions:live_opened/closed`, `engine:crash`. Caches: `confidence:leader:*`, `heartbeat:*`, `paper:rejections:1h`, `signals:rejected:1h`.

### `src/control/`
- Reads/Writes: `system_control`, `system_control_audit`. Redis cache `control:killswitch:state` 2s + pub/sub `control:killswitch_changed`. Redis `runtime_config:overrides` (no TTL) + pub/sub `runtime_config:changed`.

### `src/api/`
- Reads: virtually every PG table (dashboard SQL, ~3636 lines in queries.py).
- Writes: `risk_config_history` (audit log on POST /api/risk/update), `market_belief_states`, `decision_state_transitions` (only on /api/neural-readiness hits).
- Redis: every cache key for read-only display.

### `src/backups/`
- Reads: PG (via `pg_dump` subprocess against `DATABASE_URL`). R2 bucket (list_objects for retention).
- Writes: R2 `polymarket-backups/postgres/{YYYY}/{MM}/{ISO}.dump`.

### `src/telegram_bot/`
- Reads: Redis pub/sub (all alert channels).
- Writes: Redis `trading:mode_override` (via `/mode` command). Calls `KillswitchService` mutations.

### `scripts/batch_runner.py`
- Reads: `leaders`, `leader_profiles`, plus all profiler/error/Hawkes inputs.
- Writes: `leaders` (refresh), `markets` (sync), `follower_edges` (Hawkes), `leader_profiles` (phase upgrade + decision learning replay), Redis `confidence:leader:*` cache. DELETE on `trades_observed`.

---

## I. Migration-to-Table Cross-Reference

| Migration | Tables touched | New columns / indexes |
|---|---|---|
| 001_schema.sql | `leaders`, `trades_observed`, `positions_reconstructed`, `follower_edges`, `leader_profiles`, `markets`, `paper_trades`, `decision_log`, `schema_migrations` | base CREATE + indexes |
| 002_dashboard_compat.sql | `trades_observed`, `paper_trades`, `decision_log` | additional partial indexes |
| 003_v1_economic_spine.sql | `paper_trades`, `decision_log`, `leader_profiles`, `positions_reconstructed`, `v1_label_invalidations` (NEW), `fee_snapshots` (NEW), `signal_audits` (NEW) | economic-model versioning columns + audit tables |
| 004_portfolio_state.sql | `portfolio_state` (NEW), `portfolio_equity` (NEW) | bankroll persistence + equity time series |
| 005_neural_readiness.sql | `market_belief_states` (NEW), `decision_state_transitions` (NEW), `book_quality_snapshots` (NEW) | per-market readiness + transition log + book observability |
| 006_system_control.sql | `system_control` (NEW), `system_control_audit` (NEW) | killswitch + audit |
| 007_trades_observed_idempotency.sql | `trades_observed` | de-dup historical rows + UNIQUE INDEX `uq_trades_observed_natural_key` |
| 008_live_trades.sql | `live_trades` (NEW), `live_orders` (NEW) | live-trading state machine |
| 009_trades_category_denorm.sql | `trades_observed`, `positions_reconstructed`, `markets` | denormalized `category` column on observed/reconstructed; backfill |
| 010_risk_config_history.sql | `risk_config_history` (NEW) | runtime config audit log |

---

## J. Open Questions / INVESTIGATE Items

(consolidated from inline `INVESTIGATE` markers above)

1. `decisions:trace` Redis channel — confirmed producer is `paper_trader.py:124`; consumer not pinpointed (likely dashboard inspector or unwired).
2. `runtime_config:changed` Redis channel — published but no `pubsub().subscribe('runtime_config:changed')` in repo; effective propagation = 30s in-process cache TTL.
3. `metrics:fee_snapshot_coverage_pct` and `metrics:token_map_coverage_pct` Redis keys — read by API but no producer found in source. Likely never set.
4. `subscriptions:active_markets` Redis SET — `refresh_markets` job is the producer but is NOT registered in `src/engine/main.py`. The observer also doesn't read this SET. Apparent dead path.
5. PositionTracker has no warm-start from DB; restart loses in-flight `_open_positions`.
6. `EFFECTIVE_THRESHOLDS` is per-process; only the engine container refreshes via APScheduler. API container's copy may be stale.
7. `gamma-api makerBaseFee` interpreted as both maker AND taker fee depending on call site (paper_trader uses TAKER role).
8. `live_trader.py:568` publishes both `positions:live_opened` and `positions:live_closed` — INVESTIGATE which is which (single grep hit).
9. `scripts/setup_db.py` not opened in this audit — confirms how `schema_migrations` is populated and whether all 10 migrations run idempotently.
10. `system_control` is read by the dashboard (`/api/system`) — exact path/endpoint not pinpointed in the limited dashboard SQL we grepped.

---

*End of inventory.*
