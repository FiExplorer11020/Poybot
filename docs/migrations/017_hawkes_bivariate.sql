-- 017_hawkes_bivariate.sql
-- Phase 3 Round 2 Task X — Bivariate Hawkes refactor.
--
-- Audit reference: docs/audit/05_ml_pipeline.md § MG-5.
--
-- The legacy `follower_edges.hawkes_alpha_mu` column was computed by a
-- UNIVARIATE Hawkes MLE on the follower's own marginal trade-time series.
-- That measures follower burstiness — NOT leader→follower causality.
-- Every clustered retail trader therefore looked "confirmed" as a follower
-- of every leader, with no actual causal coupling between the two streams.
--
-- This migration adds the columns the new BIVARIATE fitter produces:
--
--   * hawkes_alpha          — raw cross-excitation strength α
--   * hawkes_mu             — baseline follower intensity μ
--   * hawkes_beta           — exponential kernel decay rate β
--   * hawkes_log_likelihood — model fit quality (the higher the better)
--   * hawkes_n_leader_events — sample size of leader stream the fit saw
--                              (visibility for downstream consumers when
--                              the answer is "low-data, not low-causality")
--   * hawkes_fit_at         — when the fit was produced (debug staleness)
--
-- The existing `hawkes_alpha_mu` column STAYS. Its meaning shifts from
-- "follower self-excitation ratio" (old, broken) to "leader-causality
-- ratio α/μ" (new, correct). This is a domain reinterpretation of the
-- value, not a schema break — downstream consumers like the audit's
-- "α/μ > 1 → confirmed follower" gate in confidence_engine / graph_engine
-- continue to work and now actually mean what they say.
--
-- All columns are nullable: rows that have never been re-fit retain their
-- old `hawkes_alpha_mu` (univariate) until the next nightly batch
-- overwrites them. After one full pass of the nightly batch, every
-- confirmed edge will carry the bivariate set.

BEGIN;

ALTER TABLE follower_edges
    ADD COLUMN IF NOT EXISTS hawkes_alpha           NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS hawkes_mu              NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS hawkes_beta            NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS hawkes_log_likelihood  NUMERIC(15,4),
    ADD COLUMN IF NOT EXISTS hawkes_n_leader_events INTEGER,
    ADD COLUMN IF NOT EXISTS hawkes_fit_at          TIMESTAMPTZ;

-- Partial index over edges that have actually been fit. Useful for the
-- dashboard's "edges with valid causal score" queries and for the
-- nightly job to skip stale rows when prioritising the LIMIT.
CREATE INDEX IF NOT EXISTS idx_follower_edges_hawkes_fit_at
    ON follower_edges (hawkes_fit_at)
    WHERE hawkes_fit_at IS NOT NULL;

-- Convenience: pick "high-causality" edges in O(log n).
CREATE INDEX IF NOT EXISTS idx_follower_edges_alpha_mu
    ON follower_edges (hawkes_alpha_mu)
    WHERE hawkes_alpha_mu IS NOT NULL;

COMMIT;
