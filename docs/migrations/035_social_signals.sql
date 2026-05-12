-- ============================================================================
-- 035_social_signals.sql
--
-- Round 12 (The Periphery) / Off-Chain Pre-Signal + Multi-Venue.
--
-- Audit reference: docs/ROUND_12_SOCIAL_AND_CROSS_MARKET.md § 3 + § 5.
--
-- The raw + classified social signal ledger. Each row is one observed
-- post on X / Telegram / Discord, with the NLP classifier's verdict
-- attached and (optionally) a resolved wallet pointer for the leaders
-- whose handles we have already mapped.
--
-- Read surface: :func:`src.profiler.feature_store.get_social_signals_asof`
-- (per-wallet aggregates for R8 features H. SOCIAL slots).
--
-- Cardinality target: < 5k rows/month sustained (top-200 leaders × low-
-- intensity posting); no partitioning needed at this scale.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS social_signals (
    signal_id         BIGSERIAL PRIMARY KEY,
    -- Originating platform. Constrained values:
    --   x         — Twitter / X firehose.
    --   telegram  — public-channel listener.
    --   discord   — public-channel REST poller.
    source            VARCHAR(20) NOT NULL,
    -- Platform-native handle of the author. Lower-cased on insert
    -- ('@alice' → 'alice') so the asof reader can match without case-
    -- folding at read time.
    author_handle     VARCHAR(100) NOT NULL,
    -- Polymarket wallet, NULL if the handle is unresolved. The
    -- resolution map lives in `cross_market_operators` (migration 036).
    resolved_wallet   VARCHAR(100),
    posted_at         TIMESTAMPTZ NOT NULL,
    text              TEXT NOT NULL,
    -- NLP classifier output (see src/social/nlp_classifier.py).
    --   entry_signal  — author signalled a position open.
    --   exit_signal   — author signalled a position close.
    --   noise         — everything else; filtered out of feature derivation.
    intent            VARCHAR(20) NOT NULL,
    intent_confidence NUMERIC(5, 4) NOT NULL,
    -- Best-effort extracted market identifier (URL slug or token id),
    -- and the inferred direction (yes/no). Both NULL when the classifier
    -- can't parse them — the feature deriver treats the signal as
    -- direction-less in that case.
    parsed_market     VARCHAR(100),
    parsed_direction  VARCHAR(4),
    -- Raw upstream payload kept for forensic + retraining purposes.
    raw_payload       JSONB
);

-- Hot read path: per-handle latest N signals (used by feature deriver
-- + tweet-to-trade lag calc).
CREATE INDEX IF NOT EXISTS idx_ss_author_time
    ON social_signals (author_handle, posted_at DESC);

-- Per-wallet read path. Partial index keeps the size bounded since the
-- majority of signals are unresolved at any given moment.
CREATE INDEX IF NOT EXISTS idx_ss_wallet_time
    ON social_signals (resolved_wallet, posted_at DESC)
    WHERE resolved_wallet IS NOT NULL;

COMMIT;
