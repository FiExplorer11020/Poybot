# 03 — Schema Audit & Evolution Roadmap

> Polymarket Leader Intelligence Bot — PostgreSQL 15, asyncpg, no TimescaleDB.
> Source migrations: `docs/migrations/001_schema.sql` … `010_risk_config_history.sql`.
> Domain context: `polymarket-bot/CLAUDE.md`, `src/observer/CLAUDE.md`.
> Hot SQL surface: `src/api/queries.py` (~3.6 kLOC, single canonical builder).
>
> Scope: shape of the data only. Code-side issues are out of scope (sister doc handles those).

---

## 1. Schema overview — bounded contexts

The 22 tables created across the 10 migrations cluster cleanly into seven bounded
contexts. Two tables straddle contexts in mildly unhealthy ways (called out
below).

| Context | Tables | Source-of-truth role |
|---|---|---|
| **Registry** (leaders + markets metadata) | `leaders`, `markets`, `fee_snapshots` | Slowly-changing dimensions; pulled from Falcon + Gamma. |
| **Observer** (raw event log) | `trades_observed`, `positions_reconstructed` | Append-only event tables fed by WS + REST polling. |
| **Graph** (social network) | `follower_edges` | Per-pair posterior state; updated O(1) per leader trade. |
| **Profiler** (behavior + error models) | `leader_profiles`, `v1_label_invalidations` | One row per leader; JSONB + serialized model blob. |
| **Engine** (decisions + trading) | `paper_trades`, `live_trades`, `live_orders`, `decision_log`, `signal_audits`, `portfolio_state`, `portfolio_equity` | Outputs of the decision pipeline. |
| **Control** (operator surfaces) | `system_control`, `system_control_audit`, `risk_config_history` | Singleton flags + audit logs. |
| **Monitoring / Neural** (market regime) | `market_belief_states`, `decision_state_transitions`, `book_quality_snapshots` | V1 readiness machine + book-quality time-series. |
| **Infra** | `schema_migrations` | Migration tracking. |

### Tables straddling contexts

- **`fee_snapshots`** lives in migration 003 (engine/economic spine) but its
  natural home is **registry** — it captures market-level fee state, not per-trade
  state. Today the engine reads it via `signal_audits.fee_snapshot_id`. Risk:
  pruning markets later (the V1 plan) leaves `fee_snapshots` orphaned with
  thousands of stale rows. Action: keep it in registry, add `market_id` FK with
  `ON DELETE CASCADE`, and prune cold rows on the registry's clock, not the
  engine's.
- **`v1_label_invalidations`** is technically profiler/engine cross-cutting:
  it logs invalidation events that cascade to `paper_trades`,
  `decision_log`, `leader_profiles`, `positions_reconstructed`. The `target_table
  + target_id` polymorphic FK pattern is brittle (no DB-enforced referential
  integrity). Today it works because the writers are well-behaved, but at 10x
  scale a polymorphic side-table is a known pain point. Action: split into
  per-target tables OR add a CHECK on `target_table` value range.

The remaining tables are correctly bounded.

---

## 2. Per-table audit

Trade-rate baseline used below: today the bot tracks `TOP_MARKETS_COUNT=50`
markets and ~200–2000 leaders. The README/CLAUDE notes ~71k historical rows on
the table at the time migration 007 was authored, plus a stated retention of
`RETENTION_TRADES_DAYS=90`. With WS + REST polling and 50 markets, observed
volume is on the order of **5k–30k trades/day** (current). The 10x target is
**50k–300k trades/day** = 1.5M–9M rows/month. All growth estimates below assume
mid-band of the 10x scenario unless noted.

### 2.1 `leaders`  (registry)
- **Purpose / SoT**: source of truth for the watchlist. PK = `wallet_address`.
- **Growth**: ~2k rows steady-state (`MAX_LEADER_COUNT=2000`). Slow churn.
- **Indexes present**: PK only.
- **Indexes missing**:
  - `(on_watchlist, excluded)` — every dashboard query filters
    `WHERE on_watchlist=TRUE AND excluded=FALSE` (`queries.py:520, 1385,
    1399, 1999`). Today this is a seq scan on 2k rows so it's tolerable, but a
    partial index `WHERE on_watchlist=TRUE AND excluded=FALSE` is essentially
    free.
  - `(last_refresh)` — `data_quality()` filters by `last_refresh < NOW() - 24h`.
    Negligible at 2k rows; flag for completeness only.
- **Constraints**: `excluded` and `on_watchlist` should be `NOT NULL` (the
  defaults make it so but the column allows NULL). `exclude_reason` should have
  a CHECK against an enum (`bot`, `falcon_no_data`, `low_score`,
  `manual`).
- **Partitioning**: not applicable (small table).

### 2.2 `trades_observed`  (observer — the hot table)
- **Purpose / SoT**: append-only event log of every observed trade.
- **Growth**: at 10x, ~3M–9M rows/month. Even at current cadence it is the
  dominant table by row count and write QPS.
