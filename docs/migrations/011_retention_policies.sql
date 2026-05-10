-- ============================================================================
-- 011_retention_policies.sql
--
-- Goal: support the retention sweep extended into `scripts/batch_runner.py`
-- (Phase 0 Task D, audit R-6 in docs/audit/01_data_inventory.md and architect
-- note M11 in docs/audit/03_schema_evolution.md).
--
-- THIS MIGRATION DOES NOT DELETE ANY DATA. It only ensures every table that
-- batch_runner.RETENTION_POLICIES targets has a B-tree index on its primary
-- timestamp column, so that:
--
--     DELETE FROM <table> WHERE <time_col> < $1
--
-- can use an index scan + tuple-fetch rather than a sequential scan. The
-- actual cleanup is operator-gated by RETENTION_ENABLED=true (default false).
--
-- ----------------------------------------------------------------------------
-- Coverage matrix (audit R-6 lists 9 unbounded tables — fee_snapshots and
-- signal_audits are currently dormant and never written today; index is added
-- pre-emptively, see comments below):
--
--   Table                       | Time column     | Index pre-existing?
--   ----------------------------+-----------------+--------------------
--   decision_log                | time            | yes (idx_decisions_time, mig 001)
--   book_quality_snapshots      | observed_at     | yes (book_quality_snapshots_recent_idx, mig 005)
--   portfolio_equity            | time (PK)       | yes (PK + portfolio_equity_time_idx, mig 004)
--   decision_state_transitions  | created_at      | yes (decision_state_transitions_recent_idx, mig 005)
--   live_orders                 | placed_at       | NO  -> created here
--   signal_audits               | created_at      | NO  -> created here (dormant table)
--   fee_snapshots               | captured_at     | NO  -> created here (dormant table)
--   system_control_audit        | changed_at      | yes (system_control_audit_recent_idx, mig 006)
--   risk_config_history         | changed_at      | yes (idx_risk_history_time, mig 010)
--
-- So this migration creates THREE new indexes; the other six are already
-- adequate for the retention sweep's range delete.
--
-- ----------------------------------------------------------------------------
-- A note on CREATE INDEX CONCURRENTLY:
--
-- Postgres forbids CONCURRENTLY inside a transaction block. The migration
-- runner in `scripts/setup_db.py` invokes each file via a single
-- `conn.execute(sql)` which wraps the statements in an implicit transaction,
-- and we additionally use BEGIN/COMMIT below for parity with migrations 003+.
-- Therefore we use plain `CREATE INDEX IF NOT EXISTS` here. The three target
-- tables are still small enough today (live_orders has 0 rows by default,
-- the two dormant tables have 0 rows, see audit A.11 / A.12 / A.21) that the
-- non-concurrent build will return in well under a second. This mirrors the
-- explicit choice made in migration 007 for trades_observed.
--
-- For a future re-run on a multi-million-row table, rebuild manually via psql
-- in autocommit:
--
--     -- as superuser, OUTSIDE this migration runner
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_live_orders_placed_at
--         ON live_orders (placed_at);
--
-- Phase 2 partitioning (audit roadmap §4.1 — currently labelled "M11" in that
-- doc) will eventually move `trades_observed` and `book_quality_snapshots` to
-- declarative partitioning, at which point retention becomes DROP PARTITION
-- and these indexes are superseded. This file is the Phase 0 stop-gap.
--
-- ----------------------------------------------------------------------------
-- Tables explicitly NOT covered (deferred to Phase 1 / 2):
--   * trades_observed         — already has 90-day cleanup since Phase 0;
--                               indexes for it land in migration 001/007.
--   * positions_reconstructed — open positions have close_time IS NULL; a
--                               simple cutoff DELETE would drop in-flight
--                               positions. Retention here needs the position
--                               lifecycle logic to land first. Phase 1.
--   * market_belief_states    — singleton-per-(market, strategy_track) with
--                               UNIQUE constraint, so it does not grow
--                               unboundedly. No retention needed.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- live_orders (mig 008)
-- ---------------------------------------------------------------------
-- Time column is `placed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`. No index
-- on it today. Cascade-deleted from `live_trades`, so retention on this
-- table is mostly defensive; once LIVE_TRADING_DRY_RUN flips, the shadow
-- rows will start accumulating and this DELETE path needs the index.
CREATE INDEX IF NOT EXISTS idx_live_orders_placed_at
    ON live_orders (placed_at);

-- ---------------------------------------------------------------------
-- signal_audits (mig 003) — currently dormant table, retention in place
-- pre-emptively. No writer exists in source today (per audit A.12).
-- Adding the index now means the day a writer is wired, we don't have to
-- ship another migration to make retention efficient.
-- ---------------------------------------------------------------------
-- Time column is `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`.
CREATE INDEX IF NOT EXISTS idx_signal_audits_created_at
    ON signal_audits (created_at);

-- ---------------------------------------------------------------------
-- fee_snapshots (mig 003) — currently dormant table, retention in place
-- pre-emptively. No writer exists in source today (per audit A.11). The
-- UNIQUE (market_id, token_id, captured_at, source) index does NOT help
-- a range scan on captured_at alone because captured_at is the third
-- column, so we add a dedicated single-column index.
-- ---------------------------------------------------------------------
-- Time column is `captured_at TIMESTAMPTZ NOT NULL`.
CREATE INDEX IF NOT EXISTS idx_fee_snapshots_captured_at
    ON fee_snapshots (captured_at);

COMMIT;
