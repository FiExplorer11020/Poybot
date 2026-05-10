-- 009_trades_category_denorm.sql
--
-- Denormalize market category onto trades_observed and positions_reconstructed
-- so per-wallet category analysis (top_categories breakdown, drilldowns) does
-- not need to JOIN markets. This unblocks pruning the markets table to a lean
-- "currently tradable" lookup and keeps the wallet-centric edge intact even
-- after resolved markets are purged.
--
-- The column is nullable on purpose: the observer write path falls back to
-- 'unknown' when no markets row exists yet, and a periodic re-tagger can
-- upgrade those rows once Falcon enrichment lands.
--
-- Idempotent: re-running this migration is a no-op.

BEGIN;

ALTER TABLE trades_observed
    ADD COLUMN IF NOT EXISTS category VARCHAR(50);

ALTER TABLE positions_reconstructed
    ADD COLUMN IF NOT EXISTS category VARCHAR(50);

-- Helpful indexes for the per-wallet category aggregations the dashboard
-- will hit on every snapshot tick.
CREATE INDEX IF NOT EXISTS idx_trades_wallet_category_time
    ON trades_observed (wallet_address, category, time)
    WHERE is_leader = TRUE;

-- Backfill from the current markets table (one-shot — second run is no-op
-- because trades_observed.category will already be populated for these rows).
UPDATE trades_observed t
SET category = m.category
FROM markets m
WHERE t.market_id = m.market_id
  AND t.category IS NULL
  AND m.category IS NOT NULL;

UPDATE positions_reconstructed p
SET category = m.category
FROM markets m
WHERE p.market_id = m.market_id
  AND p.category IS NULL
  AND m.category IS NOT NULL;

COMMIT;
