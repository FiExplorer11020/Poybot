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

WITH inserted AS (
    INSERT INTO markets (market_id, question, category)
    SELECT DISTINCT
        t.market_id,
        'Market ' || substring(t.market_id, 1, 30) || '…' AS question,
        'unknown' AS category
    FROM trades_observed t
    LEFT JOIN markets m USING (market_id)
    WHERE m.market_id IS NULL
    ON CONFLICT (market_id) DO NOTHING
    RETURNING 1
)
SELECT COUNT(*) AS orphan_stubs_created FROM inserted;
