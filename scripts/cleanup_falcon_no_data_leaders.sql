-- One-shot cleanup: exclude existing leaders that have been stamped
-- 'falcon_no_data' so they stop being counted in the active leader pool
-- and the data-quality "stale_refresh" gauge.
--
-- This is the SQL counterpart to the leader_registry.py PATCH that
-- now sets excluded=TRUE/on_watchlist=FALSE at stamp time. Existing
-- rows stamped before the patch landed need this catch-up update.
--
-- Run on the server with:
--   docker exec -i polymarket_db psql -U postgres polymarket \
--     < scripts/cleanup_falcon_no_data_leaders.sql
--
-- Safe to run repeatedly (idempotent — only updates rows where the flags
-- are still inconsistent).

BEGIN;

-- Show what we're about to do
SELECT
    'Before cleanup' AS phase,
    COUNT(*) FILTER (WHERE exclude_reason = 'falcon_no_data') AS total_falcon_no_data,
    COUNT(*) FILTER (
        WHERE exclude_reason = 'falcon_no_data'
          AND excluded = FALSE
    ) AS falcon_no_data_still_active,
    COUNT(*) FILTER (
        WHERE exclude_reason = 'falcon_no_data'
          AND on_watchlist = TRUE
    ) AS falcon_no_data_still_on_watchlist
FROM leaders;

-- Apply the catch-up
UPDATE leaders
SET excluded = TRUE,
    on_watchlist = FALSE
WHERE exclude_reason = 'falcon_no_data'
  AND (excluded = FALSE OR on_watchlist = TRUE);

-- Show after-state
SELECT
    'After cleanup' AS phase,
    COUNT(*) FILTER (WHERE exclude_reason = 'falcon_no_data') AS total_falcon_no_data,
    COUNT(*) FILTER (
        WHERE exclude_reason = 'falcon_no_data'
          AND excluded = FALSE
    ) AS falcon_no_data_still_active,
    COUNT(*) FILTER (WHERE excluded = FALSE AND on_watchlist = TRUE) AS active_pool_size
FROM leaders;

COMMIT;
