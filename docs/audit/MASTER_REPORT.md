# MASTER REPORT — Polymarket Leader Intelligence Bot
## Synthesis of audits 01–05

> Auditor: synthesis reviewer
> Inputs: `01_data_inventory.md`, `02_client_audit.md`, `03_schema_evolution.md`,
> `04_perf_hotpaths.md`, `05_ml_pipeline.md`
> Date: 2026-05-10
> All findings trace back to specialist IDs (`F-NN` client, `HP-N` perf, `MG-N`
> ML, `M-NN` migration, `R-NN` inventory red flag).

---

## Section 1 — Executive summary

**1. The killswitch is not a killswitch on the live-trading hot path.**
`KillswitchService` writes to PostgreSQL, then best-effort invalidates a Redis
cache with a 2-second TTL [F-05]. Every `RiskManager.check_can_trade` reads
through that cache. A real-execution flip therefore leaks trades for up to ~2 s
after a kill. The `control:killswitch_changed` Redis channel exists [R-7,
B.1 of inventory] but no engine subscriber consumes it — only the Telegram
notifier. For paper this is tolerable; for live trading it is unacceptable. The
fix is small: read DB on the live-trade gate, keep the cache only for dashboard
display.

**2. Three hot paths do multi-statement writes with no transaction.**
`PaperTrader.open_trade` [F-01], `PositionTracker._close_position` [F-02], and
`TradeObserver._process_trade` [F-03] each chain 3–5 SQL statements without
`conn.transaction()`. On any mid-chain failure the database is left in a torn
state — paper trades open in DB but bankroll stale, positions closed with
inconsistent denormalized category, trade rows inserted but `markets` stub not
refreshed. The reviewer-side `decision_log.outcome` UPDATE uses
`ORDER BY time DESC LIMIT 1` and can cross-attribute closes when two paper
trades on the same `(leader, market)` close back-to-back [R-9]. None of this is
hypothetical — the corruption surfaces as silent drift in PnL accounting.

**3. The gamma `makerBaseFee` is being applied as a TAKER fee.**
`markets.fee_rate_pct` is written from Gamma's `makerBaseFee` field [R-12].
`PaperTrader.calculate_polymarket_fee` then calls into `position_tracker` with
`liquidity_role=LiquidityRole.TAKER`. So every paper-trade fee accrual is
methodologically wrong — fees are systematically misreported (likely
underestimated, since maker rebates differ from taker fees). This corrupts
every backtest, every dashboard PnL number, and the entire economic-spine
calibration introduced in migration 003. It is invisible because the value
looks plausible.

**4. `liquidity_score` is sourced from the wrong Falcon agent and is 24 h
stale.** Code reads agent 574's `liquidity` field; every docstring and the
master `CLAUDE.md` claim agent 575 (Market Insights) — never called anywhere
[MG-3]. `sync_markets` only re-fetches markets older than 24 h, so the feature
the error model trusts most for market-regime context lags reality by ~12 h on
average and is the wrong field even when fresh. This is a model-validity bug
masquerading as a freshness bug. Phase 2 and Phase 3 error model predictions
are systematically blind to intraday liquidity drains.

