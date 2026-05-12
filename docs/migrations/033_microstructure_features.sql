-- ============================================================================
-- 033_microstructure_features.sql
--
-- Round 11 (The Microscope) / Sub-Trade Order-Flow Intelligence.
--
-- Audit reference: docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md § 4.
--
-- Per-minute rollup of microstructure features derived from
-- clob_book_events (migration 032). Written by
-- :class:`src.microstructure.rollup.MicrostructureRollup` after the four
-- detectors (iceberg / spoof / OFI / place-to-fill timing) flush their
-- per-bucket accumulators.
--
-- This is the READ surface that the R8 strategy classifier (and any
-- downstream consumer) reads via
-- :func:`src.profiler.feature_store.get_microstructure_features_asof`.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS microstructure_features (
    market_id              VARCHAR(100) NOT NULL,
    token_id               VARCHAR(100) NOT NULL,
    bucket_ts              TIMESTAMPTZ NOT NULL,    -- truncated to MICROSTRUCTURE_ROLLUP_BUCKET_S (default 60s)

    -- A. ICEBERG DETECTION (per spec § 3.2.A)
    iceberg_orders_count   INTEGER,
    iceberg_total_size     NUMERIC(20, 2),

    -- B. SPOOF DETECTION (per spec § 3.2.B)
    spoof_orders_count     INTEGER,
    spoof_total_size       NUMERIC(20, 2),

    -- C. ORDER FLOW IMBALANCE (per spec § 3.2.C)
    -- Signed bid_size_delta - ask_size_delta over 5s rolling window.
    -- Positive = buy pressure; negative = sell. Per-minute aggregates
    -- expose both the regime (mean) and the extremes (max/min) — a
    -- single +0.8 spike is a different signal than a sustained +0.3.
    ofi_mean               NUMERIC(10, 4),
    ofi_max                NUMERIC(10, 4),
    ofi_min                NUMERIC(10, 4),
    ofi_std                NUMERIC(10, 4),

    PRIMARY KEY (market_id, token_id, bucket_ts)
);

-- Hot read path mirrors orderbook_features_minute (migration 018):
-- get_microstructure_features_asof() does
--   WHERE market_id = $1 AND token_id = $2 AND bucket_ts <= $3
--     AND bucket_ts >= $4
--   ORDER BY bucket_ts DESC LIMIT 1
-- — the PK already covers the (market, token, time) lookup; this
-- secondary index supports cross-market range scans (e.g. operator
-- queries "what was OFI across the top-10 markets at 12:00 yesterday").
CREATE INDEX IF NOT EXISTS idx_mf_bucket
    ON microstructure_features (bucket_ts DESC);

COMMIT;
