-- ============================================================================
-- 031_instrumental_events.sql
--
-- Round 10 (The Truth Test) — Instrumental events (natural experiments).
--
-- Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.1 + § 4.
--
-- One row per detected natural experiment that the 2SLS estimator can
-- use as an instrumental variable. Sources:
--
--   * NewsEventDetector       (event_type='news')  — NewsAPI ingestion
--   * OracleUpdateDetector    (event_type='oracle_update') — UMA contract logs via R6 RPC
--   * RelatedMarketResolver   (event_type='news', source='related_market') — hourly batch
--   * LeaderGasQuirkDetector  (event_type='gas_quirk') — weekly batch on R7 mempool data
--   * APIOutageWindowDetector (event_type='api_outage') — R6 coverage_reconciler output
--   * Funding event detector  (event_type='funding') — future operator-driven
--
-- The instruments are used by the 2SLS first-stage regression
-- (leader trade intensity on instruments) to identify the causal effect
-- of leader → follower in the second stage.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS instrumental_events (
    event_id              BIGSERIAL    PRIMARY KEY,
    -- Vocabulary: news|oracle_update|api_outage|funding|gas_quirk
    -- (Enforced by application code; column kept as VARCHAR for
    -- forward-compat with new instrument types added in R12/R13.)
    event_type            VARCHAR(40)  NOT NULL,
    -- Wall-clock time of the underlying real-world event.
    event_time            TIMESTAMPTZ  NOT NULL,
    -- Comma-separated list of market_ids the event is plausibly
    -- correlated with. NULL = "all markets" (e.g., API outage).
    -- Stored at VARCHAR(2000) which fits ~30 market_ids; longer
    -- lists belong in payload_json.
    affected_market_ids   VARCHAR(2000),
    -- Free-form payload: e.g. news headline + entities for 'news',
    -- block_number + oracle_id for 'oracle_update', wallet_address
    -- for 'gas_quirk', etc. Schema is intentionally flexible — the
    -- 2SLS first stage reads only the (event_type, event_time,
    -- affected_market_ids) triple.
    payload_json          JSONB,
    -- Where the row came from: 'newsapi' | 'newsapi_fixture' |
    -- 'oracle_logs' | 'related_market' | 'mempool_observations' |
    -- 'coverage_reconciler' | etc.
    source                VARCHAR(40)  NOT NULL,
    -- Detector confidence in [0, 1]. NewsEventDetector emits ~0.8
    -- for high-quality NER matches; OracleUpdateDetector always
    -- emits 1.0 (chain truth); related-market resolver computes
    -- a co-occurrence score in [0, 1].
    confidence            NUMERIC(5, 4) NOT NULL DEFAULT 1.0,
    -- When this row was inserted. Lets us measure detection lag:
    --   detection_lag = inserted_at - event_time
    -- which the daemon's instrumental_event_lag_seconds histogram tracks.
    inserted_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT instrumental_events_confidence_chk
        CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- Hot path #1: time-range query the 2SLS first stage uses.
CREATE INDEX IF NOT EXISTS idx_ie_time
    ON instrumental_events (event_time DESC);

-- Hot path #2: type-filtered queries (e.g. "all news events in window").
CREATE INDEX IF NOT EXISTS idx_ie_type_time
    ON instrumental_events (event_type, event_time DESC);

COMMENT ON TABLE instrumental_events IS
    'Round 10 (The Truth Test) — Natural experiments / instrumental '
    'variables for 2SLS causal estimation. See docs/'
    'ROUND_10_CAUSAL_INFERENCE.md § 3.1 + § 2.1.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The InstrumentRegistry (src/causal/instruments.py) writes here.
--   2. The 2SLS estimator (src/causal/iv_estimator.py) reads here.
--   3. Rollback: DROP TABLE instrumental_events CASCADE.
-- ============================================================================
