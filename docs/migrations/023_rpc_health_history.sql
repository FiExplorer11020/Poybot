-- ============================================================================
-- 023_rpc_health_history.sql
--
-- Round 6 (The Spine) / Phase 6.A — Multi-RPC abstraction layer observability.
--
-- Audit reference: docs/ROUND_6_THE_SPINE.md § 3.2 — RPCClient cycles
-- between providers (local Erigon, Alchemy, QuickNode) and applies
-- per-provider circuit breakers + adaptive token buckets. This table is
-- the append-only ledger of those provider observations: every health
-- check, every latency sample, every circuit-breaker open/close. The
-- live Prometheus gauges in `src/monitoring/metrics.py` show the
-- moment-to-moment view; this table is the long-tail history used for
-- post-mortems and provider-reliability reports.
--
-- ----------------------------------------------------------------------------
-- Write cadence:
--
--   * One row per provider per HEALTHCHECK_INTERVAL_S (default 60s) tick.
--   * Additional rows on circuit-breaker state transitions (open/close)
--     so a closed-then-open-again pattern is reconstructible without
--     interpolation.
--
-- At 3 providers × 1 sample/min × 60min × 24h = ~4320 rows/day. ~1.6M
-- rows/year. The retention policy (see below) caps it at ~14 days so
-- steady-state size is ~60k rows — trivial.
--
-- ----------------------------------------------------------------------------
-- Retention:
--
-- Add to RETENTION_POLICIES in scripts/batch_runner.py with default 14 days:
--   { "table": "rpc_health_history", "time_col": "observed_at",
--     "days": 14, "batch": 10000 }
-- 14 days is enough to span a "did this incident correlate with provider
-- degradation last week?" investigation without growing unboundedly.
--
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS rpc_health_history (
    id            BIGSERIAL    PRIMARY KEY,
    observed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Symbolic name of the provider, e.g. 'local_erigon', 'alchemy', 'quicknode'.
    -- VARCHAR(50) is generous — these are short identifiers from settings.
    provider      VARCHAR(50)  NOT NULL,
    -- Was the provider reachable at this observation? Health-check probe
    -- result OR last-real-call result (the listener piggy-backs liveness
    -- onto every successful eth_call).
    available     BOOLEAN      NOT NULL,
    -- Round-trip latency for the health-check probe (or the most recent
    -- real call). NULL if the call failed before getting a response.
    latency_ms    INTEGER,
    -- Circuit-breaker state at the moment of this observation.
    -- 'closed' = healthy, 'open' = tripped (refusing calls),
    -- 'half_open' = probe in flight (one trial call gating recovery).
    circuit_state VARCHAR(20)  NOT NULL DEFAULT 'closed',
    -- Free-form structured detail: the eth-rpc method that triggered the
    -- observation, the HTTP status if it failed, the error class name on
    -- exception. {} in the steady-state polling rows.
    detail        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT rpc_health_circuit_state_chk
        CHECK (circuit_state IN ('closed', 'open', 'half_open'))
);

-- Hot path: "show me the last 1h of health for provider X" — driven by
-- the dashboard's RPC health panel.
CREATE INDEX IF NOT EXISTS idx_rpc_health_provider_time
    ON rpc_health_history (provider, observed_at DESC);

-- Retention sweep: scripts/batch_runner.py deletes rows older than 14d.
-- Standalone time index lets the DELETE use index scan + tuple-fetch
-- regardless of the per-provider distribution.
CREATE INDEX IF NOT EXISTS idx_rpc_health_observed_at
    ON rpc_health_history (observed_at);

-- Spot-check index for "find every circuit_open transition" investigations.
-- Partial because the 'closed' steady state dominates the row count.
CREATE INDEX IF NOT EXISTS idx_rpc_health_open_transitions
    ON rpc_health_history (provider, observed_at)
    WHERE circuit_state IN ('open', 'half_open');

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. The table is empty after this migration. RPCClient.providers
--      (src/rpc/providers.py) starts writing rows on its first
--      health-check tick.
--
--   2. Add to scripts/batch_runner.py RETENTION_POLICIES with
--      `days=14`. The retention sweep runs under RETENTION_ENABLED.
--
--   3. Companion Prometheus metrics (defined in src/monitoring/metrics.py
--      by Round 6's metrics block):
--        polybot_rpc_calls_total{provider, method, result}
--        polybot_rpc_latency_seconds{provider, method}
--        polybot_rpc_circuit_breaker_open{provider}
--        polybot_rpc_fallback_total{from_provider, to_provider}
-- ============================================================================
