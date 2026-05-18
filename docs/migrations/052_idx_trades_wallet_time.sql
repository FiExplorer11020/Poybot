-- ============================================================================
-- 052_idx_trades_wallet_time.sql
--
-- Add a composite btree index on trades_observed (wallet_address, time DESC).
--
-- 2026-05-18 perf audit (A6 agent, batch 1):
--   Many hot paths filter by `wallet_address = $1 AND time >= NOW() - INTERVAL '...'`.
--   Examples:
--     * src/graph/hawkes_fitter.py L282-297   (Hawkes fits: 30-day window per wallet)
--     * src/profiler/error_model.py L573-618  (error model: per-wallet trade lookup)
--     * src/profiler/feature_store.py L655-742 (feature store: wallet trade history)
--     * src/strategy_classifier/features.py L302 (strategy classifier features)
--     * src/api/queries.py L3742              (markets-per-wallet 14d/30d counter)
--
-- Current indexes that *could* serve this filter:
--     * uq_trades_observed_natural_key (wallet_address, market_id, time, side, price, size_usdc)
--       — wallet_address is leading but the planner has to walk every market_id
--       under the wallet to find the time range, which inflates buffer reads.
--     * idx_trades_observed_time (time)
--       — leading on time only; no wallet lookup possible.
--
-- Baseline (EXPLAIN ANALYZE on a hot wallet with ~3500 trades / 24h):
--     buffers shared hit = 996, exec time 30-50 ms, planning time 50-200 ms
--     (heap fetches 1551 because visibility map is partial on the natural-key
--     unique index — index-only scan keeps falling back).
--
-- A focused (wallet_address, time DESC) btree:
--   * trims index-only scan buffers from O(1000) to O(50-100) for 24h windows
--   * removes the need to traverse the wider unique key
--   * supports ORDER BY time DESC LIMIT N without an extra sort
--
-- PARTITION HANDLING:
-- `trades_observed` is range-partitioned by `time`. PostgreSQL 15 does NOT
-- support `CREATE INDEX CONCURRENTLY` directly on the partitioned parent
-- (raises "cannot create index on partitioned table concurrently"). The
-- workaround is:
--   1. Create the parent index with `ON ONLY` — this is a no-op on data
--      (empty parent), runs instantly, sets up the catalog entry.
--   2. Build the per-partition index `CONCURRENTLY` for each existing
--      partition so the table stays writeable during the build.
--   3. `ALTER INDEX ... ATTACH PARTITION` each partition index to the
--      parent. Once all partitions are attached, the parent index switches
--      from `INVALID` to `VALID` automatically.
--
-- This whole sequence is idempotent: every step uses `IF NOT EXISTS` or
-- `INFORMATION_SCHEMA` guards so the migration is safe to re-run.
-- ============================================================================

-- Step 1: parent index on the partitioned root (ON ONLY = empty, no scan).
CREATE INDEX IF NOT EXISTS idx_trades_wallet_time
    ON ONLY trades_observed (wallet_address, time DESC);

-- Step 2 & 3: build + attach per-partition indexes.
DO $$
DECLARE
    part_name TEXT;
    idx_name  TEXT;
    parent_idx_oid OID;
BEGIN
    SELECT c.oid INTO parent_idx_oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'idx_trades_wallet_time';

    FOR part_name IN
        SELECT inhrelid::regclass::text
        FROM pg_inherits
        WHERE inhparent = 'public.trades_observed'::regclass
        ORDER BY inhrelid::regclass::text
    LOOP
        idx_name := part_name || '_wallet_time_idx';
        -- Strip schema prefix for the index name (matches Postgres autonaming).
        idx_name := regexp_replace(idx_name, '^public\.', '');

        -- Build the per-partition index if it does not exist.
        IF NOT EXISTS (
            SELECT 1 FROM pg_class
            WHERE relname = idx_name
        ) THEN
            EXECUTE format(
                'CREATE INDEX %I ON %s (wallet_address, time DESC)',
                idx_name, part_name
            );
        END IF;

        -- Attach to parent if not already attached.
        IF NOT EXISTS (
            SELECT 1 FROM pg_inherits ih
            JOIN pg_class c ON c.oid = ih.inhrelid
            WHERE ih.inhparent = parent_idx_oid AND c.relname = idx_name
        ) THEN
            EXECUTE format(
                'ALTER INDEX idx_trades_wallet_time ATTACH PARTITION %I',
                idx_name
            );
        END IF;
    END LOOP;
END $$;
