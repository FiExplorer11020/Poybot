-- ============================================================================
-- 050_close_audit_log.sql
--
-- Pillar 5 (audit 2026-05-17) — append-only ledger of every paper trade close.
--
-- Each paper_trade close inserts ONE row capturing:
--   * which oracle source resolved the exit price (book/gamma/resolved/fail/fallback)
--   * the snapshot evidence (bid/ask, last_trade_price, resolved_outcome)
--   * the leader's last known state on this market
--   * the decision payload that opened the trade
--
-- This enables Pillar 2 (the reconciliation pass) to replay any close
-- and verify the realised PnL was computed against fresh, faithful data.
-- It also surfaces operator-visible patterns: spike in oracle_source='fail'
-- means the live data pipeline is degrading; spike in 'fallback' means
-- some legacy code path is still skipping the oracle.
--
-- IDEMPOTENCY
--   IF NOT EXISTS on the table + each CREATE INDEX. Safe to re-apply.
--
-- NOTES
--   * close_reason is VARCHAR(50) to match paper_trades.close_reason.
--   * oracle_source is constrained to the canonical set so a future
--     refactor that introduces a new source is forced to widen the
--     enum here too (anti-drift gate).
--   * Snapshot columns are NULL when not applicable for the chosen
--     oracle source (a 'book' close has book_snapshot but NULL
--     gamma_snapshot etc.) — keeps the row narrow.
--   * leader_state and decision_payload are best-effort and may be
--     NULL even on successful closes — see _snapshot_leader_state.
-- ============================================================================

CREATE TABLE IF NOT EXISTS close_audit_log (
    id BIGSERIAL PRIMARY KEY,
    paper_trade_id INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE CASCADE,
    closed_at TIMESTAMPTZ NOT NULL,
    close_reason VARCHAR(50) NOT NULL,
    oracle_source VARCHAR(20) NOT NULL,
    exit_price NUMERIC(10, 6) NOT NULL,
    computed_pnl_usdc NUMERIC(20, 2) NOT NULL,
    book_snapshot JSONB,
    gamma_snapshot JSONB,
    resolution_snapshot JSONB,
    leader_state JSONB,
    decision_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_close_audit_log_paper_trade_id
    ON close_audit_log (paper_trade_id);

CREATE INDEX IF NOT EXISTS idx_close_audit_log_closed_at
    ON close_audit_log (closed_at DESC);

CREATE INDEX IF NOT EXISTS idx_close_audit_log_oracle_source
    ON close_audit_log (oracle_source);

DO $$
BEGIN
    ALTER TABLE close_audit_log
        ADD CONSTRAINT close_audit_log_oracle_source_enum
        CHECK (oracle_source IN (
            'book',
            'gamma',
            'resolved',
            'fail',
            'fallback'
        ));
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN
        RAISE NOTICE 'close_audit_log table missing — skipping oracle_source constraint';
END$$;
