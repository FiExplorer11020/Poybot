-- ============================================================================
-- 036_cross_market_operators.sql
--
-- Round 12 (The Periphery) — cross-venue identity resolution table.
--
-- Audit reference: docs/ROUND_12_SOCIAL_AND_CROSS_MARKET.md § 4.2 + § 5.
--
-- Maps a Polymarket wallet to the same operator's accounts on Kalshi /
-- Manifold / PredictIt + their X handle. Resolution is intentionally
-- manual-in-the-loop:
--
--   * `resolution_source = 'manual'`        — operator hand-curated.
--   * `resolution_source = 'profile_link'`  — public profile link
--                                              between addresses (high
--                                              confidence by default).
--   * `resolution_source = 'fingerprint'`   — automatic match via R8
--                                              strategy class + R11
--                                              microstructure signature
--                                              (LOWER confidence; needs
--                                              operator confirmation
--                                              before production use —
--                                              the daemon writes these
--                                              with `confidence < threshold`
--                                              and the readers filter
--                                              by `confidence >=
--                                              CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE`).
--
-- Read surface:
--   * :func:`src.profiler.feature_store.get_cross_market_operator_resolution`
--     — current resolution for a Polymarket wallet.
--   * :func:`src.profiler.feature_store.get_cross_market_features_asof`
--     — per-wallet aggregates joining this table with
--       `cross_market_positions` (migration 037).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS cross_market_operators (
    operator_id        BIGSERIAL PRIMARY KEY,
    polymarket_wallet  VARCHAR(100),
    kalshi_account     VARCHAR(100),
    manifold_handle    VARCHAR(100),
    predictit_account  VARCHAR(100),
    x_handle           VARCHAR(100),
    -- Provenance tag, see header.
    resolution_source  VARCHAR(40)   NOT NULL,
    -- Confidence ∈ [0, 1]. Manual + profile_link rows default to 1.0;
    -- fingerprint rows carry the matcher's raw score.
    confidence         NUMERIC(5, 4) NOT NULL,
    resolved_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    -- Operator notes (manual seed rationale, profile-link URL, etc.).
    notes              TEXT
);

-- Hot read path: pm wallet → operator id (joined with
-- `cross_market_positions` on operator_id).
CREATE INDEX IF NOT EXISTS idx_cmo_pm
    ON cross_market_operators (polymarket_wallet);

-- Per-venue index for cross-direction lookups (e.g. "given this Kalshi
-- account, who on Polymarket?").
CREATE INDEX IF NOT EXISTS idx_cmo_kalshi
    ON cross_market_operators (kalshi_account);

COMMIT;
