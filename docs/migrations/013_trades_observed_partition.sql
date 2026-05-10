-- ============================================================================
-- 013_trades_observed_partition.sql
--
-- Phase 2 / Task A — Convert `trades_observed` to native PostgreSQL declarative
-- range partitioning by `time`. This is the architect's #1 ROI move (M11 in
-- docs/audit/03_schema_evolution.md):
--
--   "Today scripts/batch_runner.py runs DELETE FROM trades_observed WHERE
--    time < cutoff nightly — at 10x scale that becomes a multi-million-row
--    vacuum churn. Native PG declarative partitioning turns retention into
--    DROP PARTITION (instant, zero bloat) and lets every dashboard
--    `WHERE time > NOW() - X` query skip cold partitions."
--
-- ----------------------------------------------------------------------------
-- WHY OPTION (b) — REBUILD-AND-SWAP, not in-place PK change:
--
-- PG declarative partitioning requires the partition key to be part of every
-- UNIQUE constraint, including the primary key. trades_observed today is
-- PRIMARY KEY (id) — a single BIGSERIAL. To partition by `time` we need a
-- composite PK `(id, time)`.
--
-- We considered:
--   (a) ALTER TABLE in-place: drop PK, rebuild as (id, time), then convert
--       the heap to PARTITION BY RANGE. PG does not actually support
--       "convert an existing table to partitioned in-place" — it has to be
--       recreated. So (a) reduces to (b) anyway.
--   (b) CREATE TABLE trades_observed_new (...) PARTITION BY RANGE (time);
--       INSERT ... SELECT; rename swap. Standard PG recipe, well-trodden.
--
-- Choosing (b).
--
-- ----------------------------------------------------------------------------
-- LOCK / DOWNTIME ESTIMATE:
--
-- This migration runs as a single transaction (the setup_db.py runner wraps
-- each file in conn.execute, which is one implicit transaction). That means:
--   * `INSERT INTO trades_observed_new SELECT * FROM trades_observed` will
--     scan the source table; on the current ~100k-row dev DB this is < 1s.
--     At 10x (~1M rows) expect 5–20s. At 100x (~10M rows) expect 1–3 min.
--   * The rename swap is metadata-only (instant).
--   * Index creation on the new partitioned table is performed before swap
--     so it does not block live writes.
--
-- DURING the transaction, the trade observer's INSERTs will block on
-- AccessExclusiveLock on `trades_observed` once we issue the RENAME. The
-- realistic window for production at current scale is < 1 min. Operators
-- can:
--   * Run during low-activity window (e.g. weekday 02:00–04:00 UTC).
--   * Pause the trade observer container for the duration if desired
--     (writes will resume cleanly afterward — partitioning is transparent
--     to the application: same table name, same SQL).
--
-- The old non-partitioned table is renamed to `trades_observed_legacy` and
-- kept for a 7-day soak. After verification (see
-- docs/audit/phase2/A_partition_cutover.md), the operator runs
-- DROP TABLE trades_observed_legacy manually.
--
-- ----------------------------------------------------------------------------
-- COLUMN INVENTORY — mirrors migrations 001 + 007 + 009 exactly:
--
--   001 (base):                                                              col
--     id              BIGSERIAL PRIMARY KEY                                  1
--     time            TIMESTAMPTZ NOT NULL                                   2
--     market_id       VARCHAR(100) NOT NULL                                  3
--     token_id        VARCHAR(100) NOT NULL                                  4
--     wallet_address  VARCHAR(100) NOT NULL                                  5
--     side            VARCHAR(4) NOT NULL                                    6
--     price           NUMERIC(10,6) NOT NULL                                 7
--     size_usdc       NUMERIC(20,2) NOT NULL                                 8
--     source          VARCHAR(10) DEFAULT 'websocket'                        9
--     is_leader       BOOLEAN DEFAULT FALSE                                 10
--
--   009 (denorm):
--     category        VARCHAR(50)                                           11
--
--   007 (no new columns — only the UNIQUE index on the natural key)
--
-- INDEX INVENTORY:
--   001:  idx_trades_wallet_time      (wallet_address, time)
--         idx_trades_market_time      (market_id, time)
--         idx_trades_time             (time)
--         idx_trades_leader           (is_leader) WHERE is_leader = TRUE
--   002:  idx_trades_leader_wallet    (wallet_address) WHERE is_leader = TRUE
--   007:  uq_trades_observed_natural_key UNIQUE
--                                     (wallet_address, market_id, time,
--                                      side, price, size_usdc)
--   009:  idx_trades_wallet_category_time
--                                     (wallet_address, category, time)
--                                     WHERE is_leader = TRUE
--
-- All 7 indexes are recreated below on the new partitioned parent. Note that
-- when an index is created on a partitioned table, PG automatically creates
-- a matching index on each child partition (and on future partitions).
--
-- Partition-key inclusion in unique constraints:
--   * The PK is composite (id, time) — `time` included.
--   * uq_trades_observed_natural_key already includes `time` as a column —
--     no change needed, this works as-is.
--
-- ----------------------------------------------------------------------------
-- PARTITION LAYOUT:
--
-- Monthly partitions named `trades_observed_YYYYMM`, covering 6 months back
-- and 6 months forward from the migration's NOW(). Plus a DEFAULT partition
-- to safely catch any out-of-range insertions (timestamps from the far past
-- or far future). The operator script
-- scripts/maintenance/create_trades_partitions.py rolls new monthly
-- partitions forward; cron it to run on the 1st of each month.
--
-- The DEFAULT partition is a CORRECTNESS safety net. In steady state it
-- should be empty. The retention adapter in batch_runner.py reports its
-- row count so we notice if the rolling partition creator falls behind.
--
-- ----------------------------------------------------------------------------
-- COORDINATION WITH OTHER PHASE 2 TASKS:
--   * Task B (partial indexes) — uses migration 014+
--   * Task C (PositionTracker state) — uses migration 014+
--   * Task D (Redis pubsub) — uses migration 014+
-- (Communicated in docs/audit/phase2/A_partition_cutover.md.)
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Create the new partitioned table. Column-for-column mirror of the
--    existing trades_observed (001 + 009), composite PK (id, time).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades_observed_new (
    id              BIGSERIAL,
    time            TIMESTAMPTZ NOT NULL,
    market_id       VARCHAR(100) NOT NULL,
    token_id        VARCHAR(100) NOT NULL,
    wallet_address  VARCHAR(100) NOT NULL,
    side            VARCHAR(4) NOT NULL,
    price           NUMERIC(10,6) NOT NULL,
    size_usdc       NUMERIC(20,2) NOT NULL,
    source          VARCHAR(10) DEFAULT 'websocket',
    is_leader       BOOLEAN DEFAULT FALSE,
    category        VARCHAR(50),
    PRIMARY KEY (id, time)  -- composite PK required for PARTITION BY RANGE (time)
) PARTITION BY RANGE (time);

