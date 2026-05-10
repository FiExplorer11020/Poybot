-- =====================================================================
-- Migration 008 — Live Trading tables (S2.6)
-- =====================================================================
-- Adds two tables backing the LiveTrader:
--   * live_trades  — position-level mirror of paper_trades, with the
--                    extra fields we need to map a position to actual
--                    Polymarket CLOB orders (clob_order_id, tx_hash,
--                    status incl. the new 'shadow' state used while
--                    LIVE_TRADING_DRY_RUN=true).
--   * live_orders  — order-level audit trail. A single position can map
--                    to several CLOB orders if the limit order has to be
--                    cancelled and repriced; this table captures every
--                    attempt with its state machine for forensics.
--
-- Idempotent: every CREATE uses IF NOT EXISTS, the migration row uses
-- ON CONFLICT DO NOTHING.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- live_trades
-- ---------------------------------------------------------------------
-- Mirrors paper_trades for code reuse (PaperTrader and LiveTrader share
-- a lot of the same close-trigger logic) and adds:
--   * clob_order_id        — the order that produced the entry fill (if
--                            multiple fills, the FIRST one — full lineage
--                            is in live_orders).
--   * exit_clob_order_id   — order that produced the exit fill, NULL
--                            until close_trade.
--   * tx_hash              — settlement tx on Polygon (USDC <-> shares)
--                            once Polymarket batches & settles.
--   * status — extended vs paper_trades:
--       'shadow'  = no order ever sent to CLOB; LIVE_TRADING_DRY_RUN=true
--       'pending' = order placed, waiting for fill
--       'open'    = filled, position live
--       'closed'  = exit filled, pnl realized
--       'failed'  = order rejected / max retries reached
--       'canceled'= operator-initiated cancel before any fill
CREATE TABLE IF NOT EXISTS live_trades (
    id                    SERIAL PRIMARY KEY,
    opened_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at             TIMESTAMPTZ,
    market_id             VARCHAR(100) NOT NULL,
    token_id              VARCHAR(100) NOT NULL,
    direction             VARCHAR(3) NOT NULL,            -- 'yes' / 'no'
    entry_price           NUMERIC(10,6),                  -- NULL while pending/shadow
    exit_price            NUMERIC(10,6),
    size_usdc             NUMERIC(20,2) NOT NULL,         -- requested size
    filled_size_usdc      NUMERIC(20,2),                  -- actual filled (may < size_usdc on partial)
    pnl_usdc              NUMERIC(20,2),
    fee_paid_usdc         NUMERIC(20,2),
    strategy              VARCHAR(10) NOT NULL,           -- 'follow' / 'fade'
    leader_wallet         VARCHAR(100),
    leader_context        JSONB,
    confidence            NUMERIC(5,4),
    status                VARCHAR(10) NOT NULL DEFAULT 'pending',
    close_reason          VARCHAR(50),
    -- CLOB linkage
    clob_order_id         VARCHAR(100),                   -- entry order id from CLOB
    exit_clob_order_id    VARCHAR(100),
    tx_hash               VARCHAR(100),                   -- entry settlement tx
    exit_tx_hash          VARCHAR(100),
    -- Cycle tracking
    placement_attempts    INT NOT NULL DEFAULT 0,         -- how many cancel/reprice loops
    -- Versioning
    economic_model_version VARCHAR(20),
    strategy_track        VARCHAR(20)
);

CREATE INDEX IF NOT EXISTS idx_live_trades_open
    ON live_trades (status)
    WHERE status IN ('pending', 'open');
CREATE INDEX IF NOT EXISTS idx_live_trades_market
    ON live_trades (market_id);
CREATE INDEX IF NOT EXISTS idx_live_trades_leader
    ON live_trades (leader_wallet);
CREATE INDEX IF NOT EXISTS idx_live_trades_clob_order
    ON live_trades (clob_order_id)
    WHERE clob_order_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- live_orders
-- ---------------------------------------------------------------------
-- Per-CLOB-order audit trail. One live_trade -> 1..N live_orders
-- (cancel/reprice loops produce multiple rows; partial fills produce
-- one row each — the CLOB returns separate trade events per fill).
--
-- order_role
--   'entry' = order intended to OPEN the position
--   'exit'  = order intended to CLOSE the position
-- order_state (canonical machine):
--   'placed'   -> POST /order succeeded, order is on the book
--   'filled'   -> fully filled
--   'partial'  -> partially filled, then cancelled or expired
--   'canceled' -> cancelled before any fill (timeout reprice or operator)
--   'rejected' -> CLOB refused (insufficient liquidity, malformed sig, etc.)
--   'expired'  -> CLOB-side expiry (GTD)
--   'shadow'   -> never sent (LIVE_TRADING_DRY_RUN=true)
CREATE TABLE IF NOT EXISTS live_orders (
    id                  BIGSERIAL PRIMARY KEY,
    live_trade_id       INTEGER NOT NULL REFERENCES live_trades (id) ON DELETE CASCADE,
    placed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_at        TIMESTAMPTZ,
    order_role          VARCHAR(8) NOT NULL,    -- 'entry' / 'exit'
    order_state         VARCHAR(10) NOT NULL,   -- see comment above
    clob_order_id       VARCHAR(100),           -- NULL for shadow rows
    side                VARCHAR(4) NOT NULL,    -- 'BUY' / 'SELL'
    requested_price     NUMERIC(10,6) NOT NULL, -- price we sent to CLOB
    requested_size      NUMERIC(20,4) NOT NULL, -- shares we asked for
    filled_size         NUMERIC(20,4) NOT NULL DEFAULT 0,
    filled_avg_price    NUMERIC(10,6),
    fee_paid_usdc       NUMERIC(20,2),
    error_message       TEXT,                    -- populated when 'rejected'
    -- Reprice attempt counter within the trade. 0 = first try.
    attempt_index       INT NOT NULL DEFAULT 0,
    raw_clob_response   JSONB                    -- whole response for forensics
);

CREATE INDEX IF NOT EXISTS idx_live_orders_trade
    ON live_orders (live_trade_id);
CREATE INDEX IF NOT EXISTS idx_live_orders_state
    ON live_orders (order_state);
CREATE INDEX IF NOT EXISTS idx_live_orders_clob
    ON live_orders (clob_order_id)
    WHERE clob_order_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- Migration tracking
-- ---------------------------------------------------------------------
INSERT INTO schema_migrations (version) VALUES (8) ON CONFLICT DO NOTHING;

COMMIT;
