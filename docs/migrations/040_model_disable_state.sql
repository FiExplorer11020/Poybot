-- ============================================================================
-- 040_model_disable_state.sql
--
-- Round 13 (The Mirror) — Auto / manual model-disable state.
--
-- Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.4 + § 4.
--
-- One row per model. is_disabled=TRUE means the confidence_engine
-- bypasses that model's contribution to the decision flow until the
-- operator re-enables. The auto_or_manual column lets the operator
-- audit WHY the model is disabled — automatic drift trigger vs.
-- explicit operator override.
--
-- This table is intentionally tiny — at most one row per model. The
-- enumeration of model names is open: 'follow_confidence',
-- 'fade_confidence', 'strategy_class', 'volume_forecast', 'causal_ate',
-- plus any future additions. The auto-disabler enforces the constraint
-- that 'follow_confidence' is NEVER auto-disabled — it's the core
-- signal — but the operator CAN manually disable it via Telegram if
-- they really mean to.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS model_disable_state (
    model            VARCHAR(40) PRIMARY KEY,
    is_disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    disabled_at      TIMESTAMPTZ,
    disabled_reason  VARCHAR(200),
    auto_or_manual   VARCHAR(10) NOT NULL DEFAULT 'auto',
    CONSTRAINT chk_mds_auto_or_manual
        CHECK (auto_or_manual IN ('auto', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_mds_disabled
    ON model_disable_state (model)
    WHERE is_disabled = TRUE;

COMMENT ON TABLE model_disable_state IS
    'Round 13 (The Mirror) — Per-model on/off state for the confidence '
    'engine. See docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.4 + § 4.';

-- ----------------------------------------------------------------------------
-- model_drift_streak — companion table used by the drift detector to count
-- consecutive days of |z| > threshold. Auto-disable fires when the streak
-- reaches CALIBRATION_DRIFT_CONSECUTIVE_DAYS_FOR_DISABLE (default 3). The
-- streak is reset to 0 on the first clean day. One row per (model,
-- strategy_class); NULL strategy_class encodes the aggregate row.
-- ----------------------------------------------------------------------------

-- IMPORTANT: PostgreSQL implicitly NOT-NULLs PK columns. NULL strategy_class
-- encodes the aggregate row, so we cannot put (model, strategy_class) in
-- the PK. Surrogate ``streak_id`` keeps the PK; the natural pair is enforced
-- via PG-15+ ``UNIQUE ... NULLS NOT DISTINCT`` so ON CONFLICT (model,
-- strategy_class) collides on NULL.
CREATE TABLE IF NOT EXISTS model_drift_streak (
    streak_id          BIGSERIAL PRIMARY KEY,
    model              VARCHAR(40) NOT NULL,
    strategy_class     VARCHAR(20),
    consecutive_days   INTEGER NOT NULL DEFAULT 0,
    last_breach_at     DATE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mds_streak_key
    ON model_drift_streak (model, strategy_class)
    NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_mds_streak_recent
    ON model_drift_streak (last_breach_at DESC)
    WHERE consecutive_days > 0;

COMMENT ON TABLE model_drift_streak IS
    'Round 13 (The Mirror) — Per-(model, strategy_class) consecutive-day '
    'breach counter used by the drift detector to drive auto-disable. '
    'Spec § 3.3 + § 3.4.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. ModelAutoDisabler writes auto rows after 3+ consecutive drift
--      days; ModelAutoDisabler.disable_model also writes manual rows
--      via the Telegram /disable command.
--   2. The confidence engine consults this table before each model
--      contribution. Default (no row, or is_disabled=FALSE) leaves
--      pre-R13 behavior unchanged.
--   3. Rollback: DROP TABLE model_disable_state CASCADE.
-- ============================================================================