-- ---------------------------------------------------------------------------
-- 2) Default partition to catch any out-of-range rows.
--    In steady state this stays empty; monitored by batch_runner retention.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades_observed_default
    PARTITION OF trades_observed_new DEFAULT;

-- ---------------------------------------------------------------------------
-- 3) Monthly partitions: 6 months back through 6 months forward from the
--    migration's effective date. Range bounds are inclusive on FROM,
--    exclusive on TO. Hard-coded relative to 2026-05 (current month per
--    project context) so the migration is deterministic and replayable;
--    the maintenance script rolls future months forward.
-- ---------------------------------------------------------------------------

-- 6 months back
CREATE TABLE IF NOT EXISTS trades_observed_202511
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2025-11-01 00:00:00+00') TO ('2025-12-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202512
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2025-12-01 00:00:00+00') TO ('2026-01-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202601
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202602
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-02-01 00:00:00+00') TO ('2026-03-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202603
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-03-01 00:00:00+00') TO ('2026-04-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202604
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00');

-- Current month
CREATE TABLE IF NOT EXISTS trades_observed_202605
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00');

-- 6 months forward
CREATE TABLE IF NOT EXISTS trades_observed_202606
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-06-01 00:00:00+00') TO ('2026-07-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202607
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202608
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202609
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202610
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS trades_observed_202611
    PARTITION OF trades_observed_new
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');

-- ---------------------------------------------------------------------------
-- 4) Copy existing data into the partitioned table. Rows are routed to the
--    appropriate partition by their `time` value automatically. Rows outside
--    the explicit range above land in trades_observed_default (which the
--    operator will monitor; the rolling partition script can later "promote"
--    them by creating the right monthly partition before retention drops
--    them).
--
--    We include an explicit column list to be robust against future column
--    reorderings.
-- ---------------------------------------------------------------------------
INSERT INTO trades_observed_new (
    id, time, market_id, token_id, wallet_address, side,
    price, size_usdc, source, is_leader, category
)
SELECT
    id, time, market_id, token_id, wallet_address, side,
    price, size_usdc, source, is_leader, category
FROM trades_observed;

