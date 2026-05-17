-- 047_markets_event_start_time.sql
-- ------------------------------------------------------------------
-- Tier 1 fix #1 (autonomous_session_2026_05_17/02_STRUCTURAL_FIX_PLAN.md).
--
-- ROOT CAUSE: `markets.end_date` is Polymarket's *dispute window*
-- expiration, not the moment the underlying event resolves. For sport
-- markets the dispute window runs 7+ days AFTER the actual match.
-- The MIN_HOURS_TO_RESOLUTION_FOLLOW=6h filter passes "Punjab Kings
-- vs RCB" (end_date = 2026-05-24, +169h) even though the match
-- actually started 2026-05-17 10:00 UTC and resolved in ~3h.
-- Result: 9/10 paper trades closed at -96..98% on 2026-05-17.
--
-- FIX: enrich `markets` with the Gamma `gameStartTime` field
-- (top-level, populated for every live sport / esports match, NULL
-- for futures). Pair it with an `event_end_time` for symmetry and an
-- `is_live_match` boolean that the confidence engine can gate on in
-- O(1) without re-computing.
--
-- COLUMNS
--   event_start_time     TIMESTAMPTZ — Gamma `gameStartTime` parsed
--                                       to UTC. NULL for futures
--                                       (Stanley Cup champion etc.).
--   event_end_time       TIMESTAMPTZ — projected resolution wall.
--                                       For binary sport markets we
--                                       use `gameStartTime + 4h`
--                                       (covers cricket, basketball,
--                                       hockey, soccer). NULL when
--                                       gameStartTime is NULL.
--   is_live_match        BOOLEAN     — TRUE when event_start_time is
--                                       within ±2h of NOW(). Updated
--                                       by the 30-min refresh job in
--                                       scripts/maintenance_loop.py
--                                       so the confidence engine can
--                                       cheaply reject FOLLOW on the
--                                       live cohort.
--   event_metadata_source VARCHAR(50) — Provenance tag (e.g.
--                                       'gamma:gameStartTime',
--                                       'gamma:event.startDate' for
--                                       the events[0].startDate
--                                       fallback). Lets us audit
--                                       which Gamma field populated
--                                       each row.
--
-- INDEX
--   idx_markets_live_match — partial index on (is_live_match) WHERE
--   active=TRUE so the confidence engine's hot-path gate hits a
--   tiny index instead of a sequential scan of the 13k active rows.
--
-- IDEMPOTENCY: every ALTER + CREATE uses IF NOT EXISTS so the
-- migration runner is safe to re-apply.
-- ------------------------------------------------------------------

BEGIN;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS event_start_time TIMESTAMPTZ;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS event_end_time TIMESTAMPTZ;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS is_live_match BOOLEAN DEFAULT FALSE;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS event_metadata_source VARCHAR(50);

-- Partial index: keeps the cardinality low (only the live-match
-- subset of active markets). The confidence engine's gate is
-- `WHERE active=TRUE AND is_live_match=TRUE`, which this index
-- serves directly.
CREATE INDEX IF NOT EXISTS idx_markets_live_match
    ON markets(is_live_match)
    WHERE active = TRUE;

COMMIT;