- **Indexes present**:
  - `idx_trades_wallet_time (wallet_address, time)`
  - `idx_trades_market_time (market_id, time)`
  - `idx_trades_time (time)`
  - `idx_trades_leader (is_leader) WHERE is_leader=TRUE` (partial)
  - `idx_trades_leader_wallet (wallet_address) WHERE is_leader=TRUE` (002)
  - `uq_trades_observed_natural_key UNIQUE (wallet, market, time, side, price, size_usdc)` (007)
  - `idx_trades_wallet_category_time (wallet, category, time) WHERE is_leader=TRUE` (009)
- **Indexes missing / questionable**:
  - **`source`** has no index. `inspector_snapshot()` (`queries.py:2620`) does
    `GROUP BY source WHERE time > NOW()-5min`. With 5min slices and the
    `time` index this is fine; not urgent.
  - The `time DESC`-ordered scans (`recent_observed_trades`, `inspector_snapshot`,
    `overview.last_trade`) all benefit from `time DESC` BRIN once partitioned —
    today the BTREE on `(time)` works but uses a lot more pages than necessary.
  - **`idx_trades_time` is redundant** with the leading column of
    `idx_trades_wallet_time` for plain `WHERE time` predicates only when
    `wallet_address` filter is also present. The bare `WHERE time > NOW() - X`
    queries (10+ in queries.py) need `(time)` — keep it, but make it BRIN
    after partitioning (see §4.1).
- **Constraints**:
  - Missing CHECK on `side IN ('buy','sell')` and `source IN ('websocket',
    'api_market','api_wallet')`.
  - **No FK** from `trades_observed.market_id` → `markets.market_id`. This is
    intentional (the observer auto-stubs missing markets — see
    `trade_observer.py:931–939`) and should stay that way for write-path
    independence. Document the invariant.
  - **No FK** to `leaders` either. Also intentional (we observe trades from
    non-leader wallets too via the WS feed). Fine.
- **Hot rows / hot partitions**: extreme append skew on `time`. Last-24h is
  ~95% of read traffic. Cleanup runs `DELETE FROM trades_observed WHERE time
  < cutoff` (`scripts/batch_runner.py:131`) — at 10x scale this becomes a
  multi-hour vacuum-generating monster. **Top candidate for time-range
  partitioning** (see §4.1).
- **Write/read pattern**: small, append-heavy, time-clustered. BRIN on
  `(time)` would be ~100x smaller than the BTREE and almost as effective for
  range scans, especially after partitioning.

### 2.3 `positions_reconstructed`
- **Purpose / SoT**: closed-form OPEN→CLOSE cycles.
- **Growth**: ~10–20% of trade volume (most trades are not full cycles
  immediately). At 10x: ~300k–1M rows/month.
- **Indexes present**: `(wallet, open_time)`, `(market, open_time)`,
  `(close_time) WHERE close_time IS NULL` (open positions).
- **Indexes missing**:
  - `(close_time)` non-partial — `inspector_snapshot.counters` queries
    `close_time > NOW() - 1h`. The partial-on-NULL index does not help. Add
    `(close_time)` BRIN (after partitioning) or BTREE.
  - `(category, close_time)` for category drill-downs — covered weakly by 009's
    `trades_observed` denorm, but `positions_reconstructed.category` has no
    index.
- **Constraints**:
  - `direction` should CHECK `IN ('yes','no')`.
  - `close_method` should CHECK `IN ('sell','merge','resolution', NULL)`.
  - Missing **uniqueness** on `(wallet, market, token, open_time)` — a bug in
    position_tracker could insert two OPENs for the same cycle. Today nothing
    enforces single-open-per-(wallet,market,token).
- **Partitioning**: candidate for `PARTITION BY RANGE (open_time)`.

### 2.4 `follower_edges`
- **Purpose / SoT**: Beta posterior + Hawkes coefficient per (leader, follower) pair.
- **Growth**: O(leaders × followers_per_leader). With 2k leaders and ~5k
  followers/leader theoretical, but `MIN_CO_OCCURRENCES=5` filtering brings the
  realistic ceiling to ~50k–500k rows. Slow growth.
- **Indexes present**: `UNIQUE(leader_wallet, follower_wallet)`,
  `idx_edges_leader (leader_wallet)`, `idx_edges_follower (follower_wallet)`.
- **Indexes missing**:
  - `(follow_probability DESC, co_occurrences DESC)` — `overview()` (queries.py:548)
    does `ARRAY_AGG(... ORDER BY follow_probability DESC, co_occurrences DESC,
    last_observed DESC) WHERE follow_probability > 0.6 AND co_occurrences >= 5`.
    Today this is a full scan + sort on every dashboard tick. Add a partial
    index `(follow_probability DESC, co_occurrences DESC) WHERE
    follow_probability > 0.6 AND co_occurrences >= 5` — turns it into an
    index-only scan.
  - `(last_observed)` — used for staleness queries.
- **Hot rows**: most reads target the top-confirmed slice (~hundreds of rows);
  most writes are to the long tail (low-probability candidates).
- **Constraints**: `follow_probability` should CHECK `BETWEEN 0 AND 1`.

