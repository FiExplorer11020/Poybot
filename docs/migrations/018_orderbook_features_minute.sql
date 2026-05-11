-- Migration 018: orderbook_features_minute (per-minute rollup of book quality snapshots)
-- Phase 3 Round 2 Agent Z — see docs/audit/05_ml_pipeline.md summary
-- and docs/audit/01_data_inventory.md Section A.
--
-- Closes the audit's "highest-ROI new data source" recommendation: the raw
-- book_quality_snapshots feed (migration 005) lands one row per WS book
-- update — many per second per token at peak — which is too granular for
-- the error model and too noisy for FADE confidence. This migration adds
-- a per-(market_id, token_id, minute) rollup with three features baked in:
--
--   * depth_imbalance      = (bid_depth_at_best - ask_depth_at_best)
--                            / (bid_depth_at_best + ask_depth_at_best)
--                          ∈ [-1, +1].  +1 = all bids stacked, no asks.
--   * spread_bps           = (best_ask - best_bid) / midprice * 10000
--   * microprice           = (best_bid * ask_depth + best_ask * bid_depth)
--                            / (bid_depth + ask_depth)
--     microprice_deviation = |microprice - midprice|
--
-- Volume: ~50 active markets × 2 tokens × 1440 min/day ≈ 144k rows/day
-- ≈ 4.4 M rows/month. With ON CONFLICT DO UPDATE the rollup is idempotent
-- and a stale rerun never inflates the row count. Retention is handled by
-- the same script that prunes book_quality_snapshots
-- (`scripts/batch_runner.py`, `RETENTION_ORDERBOOK_FEATURES_DAYS`).
--
-- Schema choice: we store both _mean and _max for imbalance / spread so
-- the error model can see the average regime AND the worst tick in the
-- minute (a single 200 bps spread spike is a different signal than a
-- sustained 50 bps regime). microprice is a single _mean because the
-- deviation captures the variance signal already.
--
-- Source-of-truth note: book_quality_snapshots already exists from
-- migration 005 (no UNIQUE constraint — duplicate (market, token, ms)
-- rows are possible). The rollup tolerates dups because it averages
-- via SUM/COUNT; idempotency comes from the (market_id, token_id,
-- bucket_ts) PK + ON CONFLICT.

BEGIN;

CREATE TABLE IF NOT EXISTS orderbook_features_minute (
    market_id                  VARCHAR(100) NOT NULL,
    token_id                   VARCHAR(100) NOT NULL,
    bucket_ts                  TIMESTAMPTZ NOT NULL,    -- truncated to the minute
    -- Aggregated features over the minute (NULL when n_snapshots = 0):
    depth_imbalance_mean       NUMERIC(8,5),            -- ∈ [-1, +1]
    depth_imbalance_max        NUMERIC(8,5),            -- worst |skew| observed (signed)
    spread_bps_mean            NUMERIC(10,2),
    spread_bps_max             NUMERIC(10,2),
    microprice_mean            NUMERIC(10,6),
    microprice_deviation_mean  NUMERIC(10,6),
    n_snapshots                INTEGER NOT NULL,        -- raw rows rolled up
    PRIMARY KEY (market_id, token_id, bucket_ts)
);

-- Hot read path: feature_store.get_orderbook_features_asof()
-- does `WHERE token_id = $1 AND bucket_ts <= $2 ORDER BY bucket_ts DESC LIMIT 1`
-- — this index is the exact match.
CREATE INDEX IF NOT EXISTS idx_obfm_token_time
    ON orderbook_features_minute (token_id, bucket_ts DESC);

-- Secondary: retention sweep + ops queries by market.
CREATE INDEX IF NOT EXISTS idx_obfm_market_time
    ON orderbook_features_minute (market_id, bucket_ts DESC);

-- Defensive: re-assert book_quality_snapshots exists (it was created in
-- migration 005; this is a no-op if 005 ran first but keeps 018 a
-- self-contained source-of-truth for the raw feed if a fresh DB is
-- bootstrapped from a partial migration set).
CREATE TABLE IF NOT EXISTS book_quality_snapshots (
    id                      BIGSERIAL PRIMARY KEY,
    market_id               TEXT NOT NULL,
    token_id                TEXT NOT NULL,
    book_age_ms             INTEGER,
    spread_bps              NUMERIC(12,4),
    depth_top_levels        JSONB NOT NULL DEFAULT '{}'::jsonb,
    mid_price               NUMERIC(12,6),
    best_bid                NUMERIC(12,6),
    best_ask                NUMERIC(12,6),
    gap_detected            BOOLEAN NOT NULL DEFAULT FALSE,
    source_timestamp        TIMESTAMPTZ,
    observed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_reference           JSONB NOT NULL DEFAULT '{}'::jsonb,
    economic_model_version  TEXT NOT NULL DEFAULT 'v1.0.0'
);

CREATE INDEX IF NOT EXISTS book_quality_snapshots_recent_idx
    ON book_quality_snapshots (observed_at DESC);

CREATE INDEX IF NOT EXISTS book_quality_snapshots_market_token_idx
    ON book_quality_snapshots (market_id, token_id, observed_at DESC);

COMMIT;
