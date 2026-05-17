-- 046_leader_external_stats.sql
-- ------------------------------------------------------------------
-- Strategy upgrade 2026-05-17 — Lever B (Falcon prior integration).
--
-- 5,247 leaders in `leaders` already have `wallet360_json` populated
-- by the Falcon Wallet 360 enrichment job (agent 581), but the
-- `winning_trades / losing_trades / total_trades` track record those
-- payloads carry has NEVER been integrated into the confidence
-- engine's Bayesian gates. The engine only trusts our own internal
-- `leader_profiles.positions_resolved` count, which means a
-- Falcon-validated leader with 5,000 trades on Polymarket but 0
-- positions reconstructed in our DB still trips the
-- `MIN_LEADER_RESOLVED_FOR_FOLLOW=30` gate.
--
-- This migration adds the columns the import script
-- (scripts/import_falcon_external_stats_2026_05_17.py) populates
-- from `wallet360_json->>'winning_trades'` etc. The confidence
-- engine reads them via the new `_compute_effective_metrics` helper
-- and computes:
--     effective_resolved = MAX(internal_resolved, external_resolved * 0.5)
--     effective_winrate  = Laplace-smoothed Bayesian fusion
--                          (internal + 0.5 * external prior)
--
-- The 0.5 discount is the operator-tunable FALCON_EXTERNAL_DISCOUNT
-- knob (runtime_config: also mutable, default 0.5). The idea: trust
-- our own observations more than externally-reported metrics, but
-- don't ignore the external evidence completely — it's the only
-- track-record we have for cold-start leaders.
--
-- Columns landed on `leader_profiles` (not `leaders`) because:
--   * leader_profiles already owns the OTHER Bayesian state
--     (positions_resolved, profile_json.accuracy, error_model_phase);
--     keeping all posterior inputs in one table simplifies the
--     confidence engine's read path.
--   * `external_resolved_count` participates in the per-leader gate
--     just like the internal counter — they're peers, not metadata.
--
-- Idempotent: ALTER TABLE ... IF NOT EXISTS — re-running the
-- migration is a no-op. The partial index has IF NOT EXISTS too so a
-- previous-run won't conflict.
-- ------------------------------------------------------------------

BEGIN;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS external_resolved_count INTEGER DEFAULT 0;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS external_wins INTEGER DEFAULT 0;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS external_losses INTEGER DEFAULT 0;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS external_source VARCHAR(50);

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS external_last_updated TIMESTAMPTZ;

-- Partial index: lets the confidence-engine `_get_readiness` join
-- skip leaders with zero external evidence in O(log n). The WHERE
-- clause keeps the index small (only the populated subset matters
-- for the Falcon-prior path).
CREATE INDEX IF NOT EXISTS idx_leader_profiles_external_resolved
    ON leader_profiles(external_resolved_count)
    WHERE external_resolved_count > 0;

COMMIT;
