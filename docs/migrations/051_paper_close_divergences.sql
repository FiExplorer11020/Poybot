-- ============================================================================
-- 051_paper_close_divergences.sql
--
-- Pillar 2 (audit 2026-05-17) — nightly Gamma reconciliation ledger.
--
-- Each row records a divergence between what we wrote into
-- paper_trades.pnl_usdc at close time and the truth Polymarket would
-- pay out (or, when the market is still open, the fact that the close
-- was premature). The reconcile_closed_trades nightly job (04:00 UTC,
-- see scripts/reconciliation.py) computes the theoretical PnL from
-- markets.resolved_outcome / Gamma /markets and UPSERTs into this
-- table.
--
-- Without this table, the +39,784 USDC of phantom BTC PnL from the
-- 2026-05-17 ground-truth audit would have stayed invisible. The unique
-- index on paper_trade_id keeps the table compact: one row per trade,
-- updated whenever a fresh reconciliation pass finds a different
-- delta.
--
-- IDEMPOTENCY
--   IF NOT EXISTS on the table + each index. Safe to re-apply.
--
-- FLAG TAXONOMY
--   fake_win                 db_pnl > 0 AND db_pnl exceeds truth_pnl + tolerance
--                             (we booked a win that never landed)
--   fake_loss                db_pnl < -tolerance AND truth_pnl >= -tolerance
--                             (we booked a loss the position actually won/broke even)
--   still_open_in_reality    Gamma reports closed=false but DB.closed_at is in the past
--                             (we closed before Polymarket resolved)
--   premature_close          DB.closed_at predates Gamma's resolution but truth_pnl
--                             differs by more than tolerance from db_pnl
--   match_within_tolerance   never inserted — kept here for documentation only
-- ============================================================================

CREATE TABLE IF NOT EXISTS paper_close_divergences (
    id BIGSERIAL PRIMARY KEY,
    paper_trade_id INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE CASCADE,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ NOT NULL,
    market_id VARCHAR(100) NOT NULL,
    direction VARCHAR(3) NOT NULL,
    db_pnl_usdc NUMERIC(20, 2) NOT NULL,
    truth_pnl_usdc NUMERIC(20, 2) NOT NULL,
    delta_usdc NUMERIC(20, 2) NOT NULL,
    db_exit_price NUMERIC(10, 6) NOT NULL,
    truth_exit_price NUMERIC(10, 6),
    gamma_outcome VARCHAR(10),
    gamma_snapshot JSONB,
    flag VARCHAR(30) NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_close_divergences_paper_trade_id
    ON paper_close_divergences (paper_trade_id);

CREATE INDEX IF NOT EXISTS idx_paper_close_divergences_detected_at
    ON paper_close_divergences (detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_close_divergences_flag
    ON paper_close_divergences (flag);

-- One active divergence row per trade. The reconciliation job uses
-- ON CONFLICT (paper_trade_id) DO UPDATE so a re-run with fresher Gamma
-- data simply refreshes the existing row instead of accumulating
-- duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_close_divergences_trade_uniq
    ON paper_close_divergences (paper_trade_id);
