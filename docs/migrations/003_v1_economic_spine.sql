BEGIN;

CREATE TABLE IF NOT EXISTS v1_label_invalidations (
    id BIGSERIAL PRIMARY KEY,
    target_table TEXT NOT NULL,
    target_id TEXT NOT NULL,
    invalidated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason TEXT NOT NULL,
    previous_economic_model_version TEXT,
    new_economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    raw_reference JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE paper_trades
    ADD COLUMN IF NOT EXISTS strategy_track TEXT NOT NULL DEFAULT 'leader_swing',
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS size_shares NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS exit_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS spread_cost_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS slippage_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS net_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS fill_audit JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE decision_log
    ADD COLUMN IF NOT EXISTS strategy_track TEXT,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS signal_audit JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS learning_invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS learning_invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT;

ALTER TABLE positions_reconstructed
    ADD COLUMN IF NOT EXISTS size_shares NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS exit_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS net_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT;

CREATE TABLE IF NOT EXISTS fee_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    fee_enabled BOOLEAN NOT NULL,
    fee_rate NUMERIC NOT NULL,
    maker_fee_rate NUMERIC NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    compatibility JSONB NOT NULL DEFAULT '{}'::jsonb,
    economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    UNIQUE (market_id, token_id, captured_at, source)
);

CREATE TABLE IF NOT EXISTS signal_audits (
    id BIGSERIAL PRIMARY KEY,
    decision_id BIGINT,
    strategy_track TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    accepted BOOLEAN NOT NULL,
    reject_reason TEXT,
    expected_edge_usdc NUMERIC,
    expected_net_edge_usdc NUMERIC,
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_assumptions JSONB NOT NULL DEFAULT '{}'::jsonb,
    book_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    fee_snapshot_id BIGINT REFERENCES fee_snapshots(id),
    economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
