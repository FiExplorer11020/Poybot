-- 015_position_tracker_state.sql
-- Phase 2 Task C — Persistent PositionTracker state.
--
-- Audit reference: Red Flag #4 in docs/audit/01_data_inventory.md
--   "PositionTracker._open_positions is unbounded and lost on restart
--    with no DB warm-start."
--
-- The PositionTracker holds an in-memory FIFO queue of OpenPosition rows per
-- (wallet, market, token) key. Two problems before this migration:
--
--   1. Restart drops the entire queue → subsequent SELLs that should close
--      an existing OPEN are silently ignored (`_handle_sell` returns when the
--      key is missing), and complementary BUYs that should fire a merge exit
--      can't match anything → real PnL is lost.
--   2. The dict is unbounded — a long-lived process accumulates state for
--      every leader × market × token combination ever observed.
--
-- This table is the persistent shadow of `PositionTracker._open_positions`.
-- One row per OpenPosition still in `shares_remaining > 0`. PositionTracker:
--
--   * UPSERTs a row on every OPEN (including partial OPENs that mutate
--     size_usdc / shares_remaining / size_shares).
--   * DELETEs the row in the same transaction as the matching
--     positions_reconstructed INSERT on every CLOSE (sell / merge /
--     resolution) — atomicity is the point.
--   * Calls warm_start(conn) on engine boot to repopulate
--     _open_positions from this table.
--
-- The state_json catch-all column carries any tracker-internal field that
-- can't fit a typed column without ballooning the schema (today it stores
-- the dataclass round-trip; the typed columns are the canonical source of
-- truth for size / price / open_time).

BEGIN;

CREATE TABLE IF NOT EXISTS position_tracker_state (
    wallet_address    VARCHAR(100)  NOT NULL,
    market_id         VARCHAR(100)  NOT NULL,
    token_id          VARCHAR(100)  NOT NULL,
    direction         VARCHAR(3)    NOT NULL,         -- 'yes' or 'no'
    open_time         TIMESTAMPTZ   NOT NULL,
    entry_price       NUMERIC(10,6) NOT NULL,
    size_usdc         NUMERIC(20,2) NOT NULL,
    -- shares_remaining is the live closeable quantity; size_shares is the
    -- original entry size. Both are needed to faithfully reconstruct the
    -- OpenPosition dataclass — partial closes mutate shares_remaining only.
    size_shares       NUMERIC(30,10) NOT NULL,
    shares_remaining  NUMERIC(30,10) NOT NULL,
    -- fee rate frozen at open time. Reconstructed positions must use this,
    -- not the current markets.fee_rate_pct, so a fee-schedule change
    -- post-open doesn't retroactively rewrite the close PnL.
    fee_rate_pct      NUMERIC(10,6) NOT NULL DEFAULT 0,
    -- Catch-all for any tracker-internal state that doesn't deserve a typed
    -- column. Today: empty {} on every write. Reserved so we don't need a
    -- migration the day PositionTracker grows a field (e.g. funder address,
    -- merge-eligibility flag, …).
    state_json        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (wallet_address, market_id, token_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_pts_wallet
    ON position_tracker_state (wallet_address);
CREATE INDEX IF NOT EXISTS idx_pts_market
    ON position_tracker_state (market_id);
-- open_time index supports the eviction policy: when count exceeds
-- MAX_OPEN_POSITIONS_TRACKED we drop the oldest open by open_time.
CREATE INDEX IF NOT EXISTS idx_pts_open_time
    ON position_tracker_state (open_time);

COMMIT;
