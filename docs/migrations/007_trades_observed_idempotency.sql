-- ============================================================================
-- 007_trades_observed_idempotency.sql
--
-- Goal: enforce idempotency on `trades_observed` at the database layer so a
-- duplicate trade can never be inserted, regardless of what happens with the
-- Redis-side dedup cache (flush, restart, TTL boundary, race on cold start).
--
-- Why now: the bot is moving to 24/7 cloud operation. Redis is the only thing
-- standing between a noisy data-api/WS feed and our `trades_observed` table.
-- A single Redis incident would silently corrupt the table — and downstream
-- behavior profiling, leader stats and paper-trade pnl all read from there.
--
-- Strategy:
--   1) De-duplicate any rows that snuck in historically before applying the
--      uniqueness constraint, keeping the lowest `id` for each natural key
--      (i.e. the first observation chronologically since `id` is BIGSERIAL).
--   2) Create a UNIQUE index on the natural key so future inserts with
--      `ON CONFLICT … DO NOTHING` are guaranteed safe.
--
-- The natural key matches what TradeObserver._trade_exists() already uses:
--     (wallet_address, market_id, time, side, price, size_usdc)
--
-- Performance note: at 71k existing rows the synchronous CREATE UNIQUE INDEX
-- (no CONCURRENTLY because the migration runner uses a single execute call)
-- finishes well under a second, and the trade observer can comfortably wait.
-- ============================================================================

BEGIN;

-- 1) Drop any pre-existing duplicates (defensive — there should be none if
--    Redis dedup has been functional, but we cannot assume that going back to
--    the very first ingestion).
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY wallet_address, market_id, time, side, price, size_usdc
            ORDER BY id
        ) AS rn
    FROM trades_observed
)
DELETE FROM trades_observed t
USING ranked r
WHERE t.id = r.id
  AND r.rn > 1;

-- 2) Enforce uniqueness on the natural key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_observed_natural_key
    ON trades_observed (wallet_address, market_id, time, side, price, size_usdc);

COMMIT;
