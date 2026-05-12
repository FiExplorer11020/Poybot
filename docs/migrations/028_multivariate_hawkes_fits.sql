-- ============================================================================
-- 028_multivariate_hawkes_fits.sql
--
-- Round 9 (The Web) — Multivariate Hawkes fits per leader.
--
-- Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 4 + § 3.1
--
-- The Round-5 bivariate fitter (migration 017) gave us per-(leader,
-- follower) causal coupling. R9 generalises that to per-leader population
-- coupling: one fit per leader fits an N-dim multivariate Hawkes against
-- the leader's trade stream PLUS K follower-pool trade streams (pools
-- clustered by the R8 strategy classifier). Output: a block-sparse
-- α matrix + μ vector + shared β.
--
-- This migration ADDS the table; it does NOT touch the existing
-- `follower_edges` table or the per-pair bivariate columns (migration
-- 017's columns stay — the R5 fitter still runs nightly for per-pair
-- validation; the multivariate model is for population dynamics).
--
-- Persistence shape:
--
--   (leader_wallet, fit_at)   primary key — keeps a fit-history
--                             timeline for as-of training reads and
--                             drift forensics.
--   pool_classes              comma-separated list of pool labels the
--                             fit included (e.g. "directional,momentum,
--                             social,info_leak"). The α matrix JSON is
--                             keyed by these labels.
--   alpha_matrix_json         {"(i,j)": float} for FREE entries only —
--                             the block-sparse mask zeroes out the rest.
--                             Read as: how much process j excites i.
--   mu_vector_json            {"i": float} — baseline rate per process.
--   beta                      shared exponential decay rate.
--   log_likelihood            full-model log L (NOT negative).
--   bic_statistic             2·(NLL_null − NLL_full); compared against
--                             k_penalty · log(N_events) threshold.
--   accepted_couplings_json   {"(i,j)": bool} — which α entries survived
--                             the BIC test individually (per spec § 2.3).
--   convergence               'converged' | 'fallback' | 'bic_rejected'
--
-- After one full pass of the nightly multivariate Hawkes job, every
-- top-N leader will carry one row. Older rows are NOT pruned by this
-- migration; the retention policy migration (011) covers them.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS multivariate_hawkes_fits (
    leader_wallet         VARCHAR(100)  NOT NULL,
    fit_at                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    pool_classes          VARCHAR(200)  NOT NULL,
    alpha_matrix_json     JSONB         NOT NULL,
    mu_vector_json        JSONB         NOT NULL,
    beta                  NUMERIC(12,6) NOT NULL,
    log_likelihood        NUMERIC(15,4),
    bic_threshold         NUMERIC(15,4),
    bic_statistic         NUMERIC(15,4),
    accepted_couplings_json JSONB,
    n_events_total        INTEGER,
    convergence           VARCHAR(20),
    PRIMARY KEY (leader_wallet, fit_at)
);

-- Convenience index: "give me the latest fit per leader" sorts by fit_at
-- DESC and limits to 1. The partial index on convergence='converged'
-- accelerates the dashboard's "leaders with valid multivariate fits"
-- query.
CREATE INDEX IF NOT EXISTS idx_mvhawkes_fits_recent
    ON multivariate_hawkes_fits (leader_wallet, fit_at DESC);

CREATE INDEX IF NOT EXISTS idx_mvhawkes_fits_converged
    ON multivariate_hawkes_fits (leader_wallet, fit_at DESC)
    WHERE convergence = 'converged';

COMMENT ON TABLE multivariate_hawkes_fits IS
    'Round 9 (The Web) — Per-leader multivariate Hawkes fits. One row '
    'per (leader, fit_at). Coexists with follower_edges.hawkes_* '
    '(per-pair, migration 017). See docs/ROUND_9_MULTIVARIATE_HAWKES.md '
    '§ 3.1 for the matrix shape and § 2.2 for the block-sparse mask.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The R9 daemon (src/follower_volume/daemon.py) populates the table.
--   2. The MultivariateHawkesFitter writes one row per leader per
--      nightly batch; older rows remain for forensics.
--   3. Rollback: DROP TABLE multivariate_hawkes_fits CASCADE.
-- ============================================================================
