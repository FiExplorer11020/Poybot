-- 014_partial_indexes.sql — Partial indexes for the v1 economic-version filter,
-- signal_audits FK + index, and enum-like CHECK constraints.
--
-- Phase 2 Task B (audit traceability: 03_schema_evolution.md §M12 + §M13,
-- 04_perf_hotpaths.md HP-4 #4, 01_data_inventory.md §A.7/§A.8/§A.12).
--
-- ============================================================================
-- IMPORTANT — APPLY PROCEDURE
-- ============================================================================
-- The `CREATE INDEX CONCURRENTLY` statements below CANNOT run inside an
-- explicit transaction. The project's migration runner (`scripts/setup_db.py`)
-- sends the whole file as a single `conn.execute(sql)` call, which asyncpg
-- wraps in an implicit transaction. Running this file through the runner will
-- therefore FAIL with:
--     "CREATE INDEX CONCURRENTLY cannot run inside a transaction block"
--
-- Apply this migration MANUALLY, OUT-OF-BAND, with psql in autocommit:
--
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f docs/migrations/014_partial_indexes.sql
--
-- After psql succeeds, record the migration so the runner skips it:
--
--     psql "$DATABASE_URL" -c "INSERT INTO schema_migrations (version) VALUES (14)"
--
-- Re-runs are safe: every statement uses IF NOT EXISTS (indexes) or
-- IF NOT EXISTS (constraints, via DO blocks) — no duplicate-object errors.
-- See docs/audit/phase2/B_partial_indexes.md for the full rationale.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Partial indexes on the v1 economic-version filter
-- ----------------------------------------------------------------------------
-- Audit ref: 03_schema_evolution.md §M12 (Section 2.7, 2.8) — every dashboard
-- query in src/api/queries.py composes `V1_PAPER_TRADE_SQL` /
-- `V1_DECISION_D_SQL` / `V1_POSITION_SQL` (see src/economics/versioning.py)
-- which expands to:
--   economic_model_version = 'v1.0.0' AND invalidated_at IS NULL
-- Today the planner seq-scans this filter because no existing index covers
-- both predicates. After backfill `invalidated_at` is NULL for all rows, so
-- a partial index over the *active* subset is highly selective by definition
-- (filters out a future "invalidated" tail) and small.
--
-- Cited usages in src/api/queries.py:
--   V1_PAPER_TRADE_SQL    : lines 506, 516, 527, 535, 791, 803, 1311, 1317,
--                           1323, 1333, 2292, 2725, 2750
--   V1_PAPER_TRADE_PT_SQL : lines 462, 989, 1118, 1543
--   V1_DECISION_D_SQL     : lines 1124, 1239, 2515 (action='skip' subset)
--   V1_POSITION_SQL       : line 778
--
-- The leading-column choice is the per-table "natural" ORDER BY of the
-- dashboard queries so the index serves both filter AND sort. Reads:
--   paper_trades            -> ORDER BY pt.opened_at DESC (queries.py:990, 1544)
--                              and ORDER BY closed_at DESC (queries.py:1324)
--   decision_log            -> ORDER BY d.time DESC      (queries.py:1125)
--   positions_reconstructed -> ORDER BY open_time DESC   (queries.py:779)

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_paper_trades_v1_active_opened
    ON paper_trades (opened_at DESC)
    WHERE economic_model_version = 'v1.0.0' AND invalidated_at IS NULL;

-- Closed-PnL dashboards: WHERE status='closed' AND <v1> ORDER BY closed_at DESC
-- (queries.py:1320-1324). closed_at is NULL for open trades, so adding
-- closed_at IS NOT NULL to the predicate keeps the index tight.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_paper_trades_v1_active_closed
    ON paper_trades (closed_at DESC)
    WHERE economic_model_version = 'v1.0.0' AND invalidated_at IS NULL
      AND closed_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decision_log_v1_active_time
    ON decision_log (time DESC)
    WHERE economic_model_version = 'v1.0.0' AND invalidated_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_reconstructed_v1_active_opened
    ON positions_reconstructed (open_time DESC)
    WHERE economic_model_version = 'v1.0.0' AND invalidated_at IS NULL;

-- live_trades has `economic_model_version` (mig 008) but NOT `invalidated_at`
-- — its rows are never invalidated in source (no V1_LIVE_TRADE_SQL filter
-- exists in src/api/queries.py or src/economics/versioning.py). Skipped.


-- ----------------------------------------------------------------------------
-- 2. signal_audits.decision_id  — FK + supporting index
-- ----------------------------------------------------------------------------
-- INFRA: FK + index in place for when signal_audits is wired (currently
-- dormant table, see audit Red Flag #1 in 01_data_inventory.md §A.12 and
-- 03_schema_evolution.md §2.12). The column already exists as BIGINT NULL
-- (mig 003 line 66); no writer in src/ today. Adding the FK + index now
-- means the day a writer is wired, queries like
--   SELECT * FROM signal_audits WHERE decision_id = $1
-- do not seq-scan, and the FK guarantees referential integrity.
--
-- created_at already has idx_signal_audits_created_at (mig 011).

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_signal_audits_decision_id
    ON signal_audits (decision_id)
    WHERE decision_id IS NOT NULL;

-- FK is added NOT VALID + later VALIDATE so we never take an
-- ACCESS EXCLUSIVE on decision_log. Wrapped in DO so re-runs are no-ops.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_signal_audits_decision_id'
    ) THEN
        ALTER TABLE signal_audits
            ADD CONSTRAINT fk_signal_audits_decision_id
            FOREIGN KEY (decision_id) REFERENCES decision_log (id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END
$$;

-- VALIDATE is safe because today the table is empty (Red Flag #1: no writer).
-- If, by the time this runs, the writer has shipped AND some rows reference
-- a non-existent decision_log.id, this VALIDATE will fail loudly — that's
-- the correct behaviour (surfaces the data bug instead of silently masking).
ALTER TABLE signal_audits
    VALIDATE CONSTRAINT fk_signal_audits_decision_id;


-- ----------------------------------------------------------------------------
-- 3. Enum-like CHECK constraints
-- ----------------------------------------------------------------------------
-- Audit ref: 03_schema_evolution.md §M13 (proposed in §2.7, §2.8).
-- We use ADD CONSTRAINT ... NOT VALID + VALIDATE CONSTRAINT so the brief
-- ACCESS EXCLUSIVE lock only covers the metadata flip; existing rows are
-- validated under SHARE UPDATE EXCLUSIVE, allowing concurrent reads.
--
-- ABOUT EXISTING DATA: every value list below was verified against the
-- writer paths in src/ (engine/paper_trader.py, engine/confidence_engine.py,
-- observer/position_tracker.py). If a pre-existing row violates the CHECK,
-- VALIDATE will raise — that is the desired safety net. No defensive UPDATE
-- is needed because there is no legacy uppercase/lowercase drift in these
-- columns (unlike trades_observed.side, which is owned by Phase 2 Task A
-- and therefore out of scope here).
--
-- IMPORTANT — NOTE ON NOT VALID + VALIDATE:
-- NOT VALID means existing rows are NOT checked at ADD time, only new
-- writes are. The follow-up VALIDATE CONSTRAINT scans the table once.
-- If a legacy row violates the constraint, VALIDATE fails; fix the data
-- and re-run. We do NOT add defensive UPDATE statements because:
--   - paper_trades.{status,direction,strategy}    — written exclusively by
--     src/engine/paper_trader.py using literal strings 'open'/'closed' etc.
--   - decision_log.{action,outcome}               — written by
--     src/engine/confidence_engine.py + src/engine/paper_trader.py:635
--   - positions_reconstructed.{direction,close_method} — written by
--     src/observer/position_tracker.py L179,238,258,412 with literal
--     'yes'/'no' and 'sell'/'merge'/'resolution'.
-- All writers are audited and value-clean. If you suspect drift, run:
--     SELECT DISTINCT <col> FROM <table>;
-- before applying. The DO blocks below make ADD idempotent; if VALIDATE
-- raises, drop the constraint, fix the data, re-run.

-- paper_trades.direction ∈ {'yes','no'}
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_paper_trades_direction'
    ) THEN
        ALTER TABLE paper_trades
            ADD CONSTRAINT ck_paper_trades_direction
            CHECK (direction IN ('yes', 'no'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE paper_trades VALIDATE CONSTRAINT ck_paper_trades_direction;

-- paper_trades.status ∈ {'open','closed','expired','cancelled'}
-- (Today only 'open'/'closed' are written; 'expired'/'cancelled' are
-- reserved per CLAUDE.md §6 schema for future close paths.)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_paper_trades_status'
    ) THEN
        ALTER TABLE paper_trades
            ADD CONSTRAINT ck_paper_trades_status
            CHECK (status IN ('open', 'closed', 'expired', 'cancelled'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE paper_trades VALIDATE CONSTRAINT ck_paper_trades_status;

-- paper_trades.strategy ∈ {'follow','fade'}
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_paper_trades_strategy'
    ) THEN
        ALTER TABLE paper_trades
            ADD CONSTRAINT ck_paper_trades_strategy
            CHECK (strategy IN ('follow', 'fade'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE paper_trades VALIDATE CONSTRAINT ck_paper_trades_strategy;

-- positions_reconstructed.direction ∈ {'yes','no'}
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_positions_reconstructed_direction'
    ) THEN
        ALTER TABLE positions_reconstructed
            ADD CONSTRAINT ck_positions_reconstructed_direction
            CHECK (direction IN ('yes', 'no'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE positions_reconstructed VALIDATE CONSTRAINT ck_positions_reconstructed_direction;

-- positions_reconstructed.close_method ∈ {'sell','merge','resolution'} or NULL (open)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_positions_reconstructed_close_method'
    ) THEN
        ALTER TABLE positions_reconstructed
            ADD CONSTRAINT ck_positions_reconstructed_close_method
            CHECK (close_method IS NULL OR close_method IN ('sell', 'merge', 'resolution'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE positions_reconstructed VALIDATE CONSTRAINT ck_positions_reconstructed_close_method;

-- decision_log.action ∈ {'follow','fade','skip'}
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_decision_log_action'
    ) THEN
        ALTER TABLE decision_log
            ADD CONSTRAINT ck_decision_log_action
            CHECK (action IN ('follow', 'fade', 'skip'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE decision_log VALIDATE CONSTRAINT ck_decision_log_action;

-- decision_log.outcome ∈ {'win','loss'} or NULL (pending)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_decision_log_outcome'
    ) THEN
        ALTER TABLE decision_log
            ADD CONSTRAINT ck_decision_log_outcome
            CHECK (outcome IS NULL OR outcome IN ('win', 'loss'))
            NOT VALID;
    END IF;
END
$$;
ALTER TABLE decision_log VALIDATE CONSTRAINT ck_decision_log_outcome;

-- ----------------------------------------------------------------------------
-- Intentionally OUT OF SCOPE (anti-goals):
--   * trades_observed.{side,source} CHECK + any new time DESC index on it.
--     Phase 2 Task A (migration 013) owns trades_observed (partition cutover).
--   * live_trades.status / live_orders.order_state CHECK — deferred to the
--     live-trading hardening pass; today live_trades is gated by
--     LIVE_TRADING_DRY_RUN, so churn on its enum is still possible.
--   * follower_edges (follow_probability DESC) — proposed in §M12 but no
--     citation in src/api/queries.py today; would be dead weight.
-- ----------------------------------------------------------------------------
