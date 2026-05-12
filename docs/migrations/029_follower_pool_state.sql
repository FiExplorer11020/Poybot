-- ============================================================================
-- 029_follower_pool_state.sql
--
-- Round 9 (The Web) — Kalman state per (leader, pool_class).
--
-- Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.2 + § 4
--
-- Per spec § 3.2, the FollowerPoolKalman holds a 3-state vector
--   x = [pool_size_usdc, recent_response_pct, decay_rate]
-- updated on every leader-trade event. Two tables:
--
--   follower_pool_state         current state (one row per pool per
--                               leader, primary key (leader, pool))
--   follower_pool_state_history append-only snapshot for as-of training
--                               reads and drift forensics
--
-- The history pattern matches market_features_history /
-- leader_strategy_history (R8) and risk_config_history: a snapshot row
-- per Kalman update, never UPDATEd. The cross-cutting architecture
-- principle is "no destructive in-place state" so backtests can ask
-- "what was the Kalman state at 2026-04-15T12:00Z?" without replaying.
--
-- state_cov_json holds the 3×3 Kalman covariance matrix flattened to a
-- 9-element list. It's used both for forecast CI computation and for
-- diagnostics (innovation magnitude vs covariance trace).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS follower_pool_state (
    leader_wallet         VARCHAR(100)  NOT NULL,
    pool_class            VARCHAR(40)   NOT NULL,
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    pool_size_usdc        NUMERIC(20,2),
    recent_response_pct   NUMERIC(7,5),
    decay_rate            NUMERIC(10,6),
    state_cov_json        JSONB,
    n_observations        INTEGER       NOT NULL DEFAULT 0,
    last_innovation       NUMERIC(20,4),
    PRIMARY KEY (leader_wallet, pool_class)
);

CREATE INDEX IF NOT EXISTS idx_fps_updated
    ON follower_pool_state (updated_at DESC);


CREATE TABLE IF NOT EXISTS follower_pool_state_history (
    id                    BIGSERIAL     PRIMARY KEY,
    leader_wallet         VARCHAR(100)  NOT NULL,
    pool_class            VARCHAR(40)   NOT NULL,
    snapshot_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    pool_size_usdc        NUMERIC(20,2),
    recent_response_pct   NUMERIC(7,5),
    decay_rate            NUMERIC(10,6),
    state_cov_json        JSONB,
    n_observations        INTEGER       NOT NULL DEFAULT 0,
    last_innovation       NUMERIC(20,4)
);

CREATE INDEX IF NOT EXISTS idx_fps_history_lookup
    ON follower_pool_state_history (leader_wallet, pool_class, snapshot_at DESC);

COMMENT ON TABLE follower_pool_state IS
    'Round 9 (The Web) — current Kalman state per (leader, pool_class). '
    'One row per pair. See docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.2.';

COMMENT ON TABLE follower_pool_state_history IS
    'Round 9 (The Web) — append-only snapshot of every Kalman update. '
    'Read with snapshot_at <= asof_ts ORDER BY snapshot_at DESC LIMIT 1 '
    'for point-in-time training. Mirrors the as-of pattern of '
    'market_features_history / leader_strategy_history.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The Kalman update path (src/follower_volume/kalman.py) writes
--      both tables on every observation. Live state to follower_pool_state
--      via UPSERT; snapshot to follower_pool_state_history via INSERT.
--   2. Rollback:
--        DROP TABLE follower_pool_state_history;
--        DROP TABLE follower_pool_state;
-- ============================================================================
