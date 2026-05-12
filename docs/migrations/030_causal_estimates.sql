-- ============================================================================
-- 030_causal_estimates.sql
--
-- Round 10 (The Truth Test) — Causal estimates per (leader, pool_class).
--
-- Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 4 (migration block).
--
-- One row per nightly 2SLS estimate. Hawkes statistical estimates from
-- R5/R9 sit side-by-side with IV/2SLS causal estimates so the dashboard
-- can render the "Hawkes says X, causal says Y" disagreement panel and
-- the confidence engine's R10 gate can join Hawkes α/μ against
-- IV-adjusted ATE with a single SELECT.
--
-- Persistence shape:
--
--   (leader_wallet, pool_class, estimated_at)  primary key — keeps an
--     append-only timeline for as-of training reads and disagreement
--     forensics. Older rows are retained until migration 011's
--     retention sweep prunes them.
--
--   period_start / period_end         time window the 2SLS fit consumed.
--   hawkes_alpha_mu_ratio             cached α/μ from the latest R9
--                                     multivariate fit for this
--                                     (leader, pool_class).
--   hawkes_log_likelihood             cached log L from the same fit.
--   causal_ate                        2SLS coefficient on L_hat (causal
--                                     effect of leader trade intensity
--                                     on follower trade intensity).
--   causal_ate_ci_low / _ci_high      bootstrap 95% CI bounds. The R10
--                                     gate treats `ci_low > 0` as "causal
--                                     evidence positive" and `ci_high < 0`
--                                     as "causal evidence negative".
--   wu_hausman_p                      Wu-Hausman test p-value. Null
--                                     hypothesis: OLS == 2SLS (no
--                                     confounding). Small p = OLS biased,
--                                     IV-correction is doing real work.
--   first_stage_f                     First-stage F-statistic; weak-
--                                     instrument check (>10 = strong).
--   instruments_used                  comma-separated list of instrument
--                                     event_types active for this fit.
--   convergence                       'converged' | 'weak_instruments' |
--                                     'failed' (mirrors R9's vocabulary).
--
-- After one full pass of the nightly 2SLS daemon, every top-N leader
-- carries one row per (leader, pool_class). Older rows are NOT pruned
-- by this migration; the retention policy migration (011) covers them.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS causal_estimates (
    leader_wallet         VARCHAR(100)  NOT NULL,
    pool_class            VARCHAR(20)   NOT NULL,
    estimated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    period_start          TIMESTAMPTZ   NOT NULL,
    period_end            TIMESTAMPTZ   NOT NULL,
    -- Statistical (cached from R5/R9 for side-by-side comparison)
    hawkes_alpha_mu_ratio NUMERIC(10, 6),
    hawkes_log_likelihood NUMERIC(15, 4),
    -- Causal (from IV / 2SLS)
    causal_ate            NUMERIC(10, 6),
    causal_ate_ci_low     NUMERIC(10, 6),
    causal_ate_ci_high    NUMERIC(10, 6),
    wu_hausman_p          NUMERIC(8, 6),
    first_stage_f         NUMERIC(10, 2),
    instruments_used      VARCHAR(200),
    convergence           VARCHAR(20),
    PRIMARY KEY (leader_wallet, pool_class, estimated_at)
);

-- Hot path: "give me the latest causal estimate for (leader, pool)".
-- The confidence engine R10 gate is on this lookup.
CREATE INDEX IF NOT EXISTS idx_causal_estimates_latest
    ON causal_estimates (leader_wallet, pool_class, estimated_at DESC);

-- Partial index on converged rows for the dashboard's "valid causal
-- estimates" panel (mirrors migration 028 pattern).
CREATE INDEX IF NOT EXISTS idx_causal_estimates_converged
    ON causal_estimates (leader_wallet, pool_class, estimated_at DESC)
    WHERE convergence = 'converged';

COMMENT ON TABLE causal_estimates IS
    'Round 10 (The Truth Test) — Per-(leader, pool_class) causal effect '
    'estimates via 2SLS. Coexists with multivariate_hawkes_fits (R9, '
    'migration 028). See docs/ROUND_10_CAUSAL_INFERENCE.md § 4 + § 3.2.';

COMMIT;

-- ============================================================================
-- POST-MIGRATION:
--
--   1. The R10 daemon (src/causal/daemon.py) populates the table after
--      the R9 daemon writes its nightly fits.
--   2. Rollback: DROP TABLE causal_estimates CASCADE.
-- ============================================================================
