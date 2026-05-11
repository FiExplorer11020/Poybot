-- ============================================================================
-- 025_live_orders_intent_id.sql
--
-- Round 7 (The Front Door) / Phase 7.B — wire live_orders to mempool intents.
--
-- Audit reference: docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 4 — every live
-- order fired through the pre-signed pool ought to carry the mempool
-- intent_id that triggered it, so the reconciler can join a CLOB fill
-- back to the original LeaderIntent. Without this column, the live
-- order/fill audit trail has no link to the pre-confirmation path; a
-- shadow-vs-live PnL comparison would need to fall back to fuzzy
-- (wallet, market, time-window) matching.
--
-- Adds:
--   * `intent_id UUID NULL` to live_orders.
--   * `FOREIGN KEY (intent_id) REFERENCES mempool_observations(intent_id)`
--     so the link is enforced at the DB level (and the reconciler can't
--     dangle an FK pointing at a vanished observation).
--
-- The column is NULL-able because the legacy FOLLOW codepath in
-- src/engine/live_trader.py does NOT touch the mempool; only the
-- prefill IntentRouter populates the column. Existing rows from before
-- this migration stay NULL, no backfill needed.
--
-- ----------------------------------------------------------------------------
-- ON DELETE CASCADE policy:
--
-- intent_id → mempool_observations.intent_id uses ON DELETE SET NULL
-- (NOT cascade). The retention sweep on mempool_observations runs at
-- 30 d; live_orders rows live longer (the live audit retention is
-- governed separately under audit policy, typically 180 d). When the
-- observation row is purged we keep the live_order but with a NULL
-- intent_id — the row's other audit columns (clob_order_id, requested_*,
-- attempt_index, error_message) remain useful in their own right.
--
-- ============================================================================

BEGIN;

ALTER TABLE live_orders
    ADD COLUMN IF NOT EXISTS intent_id UUID;

-- ON DELETE SET NULL: observation retention is 30 d; live_orders may
-- live longer for audit. We keep the row but null out the dangling
-- reference. Wave-2 / operator step: confirm the audit policy for
-- live_orders before changing the SET NULL clause to CASCADE.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name  = 'live_orders'
          AND constraint_name = 'live_orders_intent_id_fkey'
    ) THEN
        ALTER TABLE live_orders
            ADD CONSTRAINT live_orders_intent_id_fkey
            FOREIGN KEY (intent_id)
            REFERENCES mempool_observations (intent_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- Lookup index for the reconciler's "find every live_order that was
-- triggered by intent X" query. Partial because the column is NULL
-- for every legacy row + every FOLLOW-codepath row; a full index
-- would waste pages on the dominant NULL value.
CREATE INDEX IF NOT EXISTS idx_live_orders_intent_id
    ON live_orders (intent_id)
    WHERE intent_id IS NOT NULL;

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. The new column is NULL for every existing live_orders row. The
--      next prefill-path fire (post-deploy) writes the first non-null
--      value.
--
--   2. The corresponding writer code is in
--      src/execution/prefill/intent_router.py — Wave-2 plumbs
--      intent_id through the OrderManager.place_for_position path so
--      the INSERT into live_orders carries it. The legacy FOLLOW
--      codepath in src/engine/live_trader.py is unchanged and
--      continues to write NULL.
--
--   3. No retention change here — live_orders retention follows the
--      live_trades audit policy (typically 180 d) unaffected by the
--      30 d mempool_observations sweep. The ON DELETE SET NULL clause
--      handles the timing mismatch cleanly.
--
--   4. Dashboards: a R7 panel can join
--        mempool_observations m
--          LEFT JOIN live_orders o ON o.intent_id = m.intent_id
--      to surface the full pre-confirmation lifecycle: intent_received
--      → fired → confirmed → fill price → PnL.
-- ============================================================================
