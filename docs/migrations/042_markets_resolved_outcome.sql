-- 042_markets_resolved_outcome.sql
--
-- Adds an explicit resolution-outcome column to `markets` so the paper
-- trader can close resolved positions against the true terminal token
-- value (1.0 for the winning side, 0.0 for the loser) instead of a
-- stale book bid.
--
-- Audit 2026-05-17: the previous flow read `_exit_bid` for closes on
-- resolved markets and produced ~$42k of phantom-win PnL when the
-- cached bid had drifted far from the real terminal value. Without a
-- known outcome the paper trader will now DEFER the close (logged
-- warning), letting the long-tail TIMEOUT_DAYS=30 sweep eventually
-- catch it. The maintenance loop should populate this column by
-- polling Gamma `/markets/{condition_id}` for closed markets and
-- writing the outcome from `outcomePrices`.
--
-- Safe to apply on a live DB: column defaults to NULL, no historical
-- backfill required. The bot treats NULL as "outcome unknown" — that
-- matches the pre-migration behaviour for every row.

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS resolved_outcome VARCHAR(10);

COMMENT ON COLUMN markets.resolved_outcome IS
    'One of {yes, no, NULL}. Populated by the maintenance loop from '
    'Gamma /markets when the market resolves. NULL = outcome not yet '
    'known; paper_trader defers the close until populated.';

-- Helpful index for the few queries that filter on resolved-only
-- markets (e.g. backtest fixtures and the audit job).
CREATE INDEX IF NOT EXISTS idx_markets_resolved_outcome
    ON markets (resolved_outcome)
    WHERE resolved_outcome IS NOT NULL;
