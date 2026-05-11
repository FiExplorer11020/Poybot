-- ============================================================================
-- 021_trades_observed_chain_extension.sql
--
-- Round 6 (The Spine) / Phase 6.B — On-chain CLOB listener integration.
--
-- Audit reference: docs/ROUND_6_THE_SPINE.md § 3.3 — the on-chain ingestion
-- path needs three extra columns on `trades_observed` so chain-decoded
-- events have a stable identity for cross-source dedup with REST/WS polls:
--
--   block_number  — the Polygon block this OrderFilled / OrdersMatched
--                   event was emitted in. Cheap to index, useful for
--                   "show me every trade in block N" debugging.
--   tx_hash       — the transaction hash containing the event. Together
--                   with log_index this uniquely identifies the on-chain
--                   trade — Postgres-side enforcement of the chain's own
--                   uniqueness contract.
--   log_index     — the per-tx index of the LOG event. A single tx can
--                   emit several OrderFilled events (a maker order
--                   matching multiple taker fills); log_index disambiguates.
--
-- ----------------------------------------------------------------------------
-- UNIQUE INDEX semantics:
--
-- The new UNIQUE INDEX is PARTIAL — only enforced when BOTH tx_hash and
-- log_index are NOT NULL. Rationale:
--
--   * REST-poll trades (source='api_market' / 'api_wallet') and WS-poll
--     trades (source='websocket') have NULL for both, and the existing
--     uq_trades_observed_natural_key (migration 007) handles dedup for
--     them via the (wallet, market, time, side, price, size_usdc) key.
--
--   * On-chain trades (source='onchain') populate both columns. The
--     partial unique index gives them a stronger guarantee: even if the
--     CLOB listener is restarted and re-decodes events, ON CONFLICT
--     (tx_hash, log_index) WHERE tx_hash IS NOT NULL AND log_index IS NOT NULL
--     DO NOTHING is a clean no-op.
--
-- Cross-source dedup: when an on-chain trade and a REST-polled trade
-- represent the same Polymarket fill, the REST one would arrive first
-- (5s poll cadence < 2s block + decode pipeline... usually). The on-chain
-- INSERT can detect the collision via the existing natural-key unique
-- index and just UPDATE the row in-place to fill in block_number /
-- tx_hash / log_index. CoverageReconciler (src/monitoring/) uses these
-- fields to measure cross-source agreement (§ 3.7).
--
-- ----------------------------------------------------------------------------
-- PARTITIONING note:
--
-- `trades_observed` is RANGE-partitioned by `time` since migration 013.
-- ALTER TABLE ADD COLUMN on the parent cascades to every child partition
-- automatically (PG 12+). CREATE INDEX on the parent likewise cascades.
-- No per-partition DDL needed here.
--
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Three new nullable columns. Existing rows get NULL — they were ingested
--    via REST/WS pre-Round-6 and don't have block-level provenance. Future
--    REST/WS rows also leave these NULL; only source='onchain' rows fill them.
-- ---------------------------------------------------------------------------
ALTER TABLE trades_observed
    ADD COLUMN IF NOT EXISTS block_number BIGINT;

ALTER TABLE trades_observed
    ADD COLUMN IF NOT EXISTS tx_hash VARCHAR(100);

ALTER TABLE trades_observed
    ADD COLUMN IF NOT EXISTS log_index INTEGER;

-- ---------------------------------------------------------------------------
-- 2) Partial UNIQUE INDEX on (tx_hash, log_index) for chain-source dedup.
--    Only enforced for rows where both are populated (i.e. source='onchain').
--    This is in addition to (not replacing) uq_trades_observed_natural_key,
--    which still handles the REST/WS dedup path.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_observed_chain
    ON trades_observed (tx_hash, log_index)
    WHERE tx_hash IS NOT NULL AND log_index IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3) Lookup index for "what trades happened around block N?" debugging
--    and the coverage reconciler's per-block diff queries. Partial so we
--    don't pay storage for the NULL-block REST/WS rows.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_trades_block_number
    ON trades_observed (block_number)
    WHERE block_number IS NOT NULL;

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. Existing rows have NULL block_number / tx_hash / log_index — by
--      design. No backfill is required: those rows already live in the
--      natural-key index and the on-chain listener just won't try to
--      collide-update them via tx_hash.
--
--   2. The CLOBChainListener's UPSERT statement is owned by
--      src/onchain/clob_listener.py and looks like:
--        INSERT INTO trades_observed (..., block_number, tx_hash, log_index, source)
--        VALUES (..., $N, $N+1, $N+2, 'onchain')
--        ON CONFLICT (tx_hash, log_index)
--          WHERE tx_hash IS NOT NULL AND log_index IS NOT NULL
--        DO NOTHING;
--
--   3. Migration 022 (chain_sync_state) tracks the last-processed block so
--      a listener restart resumes cleanly without flooding the bucket.
-- ============================================================================
