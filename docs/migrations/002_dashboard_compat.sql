-- 002_dashboard_compat.sql — Performance indexes for dashboard queries
-- Applied automatically by scripts/setup_db.py

-- Fast lookup: leader trades for activity feed and confidence engine readiness
CREATE INDEX IF NOT EXISTS idx_trades_leader_wallet
    ON trades_observed (wallet_address)
    WHERE is_leader = TRUE;

-- Fast lookup: open paper trades per market (exposure chart, circuit breaker)
CREATE INDEX IF NOT EXISTS idx_paper_market_open
    ON paper_trades (market_id)
    WHERE status = 'open';

-- Fast lookup: paper trades by open date (daily PnL, risk queries)
CREATE INDEX IF NOT EXISTS idx_paper_opened_date
    ON paper_trades (opened_at);

-- Fast lookup: pending decision outcomes (outcome update in close_trade)
CREATE INDEX IF NOT EXISTS idx_decisions_outcome_null
    ON decision_log (leader_wallet, market_id)
    WHERE outcome IS NULL;
