-- ============================================================================
-- 013_trades_observed_partition_DOWN.sql
--
-- Reverse migration 013. This restores the non-partitioned trades_observed
-- table from `trades_observed_legacy`.
--
-- WHEN IS THIS SAFE TO RUN?
--   * BEFORE the operator has executed `DROP TABLE trades_observed_legacy`
--     (i.e. within the 7-day soak window).
--   * AFTER the drop, this script is useless — the original data is gone.
--     Recovery in that scenario requires restoring from pg_dump/R2 backup
--     (see scripts/backup_db.py + scripts/restore_db.py).
--
-- This script ASSUMES no application is writing to trades_observed for the
-- duration of the rollback. The operator must stop the trade observer
-- container (or set the killswitch) first. The rollback is fast (rename +
-- DROP TABLE) so the window is < 10s.
--
-- The setup_db.py runner does NOT auto-apply DOWN scripts. This file is
-- executed manually via psql:
--
--     psql $DATABASE_URL -f docs/migrations/013_trades_observed_partition_DOWN.sql
--
-- and then the operator must also:
--
--     DELETE FROM schema_migrations WHERE version = 13;
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Sanity check: bail if the legacy table is missing (already cleaned up).
--    We cannot easily abort from inside a DO block on RAISE, but we can fail
--    loudly so the operator notices.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class
        WHERE relname = 'trades_observed_legacy'
          AND relkind = 'r'  -- ordinary table
    ) THEN
        RAISE EXCEPTION
            'trades_observed_legacy does not exist — the 7-day soak DROP has '
            'already happened. Rollback requires restoring from backup.';
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 2) Rename the partitioned table out of the way. We append _partitioned
--    rather than dropping it immediately, so that any rows written DURING
--    the partitioned window are not lost — the operator can manually
--    forward-port them back into the unpartitioned table if needed.
-- ---------------------------------------------------------------------------
ALTER TABLE trades_observed RENAME TO trades_observed_partitioned;

-- ---------------------------------------------------------------------------
-- 3) Restore the original heap as the canonical name.
-- ---------------------------------------------------------------------------
ALTER TABLE trades_observed_legacy RENAME TO trades_observed;

-- ---------------------------------------------------------------------------
-- 4) Re-point the sequence the legacy table needs.
-- ---------------------------------------------------------------------------
ALTER SEQUENCE IF EXISTS trades_observed_id_seq             RENAME TO trades_observed_partitioned_id_seq;
ALTER SEQUENCE IF EXISTS trades_observed_legacy_id_seq      RENAME TO trades_observed_id_seq;

COMMIT;

-- ============================================================================
-- POST-ROLLBACK (OPERATOR STEPS):
--
--   1. Optionally forward-port rows that were written to the partitioned
--      table after the UP migration but before this rollback:
--
--        INSERT INTO trades_observed (
--            time, market_id, token_id, wallet_address, side,
--            price, size_usdc, source, is_leader, category
--        )
--        SELECT
--            time, market_id, token_id, wallet_address, side,
--            price, size_usdc, source, is_leader, category
--        FROM trades_observed_partitioned
--        WHERE id NOT IN (SELECT id FROM trades_observed)
--        ON CONFLICT (wallet_address, market_id, time, side, price, size_usdc)
--        DO NOTHING;
--
--   2. Drop the partitioned remnant once forward-port is complete:
--
--        DROP TABLE trades_observed_partitioned CASCADE;
--
--   3. Remove the schema_migrations row so setup_db.py can re-apply 013
--      after the underlying issue is fixed:
--
--        DELETE FROM schema_migrations WHERE version = 13;
--
--   4. Restart the trade observer.
-- ============================================================================
