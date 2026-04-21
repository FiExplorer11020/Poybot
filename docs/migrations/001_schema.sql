-- 001_schema.sql — Polymarket Leader Intelligence Bot
-- PostgreSQL 15 (standard, no TimescaleDB)

-- Leaders identified via Falcon API
CREATE TABLE IF NOT EXISTS leaders (
    wallet_address      VARCHAR(100) PRIMARY KEY,
    falcon_score        NUMERIC(10,4),
    wallet360_json      JSONB,
    classification_json JSONB,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_refresh        TIMESTAMPTZ,
    on_watchlist        BOOLEAN DEFAULT TRUE,
    excluded            BOOLEAN DEFAULT FALSE,
    exclude_reason      VARCHAR(100)
);

-- Observed trades on leader-active markets
CREATE TABLE IF NOT EXISTS trades_observed (
    id                  BIGSERIAL PRIMARY KEY,
    time                TIMESTAMPTZ NOT NULL,
    market_id           VARCHAR(100) NOT NULL,
    token_id            VARCHAR(100) NOT NULL,
    wallet_address      VARCHAR(100) NOT NULL,
    side                VARCHAR(4) NOT NULL,
    price               NUMERIC(10,6) NOT NULL,
    size_usdc           NUMERIC(20,2) NOT NULL,
    source              VARCHAR(10) DEFAULT 'websocket',
    is_leader           BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet_time ON trades_observed (wallet_address, time);
CREATE INDEX IF NOT EXISTS idx_trades_market_time ON trades_observed (market_id, time);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades_observed (time);
CREATE INDEX IF NOT EXISTS idx_trades_leader ON trades_observed (is_leader) WHERE is_leader = TRUE;

-- Reconstructed position cycles (OPEN → CLOSE)
CREATE TABLE IF NOT EXISTS positions_reconstructed (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_address      VARCHAR(100) NOT NULL,
    market_id           VARCHAR(100) NOT NULL,
    token_id            VARCHAR(100) NOT NULL,
    direction           VARCHAR(3) NOT NULL,
    open_time           TIMESTAMPTZ NOT NULL,
    close_time          TIMESTAMPTZ,
    entry_price         NUMERIC(10,6) NOT NULL,
    exit_price          NUMERIC(10,6),
    size_usdc           NUMERIC(20,2) NOT NULL,
    pnl_usdc            NUMERIC(20,2),
    pnl_pct             NUMERIC(10,4),
    holding_period_s    INTEGER,
    close_method        VARCHAR(10)
);

CREATE INDEX IF NOT EXISTS idx_positions_wallet_time ON positions_reconstructed (wallet_address, open_time);
CREATE INDEX IF NOT EXISTS idx_positions_market_time ON positions_reconstructed (market_id, open_time);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions_reconstructed (close_time) WHERE close_time IS NULL;

-- Leader → Follower graph edges
CREATE TABLE IF NOT EXISTS follower_edges (
    id                  BIGSERIAL PRIMARY KEY,
    leader_wallet       VARCHAR(100) NOT NULL,
    follower_wallet     VARCHAR(100) NOT NULL,
    co_occurrences      INTEGER DEFAULT 0,
    hawkes_alpha_mu     NUMERIC(10,6),
    follow_probability  NUMERIC(5,4),
    follow_beta_a       NUMERIC(10,4) DEFAULT 1.0,
    follow_beta_b       NUMERIC(10,4) DEFAULT 1.0,
    avg_delay_s         NUMERIC(10,2),
    same_direction_rate NUMERIC(5,4),
    trapped_rate        NUMERIC(5,4),
    first_observed      TIMESTAMPTZ DEFAULT NOW(),
    last_observed       TIMESTAMPTZ,
    UNIQUE(leader_wallet, follower_wallet)
);

CREATE INDEX IF NOT EXISTS idx_edges_leader ON follower_edges (leader_wallet);
CREATE INDEX IF NOT EXISTS idx_edges_follower ON follower_edges (follower_wallet);

-- Behavioral profiles and error models per leader
CREATE TABLE IF NOT EXISTS leader_profiles (
    wallet_address          VARCHAR(100) PRIMARY KEY REFERENCES leaders(wallet_address),
    profile_json            JSONB NOT NULL DEFAULT '{}',
    error_model_phase       INTEGER DEFAULT 1,
    error_model_blob        BYTEA,
    profile_maturity        NUMERIC(5,4) DEFAULT 0,
    trades_observed         INTEGER DEFAULT 0,
    positions_resolved      INTEGER DEFAULT 0,
    last_updated            TIMESTAMPTZ
);

-- Market metadata
CREATE TABLE IF NOT EXISTS markets (
    market_id       VARCHAR(100) PRIMARY KEY,
    question        TEXT NOT NULL,
    category        VARCHAR(50),
    token_yes       VARCHAR(100),
    token_no        VARCHAR(100),
    end_date        TIMESTAMPTZ,
    volume_24h      NUMERIC(20,2),
    liquidity_score NUMERIC(10,4),
    active          BOOLEAN DEFAULT TRUE,
    fee_rate_pct    NUMERIC(5,4) DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Paper trades (virtual portfolio)
CREATE TABLE IF NOT EXISTS paper_trades (
    id              SERIAL PRIMARY KEY,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    market_id       VARCHAR(100) NOT NULL,
    token_id        VARCHAR(100) NOT NULL,
    direction       VARCHAR(3) NOT NULL,
    entry_price     NUMERIC(10,6) NOT NULL,
    exit_price      NUMERIC(10,6),
    size_usdc       NUMERIC(20,2) NOT NULL,
    pnl_usdc        NUMERIC(20,2),
    fee_paid_usdc   NUMERIC(20,2),
    strategy        VARCHAR(10) NOT NULL,
    leader_wallet   VARCHAR(100),
    leader_context  JSONB,
    confidence      NUMERIC(5,4),
    status          VARCHAR(10) DEFAULT 'open',
    close_reason    VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades (status) WHERE status = 'open';

-- Decision audit log
CREATE TABLE IF NOT EXISTS decision_log (
    id              BIGSERIAL PRIMARY KEY,
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leader_wallet   VARCHAR(100) NOT NULL,
    market_id       VARCHAR(100) NOT NULL,
    action          VARCHAR(10) NOT NULL,
    thompson_follow NUMERIC(5,4),
    thompson_fade   NUMERIC(5,4),
    kelly_fraction  NUMERIC(5,4),
    confidence      NUMERIC(5,4),
    reason          TEXT,
    outcome         VARCHAR(10)
);

CREATE INDEX IF NOT EXISTS idx_decisions_time ON decision_log (time);
CREATE INDEX IF NOT EXISTS idx_decisions_leader ON decision_log (leader_wallet);

-- Schema migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);
