-- ============================================================================
-- 039_calibration_loss_history.sql
--
-- Round 13 (The Mirror) — Per-(model, strategy_class, measured_at) loss
-- history rolled up by the nightly loss aggregator.
--
-- Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.2 + § 4.
--
-- Each row records the calibration loss for ONE model over the decisions
-- whose outcomes were observed on a given DATE. strategy_class is the
-- partition key — NULL means "aggregate across all strategy classes for
-- this model on this date". The loss aggregator writes both the
-- aggregate row AND one row per non-NULL strategy_class per pass, so
-- the drift detector can monitor at both granularities.
--
-- Loss vocabulary (one of these columns is populated per row, others
-- left NULL depending on the model):
--   brier_score    — Brier loss for binary win/loss prediction
--                    (follow_confidence model).
--   log_loss       — Log-loss for multi-class strategy prediction
--                    (strategy_class model).
--   mape           — Mean Absolute Percentage Error for numeric forecast
--                    (volume_forecast model).
--   ci_coverage    — Fraction of decisions where the actual value fell
--                    inside the predicted 95% CI. Targets ≈ 0.95 for
--                    well-calibrated models.
-- ============================================================================

BEGIN;

-- IMPORTANT: PostgreSQL implicitly NOT-NULLs every PRIMARY KEY column. We
-- store ``strategy_class = NULL`` for the aggregate-across-classes row, so
-- we cannot use the natural triple as the primary key. Surrogate
-- ``history_id`` keeps the PK; the natural key is enforced via a UNIQUE
-- INDEX with PG-15+ ``NULLS NOT DISTINCT`` so NULL strategy_class collides
-- with NULL on idempotent ON CONFLICT writes.
CREATE TABLE IF NOT EXISTS calibration_loss_history (
    history_id      BIGSERIAL PRIMARY KEY,
    model           VARCHAR(40) NOT NULL,
    strategy_class  VARCHAR(20),  -- NULL = aggregate across classes
    measured_at     DATE NOT NULL,
    n_decisions     INTEGER NOT NULL DEFAULT 0,
    brier_score     NUMERIC(8, 6),
    log_loss        NUMERIC(8, 6),
    mape            NUMERIC(8, 6),
    ci_coverage     NUMERIC(5, 4)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clh_natural_key
    ON calibration_loss_history (model, strategy_class, measured_at)
    NULLS NOT DISTINCT;

-- The drift detector's rolling 30-day window joins on measured_at DESC
-- per (model, strategy_class) — keep that path indexed.
CREATE INDEX IF NOT EXISTS idx_clh_measured
    ON calibration_loss_history (measured_at DESC);

CREATE INDEX IF NOT EXISTS idx_clh_model_measured
    ON calibration_loss_history (model, measured_at DESC);

COMMENT ON TABLE calibration_loss_history IS
    'Round 13 (The Mirror) — Per-model calibration loss daily roll-up. '
    'See docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.2 + § 4.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The nightly src/calibration/loss_aggregator.py populates one row
--      per (model, strategy_class, yesterday) every batch run.
--   2. Backfill mode reconstructs up to 90 days from existing
--      decision_log + decision_predictions rows.
--   3. Rollback: DROP TABLE calibration_loss_history CASCADE.
-- ============================================================================
