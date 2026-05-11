-- ============================================================================
-- 022_chain_sync_state.sql
--
-- Round 6 (The Spine) / Phase 6.B — On-chain CLOB listener resume state.
--
-- Audit reference: docs/ROUND_6_THE_SPINE.md § 3.3 — the CLOBChainListener
-- needs to know which Polygon block it last processed so a restart can
-- resume from that point rather than re-subscribing from chain-head (which
-- would silently drop any events emitted while the listener was down).
--
-- This is a single-row table by design. There's only one listener instance
-- per environment; the row PK is a constant `singleton` so a buggy
-- restart-races-itself path INSERTs into the same row rather than creating
-- a parallel ghost cursor.
--
-- ----------------------------------------------------------------------------
-- Write semantics:
--
-- The listener writes this row on a tunable cadence:
--   * After every BATCH_COMMIT_BLOCKS blocks (default 50), OR
--   * After every BATCH_COMMIT_INTERVAL_S seconds (default 5)
-- whichever comes first. The transactional contract is:
--
--   BEGIN;
--     INSERT INTO trades_observed (..., block_number, tx_hash, log_index, source)
--     VALUES (...);
--     ...
--     INSERT INTO chain_sync_state (id, last_processed_block, last_updated_at)
--     VALUES ('singleton', $1, NOW())
--     ON CONFLICT (id) DO UPDATE
--       SET last_processed_block = EXCLUDED.last_processed_block,
--           last_updated_at      = EXCLUDED.last_updated_at;
--   COMMIT;
--
-- So the cursor advances ONLY after the batch of trades it covers is
-- durably committed. Crash mid-batch → next boot re-processes from the
-- previous cursor, and the UNIQUE INDEX from migration 021 handles the
-- replayed events as no-ops.
--
-- ----------------------------------------------------------------------------
-- Bootstrap:
--
-- On first boot of the listener, this table is empty. The listener:
--   1. Reads chain head via RPCClient.eth_getBlockByNumber('latest')
--   2. Starts subscribing from `head - CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS`
--      (default ~256 = ~8 min of history, enough to bridge a kubectl-rollout
--      window without re-decoding hours of unrelated traffic)
--   3. INSERTs the first row on the first successful batch commit
--
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS chain_sync_state (
    -- Constant 'singleton' — enforces single-row semantics.
    id                    VARCHAR(20)  PRIMARY KEY DEFAULT 'singleton',
    last_processed_block  BIGINT       NOT NULL,
    last_updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Distance from chain head at the moment we wrote this row. Used by
    -- the dashboard and the `polybot_chain_blocks_behind` Prometheus gauge.
    -- Nullable because the listener may not always have a fresh head reading.
    blocks_behind_at_write INTEGER,
    -- Free-form JSON for any future cursor metadata (e.g. per-event-type
    -- filters, replay-mode flag). Empty {} in steady state.
    metadata              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chain_sync_state_singleton CHECK (id = 'singleton')
);

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. The table is empty after this migration. The CLOBChainListener
--      INSERTs the first row on its first batch commit (see
--      src/onchain/clob_listener.py::_update_sync_state).
--
--   2. Recovery from a bad cursor (e.g. operator wants to replay the
--      last hour): manually UPDATE the row, then restart the listener.
--      The UNIQUE INDEX on (tx_hash, log_index) makes the replay safe.
--
--      UPDATE chain_sync_state
--      SET last_processed_block = <block N>,
--          last_updated_at = NOW()
--      WHERE id = 'singleton';
--
--   3. There is NO retention here — the table never grows beyond one row.
-- ============================================================================
