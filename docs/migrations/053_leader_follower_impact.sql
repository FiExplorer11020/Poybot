-- ============================================================================
-- 053_leader_follower_impact.sql
--
-- Plan 2026-05-19 P3 — make follower_impact a queryable, indexable signal.
--
-- The schema in master CLAUDE.md § 6 documents a JSONB `follower_impact`
-- field with three sub-fields (avg_volume_induced, avg_price_move,
-- followers_activated) but `behavior_profiler.py` only ever initialises
-- it to zero and never writes real values. Audit agent confirmed the
-- field is never populated.
--
-- This migration lifts the three values out of the JSONB blob into
-- typed columns on `leader_profiles` so the engine's Kelly sizing can
-- consult them on the hot path without JSON parsing each tick. Default
-- to 0 (legacy behavior) so the gate stays neutral until the dedicated
-- backfill job populates real values.
--
-- A future job (scripts/backfill_follower_impact.py — Plan P3, deferred
-- to post-deploy) populates these columns by walking
-- `positions_reconstructed` for each leader's resolved positions and
-- counting follower activity in the 5-min window after each entry.
-- ============================================================================

BEGIN;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS avg_volume_induced  NUMERIC(20, 2) DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS avg_price_move      NUMERIC(10, 6) DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS followers_activated INTEGER        DEFAULT 0,
    ADD COLUMN IF NOT EXISTS follower_impact_updated_at TIMESTAMPTZ;

-- Partial index — only the rows the engine cares about (leaders with
-- measured impact > 0) participate in the index. Keeps the index small
-- and the scan fast on the hot path.
CREATE INDEX IF NOT EXISTS idx_leader_profiles_follower_impact
    ON leader_profiles (avg_volume_induced DESC, followers_activated DESC)
    WHERE avg_volume_induced > 0 OR followers_activated > 0;

COMMIT;
