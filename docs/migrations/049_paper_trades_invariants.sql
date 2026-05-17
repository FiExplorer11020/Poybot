-- ============================================================================
-- 049_paper_trades_invariants.sql
--
-- Audit 2026-05-17 (QW5) — invariant CHECK constraints on paper_trades.
--
-- The ground-truth audit on May 17, 2026 surfaced trades with structurally
-- impossible state: size_usdc=0, fee_paid_usdc<0, and rows with status
-- values that no part of the code base ever writes. None of these were
-- caught because the table has no column-level invariants. Adding the
-- constraints now both (a) blocks future malformed inserts at the DB
-- layer and (b) lets the upcoming "invalidate trade #1, #2" cleanup
-- write `status='audit_invalidated'` without expanding an enum check
-- separately.
--
-- INVARIANTS
--   size_usdc        > 0  — a zero-size trade is a logic bug, not a
--                            valid position. Existing code asserts this
--                            in paper_trader.open_trade (MIN_POSITION_USDC
--                            gate) but the DB had no backstop.
--   fee_paid_usdc   >= 0  — fees can be 0 (zero-fee market types like
--                            sports / geopolitical) but never negative.
--   status enum     — the 5 strings that paper_trader actually writes,
--                            plus `audit_invalidated` reserved for the
--                            audit-cleanup migration that will retro-fix
--                            paper_trades #1 + #2 (closed at phantom-win
--                            exits via stale book cache).
--
-- IDEMPOTENCY
--   Each constraint is wrapped in a `DO $$ ... EXCEPTION` block so
--   re-applying the migration is a no-op. PostgreSQL has no
--   `IF NOT EXISTS` for table constraints, so this is the standard
--   idiom (used by 042_markets_resolved_outcome.sql and earlier).
--
-- NOTES
--   * If existing rows violate a new constraint, the ALTER TABLE will
--     fail. Run `SELECT count(*) FROM paper_trades WHERE size_usdc <= 0;`
--     etc. before applying. The audit prep pass (separate task) is
--     expected to repair pre-existing rows.
--   * `audit_invalidated` is added pre-emptively here so the cleanup
--     migration that depends on it does not need to expand the enum
--     check.
-- ============================================================================

DO $$
BEGIN
    ALTER TABLE paper_trades
        ADD CONSTRAINT paper_trades_size_positive
        CHECK (size_usdc > 0);
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN
        RAISE NOTICE 'paper_trades table missing — skipping size constraint';
END$$;

DO $$
BEGIN
    ALTER TABLE paper_trades
        ADD CONSTRAINT paper_trades_fee_nonneg
        CHECK (fee_paid_usdc IS NULL OR fee_paid_usdc >= 0);
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN
        RAISE NOTICE 'paper_trades table missing — skipping fee constraint';
END$$;

DO $$
BEGIN
    ALTER TABLE paper_trades
        ADD CONSTRAINT paper_trades_status_enum
        CHECK (status IN (
            'open',
            'closed',
            'expired',
            'cancelled',
            'audit_invalidated'
        ));
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN
        RAISE NOTICE 'paper_trades table missing — skipping status constraint';
END$$;
