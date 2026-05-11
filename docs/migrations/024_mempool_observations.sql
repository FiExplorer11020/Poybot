-- ============================================================================
-- 024_mempool_observations.sql
--
-- Round 7 (The Front Door) / Phase 7.A — Mempool-watcher observation ledger.
--
-- Audit reference: docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 4. Every decoded
-- leader intent the mempool watcher publishes to mempool:leader_intent
-- gets one row here, indexed by a synthetic intent_id (UUID). The same
-- row is updated by the IntentRouter when it decides what to do (fire /
-- pool_miss / risk_blocked / killswitch_off / shadow) and again when the
-- chain eventually confirms the leader's actual transaction.
--
-- The table is the A/B-testing substrate for the 30-day shadow soak
-- (R7 § 3.7): every shadow paper trade is comparable against what the
-- live path would have done, joined by intent_id. Post-soak the same
-- table doubles as the latency-budget audit trail (intent_received_at →
-- fired_at → confirmed_at gives us the per-hop latency breakdown the
-- §2 table promises).
--
-- ----------------------------------------------------------------------------
-- Write semantics:
--
--   * INSERT (single row) by IntentRouter on every consumed
--     mempool:leader_intent entry. fire_result starts as one of
--     {'shadow', 'pool_miss', 'risk_blocked', 'killswitch_off', 'filled'}.
--     fired_at + latency_ms_to_fire set on the same INSERT.
--
--   * UPDATE (one or zero times) by the cross-source reconciler when
--     the corresponding on-chain trade lands: set confirmed_at,
--     confirmed_block, latency_ms_to_confirm. The join is on
--     (wallet_address, market_id, token_id, nonce) — the on-chain
--     listener has wallet+market+token via R6 § 3.3, and nonce is
--     part of the tx receipt the listener already decodes. If the
--     reconciler can't find a match in N minutes the row stays
--     un-confirmed-at — that's a useful signal (the leader tx was
--     replaced or dropped without mining; the replacement chain
--     histogram surfaces it).
--
-- ----------------------------------------------------------------------------
-- Volume estimate:
--
--   ~2000 watched wallets × ~5 intents/day (Polymarket-active wallets
--   trade a few times a day on average) ≈ 10k rows/day. ~3.5M rows/year
--   without retention. At ~200 bytes per row that's ~700 MB/year —
--   trivial. The retention sweep below caps it at 30 days for steady
--   state (~300k rows / ~60 MB) which is more than enough for the
--   weekly latency-audit query the operator runs from the dashboard.
--
-- Retention:
--
-- Add to RETENTION_POLICIES in scripts/batch_runner.py with default 30 days:
--   { "table": "mempool_observations", "time_col": "intent_received_at",
--     "days": 30, "batch": 10000 }
--
-- 30 days matches the R7 § 7 shadow-soak duration so any post-mortem on
-- the soak has its raw substrate available; longer is unhelpful at
-- this data granularity.
--
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS mempool_observations (
    -- Synthetic id minted by the decoder when it builds the
    -- LeaderIntent. UUID rather than BIGSERIAL because the id flows
    -- across modules (mempool watcher → router → paper/live trader →
    -- reconciler) and a deterministic value avoids round-trip
    -- INSERT...RETURNING dependencies.
    intent_id              UUID         PRIMARY KEY,

    -- Leader EOA, 0x-prefixed lowercase. Matches the casing in
    -- wallet_universe + leaders + trades_observed.
    wallet_address         VARCHAR(100) NOT NULL,

    -- Polymarket condition id / token id. Same shape as the
    -- corresponding columns in trades_observed.
    market_id              VARCHAR(100) NOT NULL,
    token_id               VARCHAR(100) NOT NULL,

    -- Trade side. 'buy' | 'sell'.
    side                   VARCHAR(4)   NOT NULL,

    -- Notional in USDC. NUMERIC(20,2) — same precision as trades_observed.size_usdc.
    size_usdc              NUMERIC(20,2) NOT NULL,

    -- Wall-clock time the mempool subscription handed the tx to the
    -- decoder. This is t=0 for the latency budget in R7 § 2.
    intent_received_at     TIMESTAMPTZ  NOT NULL,

    -- Mempool tx_hash — 0x-prefixed lowercase, 66 chars. Stored at
    -- VARCHAR(100) to match the existing trades_observed.tx_hash
    -- column added in migration 021.
    tx_hash                VARCHAR(100) NOT NULL,

    -- The wallet nonce on the tx. BIGINT — wallet nonces grow
    -- unboundedly but a BIGINT is safe for ~9 × 10^18 trades.
    nonce                  BIGINT       NOT NULL,

    -- If this intent replaces an earlier mempool tx in the
    -- (wallet_address, nonce) chain, the displaced hash sits here.
    -- NULL = first sighting at this nonce.
    replaces_tx_hash       VARCHAR(100),

    -- Block number the decoder expected this tx to land in (chain
    -- head + 1 at decode time). NULL if the RPC call to fetch head
    -- failed; the row still gets written.
    expected_block         BIGINT,

    -- IntentRouter outcome timestamps. fired_at is set in the same
    -- transaction as the initial INSERT in 99% of cases (the router
    -- decides synchronously); confirmed_at is set by a later UPDATE
    -- from the reconciler.
    fired_at               TIMESTAMPTZ,
    fire_result            VARCHAR(20),  -- 'filled' | 'pool_miss' | 'risk_blocked' | 'killswitch_off' | 'shadow' | 'cooldown' | 'confidence_skip' | 'size_cap'
    confirmed_at           TIMESTAMPTZ,
    confirmed_block        BIGINT,

    -- Pre-computed latency in milliseconds. Materialised here rather
    -- than computed at query time so the dashboard's R7 latency-budget
    -- panel is an O(1) SELECT.
    latency_ms_to_fire     INTEGER,
    latency_ms_to_confirm  INTEGER,

    -- Defensive CHECK on fire_result vocabulary. Lower-case strings
    -- only; the IntentRouter writes these constants directly.
    CONSTRAINT mempool_obs_fire_result_chk
        CHECK (
            fire_result IS NULL
            OR fire_result IN (
                'filled', 'pool_miss', 'risk_blocked', 'killswitch_off',
                'shadow', 'cooldown', 'confidence_skip', 'size_cap'
            )
        ),
    -- Defensive CHECK on side. Mirrors trades_observed.side.
    CONSTRAINT mempool_obs_side_chk
        CHECK (side IN ('buy', 'sell'))
);

