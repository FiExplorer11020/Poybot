-- ============================================================================
-- 027_leaders_classification_json.sql
--
-- Round 8 (The Lens) — formalize the existing `leaders.classification_json`
-- column schema.
--
-- Audit reference: docs/ROUND_8_STRATEGY_CLASSIFIER.md § 3.3 + § 4
-- (migration sequence) — the column already exists (master CLAUDE.md
-- § 6) and is populated ad-hoc by the registry/profiler. Round 8 adds
-- a schema-stable sub-object `strategy_fingerprint` so the classifier
-- writes don't collide with the existing influence/horizon/copiable
-- keys, and so the dashboard can lock onto a stable shape.
--
-- This migration is INTENTIONALLY SOFT — it documents the contract via
-- a COMMENT plus an optional GIN index. We do NOT enforce the shape
-- with a CHECK constraint because:
--   1. The legacy column may carry partial / NULL fields from older
--      writes; rejecting them at the DB layer would brick the registry.
--   2. JSONB CHECK constraints fire on every UPDATE, and the registry
--      updates `classification_json` on every Falcon refresh (hot path).
--      Validation belongs in the Python layer (Pydantic) where it's
--      already audited.
--
-- The expected shape (validated by `src/strategy_classifier/model.py`
-- before writing):
--
--   {
--     // existing keys (pre-Round-8, written by registry/profiler):
--     "strategy":  "directional" | "structural" | "cognitive",
--     "influence": "whale" | "top_trader" | "community",
--     "horizon":   "scalper" | "swing" | "holder",
--     "copiable":  true | false,
--
--     // Round 8 additions (written by src.strategy_classifier):
--     "strategy_fingerprint": {
--         "primary_strategy": "directional",   // one of 9 classes
--         "confidence": 0.74,                  // float in [0,1]
--         "strategy_probs": {                  // sums to ~1.0
--             "directional": 0.74,
--             "momentum":    0.12,
--             ...
--         },
--         "model_version": "sc.v1.0",          // matches leader_strategy_history
--         "classified_at": "2026-05-12T03:00:00Z",
--         "drift_detected": false              // boolean
--     }
--   }
--
-- Reading code (engine, dashboard) must defensively coalesce missing
-- keys to defaults; ABSENCE of `strategy_fingerprint` means the
-- classifier hasn't run on this wallet yet — fall back to the
-- pre-Round-8 strategy field.
-- ============================================================================

BEGIN;

-- The column already exists from migration 002 (or earlier; see the
-- master schema in CLAUDE.md § 6). This block is defensive: it'll add
-- the column only if a fresh install somehow landed without it.
ALTER TABLE leaders
    ADD COLUMN IF NOT EXISTS classification_json JSONB DEFAULT '{}'::jsonb;

-- GIN index on the JSONB column so dashboard queries filtering by
-- `strategy_fingerprint -> 'primary_strategy'` use an index, not a
-- full-table scan. Partial-index on rows where the fingerprint has
-- actually been populated to keep the index tight.
CREATE INDEX IF NOT EXISTS idx_leaders_strategy_fingerprint
    ON leaders USING GIN ((classification_json -> 'strategy_fingerprint'))
    WHERE classification_json ? 'strategy_fingerprint';

-- Schema-documentation comment. Visible via `\d+ leaders` in psql and
-- via information_schema.columns.col_description.
COMMENT ON COLUMN leaders.classification_json IS
    'Per-wallet classification metadata. Pre-Round-8 keys: strategy '
    '(directional|structural|cognitive), influence (whale|top_trader|community), '
    'horizon (scalper|swing|holder), copiable (bool). Round 8 adds a '
    'strategy_fingerprint sub-object with the 9-class classifier output: '
    '{primary_strategy, confidence, strategy_probs, model_version, '
    'classified_at, drift_detected}. See docs/ROUND_8_STRATEGY_CLASSIFIER.md '
    '§ 3.3 + migration 027_leaders_classification_json.sql for the full shape.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The column shape is enforced in code (`StrategyClassifier.save_to_leader`
--      in src/strategy_classifier/model.py). No DB-side CHECK.
--
--   2. To roll back: drop the new GIN index. The column itself was
--      shared with pre-Round-8 code so DROP COLUMN is not safe.
-- ============================================================================
