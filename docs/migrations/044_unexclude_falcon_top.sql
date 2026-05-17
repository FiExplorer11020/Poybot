-- 044_unexclude_falcon_top.sql
-- ------------------------------------------------------------------
-- Unexclude the top-Falcon wallets that were stamped excluded=TRUE
-- with an EMPTY exclude_reason. These rows are accidental exclusions
-- (the enrich pipeline used to write an empty string instead of NULL
-- when no falcon_no_data was returned); the 2026-05-17 diagnosis
-- found 5 top-20 Falcon wallets in this state and the bot was
-- silently skipping them in every bootstrap.
--
-- We only target rows where:
--   * excluded = TRUE (don't reactivate already-active leaders);
--   * exclude_reason IS NULL OR '' (don't un-exclude wallets that
--     were excluded with a real reason — bot detection, manual ban,
--     falcon_no_data, etc.);
--   * falcon_score IS NOT NULL (must have a measured Falcon score
--     to merit reinstatement);
--   * top 20 by falcon_score (cap the blast radius — this migration
--     is meant to reverse a known data accident, not bulk-reactivate).
--
-- ------------------------------------------------------------------
-- PREVIEW QUERY (run first to inspect what would change):
--
--   SELECT wallet_address, falcon_score, excluded, exclude_reason
--   FROM leaders
--   WHERE excluded = TRUE
--     AND (exclude_reason IS NULL OR exclude_reason = '')
--     AND falcon_score IS NOT NULL
--   ORDER BY falcon_score DESC
--   LIMIT 20;
--
-- Expected rows: ≤20, including the 5 top-20 wallets flagged in the
-- diagnosis. If the count is much higher than 20 the enrich pipeline
-- has a fresh bug — stop and investigate before running the UPDATE.
-- ------------------------------------------------------------------

BEGIN;

UPDATE leaders
SET excluded = FALSE,
    exclude_reason = NULL,
    on_watchlist = TRUE
WHERE wallet_address IN (
    SELECT wallet_address
    FROM leaders
    WHERE excluded = TRUE
      AND (exclude_reason IS NULL OR exclude_reason = '')
      AND falcon_score IS NOT NULL
    ORDER BY falcon_score DESC
    LIMIT 20
);

COMMIT;