-- Hot path #1: "show me every recent intent for wallet W". Used by
-- the dashboard's wallet-detail drill-down and by the per-leader
-- latency audit in nightly batch.
CREATE INDEX IF NOT EXISTS idx_mempool_obs_wallet_time
    ON mempool_observations (wallet_address, intent_received_at DESC);

-- Hot path #2: "find the observation for tx_hash X". The reconciler
-- uses this when a chain trade lands and we need to update the
-- matching row's confirmed_at / confirmed_block.
CREATE INDEX IF NOT EXISTS idx_mempool_obs_tx_hash
    ON mempool_observations (tx_hash);

-- Retention sweep: scripts/batch_runner.py deletes rows older than
-- 30 d. Standalone time index lets the DELETE use index scan +
-- tuple-fetch regardless of the per-wallet distribution.
CREATE INDEX IF NOT EXISTS idx_mempool_obs_intent_received_at
    ON mempool_observations (intent_received_at);

-- Spot-check index for "find every shadow-soak observation". Partial
-- because at steady state (post-soak) the row count for shadow drops
-- to zero — keeping a partial index here is essentially free.
CREATE INDEX IF NOT EXISTS idx_mempool_obs_shadow
    ON mempool_observations (intent_received_at)
    WHERE fire_result = 'shadow';

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. The table is empty after this migration. The IntentRouter
--      INSERTs the first row on its first consumed mempool:leader_intent
--      stream entry.
--
--   2. Add to scripts/batch_runner.py RETENTION_POLICIES with
--      `days=30`. The retention sweep runs under RETENTION_ENABLED.
--
--   3. Companion Prometheus metrics (defined in src/monitoring/metrics.py
--      by Round 7's metrics block):
--        polybot_intent_router_decisions_total{result}
--        polybot_intent_router_latency_seconds
--        polybot_mempool_intent_to_confirm_seconds
--        polybot_mempool_shadow_vs_live_pnl_diff_usdc
--
--   4. The 30-day shadow soak (R7 § 7 Phase 7.A → 7.B → 7.C) reads
--      this table to compare paper PnL vs the would-have-been live
--      PnL grouped by intent_id. Operators MUST validate the soak
--      metrics here before flipping PREFILL_LIVE_ENABLED.
-- ============================================================================
