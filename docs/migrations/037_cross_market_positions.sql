-- ============================================================================
-- 037_cross_market_positions.sql
--
-- Round 12 (The Periphery) — unified cross-venue position snapshots.
--
-- Audit reference: docs/ROUND_12_SOCIAL_AND_CROSS_MARKET.md § 4.3 + § 5.
--
-- One row per (operator_id, venue, market_id, snapshot_at) tuple.
-- Written by :class:`CrossMarketPositionAggregator` once per
-- `settings.CROSS_MARKET_POLL_INTERVAL_H` hours.
--
-- Read surface: :func:`src.profiler.feature_store.get_cross_market_features_asof`
-- (per-wallet aggregates for R8 cross-market features).
--
-- Cardinality target: ~10 resolved operators × 3 venues × ~20 positions
-- × 24 snapshots/day = ~14k rows/day initially. Steady-state ~150k rows
-- in the resolution-50 scenario — trivial; no partitioning.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS cross_market_positions (
    snapshot_id BIGSERIAL PRIMARY KEY,
    operator_id BIGINT       NOT NULL
        REFERENCES cross_market_operators(operator_id) ON DELETE CASCADE,
    -- Originating venue. Constrained values:
    --   polymarket  — control row (this is what we already know).
    --   kalshi      — Kalshi REST.
    --   manifold    — Manifold REST.
    --   predictit   — PredictIt market-data REST.
    venue       VARCHAR(20)  NOT NULL,
    market_id   VARCHAR(200) NOT NULL,
    -- Direction-encoded: 'yes', 'no', or for venue-specific sides
    -- (Kalshi binary), the venue's literal label.
    side        VARCHAR(10)  NOT NULL,
    size_usdc   NUMERIC(20, 2) NOT NULL,
    opened_at   TIMESTAMPTZ,
    closed_at   TIMESTAMPTZ,
    snapshot_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Hot read path: per-operator history (feeds the
-- `cross_venue_correlation` + `cross_venue_lag_s` features).
CREATE INDEX IF NOT EXISTS idx_cmp_operator_time
    ON cross_market_positions (operator_id, snapshot_at DESC);

-- Per-venue + market index for the same-event correlation calc.
CREATE INDEX IF NOT EXISTS idx_cmp_venue_market
    ON cross_market_positions (venue, market_id);

COMMIT;
