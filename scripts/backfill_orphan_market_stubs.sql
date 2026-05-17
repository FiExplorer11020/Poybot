-- One-shot backfill: create stub `markets` rows for orphan trades.
--
-- Why
-- ---
-- 14,533+ distinct market_ids in trades_observed (last 7d) have no
-- corresponding `markets` row. Dominated by source='onchain' rows where
-- market_id = token_id (placeholder pending Wave-3 economic decoder per
-- CLAUDE.md §15). Without a stub, every LEFT JOIN markets m USING
-- (market_id) returns NULLs, the unmapped_tokens DQ counter stays
-- inflated, and `sync_markets` keeps retrying Falcon enrichment on
-- garbage condition_ids — burning agent-574 quota.
--
-- The 2026-05-17 commit 6615d0a (ml: exclude source='onchain' ...) adds
-- markets-stub inserts at the WRITE PATH (clob_listener._insert_trade
-- and backfill_polymarket_trades.bulk_insert) so all FUTURE onchain
-- rows get a stub at insert time. This script catches the HISTORICAL
-- orphans in one idempotent pass.
--
-- Idempotent (ON CONFLICT DO NOTHING). Safe to run multiple times.
--
-- How to run (on the VM)
-- ----------------------
--   docker exec -i polymarket_db psql -U polymarket -d polymarket \
--     < scripts/backfill_orphan_market_stubs.sql
--
-- Followups
-- ---------
-- After this lands, `sync_markets` (with the source='onchain' filter
-- added in commit 60cb53a) will SKIP these stubs for Falcon enrichment
-- since onchain rows are now excluded from its discovery query. The
-- stubs remain in `markets` with category='unknown' until either the
-- Wave-3 decoder lands (resolving market_id → real condition_id) or an
-- operator manually deletes them.

\timing on

-- Stubs are inserted with active=FALSE: they are placeholders for trades
-- whose real market metadata is either unknown (Wave-3 onchain rows whose
-- market_id is really a token_id) or temporarily missing. A stub marked
-- active=TRUE would inflate the data_quality "unmapped_tokens" counter
-- (which filters by markets.active=TRUE) by tens of thousands and obscure
-- the real backlog of genuinely-active live markets that still need
-- enrichment.
WITH inserted AS (
    INSERT INTO markets (market_id, question, category, active)
    SELECT DISTINCT
        t.market_id,
        'Market ' || substring(t.market_id, 1, 30) || '…' AS question,
        'unknown' AS category,
        FALSE AS active
    FROM trades_observed t
    LEFT JOIN markets m USING (market_id)
    WHERE m.market_id IS NULL
    ON CONFLICT (market_id) DO NOTHING
    RETURNING 1
)
SELECT COUNT(*) AS orphan_stubs_created FROM inserted;

-- Safety net: if anyone re-runs this script after stubs already exist
-- (e.g., earlier version of the script without the `active=FALSE` in
-- the INSERT, or a future operator who accidentally flips them back),
-- this idempotent UPDATE keeps the invariant clean. Targets ONLY rows
-- that look like our stubs (unknown category + both tokens NULL).
UPDATE markets
SET active = FALSE
WHERE category = 'unknown'
  AND token_yes IS NULL
  AND token_no IS NULL
  AND active = TRUE;
