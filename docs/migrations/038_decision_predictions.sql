-- ============================================================================
-- 038_decision_predictions.sql
--
-- Round 13 (The Mirror) — Per-decision counterfactual predictions.
--
-- Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.1.
--
-- For every row in decision_log, this sister table records what each
-- model PREDICTED at the time the decision was made. When the position
-- subsequently closes (paper or live), the actual outcome columns get
-- filled in. Together, decision_log + decision_predictions are the
-- experience-replay buffer that the loss aggregator (§ 3.2), drift
-- detector (§ 3.3), and auto-disabler (§ 3.4) feed off of.
--
-- Persistence shape:
--
--   decision_id BIGINT FK to decision_log.id (PRIMARY KEY — one row of
--     predictions per decision).
--   predicted_at TIMESTAMPTZ — when the predictions were captured.
--     This must match decision_log.time within a small tolerance; the
--     loss aggregator joins on decision_id so this is a sanity column
--     only.
--   follow_confidence / fade_confidence — Thompson sample magnitudes
--     output by the confidence engine for this leader at decision time.
--   strategy_class / strategy_confidence — R8 strategy fingerprint
--     primary class + the classifier's confidence on it.
--   hawkes_alpha_mu — R9 multivariate Hawkes coupling ratio (cached
--     from causal_estimates / multivariate_hawkes_fits at decision time).
--   volume_forecast_usdc / _ci_low / _ci_high — R9 FollowerVolumePredictor
--     point forecast + 95% bootstrap CI bounds for the next-window
--     follower-pool volume.
--   causal_ate / _ci_low / _ci_high — R10 IV-corrected ATE point estimate
--     + 95% bootstrap CI bounds for the (leader, pool) pair active at
--     decision time.
--
-- Outcomes (filled later when the position closes — NULL until then):
--
--   actual_pnl_usdc — net realised PnL for the position that opened on
--     this decision (matches paper_trades.pnl_usdc).
--   actual_followup_volume_usdc — total follower-pool USDC volume
--     observed in the (predicted_at, predicted_at + window) bucket. Used
--     by the loss aggregator's MAPE / CI-coverage computation for the
--     volume_forecast model.
--   closed_at — TIMESTAMPTZ when actual_* were filled in.
--
-- A NULL outcomes triplet is a "still-open" or "never-traded" decision;
-- the loss aggregator skips those rows.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS decision_predictions (
    decision_id                 BIGINT NOT NULL,
    predicted_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Per-model predictions captured at decision time:
    follow_confidence           NUMERIC(8, 6),
    fade_confidence             NUMERIC(8, 6),
    strategy_class              VARCHAR(20),
    strategy_confidence         NUMERIC(8, 6),
    hawkes_alpha_mu             NUMERIC(10, 6),
    volume_forecast_usdc        NUMERIC(20, 2),
    volume_forecast_ci_low      NUMERIC(20, 2),
    volume_forecast_ci_high     NUMERIC(20, 2),
    causal_ate                  NUMERIC(10, 6),
    causal_ate_ci_low           NUMERIC(10, 6),
    causal_ate_ci_high          NUMERIC(10, 6),

    -- Outcomes (filled later when the position closes):
    actual_pnl_usdc             NUMERIC(20, 2),
    actual_followup_volume_usdc NUMERIC(20, 2),
    closed_at                   TIMESTAMPTZ,

    PRIMARY KEY (decision_id),
    CONSTRAINT fk_decision_predictions_decision
        FOREIGN KEY (decision_id) REFERENCES decision_log(id) ON DELETE CASCADE
);

-- Hot path index for the nightly loss aggregator's "yesterday's
-- decisions" scan and the drift detector's rolling 30-day window.
CREATE INDEX IF NOT EXISTS idx_decision_predictions_predicted_at
    ON decision_predictions (predicted_at DESC);

-- Partial index over closed decisions (the only rows the loss
-- aggregator processes). Keeps the aggregator's nightly batch index
-- scan tiny.
CREATE INDEX IF NOT EXISTS idx_decision_predictions_closed
    ON decision_predictions (closed_at DESC)
    WHERE closed_at IS NOT NULL;

-- Strategy-class filter (loss aggregator computes per-class loss).
CREATE INDEX IF NOT EXISTS idx_decision_predictions_strategy_class
    ON decision_predictions (strategy_class, predicted_at DESC)
    WHERE strategy_class IS NOT NULL;

COMMENT ON TABLE decision_predictions IS
    'Round 13 (The Mirror) — Per-decision counterfactual predictions + '
    'outcomes. One row per decision_log entry. See '
    'docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.1.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. confidence_engine.decide() writes a row in the same transaction
--      as the decision_log insert (R13 § 3.1).
--   2. position_tracker close hook fills in actual_pnl_usdc /
--      actual_followup_volume_usdc / closed_at.
--   3. Rollback: DROP TABLE decision_predictions CASCADE.
-- ============================================================================
