-- Migration 012: liquidity_score as-of metadata
-- Phase 0 Task C — see docs/audit/05_ml_pipeline.md MG-3 and
-- docs/audit/phase0/C_liquidity.md.
--
-- Lays the groundwork for fixing the training-leakage MG-3 sub-bug
-- ("`error_model._fetch_training_data` reads `markets.liquidity_score`
-- AS-OF-NOW for historical positions"). The full feature-store fix is
-- deferred to Phase 3 (a `market_liquidity_history(market_id, ts,
-- score)` table feeding an as-of read in
-- `error_model._fetch_training_data`). For now we make every future
-- liquidity_score write self-describing so a later as-of join is
-- meaningful:
--
--   liquidity_score_updated_at TIMESTAMPTZ
--     — when the current `liquidity_score` value was stamped. Distinct
--       from `updated_at` (whole-row touch). Without this column we
--       cannot tell whether a row's `liquidity_score=0.55` is from
--       5 min ago or from the 24h-cache-skipped sync 23h ago.
--
--   liquidity_score_source VARCHAR(32)
--     — provenance tag: 'falcon_575' (agent 575 / Market Insights —
--       the documented source), 'falcon_574' (agent 574 / Polymarket
--       Markets `liquidity` field — the legacy mis-sourced fallback
--       fixed in Phase 0 Task C), or 'gamma' (Gamma API
--       `liquidity` field fallback in `sync_markets`). Cheap to
--       audit which leakage rows came from which provenance.
--
-- Both columns are nullable so the migration is safe on existing rows.
-- Existing pre-Task-C rows carry the WRONG field (agent 574
-- `liquidity` written under `liquidity_score`); they will be
-- overwritten by `sync_markets` within 24h of deploy. No
-- backfill UPDATE is run by this migration — we let the natural
-- 24h refresh path drain stale rows. If the operator wants a faster
-- transition, they can run on a deploy window:
--
--   UPDATE markets SET updated_at = NOW() - INTERVAL '25 hours'
--   WHERE end_date IS NULL OR end_date > NOW() - INTERVAL '24 hours';
--
-- which forces every live market to re-sync on the next registry cycle.

BEGIN;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS liquidity_score_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS liquidity_score_source     VARCHAR(32);

-- Index helps the deferred as-of read path (Phase 3 feature store) and
-- the dashboard query "show me liquidity rows stamped in the last hour".
CREATE INDEX IF NOT EXISTS idx_markets_liq_updated_at
    ON markets (liquidity_score_updated_at DESC NULLS LAST);

COMMIT;