### 2.5 `leader_profiles`
- **Purpose / SoT**: one JSONB blob + binary error model per leader.
- **Growth**: ~2k rows, 1:1 with `leaders` (FK present).
- **Indexes present**: PK only.
- **Indexes missing**:
  - `(error_model_phase)` — `alpha_extras.totals` filters by phase. 2k rows so
    minor.
  - `(last_updated DESC)` — staleness queries.
  - **GIN on `profile_json`** if any deep introspection is added (today
    `profiler_health` reads the whole row and unpacks in Python — that's fine).
- **Constraints**: `error_model_phase IN (1,2,3)` CHECK.
- **Hot rows**: every leader is read on every batch refit. Read-mostly.

### 2.6 `markets`
- **Purpose / SoT**: market metadata (Gamma + Falcon-derived).
- **Growth**: ~10k–100k lifetime markets across Polymarket; bot holds
  ~1k–10k. Slow.
- **Indexes present**: PK only.
- **Indexes missing**:
  - `(active)` — every "live markets only" filter is a seq scan.
  - `(end_date)` — `sync_markets` and `data_quality` filter on this.
  - `(category)` — needed for category aggregations now that the denorm in 009
    is in place.
- **Constraints**: missing FK targets from `fee_snapshots` (see §1).

### 2.7 `paper_trades`
- **Purpose / SoT**: virtual portfolio ledger.
- **Growth**: bounded by signal frequency; at current cadence ~10–50/day. At 10x
  scale ~100–500/day.
- **Indexes present**: `idx_paper_open WHERE status='open'`, `idx_paper_market_open`
  (002), `idx_paper_opened_date`.
- **Indexes missing**:
  - **`(economic_model_version, invalidated_at)`** — every dashboard query
    appends `WHERE economic_model_version = $V AND invalidated_at IS NULL`
    (`V1_PAPER_TRADE_SQL`, `economics/versioning.py:8`). Today this filter is
    not selectable via index; the planner falls back to full scans on a table
    that gets larger every day. Add partial:
    `(opened_at) WHERE economic_model_version='v1.0.0' AND invalidated_at IS NULL`.
  - `(closed_at, status)` for daily-PnL queries (`SELECT DATE(closed_at), SUM(pnl)
    WHERE status='closed'`).
- **Constraints**: `status` enum CHECK; `direction` enum CHECK; `strategy`
  enum CHECK.

### 2.8 `decision_log`
- **Purpose / SoT**: every decision the engine makes (follow / fade / skip).
- **Growth**: highest-rate engine table. One row per leader-trade ingestion that
  passes the gate, ~1× to 5× the leader-trade rate. At 10x scale: ~50k–250k
  rows/month.
- **Indexes present**: `(time)`, `(leader_wallet)`, `(leader_wallet, market_id)
  WHERE outcome IS NULL`.
- **Indexes missing**:
  - **`(economic_model_version, invalidated_at, time)` partial** — same pattern
    as paper_trades. Every read filters this.
  - `(action)` for action-bucket aggregations (low cardinality, BRIN OK).
- **Constraints**: `action IN ('follow','fade','skip')`, `outcome IN
  ('win','loss',NULL)` CHECKs.
- **Partitioning**: candidate for `PARTITION BY RANGE (time)` once volume
  hits ~1M rows.

### 2.9 `live_trades` / `live_orders`  (008)
- **Purpose / SoT**: real-money mirror of paper_trades + per-CLOB-order audit.
- **Growth**: today minimal (still in shadow). Production target: same
  cadence as paper_trades + ~3–5 CLOB orders per trade due to
  cancel/reprice loops.
- **Indexes present**: status (partial), market_id, leader_wallet, clob_order_id
  (partial); orders: live_trade_id, order_state, clob_order_id (partial).
- **Indexes missing**:
  - `live_trades.opened_at DESC` — no time index, but every "recent live
    trades" listing will need it. Mirror `idx_paper_opened_date`.
  - `live_trades.tx_hash` — for forensics lookup by Polygon tx.
  - `live_orders.placed_at` — for time-range audits.
- **Constraints**: enum CHECKs on `status` and `order_state` are documented in
  comments but not enforced in DB. Convert to CHECK constraints.
- **Naming inconsistency**: `paper_trades.id` is `SERIAL`, `live_trades.id` is
  also `SERIAL` — fine. But `live_orders.id BIGSERIAL` mismatches its parent
  `live_trade_id INTEGER`. Cosmetic, but at high write rates `paper_trades.id`
  and `live_trades.id` should both be `BIGSERIAL` for safety.

### 2.10 `portfolio_equity`  (004)
- **Purpose / SoT**: equity time-series (closed PnL + mark-to-market).
- **Growth**: written every paper close + periodic monitor tick. Estimate
  1–10 rows/min ≈ 1.5k–15k/day → at 10x ~150k/month.
- **Indexes**: PK = `(time)`, plus `portfolio_equity_time_idx (time DESC)`. The
  PK already serves DESC scans; the second index is redundant.
- **Constraints**: PK on `time` alone means a tick collision (two writers same
  microsecond) raises a duplicate-key error. Consider `(time, source)` PK or
  add a tiebreaker `id BIGSERIAL`.
- **Partitioning**: candidate, but only after multiple months of data.

### 2.11 `market_belief_states` / `decision_state_transitions` / `book_quality_snapshots`  (005)
- **Purpose / SoT**: V1 Neural Readiness state machine + per-token book quality.
- **Growth**:
  - `market_belief_states`: one row per `(market, strategy_track)` — bounded.
  - `decision_state_transitions`: append-only, one row per state flip. Low.
  - `book_quality_snapshots`: written every WS book event at 50 markets × 2
    tokens. **At 1Hz this is 50–500 rows/min = 70k–700k/day**. This is
    **second only to `trades_observed`** by row count and is currently
    unbounded — there is no retention policy.
- **Indexes**: `(observed_at DESC)`, `(market, token, observed_at DESC)`. Good for
  reads, fine for writes.
- **Indexes missing**:
  - For the `latest_books` query in `market_scanner_rows` (queries.py:1668), the
    `DISTINCT ON (market_id, token_id) ... ORDER BY market_id, token_id,
    observed_at DESC` pattern is the textbook case for an index on
    `(market_id, token_id, observed_at DESC)` — already present. Good.
- **Constraints**: missing retention. **Add a retention job** (mirror
  `RETENTION_TRADES_DAYS`) or convert to a partitioned table with drop-old.
- **Partitioning**: top-3 candidate alongside `trades_observed`.

### 2.12 `signal_audits` / `fee_snapshots`  (003)
- **Purpose / SoT**: per-decision audit + per-market fee snapshot.
- **Growth**: `signal_audits` ≈ rate of decision_log; `fee_snapshots` ≈ ~1
  per active market per day.
- **Indexes**: only PK + `UNIQUE(market, token, captured_at, source)` on
  fee_snapshots. **`signal_audits` has zero indexes** beyond PK and the FK.
- **Indexes missing**:
  - `signal_audits.created_at` — for "recent rejections" queries.
  - `signal_audits.market_id, created_at DESC` — used by
    `decision_rejections_breakdown`.
  - `signal_audits.decision_id` — already covered by FK? No, FKs do not auto-create
    indexes in Postgres. **Add it.** This will bite at scale.
  - `fee_snapshots.market_id` — for current-fee lookups.
- **Constraints**: `signal_audits.decision_id` should reference
  `decision_log(id)` (today it's a `BIGINT` with no FK).

### 2.13 Control + small tables
- `system_control` (singleton, PK=1 with CHECK), `system_control_audit`,
  `risk_config_history`: properly indexed by `changed_at DESC` and `key`.
  Nothing to do.
- `v1_label_invalidations`: no indexes on `target_table, target_id`. If we ever
  query "find all invalidations for paper_trade=X" we'll seq-scan. Add
  `(target_table, target_id)`.
- `schema_migrations`: trivial.

---

## 3. Migration cohesion review

### Schema drift
- **`trades_observed.category`** (migration 009) is a denorm column written
  by the observer at insert time. Backfill in 009 covered existing rows. Code
  side reads it via the `idx_trades_wallet_category_time` partial index. Healthy.
- **`paper_trades.size_shares`, `entry_fee_usdc`, `exit_fee_usdc`,
  `spread_cost_usdc`, `slippage_usdc`, `gross_pnl_usdc`, `net_pnl_usdc`** added in
  003. A grep shows `net_pnl_usdc` and `gross_pnl_usdc` are read by
  `paper_trader.py` — used. `spread_cost_usdc` and `slippage_usdc` are
  populated from `fill_audit` JSONB but rarely consumed by the dashboard.
  **Verify with the SQL agent**: are `slippage_usdc` and `spread_cost_usdc`
  values written or always NULL?
- **`positions_reconstructed.size_shares` / fee columns** — same pattern. Read
  by the engine but not yet surfaced in any dashboard. Acceptable.
- **`leader_profiles.learning_invalidated_at` / `_reason` /
  `economic_model_version`** — added in 003. `V1_PROFILE_P_SQL` filters on
  `economic_model_version = $V AND learning_invalidated_at IS NULL`. Used.

### Backfill correctness
- **009 backfill is correct** — `UPDATE … SET category = m.category WHERE
  t.category IS NULL AND m.category IS NOT NULL`. Idempotent on re-run. Good.
- **003 has no backfill** for the new columns on existing
  `paper_trades`/`decision_log` rows. They stay NULL on
  `economic_model_version`, which means `V1_*_SQL` filters silently exclude
  pre-v1 rows. This is by design (pre-v1 economics aren't comparable), but it
  should be documented in the migration header.
- **006 seeds the singleton row** correctly via INSERT … ON CONFLICT DO NOTHING.

### Reversibility / DOWN scripts
- **No DOWN scripts exist for any of the 10 migrations.** `setup_db.py` is
  forward-only. This is a known gap. For a green-field bot it's tolerable, but
  for production schema changes from M11 onward, every migration should ship
  with a `XXX_*_down.sql` and the runner should support it (even if only via
  manual invocation).

### Index-creation locking
- **All `CREATE INDEX` statements in the migrations are non-CONCURRENTLY.**
  `setup_db.py` wraps each migration file in a single `conn.execute(sql)`,
  which means transactional execution — `CONCURRENTLY` is forbidden inside
  transactions anyway. At 71k rows (when 007 was authored) this finished in <1s.
  At 10x scale (~10M rows) any new index on `trades_observed` will lock writes
  for **minutes**.
- **Action**: M11+ must support CONCURRENTLY. This requires the runner to
  detect statements outside `BEGIN/COMMIT` and run them in autocommit mode, or
  split each migration into "txn" and "concurrent" sections.

### Migration naming / ordering / idempotency
- Naming is consistent (`NNN_short_name.sql`). Ordering is lexicographic and
  works for 3-digit prefixes up to 999.
- **Idempotency is excellent.** Every migration uses `CREATE TABLE IF NOT
  EXISTS`, `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `INSERT …
  ON CONFLICT DO NOTHING`, plus a wrapping `BEGIN/COMMIT`. Re-running any
  migration is a no-op.
- **Inconsistency**: only migration 008 explicitly `INSERT INTO
  schema_migrations (version) VALUES (8)`. The runner already does this in
  `setup_db.py:53` after each file applies. The 008 self-insert is harmless
  (DO NOTHING) but redundant — remove from M11 onward to keep migrations
  pure-DDL.
- **No migration 002 starts with `BEGIN/COMMIT`.** It applies five
  `CREATE INDEX IF NOT EXISTS` statements outside an explicit transaction. The
  runner wraps them implicitly. Cosmetic but worth aligning.

---

## 4. Evolution roadmap — supporting 10x acquisition

The architectural arc: today every consumer (profiler, graph, dashboard) reads
`trades_observed` directly. At 10x, that single table becomes the bottleneck for
both writes (idempotent UPSERTs across 6-column natural key) and reads (every
dashboard tick scans `WHERE time > NOW() - X`). The roadmap converts the table
from "polled hot ledger" into "partitioned event log with downstream
materializations".

### 4.1 Partition `trades_observed` by `time`  (highest ROI)

**Problem**:
- `scripts/batch_runner.py:131` does `DELETE FROM trades_observed WHERE time <
  cutoff` — at 10x, this deletes ~600k–3M rows per nightly run. Generates dead
  tuples → vacuum churn → bloat.
- Every dashboard query filters `WHERE time > NOW() - X` (10+ usages in
  `queries.py`). Full BTREE traversal grows with table size.
- The 6-column `UNIQUE` index on `(wallet, market, time, side, price,
  size_usdc)` is the largest index by far and grows linearly with row count.

**Change**: convert `trades_observed` to native PG declarative partitioning.

```sql
-- M11 (sketch)
CREATE TABLE trades_observed_new (LIKE trades_observed INCLUDING ALL)
    PARTITION BY RANGE (time);

CREATE TABLE trades_observed_y2026m05 PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
-- ... pre-create next 3 months
-- Use pg_partman or a cron job to roll new partitions monthly.

-- Backfill (one-shot, off-hours):
INSERT INTO trades_observed_new SELECT * FROM trades_observed;

-- Atomic swap:
BEGIN;
ALTER TABLE trades_observed RENAME TO trades_observed_old;
ALTER TABLE trades_observed_new RENAME TO trades_observed;
COMMIT;

-- Verify, then DROP TABLE trades_observed_old after 7 days.
```

Each partition gets its own indexes (the global natural-key UNIQUE becomes a
per-partition UNIQUE — Postgres 15 supports this on partitioned tables as long
as the partition key is included in the unique constraint, so we extend the
natural key to include `time`-truncated hour or accept partition-local
uniqueness; the existing key already includes `time` so this is a no-op).

**Migration sequence**: M11 (UP creates new table + partitions, copies data,
swaps); `M11_down.sql` (renames back, drops new table).
**Rollout risk**: medium. The atomic swap is fast, but the backfill is
not. Mitigation: backfill in chunks (`INSERT … WHERE time BETWEEN A AND B`)
during off-hours, drop the old table after a week of soak.
**Expected gain**: nightly DELETE becomes `DROP PARTITION` (instant, no
vacuum). Range queries skip non-matching partitions entirely. Index size on
hot partition stays bounded at ~1 month of data.

### 4.2 BRIN index on `trades_observed.time`

After partitioning, replace the BTREE on `(time)` inside each partition with a
BRIN. Append-only insertion means BRIN's per-block min/max is highly selective.
~100× smaller, virtually free to maintain.

```sql
DROP INDEX idx_trades_time;
CREATE INDEX idx_trades_time_brin ON trades_observed USING BRIN (time)
    WITH (pages_per_range = 32);
```

(Migration M12 — pairs with M11.)

**Gain**: index footprint drops from ~hundreds of MB to a few MB, with
equivalent range-scan performance for `time`-filtered queries.

### 4.3 Materialized views for dashboard hot queries

The dashboard's `terminal_snapshot` and the 1s-TTL cache fan out to ~15 SQL
queries per tick. Several do non-trivial aggregation that hits the same window
repeatedly.

**Top candidates from `src/api/queries.py`**:

1. **`overview.activity_feed` (line 540)** — the 20-min CTE with
   `follower_map` + `LEFT JOIN markets + leaders + follower_edges`. Fired every
   1s. Runs a triple JOIN on the hottest table. **Mat-view it** with refresh
   every 5–15s.
2. **`alpha_extras.timeline` (line 2098)** — 24h × 12 buckets of 2h, four
   subqueries per bucket. Today this is 4×12=48 index scans per call.
   Materialize as `mv_alpha_timeline_2h` refreshed every 2 minutes; the underlying
   data does not change second-to-second.
3. **`market_scanner_rows.trade_stats` (line 1683)** — counts trades per
   market in 1m/5m/30m windows. Refresh every 30s.
4. **`decisions_stats` (line 1226)** — group-by-action over a 24h window. Refresh
   every minute.

```sql
-- M13 (sketch)
CREATE MATERIALIZED VIEW mv_alpha_timeline_2h AS
SELECT
    date_trunc('hour', time) - (extract(hour FROM time)::int % 2) * INTERVAL '1 hour' AS bucket_start,
    COUNT(*) AS trades,
    COUNT(*) FILTER (WHERE is_leader) AS leader_trades
FROM trades_observed
WHERE time >= NOW() - INTERVAL '24 hours'
GROUP BY 1;

CREATE UNIQUE INDEX ON mv_alpha_timeline_2h (bucket_start);
-- Refresh: SELECT pg_cron schedule or APScheduler job every 2 min.
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_alpha_timeline_2h;
```

**Rollout risk**: low — additive, queries fall back to base tables on view
refresh failure.
**Gain**: dashboard TTFB stable as `trades_observed` grows; aggregation cost
amortized across hundreds of dashboard ticks.

### 4.4 Cold-tier export for `trades_observed`

Today `RETENTION_TRADES_DAYS=90` deletes data permanently. For a leader
intelligence bot, **historical trades are training data** — discarding them is
a cost-driven anti-feature. After partitioning (4.1), cold partitions can be
detached and either:

- **Option A** — kept attached but compressed (Postgres 15 has no native
  compression; would need TimescaleDB hypertable compression — explicitly
  excluded by `CLAUDE.md` line 185 ("standard, NOT TimescaleDB")).
- **Option B (recommended)** — `pg_dump` cold partitions to Parquet via
  duckdb's postgres scanner, push to Cloudflare R2 (already wired in
  `src/backups/`), drop partition. Dashboard never reads >90d data; the
  profiler's nightly Hawkes refit uses 30d (`HAWKES_LOOKBACK_DAYS=30`); the
  error-model phase 3 LightGBM uses "all resolved data" — but resolved data
  lives in `positions_reconstructed`, not `trades_observed`. So cold-tier
  archive is safe.

Defer hypertables/columnar storage. The volume targets here (10x = 9M
rows/month) are well within stock Postgres 15 capabilities. **Anti-goal:
introducing TimescaleDB for this — it's a config explosion the project's
volume does not justify.**

### 4.5 Read replicas — does the dashboard justify one?

**Today**: the dashboard fires ~15 queries per second across all open
sessions, hitting the same primary as the engine writers. The expensive ones
(activity feed, timeline) read `trades_observed` while the observer writes to
it. Lock contention is currently invisible because volume is low.

**Cited endpoints likely to push primary at 10x**:
- `GET /api/snapshot` (terminal_snapshot composes ~15 queries) — fired every 1s.
- `GET /api/alpha/extras` (queries.alpha_extras) — every 30s, includes 12-bucket
  scans of trades_observed.
- `GET /api/inspector/snapshot` — every 5s in inspector tab, full
  trades_observed lookback for 1h counters.

**Recommendation**: **defer the read replica until §4.3 (materialized views) is
in place**. Mat-views absorb 80% of the dashboard read load. If, after that,
the primary still shows lock waits or replication lag, then add a streaming
replica and pin all `api/queries.py` endpoints to it via a separate asyncpg
pool. The change is mechanical — `database/connection.py` exposes one pool;
add a `get_db_replica()` and route the `api/` package through it.

**Anti-goal**: don't build replicas before mat-views — replicas double the
cost and complexity to mask a workload that doesn't yet need it.

### 4.6 CDC / event log — stop polling `trades_observed`

**Problem**: today every downstream of the observer (profiler, graph engine,
position tracker, ws_bridge) **either reads from Redis pub/sub or polls
`trades_observed`**. Redis pub/sub is fire-and-forget — a subscriber down for
1s permanently misses messages. The recovery path is to backfill from
`trades_observed`, which means downstream consumers query the hot table.

**Change**: emit a logical-replication stream from `trades_observed` to a
durable consumer queue. Concretely:

1. Enable `wal_level = logical` and create a `PUBLICATION`:
   ```sql
   ALTER SYSTEM SET wal_level = 'logical';
   -- requires restart
   CREATE PUBLICATION trades_observed_cdc FOR TABLE trades_observed;
   ```
2. A small Python worker (`src/observer/cdc_relay.py`) uses
   `psycopg2.replication` (or `wal2json`) to consume the slot and republish
   to:
   - **Redis Streams** (not pub/sub) — durable, replayable from any offset.
     Consumers (profiler, graph, decision_router) read via XREAD with
     consumer groups. No polling.
3. Decommission the observer's direct `pubsub publish trades:observed` call —
   the CDC relay becomes the single fan-out point.

**Rollout risk**: medium. Requires a Postgres restart for `wal_level`. The
relay is a new always-on service. Mitigation: dual-publish (pubsub +
streams) for two weeks, then cut over.

**Expected gain**:
- Zero polling on `trades_observed` from internal consumers.
- Replay window for any consumer that crashes (set stream MAXLEN to 24h).
- Backpressure visible at the relay, not at the database.

This is the "evolved form" the prompt asks for: the bot's internal architecture
becomes one writer + one log + N readers, not N readers competing for
`trades_observed`.

### 4.7 Denormalization debt cleanup

Migration 009 denormed `category` onto `trades_observed` and
`positions_reconstructed`. Same logic applies to:

- **`is_leader`** — already on `trades_observed`. Good. But the value comes
  from a registry lookup at insert time and never updates if a leader is
  excluded later. **Risk**: a leader marked `excluded=TRUE` post-hoc still has
  thousands of `is_leader=TRUE` rows in trades_observed. Dashboard queries that
  filter `WHERE is_leader=TRUE AND wallet IN (active_leaders)` already
  re-validate; queries that just `WHERE is_leader=TRUE` over-count. Document
  the invariant or add a `leader_status_at_observation VARCHAR` column.
- **`fee_rate_pct` snapshot at trade time** — today
  `paper_trades.fee_paid_usdc` is computed from `markets.fee_rate_pct` at
  signal time. If the market's fee changes (Polymarket changes the schedule),
  historical PnL recomputation re-uses the new fee. **Add `fee_rate_pct_snapshot`
  to `paper_trades` and `live_trades`** to lock in the value at decision time.
  The economics spine in 003 partly addresses this via `fill_audit JSONB` and
  `fee_snapshots`, but the snapshot is referenced by ID, not embedded.
- **`leader_classification_snapshot`** on `decision_log` — today
  `decision_log` stores no leader classification. To audit "this signal
  was generated when the leader was classified as `swing` but they had become
  `scalper` by resolution", we need a frozen JSONB column. Add
  `leader_classification_snapshot JSONB` to `decision_log`.

### 4.8 New tables for new acquisition sources

Three are missing today:

#### A. `order_book_snapshots`  (raw order book history)
The bot has `book_quality_snapshots` (top-of-book metrics) but **no full L2
order book history**. Falcon agent 572 (Polymarket Orderbook) is
referenced but not persisted.

```sql
CREATE TABLE order_book_snapshots (
    id              BIGSERIAL,
    market_id       VARCHAR(100) NOT NULL,
    token_id        VARCHAR(100) NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL,
    bids            JSONB NOT NULL,   -- [{price, size}, ...] truncated to top-25
    asks            JSONB NOT NULL,
    source          VARCHAR(20) NOT NULL,  -- 'falcon_572','clob_ws_book'
    PRIMARY KEY (captured_at, market_id, token_id)
) PARTITION BY RANGE (captured_at);
```

Use case: train the error model on book imbalance at decision time, replay
historical liquidity, detect spoofing.

#### B. `markets_history`  (market metadata versioning)
Today `markets` is mutated in place. We lose the history of fee schedule
changes, end-date moves, liquidity-score evolution.

```sql
CREATE TABLE markets_history (
    market_id       VARCHAR(100) NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,           -- NULL = current
    snapshot        JSONB NOT NULL,        -- entire row at valid_from
    PRIMARY KEY (market_id, valid_from)
);

-- Trigger on UPDATE markets to close the previous version + insert new.
```

Versioning is SCD-Type-2. Without this, the economic model is non-reproducible
("what was the fee_rate when we made this trade?").

#### C. `leader_followers_materialized`  (graph denorm for fast reads)
The dashboard repeatedly does the same JOIN: `leaders → leader_profiles →
follower_edges (filtered) → COUNT(*)` (queries.py:1389, 2154). This is the
same expensive 4-way JOIN every snapshot tick.

```sql
CREATE MATERIALIZED VIEW mv_leader_followers AS
SELECT
    l.wallet_address,
    COALESCE(p.trades_observed, 0) AS trades_observed,
    COALESCE(p.positions_resolved, 0) AS positions_resolved,
    COUNT(e.id) FILTER (WHERE e.follow_probability > 0.6 AND e.co_occurrences >= 5) AS confirmed_followers,
    COUNT(e.id) AS total_edges,
    MAX(e.last_observed) AS edges_last_observed
FROM leaders l
LEFT JOIN leader_profiles p USING (wallet_address)
LEFT JOIN follower_edges e ON e.leader_wallet = l.wallet_address
WHERE l.on_watchlist = TRUE AND l.excluded = FALSE
GROUP BY l.wallet_address, p.trades_observed, p.positions_resolved;

CREATE UNIQUE INDEX ON mv_leader_followers (wallet_address);
-- Refresh: every 30s via APScheduler job.
```

Removes the worst 4-way JOIN from the snapshot path. Pairs naturally with
§4.3.

---

## 5. Anti-goals — what NOT to do

1. **Do not adopt TimescaleDB.** Volume targets (10x ≈ 9M rows/month, hot
   table) are well within stock Postgres 15. CLAUDE.md explicitly excluded it.
   Native partitioning + BRIN gets us most of the way.
2. **Do not shard before vertical scale exhausts.** Hetzner Helsinki box
   today is ~16GB / 4 core. We have 5–10x of headroom on a single node before
   sharding becomes the right answer. Sharding `trades_observed` introduces a
   cross-shard JOIN problem for every wallet-centric query.
3. **Do not migrate to a different DB engine.** No clickhouse, no pinot, no
   duckdb-as-backend. Cold-tier Parquet on R2 (§4.4) covers the OLAP-style
   needs for backtests; the hot path stays on Postgres.
4. **Do not build a generic "events" superclass table.** The current
   per-context tables (trades_observed, decision_log, paper_trades, live_trades)
   are correctly bounded. Forcing them into a polymorphic super-table just
   to "simplify" trades reading destroys the indexability we have today.
5. **Do not auto-create indexes on every JSONB blob.** GIN on `profile_json`
   etc. would balloon write amplification. Index only the JSONB paths the
   dashboard actually queries — today, none.
6. **Do not pre-emptively introduce read replicas** before mat-views (§4.3)
   land. They mask the problem at 2x the cost.
7. **Do not abandon `setup_db.py` for alembic right now.** The current
   forward-only IF-NOT-EXISTS pattern is robust. Add DOWN scripts as plain
   `XXX_*_down.sql` siblings; revisit alembic only if M11+ migration
   complexity demands branching.

---

## 6. Sequenced migration plan (M11 → M14+)

Each migration is sized to ship in one PR with rollback notes. Filenames assume
the existing pattern.

### `011_trades_observed_partition.sql`
Convert `trades_observed` to declarative time-range partitioning (RANGE on
`time`, monthly partitions). Create `_y{YYYY}m{MM}` partitions for the last 3
months + next 3 months. Backfill via `INSERT … SELECT … WHERE time BETWEEN
…`. Atomic-swap rename. Ship pg_partman config OR an APScheduler job that
pre-creates next-month partitions on day-25. Pairs with `011_*_down.sql` that
re-merges partitions back into a flat table.

### `012_brin_and_partial_indexes.sql`
- Replace `idx_trades_time` (BTREE) with BRIN on each partition.
- Add the missing partial indexes from §2: `paper_trades(opened_at) WHERE
  economic_model_version='v1.0.0' AND invalidated_at IS NULL`, same for
  `decision_log`, plus `follower_edges(follow_probability DESC,
  co_occurrences DESC) WHERE …`. All `CREATE INDEX CONCURRENTLY` —
  requires runner support for non-transactional migrations.

### `013_fk_and_check_constraints.sql`
Tighten the schema:
- CHECK on `trades_observed.side`, `trades_observed.source`,
  `paper_trades.status`, `paper_trades.direction`, `live_trades.status`,
  `live_orders.order_state`, `decision_log.action`, `decision_log.outcome`.
- Add `signal_audits.decision_id` FK to `decision_log(id)`.
- Add `fee_snapshots.market_id` FK to `markets(market_id) ON DELETE CASCADE`.
- Index `signal_audits.decision_id` and `signal_audits.created_at`.
- Add `(wallet, market, token, open_time)` UNIQUE on
  `positions_reconstructed`.

### `014_materialized_views.sql`
Create `mv_alpha_timeline_2h`, `mv_leader_followers`, `mv_market_scanner_stats`
matching the queries flagged in §4.3. Add an APScheduler job
(`engine/jobs/refresh_mat_views.py`) that calls `REFRESH MATERIALIZED VIEW
CONCURRENTLY` on a per-view interval. Update `api/queries.py` consumers to
read from the views with a fallback to base tables.

### `015_book_quality_snapshots_partition.sql`
Same treatment as M11, applied to `book_quality_snapshots` (the second-largest
write-rate table). Add a 30-day retention job.

### `016_cdc_publication.sql`
Create `PUBLICATION trades_observed_cdc FOR TABLE trades_observed`. Document
that operators must set `wal_level=logical` in `postgresql.conf` and restart.
The companion service (`src/observer/cdc_relay.py`) is a code change, not a
migration. Pair with a runbook in `docs/`.

### `017_versioning_and_audit_tables.sql`
- Create `markets_history` + trigger on `markets` UPDATE for SCD-2 capture.
- Create `order_book_snapshots` partitioned table (monthly).
- Add `decision_log.leader_classification_snapshot JSONB`.
- Add `paper_trades.fee_rate_pct_snapshot NUMERIC` and same on `live_trades`.

### `018_cold_tier_export.sql`
No DDL — purely a runbook + scripts under `scripts/cold_export/`. Script
detaches partitions older than 90 days, dumps to Parquet via duckdb, ships to
R2, drops the detached partition. The migration file itself records the
version + adds a `cold_partitions_log` table.

### `019_runner_supports_concurrent.sql`
Code change to `scripts/setup_db.py`: detect statements that begin with `--
ASYNC` directive and run them in autocommit. Migration file is the
documentation + a no-op `SELECT 1` so the version is recorded.

---

## Appendix A — Top three "no-DDL" wins to pair with the migrations

1. **Add per-table autovacuum tuning** for `trades_observed` and
   `book_quality_snapshots` via `ALTER TABLE … SET (autovacuum_vacuum_scale_factor
   = 0.02, autovacuum_analyze_scale_factor = 0.01)`. Append-only tables benefit
   from aggressive analyze for the planner.
2. **Snapshot `pg_stat_statements`** before/after each migration to validate
   the index changes did what we expected.
3. **Add a `slow_query_log` channel** in monitoring/ that surfaces queries
   >250 ms to the dashboard's Bot Health tab. Without it, we have no
   feedback loop on schema changes.
