-- ============================================================================
-- 034_wallet_microstructure_signature.sql
--
-- Round 11 (The Microscope) / Sub-Trade Order-Flow Intelligence.
--
-- Audit reference: docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md § 4.
--
-- Per-wallet 30-day rolling microstructure signature. Written nightly by
-- :class:`src.microstructure.wallet_signature.WalletSignatureBatch`,
-- which iterates wallet_universe tier-0/1 wallets and derives the
-- signature from clob_book_events (migration 032) joined with
-- trades_observed for wallet attribution (since wallet_address is NULL
-- on placement events — see migration 032 § 3.1 caveat).
--
-- This is the per-wallet read surface for the R8 strategy classifier:
-- :func:`src.profiler.feature_store.get_wallet_microstructure_signature_asof`
-- reads it and feeds the values into the E/F microstructure slots of
-- :class:`LeaderFeatureExtractor` (R8 features.py).
--
-- The headline R8 acceptance criterion (R11 § 6) — ≥ 3 percentage points
-- accuracy improvement on the classifier after retraining with these
-- features — depends on this table being populated for tier-0/1 wallets
-- across a 30-day lookback window.
--
-- Cardinality:
--   * ~2,000 tier-0/1 wallets × 1 row/day = ~2,000 rows/day ≈ 60k/month.
--   * Steady state ~720k rows/year — trivial. No partitioning needed.
--   * Retention: keep all rows; the time series is intentionally short
--     (operator-tunable via batch_runner).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS wallet_microstructure_signature (
    wallet_address               VARCHAR(100) NOT NULL,
    rollup_at                    TIMESTAMPTZ NOT NULL,

    -- Cancel/fill ratio: n_cancellations / n_fills over the trailing 30d.
    -- High ratio (>5) → market-maker or spoofer behaviour pattern.
    -- Per spec § 3.2.E — tier-0/1 wallets only (tier-2 too high-cardinality).
    cancel_to_fill_ratio_30d     NUMERIC(8, 4),

    -- Iceberg score: count of iceberg detections normalised by n_orders.
    -- Per spec § 3.2.A — rolling 60s detector flagged this wallet's
    -- price-anchored refill behaviour.
    iceberg_score_30d            NUMERIC(8, 4),

    -- Spoof score: count of spoof detections normalised by n_orders.
    -- Per spec § 3.2.B — large+cancelled-fast+zero-fill orders, plus
    -- opposite-side repetition.
    spoof_score_30d              NUMERIC(8, 4),

    -- Place-to-fill timing distribution percentiles (in seconds).
    -- Per spec § 3.2.D — low p50 = aggressive taker; high p50 = patient
    -- maker. p99 catches the outlier "rests for an hour" pattern.
    place_to_fill_seconds_p50    NUMERIC(10, 4),
    place_to_fill_seconds_p99    NUMERIC(10, 4),

    -- Volume counters used both for normalisation (above scores) and
    -- as a cold-start guard (if n_orders_30d < 50 the signature is too
    -- thin to trust — readers can fall back to None on this gate).
    n_orders_30d                 INTEGER,
    n_fills_30d                  INTEGER,

    PRIMARY KEY (wallet_address, rollup_at)
);

-- Hot read path: get_wallet_microstructure_signature_asof() does
--   WHERE wallet_address = $1 AND rollup_at <= $2
--   ORDER BY rollup_at DESC LIMIT 1
-- The PK already covers (wallet, rollup_at); the index below supports
-- ops queries "most recently updated signatures across all wallets".
CREATE INDEX IF NOT EXISTS idx_wms_rollup_at
    ON wallet_microstructure_signature (rollup_at DESC);

COMMIT;
