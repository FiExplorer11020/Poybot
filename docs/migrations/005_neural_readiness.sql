-- 005_neural_readiness.sql
-- V1 Neural Readiness persistence primitives.
--
-- These tables are additive. The first implementation can serve live readiness
-- from existing health/risk/activation data, while future workers persist
-- market belief states, transitions, and book quality snapshots here.

BEGIN;

CREATE TABLE IF NOT EXISTS market_belief_states (
    id                              BIGSERIAL PRIMARY KEY,
    market_id                       TEXT NOT NULL,
    strategy_track                  TEXT NOT NULL,
    current_state                   TEXT NOT NULL,
    belief_follow                   NUMERIC(8,6) NOT NULL DEFAULT 0,
    belief_fade                     NUMERIC(8,6) NOT NULL DEFAULT 0,
    belief_skip                     NUMERIC(8,6) NOT NULL DEFAULT 0,
    belief_no_go                    NUMERIC(8,6) NOT NULL DEFAULT 0,
    data_readiness_pct              NUMERIC(5,2) NOT NULL DEFAULT 0,
    first_position_readiness_pct    NUMERIC(5,2) NOT NULL DEFAULT 0,
    belief_stability_pct            NUMERIC(5,2) NOT NULL DEFAULT 0,
    portfolio_readiness_pct         NUMERIC(5,2) NOT NULL DEFAULT 0,
    v1_go_no_go_pct                 NUMERIC(5,2) NOT NULL DEFAULT 0,
    expected_gross_edge_bps         NUMERIC(12,4),
    expected_net_edge_bps           NUMERIC(12,4),
    oscillation_score               NUMERIC(8,6) NOT NULL DEFAULT 0,
    blockers                        JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_transition_reason          TEXT,
    economic_model_version          TEXT NOT NULL DEFAULT 'v1.0.0',
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, strategy_track)
);

CREATE INDEX IF NOT EXISTS market_belief_states_state_idx
    ON market_belief_states (current_state, updated_at DESC);

CREATE TABLE IF NOT EXISTS decision_state_transitions (
    id                      BIGSERIAL PRIMARY KEY,
    market_id               TEXT NOT NULL,
    strategy_track          TEXT NOT NULL,
    from_state              TEXT NOT NULL,
    to_state                TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    trigger_event_type      TEXT,
    trigger_event_ref       JSONB NOT NULL DEFAULT '{}'::jsonb,
    blockers_before         JSONB NOT NULL DEFAULT '[]'::jsonb,
    blockers_after          JSONB NOT NULL DEFAULT '[]'::jsonb,
    economic_model_version  TEXT NOT NULL DEFAULT 'v1.0.0',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS decision_state_transitions_recent_idx
    ON decision_state_transitions (created_at DESC);

CREATE INDEX IF NOT EXISTS decision_state_transitions_market_track_idx
    ON decision_state_transitions (market_id, strategy_track, created_at DESC);

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
