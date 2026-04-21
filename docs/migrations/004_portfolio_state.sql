-- 004_portfolio_state.sql
-- Persistent portfolio state + equity time-series so capital survives restarts
-- and the dashboard can render a real equity curve including unrealized PnL.

BEGIN;

-- Singleton-style state row (id=1). Keep it as a normal table for simplicity;
-- the engine layer enforces single-row semantics.
CREATE TABLE IF NOT EXISTS portfolio_state (
    id                      INTEGER     PRIMARY KEY,
    capital                 NUMERIC(20,2) NOT NULL,
    peak_capital            NUMERIC(20,2) NOT NULL,
    realized_pnl_cum        NUMERIC(20,2) NOT NULL DEFAULT 0,
    consecutive_losses      INTEGER     NOT NULL DEFAULT 0,
    open_positions          INTEGER     NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Time-series of portfolio equity. Written by the paper trader on every
-- close and periodically from the monitor loop.
CREATE TABLE IF NOT EXISTS portfolio_equity (
    time                TIMESTAMPTZ NOT NULL,
    capital             NUMERIC(20,2) NOT NULL,  -- free cash + closed PnL (realized-only bankroll)
    equity              NUMERIC(20,2) NOT NULL,  -- capital + unrealized PnL (mark-to-market total)
    unrealized_pnl      NUMERIC(20,2) NOT NULL DEFAULT 0,
    realized_pnl_cum    NUMERIC(20,2) NOT NULL DEFAULT 0,
    open_positions      INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (time)
);

CREATE INDEX IF NOT EXISTS portfolio_equity_time_idx
    ON portfolio_equity (time DESC);

COMMIT;
