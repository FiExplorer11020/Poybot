-- 045_decision_log_widen_again.sql
-- ------------------------------------------------------------------
-- Second widen of decision_log numeric columns. Migration 043 widened
-- from NUMERIC(5,4) (abs<10) to NUMERIC(7,4) (abs<1000), but post-deploy
-- production logs show:
--   numeric field overflow
--   DETAIL: A field with precision 7, scale 4 must round to an
--   absolute value less than 10^3.
--
-- This means the engine is writing |x| >= 1000 to at least one of
-- {thompson_follow, thompson_fade, kelly_fraction, confidence}.
-- Most likely culprit: `kelly_fraction` — the raw Bayesian Kelly
-- formula `(p*b - q) / b` can blow up when `b` (decimal odds payoff)
-- is very small (entry price near 0 or near 1 — Kelly is ill-defined
-- at the boundaries).
--
-- The audit principle (per migration 043 header) is "widen, don't
-- clamp" so the audit trail remains faithful. Widen to NUMERIC(12,4):
-- |x| < 10^8 = 100,000,000. That's enough headroom for any
-- numerically-degenerate Kelly the engine could emit before the
-- sizing-cap clamps it for actual sizing.
--
-- Same as 043: idempotent on hot-deploy.
-- ------------------------------------------------------------------

BEGIN;

ALTER TABLE decision_log
    ALTER COLUMN thompson_follow TYPE NUMERIC(12,4)
    USING thompson_follow::NUMERIC(12,4);

ALTER TABLE decision_log
    ALTER COLUMN thompson_fade TYPE NUMERIC(12,4)
    USING thompson_fade::NUMERIC(12,4);

ALTER TABLE decision_log
    ALTER COLUMN kelly_fraction TYPE NUMERIC(12,4)
    USING kelly_fraction::NUMERIC(12,4);

ALTER TABLE decision_log
    ALTER COLUMN confidence TYPE NUMERIC(12,4)
    USING confidence::NUMERIC(12,4);

COMMIT;
