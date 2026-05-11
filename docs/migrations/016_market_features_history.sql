-- Migration 016: market_features_history (point-in-time feature store)
-- Phase 3 Round 2 Agent Y — see docs/audit/05_ml_pipeline.md MG-3 § 3.1
-- and docs/audit/phase0/C_liquidity.md (which laid the column-level
-- groundwork in migration 012).
--
-- Closes the audit's training-leakage finding: today
-- `error_model._fetch_training_data` reads `markets.liquidity_score`
-- AS-OF-NOW for historical positions. A market that became liquid two
-- weeks AFTER `pr.open_time` looks liquid in training but was
-- illiquid at decision time — classic train/serve skew.
--
-- This migration adds an APPEND-ONLY history table that
-- `sync_markets` dual-writes to alongside the existing `markets`
-- row. The error model then reads the most-recent row with
-- `captured_at <= pr.open_time` via a LATERAL JOIN, yielding a
-- point-in-time-correct liquidity feature. Volume estimate:
-- ~50 markets/day × 1-2 refreshes = ~100 rows/day = ~36k rows/year.
-- Trivial. Retention is handled in `scripts/batch_runner.py`
-- (`RETENTION_MARKET_FEATURES_HISTORY_DAYS`, default 540d = 18 months).
--
-- Schema choice: we mirror only the *features* the error model
-- currently reads (liquidity_score, volume_24h, category, fee_rate_pct)
-- plus a `source` provenance tag (mirrors `markets.liquidity_score_source`
-- from migration 012) and an `extra_json` slot for future features
-- (OB imbalance from Agent Z, microprice deviation, etc.). New feature
-- columns can be added via a later migration without breaking the
-- read path because the read does `SELECT * FROM ... LIMIT 1` and
-- the consumer is dict-based.
--
-- Index choice: `(market_id, captured_at DESC)` — covers the dominant
-- read pattern (most-recent row at-or-before asof for a given
-- market_id). PG can do an index-only descent for the LIMIT 1 inside
-- the LATERAL.

BEGIN;

CREATE TABLE IF NOT EXISTS market_features_history (
    id              BIGSERIAL PRIMARY KEY,
    market_id       VARCHAR(100) NOT NULL,
    captured_at     TIMESTAMPTZ  NOT NULL,
    -- Feature columns mirror the live `markets` row:
    liquidity_score NUMERIC(10,4),
    volume_24h      NUMERIC(20,2),
    category        VARCHAR(50),
    fee_rate_pct    NUMERIC(5,4),
    -- Provenance — same value-domain as markets.liquidity_score_source:
    --   'falcon_575' (Market Insights, documented source)
    --   'falcon_574' (legacy fallback)
    --   'gamma'      (gamma-api fallback)
    --   'manual'     (operator backfill via
    --                 scripts/backfill_market_features_history.py)
    source          VARCHAR(32),
    -- Future-feature slot — Agent Z (OB imbalance) and follow-up rounds
    -- can add fields here without an ALTER TABLE and without breaking
    -- the existing read path.
    extra_json      JSONB
);

-- Primary read pattern: for a given (market_id, asof_ts) the LATERAL
-- subquery does
--   SELECT * FROM market_features_history
--   WHERE market_id = $1 AND captured_at <= $2
--   ORDER BY captured_at DESC LIMIT 1
-- which an index DESC on (market_id, captured_at) serves with an
-- index-only LIMIT 1 descent.
CREATE INDEX IF NOT EXISTS idx_mfh_market_time
    ON market_features_history (market_id, captured_at DESC);

COMMIT;