**5. Eight tables have no retention policy.** `decision_log`,
`book_quality_snapshots`, `portfolio_equity`, `decision_state_transitions`,
`live_orders`, `system_control_audit`, `risk_config_history`,
`signal_audits`/`fee_snapshots` (the last two empty by accident, see below) —
none get cleaned [R-6, F#16-19 in inventory]. `book_quality_snapshots` is the
worst: 70k–700k rows/day at full WS subscription. At 10× target volume
[architect §2.2] the nightly DELETE on `trades_observed` itself becomes a
multi-hour vacuum-generating monster.

**6. The 30-second REST poll is the system's central pathology.** The CLOB
WebSocket does not carry wallet addresses [observer/CLAUDE.md], so every leader
trade is detected via `data-api` polling at `TRADE_OBSERVER_POLL_INTERVAL_S=30s`
[HP-1, MG-2, R-4]. Median trade-to-react latency is ~16 s, p99 ~32 s. The
wallet-by-wallet loop runs serially with no rate-limit protection and no
backoff. Combined with `LIVE_DECISION_MAX_TRADE_AGE_S=120 s`, a real tail of
leader signals is silently SKIPped at `confidence_engine.py:142`. Everything
downstream — Hawkes excitation kernels, FADE timing, the entire decision
freshness story — is gated by this 30 s clock.

**7. Dead code, dead writes, and dead tables outnumber live ones in the
economic-spine layer.** Migration 003 created `fee_snapshots` and
`signal_audits`; no production code path INSERTs into either [R-1, R-2].
`subscriptions:active_markets` Redis SET is written by a job that is not
registered in `engine/main.py` and is read by nobody [R-3]. The
`runtime_config:changed` pub/sub channel publishes with no subscriber [R-7].
`refresh_markets` job only runs inside the legacy `scripts/run_all.py`. The
Falcon agents 575, 568, 572, 585 declared in `CLAUDE.md` have zero callers. The
codebase is carrying significant aspirational architecture that the synthesis
must clean up before optimizing what's left.

**Single highest-leverage move this week.** Wrap `TradeObserver._process_trade`,
`PositionTracker._close_position`, and `PaperTrader.open_trade` in
`conn.transaction()` blocks [F-01/02/03], fix the killswitch read path to go
straight to DB on the live-trade gate [F-05], correct the gamma
maker-vs-taker-fee misinterpretation [R-12], fix the agent 574→575 source for
`liquidity_score` [MG-3], and add a retention policy to the 8 unbounded
tables [architect M-13/15, R-6]. Five problems, ≤5 dev-days, eliminate the
silent data-corruption surface entirely. Everything else (the 30 s poll, the
materialized views, the bivariate Hawkes) is a downstream improvement — but
none of it matters if PnL accounting and the killswitch are quietly wrong.

---

## Section 2 — Findings matrix

Severity: P0 = production-safety / correctness, P1 = perf or freshness with
real user impact, P2 = hygiene. Effort: S ≤1 day, M ≤1 week, L >1 week.
Domain: correctness / safety / perf / ml / architecture.

| ID | Title | Sources | Sev | Domain | Effort | Phase |
|---|---|---|---|---|---|---|
| MR-01 | Killswitch reads stale Redis cache for up to 2 s on flip; live trades can leak | F-05, inventory A.18, R-7 | P0 | safety | S | 0 |
| MR-02 | `paper_trader.open_trade` chains INSERT + 2 separate-connection updates without a transaction | F-01 | P0 | correctness | S | 0 |
| MR-03 | `position_tracker._close_position` runs SELECT+SELECT+INSERT without transaction; Redis publish fires even on DB inconsistency | F-02, inventory A.3 | P0 | correctness | S | 0 |
| MR-04 | `trade_observer._process_trade` chains 5 SQL statements + an inner repair function without `conn.transaction()` | F-03 | P0 | correctness | S | 0 |
| MR-05 | Gamma `makerBaseFee` written to `markets.fee_rate_pct`, applied as TAKER fee in paper_trader and position_tracker | R-12, MG (cross-ref) | P0 | correctness | S | 0 |
| MR-06 | `markets.liquidity_score` sourced from Falcon agent 574, not 575; doc-vs-code drift; 24 h staleness gate too coarse | MG-3, R doc mismatch | P0 | ml | M | 0/1 |
| MR-07 | 8 unbounded tables: decision_log, book_quality_snapshots, portfolio_equity, decision_state_transitions, live_orders, system_control_audit, risk_config_history, signal_audits/fee_snapshots | R-6, F#16-19 inventory, M-15 retention | P0 | architecture | S | 0 |
| MR-08 | `decision_log.outcome` UPDATE uses `ORDER BY time DESC LIMIT 1` — races when two paper trades on same (leader, market) close together | R-9, F-? cross-cutting | P0 | correctness | S | 0 |
| MR-09 | Redis pub/sub shares command client; subscribers silently drop on reconnect; six subscribers in engine container | F-04, F-13 | P1 | safety | M | 2 |
| MR-10 | `runtime_config:changed` published but no subscriber; 30 s in-process cache is the only propagation path | R-7, inventory B.1, F-9 (writer holds lock through Redis I/O) | P1 | architecture | S | 1 |
| MR-11 | Trade-to-react latency dominated by 30 s `data-api` poll; no wallet-attribution stream on WS | HP-1, MG-2, R-4 | P1 | perf | M | 1 |
| MR-12 | `_backfill_wallet_trades` runs 200 leaders serially with no rate-limit / backoff / cache | R-4, HP-1 #2 | P1 | perf | S | 1 |
| MR-13 | Per-trade processing does 3–7 DB roundtrips serially; no micro-batching of `trades_observed` writes | HP-1 #3, HP-1 #4 | P1 | perf | M | 1 |
| MR-14 | `LeaderRegistry.run` holds one DB connection across Falcon HTTP calls (300 wallets × ~1 s each) | F-07, HP-2 | P1 | perf | M | 1 |
| MR-15 | `FalconClient` uses Semaphore(1); serializes ALL Falcon calls system-wide irrespective of caller | HP-2 #1 | P1 | perf | S | 1 |
| MR-16 | `confidence_engine.precompute_redis_cache` writes N SETs sequentially; should pipeline | F-06 | P1 | perf | S | 1 |
| MR-17 | `runtime_config.set_overrides` does Redis I/O inside an asyncio lock; slow Redis blocks all `_load_overrides` callers (RiskManager hot path) | F-09 | P1 | perf | S | 1 |
| MR-18 | No backpressure between WS coroutine and DB writes; Postgres slowdown will OOM observer process | HP-1 #6, perf cross-cutting | P1 | safety | M | 1/2 |
| MR-19 | No Prometheus histograms anywhere; cannot validate any perf fix without metrics | HP-3 #4, perf observability gaps | P1 | architecture | S | 1 |
| MR-20 | API `_pool` 2/10 saturated by 17-way snapshot fan-out + 1 s push loop | F-17, HP-4 #1, F-38 | P1 | perf | S | 1 |
| MR-21 | `_get_terminal_snapshot` fans out 17 queries per second from a 10-connection pool | HP-4, F-17 | P1 | perf | M | 2 |
| MR-22 | `open_positions_with_prices` is N+1 (one Redis or DB lookup per open position) | HP-4 #5, F-37 | P1 | perf | S | 1 |
| MR-23 | Hawkes batch is univariate (fitted on follower's own time series); leader timestamps discarded | MG-5, HP-5 | P1 | ml | L | 3 |
| MR-24 | `error_model._fetch_training_data` reads `liquidity_score` and `is_leader` AS-OF-NOW for historical positions — train/serve skew + leakage | MG section 3.1, MG-3 | P1 | ml | M | 3 |
| MR-25 | `CalibratedClassifierCV(cv='prefit')` calibrates on same data the base model was fit on — over-confident | MG section 3.2 | P1 | ml | S | 3 |
| MR-26 | Class imbalance unhandled in Phase 3 LightGBM; no `class_weight`/`scale_pos_weight`; FADE edge systematically weak | MG section 3.2 | P1 | ml | S | 3 |
| MR-27 | `PositionTracker._open_positions` in-memory only; restart loses in-flight open positions; subsequent SELLs silently dropped | R-5, inventory A.3, F-? | P1 | correctness | M | 2 |
| MR-28 | `live_trades` open as 3 atomicity domains (pending insert → OrderManager writes → open update); engine restart leaves orphan pending rows | F-19 | P1 | correctness | M | 2 |
| MR-29 | `EFFECTIVE_THRESHOLDS` is a per-process dict; only the engine refreshes; API container reads stale values | R-18 | P2 | architecture | S | 2 |
| MR-30 | KDE timing model documented but never implemented; cyclical hour_sin/cos features are raw clock | MG-1 | P2 | ml | S | 3 |
| MR-31 | `trapped_rate` column exists, documented, never written; missed FADE signal | MG-4 | P2 | ml | S | 3 |
| MR-32 | Resolution events not subscribed to; positions held to resolution never feed profiler; selection bias in training data | MG-8 | P2 | ml | M | 3 |
| MR-33 | Thompson posteriors can read stale Redis cache on restart if cache is older than last paper close | MG-6 | P2 | ml | S | 2 |
| MR-34 | CUSUM state dual-stored (in-memory + JSONB) with `setdefault` precedence | MG-7 | P2 | ml | S | 2 |
| MR-35 | `falcon_no_data` is a hard exclusion; new wallets active <3 days are permanently excluded | MG section 3.3 | P2 | ml | S | 2 |
| MR-36 | Error model blobs have no recorded feature schema version; silent misalignment if `_build_features` changes | MG section 3.4 | P2 | ml | S | 2 |
| MR-37 | `trades_observed` heading toward partition-shaped pain (90-day DELETE generates multi-hour vacuum at 10× volume) | M-11 architect, R-6 | P1 | architecture | M | 2 |
| MR-38 | `book_quality_snapshots` is the second-fastest growing table and has no retention; unbounded | architect §2.11, R-6 | P1 | architecture | M | 2 |
| MR-39 | `positions_reconstructed` has no UNIQUE constraint; double-close from retry creates duplicate rows | R-8, architect §2.3 | P2 | correctness | S | 2 |
| MR-40 | Missing indexes: `paper_trades(opened_at) WHERE economic_model_version=v1.0.0 AND invalidated_at IS NULL`; same for `decision_log`; `follower_edges(follow_probability DESC, co_occurrences DESC)` | M-12 architect §2.7/2.4/2.8 | P2 | perf | S | 2 |
| MR-41 | No DOWN scripts for any of the 10 migrations | architect §3 reversibility | P2 | architecture | S | 2 |
| MR-42 | `CREATE INDEX` is non-CONCURRENTLY; locks writes for minutes at 10× volume | architect §3 index-creation-locking | P2 | architecture | S | 2 |
| MR-43 | No `markets_history` SCD-2 table; fee schedule and end-date changes lose history; economic model non-reproducible | architect §4.7, §4.8 B | P2 | architecture | M | 2 |
| MR-44 | No order-book imbalance per-minute rollup; data is in `book_quality_snapshots` but not aggregated | MG §5 #1, architect §4.8 A | P2 | ml | M | 3 |
| MR-45 | No CDC out of `trades_observed`; every downstream re-polls the hot table | architect §4.6 | P2 | architecture | L | 3 |
| MR-46 | `paper_trader._is_market_resolved` and 6 siblings each open their own DB connection per eligibility check | F-36 | P3 | perf | M | 2 |
| MR-47 | `_fetch_market_metadata_from_gamma` opens a fresh aiohttp session per call | F-34 | P3 | perf | M | 2 |
| MR-48 | `risk_manager` uses f-string SQL with `V1_PAPER_TRADE_SQL` constant; not injectable today but pattern is fragile | F-15 | P3 | safety | M | 2 |
| MR-49 | `api/queries.wallet_markets` interpolates `window_days` into INTERVAL via f-string | F-16 | P3 | safety | S | 1 |
| MR-50 | `_record_book_metrics` does up to 4 Redis writes serially per WS book event | F-30, F-29 | P3 | perf | S | 1 |

Notes on disagreements between specialists:
- HP-2 frames `FalconClient.Semaphore(1)` as a perf bug; F-07 frames the
  pool-acquisition pattern as the bug. Both are real. The synthesis treats
  them as MR-14 (don't hold DB conn across HTTP) and MR-15 (don't serialize
  all Falcon calls). Fix both; they don't interact.
- HP-1 #1 proposes dropping the global market sweep poll to 5 s; MG-2 proposes
  10 s for the per-wallet REST endpoint and a per-wallet authenticated WS
  channel for the top 50 leaders. Synthesis call: ship the 5 s global sweep
  in Phase 1 (single config flip); ship the user-channel WS subscription in
  Phase 3 (engineering work, blockchain auth, only meaningful once the
  trivial wins land). They are independent.
- Architect §4.5 says defer the read replica until mat-views land; HP-4
  bumps the API pool size in Phase 1. Both are correct. Bigger pool buys
  time; mat-views are the real fix. Synthesis sequences pool-bump → mat-views
  → revisit replicas.

---

## Section 3 — The phased roadmap

Each line is tagged with its specialist source ID for traceability. Effort
caps are dev-days assuming one engineer working full-time, not calendar
days.

### Phase 0 — Stop the bleeding (this week). Effort cap: 5 dev-days.

Production-safety only. No new features, no perf work.

- **Wrap the three critical hot paths in `conn.transaction()`** with the
  Redis publish moved to post-commit:
  - `PaperTrader.open_trade` + `close_trade` [F-01]
  - `PositionTracker._close_position` [F-02]
  - `TradeObserver._process_trade` [F-03]
- **Fix the killswitch live-trade gate**: read DB directly on every
  `is_real_execution_enabled()` call; keep Redis cache only for dashboard
  read paths. Optionally: have the engine subscribe to
  `control:killswitch_changed` and push-invalidate. [F-05]
- **Correct the gamma maker-vs-taker fee bug**: either rename
  `markets.fee_rate_pct` to `markets.maker_fee_rate_pct` and use it only when
  acting as maker, or pull the taker fee directly from CLOB
  `getClobMarketInfo` into `fee_snapshots` (and start actually populating
  that table — see Phase 1). [R-12, MR-05]
- **Fix `liquidity_score` source agent**: wire Falcon agent 575 in
  `falcon_client.py` and replace the agent-574 `liquidity` field write at
  `leader_registry.py:348`. Drop the 24 h staleness gate to 1 h for active
  markets. [MG-3]
- **Add retention policies to the 8 unbounded tables**: APScheduler jobs that
  DELETE rows older than `RETENTION_*_DAYS` for `decision_log` (90 d),
  `book_quality_snapshots` (30 d), `portfolio_equity` (180 d),
  `decision_state_transitions` (90 d), `live_orders` (180 d),
  `system_control_audit` (365 d), `risk_config_history` (365 d). [R-6,
  M-15 retention]
- **Fix `decision_log.outcome` UPDATE race**: scope the UPDATE to a
  `paper_trades.id`-based lookup, not `ORDER BY time DESC LIMIT 1`. [R-9]

Validation: see Section 5.

### Phase 1 — Refresh ceiling (weeks 2–3). The user's stated "data acquisition refreshing" goal is met at the end of this phase.

- **Drop global `_backfill_market_activity` poll from 30 s to 5 s** (single
  config change after a HEAD probe confirms data-api accepts the rate).
  [HP-1 #1, MG-2]
- **Parallelize `_backfill_wallet_trades`** with
  `asyncio.gather(...)` + `asyncio.Semaphore(20)`. Move from 200-serial
  worst-case 26 min to ~80 s. [HP-1 #2, MR-12]
- **`FalconClient.Semaphore(1)` → `Semaphore(8)`**, keep the RPM limiter.
  [HP-2 #1, MR-15]
- **Don't hold DB connections across Falcon HTTP calls** in
  `enrich_leaders` and `sync_markets`. Refactor to: fetch wallet list,
  release; do HTTP; re-acquire briefly to UPDATE. [F-07, HP-2]
- **Micro-batch `trades_observed` writes** via a bounded
  `asyncio.Queue(maxsize=10_000)` + `TradeWriter._flush` every 100 ms or
  200 rows. Decouple WS+REST loops from DB writes. [HP-1 #3, MR-13, MR-18]
- **Collapse the read-after-write on `markets`** in `_process_trade` via
  `INSERT … RETURNING …`. Saves ~25% of `_process_trade` wall time. [HP-1
  #4, MR-13]
- **Delete the `_trade_exists` DB re-check** in dedup hit path; trust the
  UNIQUE INDEX. [F-14, HP-1 #5]
- **Pipeline `confidence_engine.precompute_redis_cache`** SETs. [F-06,
  MR-16]
- **Pipeline `_record_book_metrics` Redis writes** (INCRBY+EXPIRE in one
  pipeline; same for the 4-write sequence). [F-29, F-30, MR-50]
- **Bump API DB pool from 2/10 to 5/30**; route through `settings.DB_POOL_*`
  for consistency. [F-17, F-38, HP-4 #1, MR-20]
- **Replace `open_positions_with_prices` N+1** with a single HGETALL +
  single SQL LATERAL. [HP-4 #5, F-37, MR-22]
- **`runtime_config.set_overrides`: do Redis I/O outside the asyncio lock.**
  Take lock only to mutate cache. [F-09, MR-17]
- **`api/queries.wallet_markets`: parameterize the INTERVAL.** Pass
  `window_days` as `$N`, not f-string. [F-16, MR-49]
- **Add Prometheus histograms** for `trade_observer_lag_s`,
  `falcon_request_seconds`, `scheduler_job_duration_seconds`,
  `terminal_snapshot_build_seconds`, `db_pool_acquire_seconds`. Without
  these you cannot validate the rest of this phase landed. [HP-3 #4, MR-19,
  perf observability gaps]
- **Add `asyncio.wait_for(..., timeout=...)` to every `_safe_run`** in the
  APScheduler runner. `nightly_batch` cap = 3600 s; intervals = 60 s.
  [HP-3 #2]
- **Diff-based `refresh_leaderboard`**: only upsert wallets whose
  falcon_score actually changed. [HP-2 #3]

End-state target: trade-to-react p50 ≈ 3 s, p99 ≈ 5–7 s (today: ~16 s p50,
~32 s p99 [HP-1]). Registry cycle 5–8 min (today: 10–20 min).

### Phase 2 — Schema evolution and architectural cleanup (weeks 4–7).

- **Partition `trades_observed` by `time` (monthly)** via declarative PG
  partitioning. Atomic swap, pre-create next 3 months. Nightly DELETE
  becomes DROP PARTITION. [M-11 architect §4.1, MR-37]
- **BRIN index on `trades_observed.time`** in each partition; drop the
  BTREE on `(time)`. ~100× smaller, equivalent range-scan perf. [M-12
  architect §4.2]
- **Partition `book_quality_snapshots` by `observed_at`**; add 30-day
  retention drop-old job. [M-15 architect §2.11, MR-38]
- **Add missing partial indexes** (CREATE INDEX CONCURRENTLY — requires
  runner support):
  - `paper_trades(opened_at) WHERE economic_model_version='v1.0.0' AND invalidated_at IS NULL` [architect §2.7, MR-40]
  - same for `decision_log` [architect §2.8]
  - `follower_edges(follow_probability DESC, co_occurrences DESC) WHERE follow_probability > 0.6 AND co_occurrences >= 5` [architect §2.4]
- **Add CHECK constraints + missing FKs**: enum CHECKs on
  `trades_observed.side`/`source`, `paper_trades.status`/`direction`,
  `live_trades.status`, `live_orders.order_state`,
  `decision_log.action`/`outcome`; `signal_audits.decision_id` FK to
  `decision_log`; `fee_snapshots.market_id` FK to `markets ON DELETE CASCADE`;
  `(wallet, market, token, open_time)` UNIQUE on
  `positions_reconstructed`. [architect §2 + M-13, MR-39]
- **Materialized views for dashboard hot queries**:
  - `mv_alpha_timeline_2h` (refresh every 2 min) [architect §4.3]
  - `mv_leader_followers` (refresh every 30 s) [architect §4.8 C]
  - `mv_market_scanner_stats` (refresh every 30 s) [architect §4.3]
- **Migrate `setup_db.py` to support `CONCURRENTLY`** sections (detect
  `-- ASYNC` directive, run in autocommit). [M-19 architect §3]
- **Add DOWN scripts** for all M11+ migrations. [architect §3]
- **Dedicated Redis pub/sub clients per subscriber** with reconnect +
  resubscribe loops; the engine container's 6 subscribers each get their own
  `Redis.from_url(...)`. [F-04, F-13, F-26, F-31, MR-09]
- **Persist `PositionTracker` state** on restart: warm-start
  `_open_positions` from `positions_reconstructed WHERE close_time IS NULL`
  joined to the FIFO unwinding from `trades_observed`. Bound the dict.
  [R-5, MR-27]
- **Reconcile orphan `live_trades` with status='pending'`** on restart —
  if a `live_orders` row is `filled`, promote to `open`. [F-19, MR-28]
- **`EFFECTIVE_THRESHOLDS` refresh in API container**: call
  `refresh_effective_thresholds` from the FastAPI lifespan. [R-18, MR-29]
- **Push-driven dashboard**: replace the 1-s `_stats_push_loop` that
  rebuilds the snapshot unconditionally with Redis-pub/sub-driven diff
  frames; full snapshot only on reconnect. [HP-4 #2, MR-21]
- **CUSUM single source of truth**: drop the in-memory dict; use
  `profile_json.error_model_runtime.cusum_state` directly. [MG-7, MR-34]
- **Thompson cache freshness**: invalidate
  `confidence:leader:{wallet}` on every `record_decision_outcome`. [MG-6,
  MR-33]
- **Error model blob metadata sidecar**: JSONB column with
  `trained_at`, `training_samples`, `training_window`,
  `feature_schema_version`, `hparams`. Refuse to load on schema mismatch.
  [MG section 3.4, MR-36]
- **Collapse `paper_trader` eligibility checks** into one SQL CTE returning
  all booleans. [F-36, MR-46]
- **Class-level `aiohttp.ClientSession`** for Gamma + data-api; opened in
  `start()`, closed in `stop()`. [F-34, MR-47]
- **`falcon_no_data` short-TTL negative cache** (Redis, 7 d) instead of
  hard exclusion; auto-retry after window. [MG section 3.3, MR-35]
- **Clean up dead code**: remove
  `_conn()` in api/main.py [F-27]; delete unregistered
  `refresh_markets` job or register it [R-3]; subscribe to
  `runtime_config:changed` (or document the 30 s cache as the propagation
  contract) [R-7, MR-10]; remove unused `inspect.isawaitable` paths in
  killswitch [F-28]; collapse `_get_recent_leader_market_ids` duplicate
  query [F-20].

### Phase 3 — Evolved form (weeks 8–12).

- **CDC out of `trades_observed`**: enable `wal_level=logical`, create
  `PUBLICATION trades_observed_cdc`, build `src/observer/cdc_relay.py` that
  consumes the slot and writes to Redis Streams. Migrate profiler, graph
  engine, position_tracker, ws_bridge from `pubsub publish` to `XREADGROUP`
  consumer groups. Replayable, durable, backpressure visible. [M-16
  architect §4.6, MR-45]
- **Feature store with point-in-time correctness**: `market_liquidity_history`,
  `market_volatility_history`, `leader_state_snapshot`, `edge_state_snapshot`.
  Rewrite `error_model._fetch_training_data` to do as-of joins against the
  history tables, eliminating the leakage at MG section 3.1. [MG §4.1,
  MR-24]
- **`markets_history` SCD-2 table** with trigger on `markets` UPDATE.
  Economic model becomes reproducible. [M-17 architect §4.8 B, MR-43]
- **Order-book imbalance per-minute rollup**: `book_imbalance_minute` table
  fed from `book_quality_snapshots`; columns for `bid_depth_5`, `ask_depth_5`,
  `imbalance`, `spread_bps`, `microprice`. Add 3 features to error model.
  [MG §5 #1, MR-44]
- **Bivariate Hawkes**: switch `hawkes_fitter.fit_edge` to use leader
  timestamps as the exciting input series; compute the true cross-excitation
  parameter `α_FL / μ_F`. Either adopt `tick.hawkes` or write a custom
  log-likelihood. Stop-gap: Granger-style cross-correlation strength via
  permutation. [MG-5, HP-5 #1, MR-23]
- **Vectorize Hawkes likelihood inner loop**; run batch in
  `ProcessPoolExecutor`. [HP-5 #1, HP-5 #3, HP-3 #1]
- **Per-wallet authenticated CLOB WS user-channel** for the top 50 leaders;
  drops attribution lag to <1 s for those. [MG-2, HP-1 #7 shard]
- **Class-imbalance + proper calibration in Phase 3 LightGBM**:
  `class_weight='balanced'` or `scale_pos_weight=neg/pos`; stratified 80/20
  split; `CalibratedClassifierCV` on held-out 20%, not on training data.
  [MG section 3.2, MR-25, MR-26]
- **KDE timing density**: per-leader rolling 24-bin histogram of
  `(hour, dow)`; expose `time_anomaly_score` as a feature. [MG-1, MR-30]
- **`trapped_rate` population**: hook in `_close_position` that counts
  followers still in same (market, token) at leader close; persist to
  `follower_edges`. Add to FADE confidence input. [MG-4, MR-31]
- **Resolution reconciliation job**: nightly, for every market with
  `end_date < NOW()`, look up the resolution outcome via Gamma; close any
  open `positions_reconstructed` rows with
  `close_method='resolution'`. [MG-8, MR-32]
- **Model promotion log table** with Brier-before / Brier-after gating;
  candidate runs in shadow against next 50 closes before promotion. [MG
  §4.5]
- **Sharded Polymarket WS** (2–4 clients, fan-in queue) to eliminate
  head-of-line blocking. [HP-1 #7]
- **Cold-tier export of `trades_observed`**: detach partitions older than
  90 d, dump to Parquet via duckdb, ship to R2 (already wired in
  `src/backups/`), drop partition. Historical training data preserved.
  [M-18 architect §4.4]
- **OpenTelemetry spans on `_process_trade`, `_get_terminal_snapshot`,
  `enrich_leaders`, nightly batch boundaries.** End-to-end trace context
  propagated through pub/sub. [HP-3 #4 advanced]

---

## Section 4 — Risks and anti-goals

1. **Do not adopt TimescaleDB.** Volume targets (10× ≈ 9 M rows/month) are
   well within stock Postgres 15. Native partitioning + BRIN gets us most of
   the way. The `CLAUDE.md` master file explicitly excludes TimescaleDB.
   [architect §5 #1]
2. **Do not shard the database.** Hetzner Helsinki box has 5–10× of vertical
   headroom on a single node before sharding becomes the right answer. A
   wallet-centric query against a sharded `trades_observed` is a
   cross-shard join. [architect §5 #2]
3. **Do not introduce read replicas before materialized views land.**
   Replicas mask the problem at 2× the cost. Mat-views absorb the dashboard
   read load; revisit replicas only if primary still shows lock waits after
   Phase 2. [architect §4.5, §5 #6]
4. **Do not ship the feature store (Phase 3) before retention and
   partitioning land (Phase 2).** A feature store on top of unbounded tables
   will outgrow the box. Order matters.
5. **Do not refactor any hot path before Phase 1's Prometheus histograms
   exist.** You cannot tune what you cannot measure. The temptation to "fix
   the obvious" without measuring is how `_record_book_metrics` ended up
   doing a DB insert per WS book event. [perf observability gaps]
6. **Do not migrate to alembic.** Current forward-only `IF NOT EXISTS`
   pattern is robust. Add DOWN scripts as plain `XXX_*_down.sql` siblings.
   [architect §5 #7]
7. **Do not build a generic `events` superclass table.** The current
   per-context tables (trades_observed, decision_log, paper_trades,
   live_trades) are correctly bounded. A polymorphic super-table destroys
   indexability. [architect §5 #4]
8. **Do not start auto-creating indexes on JSONB blobs.** GIN on
   `profile_json` etc. would balloon write amplification. Index only the
   JSONB paths the dashboard actually queries — today, none. [architect §5
   #5]
9. **Do not commit live trading until F-19 and MR-28 (orphan pending
   live_trades reconciliation) are fixed.** Today a crash mid-place leaves
   live_trades in `status='pending'` forever and the row is invisible to
   reload. [F-19]
10. **Do not promote a new error-model phase (1→2 or 2→3) just because the
    sample count crossed the threshold.** Add the Brier-improvement gate
    (Phase 3, MG §4.5). Today the system can ship an overfit LightGBM on 150
    samples with no quality gate. [MG §4.5]

---

## Section 5 — Validation plan

Phase-by-phase concrete metrics. Each metric must be measurable from
Prometheus histograms (added in Phase 1) or from direct DB queries against
new audit tables.

### Phase 0
- **Zero crash-corrupted rows.** Inject a synthetic crash between INSERT and
  UPDATE in `_process_trade`/`paper_trader.open_trade`/`_close_position` in
  a test harness; assert the partial-commit case is caught. Pre-fix: each
  injection leaves a torn row. Post-fix: zero torn rows.
- **Killswitch propagation.** Measure time from `POST /api/control/killswitch`
  to first live-trade gate rejecting the next attempt. Pre-fix: ~2 s
  worst-case (cache TTL). Post-fix: <50 ms (direct DB read on gate).
- **Fee accounting parity.** Take 100 historical paper_trades; recompute
  fees with both the maker-as-taker bug and the corrected taker source;
  expect the corrected number to differ by ≥0.1 % of notional on every
  trade. Document the delta before backfilling.
- **`liquidity_score` source.** `SELECT COUNT(*) FROM markets WHERE
  liquidity_score IS NOT NULL AND updated_at > NOW() - INTERVAL '1 hour'`
  should be > 50 % of active markets within 1 h of the fix. Pre-fix: ~0 %
  (24 h gate).
- **Retention job rowcount drop.** For each of the 8 tables, schedule the
  new retention job, run it once, assert the table is at the expected
  steady-state size (within 10 %).

### Phase 1
- **p99 leader-trade-to-decision latency < 5 s** (today: ~32 s).
  Measured via `trade_observer_lag_s` histogram, end-to-end from
  `data-api.trades.ts` to `decision_log.time`.
- **`falcon_request_seconds` p99 unchanged or improved** despite increased
  concurrency. Sanity check that `Semaphore(8)` doesn't blow up the RPM
  limiter.
- **Registry cycle wall-time < 8 min** (today: 10–20 min). Measured via
  `scheduler_job_duration_seconds{job="registry.run"}`.
- **API pool acquire p99 < 50 ms** at peak (today: pool saturates at 10
  during 17-way fan-out). Measured via `db_pool_acquire_seconds`.
- **Dashboard snapshot build p99 < 200 ms** (today: 100–300 p50, 800 p99).
  Measured via `terminal_snapshot_build_seconds`.
- **Zero N+1 patterns in open-positions monitoring.** Trace shows one Redis
  HGETALL per snapshot, not N GETs.
- **No backpressure-driven OOM under 10× synthetic load.** Run a 10-min
  burst of 100 trades/s into the WS layer; observer process RSS stays
  bounded, `ws_messages_dropped_total` accounts for any drop.

### Phase 2
- **Nightly DELETE on `trades_observed` < 1 s** (today: minutes; at 10×,
  hours). Replaced by `DROP PARTITION`. Measured via APScheduler job
  duration.
- **Dashboard read-path index-only scans.** `EXPLAIN ANALYZE` on the top 5
  snapshot queries shows index-only scans on the new mat-views.
- **Pub/sub message-drop rate 0** during a Redis blip. Inject a 30 s Redis
  outage; assert all subscribers reconnect and resubscribe within 5 s,
  zero permanent message loss. Measured via consumer-side counter.
- **Position state survives restart.** Kill the engine mid-cycle with 5 open
  positions; on restart, `PositionTracker._open_positions` rehydrates all
  5; subsequent SELLs match the warm-started state.
- **Live-trade reconciliation.** Inject crash between OrderManager `place`
  and `live_trades` open-update; on restart, the orphan promotes to `open`
  within 10 s if `live_orders` shows fill.

### Phase 3
- **Online/offline feature parity test passes** for 100 closed positions:
  L∞ distance between online `_build_features` and offline reconstruction
  < 0.05 per feature. [MG §4.4]
- **Bivariate Hawkes `α_FL / μ_F` correlates with paper-trade FOLLOW
  PnL** at >0.3 Spearman on the next 200 closes — quantifies that the
  fix actually captures causality, not coincidence.
- **Phase 3 error model Brier on held-out 30 d < Phase 2 Brier**.
  Measured by the new `model_eval` table.
- **CDC consumer lag p99 < 1 s.** Measured at the Redis Streams XADD vs
  XREADGROUP timestamps.
- **Cold-tier export round-trips correctly.** Detach a 90-d-old partition,
  export to R2, drop, re-import to a staging DB, assert row-for-row
  equality.

---

## Section 6 — Open questions for the owner

Decisions that cannot be made from the code; needed before Phase 0/1
commitments are sized.

1. **Killswitch latency budget.** Is sub-second propagation required for the
   live-trade gate, or is 5 s acceptable? Sub-second forces the DB-on-gate
   read; 5 s allows a pub/sub-push model. The current 2 s window is the
   worst of both. [MR-01]
2. **Live trading timeline.** Is live trading expected to ship in 2026 H2
   or is paper still the production mode through year-end? This determines
   whether F-19 / MR-28 (orphan live_trades reconciliation) is Phase-2
   blocker or Phase-3 nice-to-have. The CLAUDE.md `PAPER_TRADING=true`
   default suggests deferred.
3. **Fee correction backfill scope.** Once the maker-vs-taker fee bug is
   fixed [MR-05], do we (a) recompute fees for all historical
   `paper_trades`, (b) leave history alone and tag with
   `economic_model_version='v1.1.0'`, or (c) mark the affected rows
   `invalidated_at`? The economic-spine machinery in migration 003 supports
   all three.
4. **Bivariate Hawkes urgency.** Is the Hawkes correctness fix a Phase-3
   model rewrite (full bivariate via `tick.hawkes`) or a Phase-1 hotfix
   (Granger-style stop-gap)? The stop-gap is M-effort and lifts the false-
   positive rate noticeably; the full fix is L-effort. [MG-5, MR-23]
5. **`falcon_no_data` policy.** Today it's a hard permanent exclusion. Is
   the desired behavior: (a) auto-retry every 7 d, (b) auto-retry every
   30 d, or (c) keep hard exclusion and surface a Telegram alert for manual
   re-enable? Affects new-wallet discovery rate. [MG section 3.3, MR-35]
6. **Tolerable trade-attribution lag.** Phase 1 brings p99 to ~5 s globally.
   The per-wallet WS user-channel work (Phase 3) takes the top-50 leaders
   to <1 s but costs WS auth per wallet. Is 5 s globally good enough, or is
   the sub-second tier specifically needed for top wallets? Drives the
   Phase 3 investment. [MG-2, MR-11]
7. **Data-api rate-limit headroom.** Phase 1 #1 drops the global market
   sweep from 30 s to 5 s. Does Polymarket data-api accept this? A HEAD
   probe + a polite email to their team would be cheaper than discovering
   the cap by being rate-limited. [HP-1 #1]
8. **Retention windows.** Are 90 d (`decision_log`), 30 d
   (`book_quality_snapshots`), 180 d (`portfolio_equity`), 90 d
   (`decision_state_transitions`), 180 d (`live_orders`), 365 d
   (`system_control_audit`, `risk_config_history`) acceptable? Or does
   compliance/research require longer? [MR-07]
9. **Cold-tier archive policy.** Phase 3 §4.4 archives 90 d+ partitions to
   R2. Acceptable, or does the leader-intelligence training story need
   180+ day windows live in the warm DB? Hawkes uses 30 d; LightGBM phase 3
   uses "all resolved", which lives in `positions_reconstructed`, not
   `trades_observed`. Confirm.
10. **Promotion gate strictness.** The Brier-improvement gate for phase
    1→2 and 2→3 (MG §4.5) blocks promotion if the candidate doesn't beat
    incumbent by ≥0.01. Is 0.01 too aggressive (too conservative) for cold-
    start wallets? [MG §4.5]
11. **OpenTelemetry stack choice.** Phase 3 #25 proposes OTLP spans. Is
    there a preferred backend (Jaeger, Tempo, Honeycomb), or is this a
    greenfield decision? Affects tooling cost.
12. **Anti-bot exclusion threshold.** Today `excluded=TRUE` is set when
    Falcon returns no data. Should we also auto-exclude wallets whose
    measured execution speed is <1 s consistently (the structural/bot
    detection from `CLAUDE.md` §3)? No code path does this today; it would
    be a small profiler addition. Flag if desired.

---

*End of master report.*