-- ---------------------------------------------------------------------------
-- 5) Advance the BIGSERIAL sequence to the max id seen, so new inserts get
--    a non-conflicting id. (INSERT ... SELECT does NOT bump the sequence.)
-- ---------------------------------------------------------------------------
SELECT setval(
    pg_get_serial_sequence('trades_observed_new', 'id'),
    COALESCE((SELECT MAX(id) FROM trades_observed_new), 1),
    true
);

-- ---------------------------------------------------------------------------
-- 6) Recreate every index from migrations 001 + 002 + 007 + 009 on the
--    partitioned parent. PG cascades these to every existing partition and
--    to all future partitions automatically.
--
--    Naming convention: keep the original index name so application code
--    that references them by name (e.g. EXPLAIN output, pg_stat_user_indexes
--    dashboards) keeps working. We suffix the *partitioned-parent* index
--    with no suffix; PG will name the per-partition children
--    "<parent_name>_<part_suffix>" automatically.
-- ---------------------------------------------------------------------------

-- (001) wallet-time scan — heavy use by graph_engine + profiler
CREATE INDEX IF NOT EXISTS idx_trades_wallet_time
    ON trades_observed_new (wallet_address, time);

-- (001) market-time scan — used by per-market activity feeds
CREATE INDEX IF NOT EXISTS idx_trades_market_time
    ON trades_observed_new (market_id, time);

-- (001) Plain time index — kept for compatibility with arbitrary range
--       scans the dashboard issues. Phase 2.2 (audit M12) plans to replace
--       this with BRIN; left BTREE for now to preserve current EXPLAIN
--       behaviour during cutover.
CREATE INDEX IF NOT EXISTS idx_trades_time
    ON trades_observed_new (time);

-- (001) Partial index — only the leader subset, which is what the
--       confidence engine reads.
CREATE INDEX IF NOT EXISTS idx_trades_leader
    ON trades_observed_new (is_leader)
    WHERE is_leader = TRUE;

-- (002) Partial wallet-only index for "leader wallets, no time predicate".
CREATE INDEX IF NOT EXISTS idx_trades_leader_wallet
    ON trades_observed_new (wallet_address)
    WHERE is_leader = TRUE;

-- (009) Per-wallet category aggregations for dashboard snapshot.
CREATE INDEX IF NOT EXISTS idx_trades_wallet_category_time
    ON trades_observed_new (wallet_address, category, time)
    WHERE is_leader = TRUE;

-- (007) Natural-key uniqueness — guarantees idempotent observer writes.
--       `time` is column #3, already in the key, so partition-key
--       inclusion is satisfied.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_observed_natural_key
    ON trades_observed_new (wallet_address, market_id, time, side, price, size_usdc);

-- ---------------------------------------------------------------------------
-- 7) Atomic name swap.
--
--    AccessExclusiveLock on both tables for the duration of the rename
--    (metadata-only, milliseconds).
--
--    After this, `trades_observed` IS the partitioned table; the old heap
--    is renamed to `trades_observed_legacy` and kept for the soak window.
-- ---------------------------------------------------------------------------
ALTER TABLE trades_observed RENAME TO trades_observed_legacy;
ALTER TABLE trades_observed_new RENAME TO trades_observed;

-- The sequence that backs `id` is owned by the renamed-old table; we want
-- it to continue feeding the new one. asyncpg's BIGSERIAL writes to the
-- DEFAULT, so we re-point the default to the surviving sequence and let
-- pg_get_serial_sequence resolve on the new table from now on.
--
-- In practice PG already auto-renames sequences owned by renamed tables,
-- but we don't rely on that. We rename it explicitly for clarity:
ALTER SEQUENCE IF EXISTS trades_observed_id_seq      RENAME TO trades_observed_legacy_id_seq;
ALTER SEQUENCE IF EXISTS trades_observed_new_id_seq  RENAME TO trades_observed_id_seq;

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. Verify row counts match:
--        SELECT
--            (SELECT COUNT(*) FROM trades_observed)        AS new_count,
--            (SELECT COUNT(*) FROM trades_observed_legacy) AS legacy_count;
--      The two should be identical.
--
--   2. Run the bot end-to-end for 24h. Verify trade_observer can INSERT,
--      verify dashboard queries return data.
--
--   3. After a 7-day soak with no regressions, run:
--        DROP TABLE trades_observed_legacy;
--
--   4. Cron `scripts/maintenance/create_trades_partitions.py` to run on
--      the 1st of each month at 00:30 UTC.
--
-- See docs/audit/phase2/A_partition_cutover.md for the full runbook.
-- ============================================================================
