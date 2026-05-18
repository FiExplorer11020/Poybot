-- ============================================================================
-- 054_fee_rate_normalize_bps_to_decimal.sql
--
-- Plan 2026-05-19 P4-1 — normalise `markets.fee_rate_pct` from BPS
-- (legacy write convention) to DECIMAL (downstream convention).
--
-- Context
-- -------
-- `src/observer/trade_observer.py` wrote Gamma's `takerBaseFee` (in
-- BPS, e.g. 156 = 1.56%) directly to `markets.fee_rate_pct`. The
-- downstream `src/economics/fees.py::calculate_polymarket_fee`
-- interprets `fee_rate` as a DECIMAL (0.0156). Result: every crypto /
-- sport-fee market over-charged paper-trade fees by 10,000×.
--
-- The audit doc explicitly flagged this as a documented bug deferred
-- to a future fix. Plan P4-1 closes it.
--
-- Fix
-- ---
-- Detect bps-encoded rows by `fee_rate_pct > 1.0` (a real decimal fee
-- rate maxes at 0.10 for 10% fees — anything > 1 is bps). Divide those
-- rows by 10000 to land on the decimal convention. Rows already < 1.0
-- are left untouched (some sport markets ship fee_rate_pct = 0.0
-- legitimately).
-- ============================================================================

BEGIN;

UPDATE markets
   SET fee_rate_pct = fee_rate_pct / 10000.0
 WHERE fee_rate_pct IS NOT NULL
   AND fee_rate_pct > 1.0;

-- fee_snapshots inherits the same legacy bps. Same fix.
UPDATE fee_snapshots
   SET fee_rate = fee_rate / 10000.0
 WHERE fee_rate IS NOT NULL
   AND fee_rate > 1.0;

COMMIT;
