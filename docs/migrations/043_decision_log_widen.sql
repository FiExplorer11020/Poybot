-- 043_decision_log_widen.sql
-- ------------------------------------------------------------------
-- Widen numeric(5,4) columns on `decision_log` to numeric(7,4) so
-- they accept absolute values up to 999.9999.
--
-- Root cause: NUMERIC(5,4) constrains values to abs(x) < 10^1, i.e.
-- |x| < 10. Several columns can legitimately exceed that bound:
--   * confidence — Thompson-Sampling outputs are bounded to [0, 1]
--     but some pre-aggregation paths upstream of the INSERT can
--     produce values > 1 transiently (e.g. exp(logit) before sigmoid);
--   * thompson_follow / thompson_fade — Beta samples are in [0, 1]
--     but the engine sometimes writes the raw Beta α/β posterior
--     mean (positive but unbounded) when no sample is available;
--   * kelly_fraction — the formula (p*b - q) / b can exceed 1 for
--     extremely high p; the bot clamps for sizing but the audit log
--     stores the raw value for backtest replay.
--
-- The 2026-05-17 diagnosis surfaced this as repeated
--   "Failed to log decision: numeric field overflow.
--    A field with precision 5, scale 4 must round to an absolute
--    value less than 10^1."
-- errors in engine logs, which silently lost the extended-audit
-- decision rows. Bot operation continued (paper trades open against
-- the in-memory decision) but the offline backtest pipeline lost
-- the corresponding rows.
--
-- Fix: NUMERIC(7,4) keeps 4 decimal digits but allows |x| < 1000.
-- That's enough headroom for any plausible posterior or Kelly value
-- the engine can emit without clamping; if a column ever grows past
-- that, the right move is to widen ONCE more rather than re-introduce
-- clamping at the write boundary (clamping silently lies, widening
-- preserves the audit trail).
--
-- Idempotent: `ALTER TABLE … ALTER COLUMN … TYPE` is a no-op when the
-- column is already NUMERIC(7,4). Safe to re-apply on hot-deploy.
-- ------------------------------------------------------------------

BEGIN;

-- thompson_follow: Beta sample for FOLLOW action.
ALTER TABLE decision_log
    ALTER COLUMN thompson_follow TYPE NUMERIC(7,4)
    USING thompson_follow::NUMERIC(7,4);

-- thompson_fade: Beta sample for FADE action.
ALTER TABLE decision_log
    ALTER COLUMN thompson_fade TYPE NUMERIC(7,4)
    USING thompson_fade::NUMERIC(7,4);

-- kelly_fraction: raw Kelly f* before any sizing clamp.
ALTER TABLE decision_log
    ALTER COLUMN kelly_fraction TYPE NUMERIC(7,4)
    USING kelly_fraction::NUMERIC(7,4);

-- confidence: pre-clamp confidence value (engine sometimes writes
-- raw logits before the sigmoid is applied).
ALTER TABLE decision_log
    ALTER COLUMN confidence TYPE NUMERIC(7,4)
    USING confidence::NUMERIC(7,4);

COMMIT;
