"""
Bivariate Hawkes Process Fitter — batch job for causal follower detection.

Audit reference: docs/audit/05_ml_pipeline.md § MG-5.

The legacy implementation was univariate: it discarded the leader timestamps
and fitted a self-exciting process on the follower's own marginal series, so
`alpha_mu_ratio` measured follower burstiness, not leader→follower causality.
Every clustered retail trader got "confirmed" as a follower of every leader.

This module replaces that with the BIVARIATE conditional intensity:

    λ_F(t) = μ + α · Σ_{t_j ∈ leader_trades, t_j < t} exp(-β · (t − t_j))

where the integral is over the LEADER's prior trade times. `α / μ` is then a
true causal coupling: ratios > 1.0 mean each leader trade excites ≥1 follower
trade on average (the audit's "confirmed follower" gate); ratios < 0.3 mean
the two streams are effectively independent.

References:
    Ozaki, T. (1979). Maximum likelihood estimation of Hawkes' self-exciting
        point processes. Ann. Inst. Statist. Math. 31, 145-155.
    Daley, D. J. & Vere-Jones, D. (2003). An Introduction to the Theory of
        Point Processes, vol. I, ch. 7 (exponential-kernel MLE).
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np
from loguru import logger
from scipy.optimize import minimize

from src.config import settings
from src.database.connection import get_db
from src.monitoring.metrics import (
    hawkes_alpha_mu_ratio,
    hawkes_fit_duration_seconds,
    hawkes_fits_total,
)

# ---------------------------------------------------------------------------
# Tuning constants. Kept module-level so the tests can monkey-patch.
# ---------------------------------------------------------------------------

# Minimum events per side to attempt a bivariate fit. With fewer, the MLE is
# numerically degenerate (see test_numerical_stability).
MIN_LEADER_EVENTS = 5
MIN_FOLLOWER_EVENTS = 5

# Default half-life used to seed β when no prior is available. 5 minutes is
# the same scale as FOLLOWER_WINDOW_S so the kernel actually peaks inside the
# audit's causal window. The optimizer is free to walk away from this seed.
HAWKES_HALFLIFE_S = 300.0

# Numerical floor used to guard log(λ) and the box constraints. The fit is
# scale-invariant under uniform rescaling of (μ, α), so the absolute value of
# the floor does not affect identifiability — it just keeps the optimiser
# from walking off into NaN-land.
_PARAM_FLOOR = 1e-9
_INVALID_NLL = 1e10

# Round 5 — Significance threshold for accepting α > 0 (model selection).
#
# On independent or weakly-coupled data the MLE was systematically returning
# α/μ ≈ 5+ (test_independence_yields_low_alpha_mu in test_hawkes_bivariate.py)
# because the optimiser picks up phantom coincidences and there's no
# regularisation pushing α toward 0.
#
# We use BIC (Bayesian Information Criterion) rather than a fixed-α LRT
# threshold. BIC penalises the bivariate model by `log(N)` for the extra
# parameter, where N is the number of follower events. Concretely:
#
#       2 · (NLL_at_α=0 − NLL_at_MLE) > log(N) · HAWKES_BIC_K_PENALTY
#
# This is equivalent to "the bivariate model wins only if it reduces NLL by
# more than ½·log(N) per added parameter." For typical follower counts
# (N ≈ 1k-5k), the threshold sits at ~7-8 — much stricter than the χ²(1,
# 5%) value of 3.84 and stricter still than typical false-positive rates of
# raw LRT. Smaller samples are penalised LESS strictly (small log(N)), so
# we still detect strong coupling in sparse-data edges.
#
# The fallback floor `HAWKES_LRT_FLOOR` guards against degenerate
# log(N)·k giving an absurdly-low threshold for tiny N (e.g. log(20)·1=3.0).
HAWKES_BIC_K_PENALTY = 1  # bivariate adds 1 free parameter (α) over null
HAWKES_LRT_FLOOR = 3.84    # absolute floor = χ²(1, 5%) — never accept below this


# ---------------------------------------------------------------------------
# Log-likelihood: BIVARIATE exponential kernel
# ---------------------------------------------------------------------------


def bivariate_hawkes_nll(
    params: np.ndarray,
    leader_times: np.ndarray,
    follower_times: np.ndarray,
    window_end: float,
) -> float:
    """
    Negative log-likelihood for the bivariate (leader → follower) Hawkes
    process with exponential excitation kernel.

    Conditional intensity of the follower:

        λ_F(t) = μ + α · Σ_{t_j ∈ leader_times, t_j < t} exp(-β · (t - t_j))

    Closed-form NLL (Ozaki 1979 / Daley & Vere-Jones 2003 §7.2):

        NLL = ∫₀^T λ_F(s) ds − Σ_i log λ_F(t_i^F)
            = μT + (α/β) · Σ_j [ 1 − exp(-β (T − t_j^L)) ]
              − Σ_i log( μ + α · S_i )

    where S_i = Σ_{t_j^L < t_i^F} exp(-β (t_i^F − t_j^L)).

    The recursive update on S_i used by the univariate code does NOT apply
    directly here because we step through follower times while the excitation
    sum is over leader times. Instead we walk a two-pointer merge: keep a
    running excitation E that "ages" by the elapsed follower-time gap, and
    fold in any leader events that fired in (t_{i-1}^F, t_i^F].

    Args:
        params: array of [mu, alpha, beta], all ≥ 0.
        leader_times: 1-D, sorted, non-negative leader event times (seconds).
        follower_times: 1-D, sorted, non-negative follower event times.
        window_end: observation horizon T (seconds). Must satisfy
            window_end ≥ max(leader_times[-1], follower_times[-1]).

    Returns:
        Negative log-likelihood. Returns _INVALID_NLL (1e10) for invalid
        parameters, empty follower stream, or any numerical violation —
        the optimiser then sees a large finite value and steers away.
    """
    mu, alpha, beta = params
    # Guard the parameter space. β=0 is a measure-zero degenerate case
    # (kernel becomes a constant — no excitation decay) — bounce.
    if mu < _PARAM_FLOOR or alpha < 0.0 or beta <= _PARAM_FLOOR:
        return _INVALID_NLL

    nF = len(follower_times)
    if nF == 0:
        return _INVALID_NLL

    nL = len(leader_times)

    # ----- Integral term ∫₀^T λ_F(s) ds = μ·T + (α/β) Σ_j(1 - exp(-β(T-tj))) -----
    if nL == 0:
        integral = mu * window_end  # α-term collapses
    else:
        # Clip the exponent to avoid overflow when β(T-t) is large negative
        # (well-decayed events) — exp(-large) is just 0 to double precision.
        decay = np.exp(-beta * np.clip(window_end - leader_times, 0.0, 700.0 / max(beta, _PARAM_FLOOR)))
        integral = mu * window_end + (alpha / beta) * np.sum(1.0 - decay)

    # ----- Sum-log-intensity term Σ_i log λ_F(t_i^F) via two-pointer merge -----
    log_sum = 0.0
    excitation = 0.0  # = Σ_{t_j^L ≤ last visited time} exp(-β(now - t_j^L))
    last_time = 0.0
    j = 0  # leader pointer

    # No need for a recursive aging if we just re-anchor against the previous
    # follower time. The recursion is:
    #   excitation_new = excitation_old * exp(-β·Δt) + Σ_{new leader events} exp(-β·(t_i^F − t_j^L))
    for i in range(nF):
        ti = follower_times[i]
        # Age the previous excitation forward to ti.
        if i > 0:
            dt = ti - last_time
            if dt < 0.0:
                # Inputs not sorted — treat as invalid.
                return _INVALID_NLL
            excitation *= np.exp(-beta * dt) if dt * beta < 700.0 else 0.0

        # Fold in any leader events that fired in (last_time, ti].
        # All leader events ≤ ti contribute — strictly we want t_j^L < ti
        # for the open-left convention, but ties have measure zero. We use
        # <= because real timestamps can collide at the second resolution.
        while j < nL and leader_times[j] <= ti:
            tj = leader_times[j]
            # Each new leader event contributes exp(-β·(ti - tj)).
            gap = ti - tj
            if gap * beta < 700.0:
                excitation += np.exp(-beta * gap)
            j += 1

        lam = mu + alpha * excitation
        if lam <= 0.0:
            return _INVALID_NLL
        log_sum += np.log(lam)
        last_time = ti

    nll = integral - log_sum
    if not np.isfinite(nll):
        return _INVALID_NLL
    return float(nll)


# Backward-compat alias — `hawkes_log_likelihood` was the name used by the
# legacy univariate fitter. Kept so any external import resolves; the new
# code should use `bivariate_hawkes_nll` directly.
def hawkes_log_likelihood(
    params: np.ndarray,
    timestamps: np.ndarray,
    window_end: float,
) -> float:
    """Legacy univariate NLL — superseded by `bivariate_hawkes_nll`.

    Deprecated. Retained so existing tests in test_hawkes_fitter.py still
    pass while the codebase migrates. Internally implements a univariate
    self-exciting fit (the old behavior) for backwards compatibility.
    """
    mu, alpha, beta = params
    if mu <= 0 or alpha <= 0 or beta <= 0:
        return _INVALID_NLL

    n = len(timestamps)
    if n == 0:
        return _INVALID_NLL

    integral = mu * window_end + (alpha / beta) * np.sum(
        1.0 - np.exp(-beta * (window_end - timestamps))
    )

    log_sum = 0.0
    excitation = 0.0
    for i in range(n):
        if i > 0:
            excitation = np.exp(-beta * (timestamps[i] - timestamps[i - 1])) * (1.0 + excitation)
        lam_i = mu + alpha * excitation
        if lam_i <= 0:
            return _INVALID_NLL
        log_sum += np.log(lam_i)

    return -(log_sum - integral)


# ---------------------------------------------------------------------------
# Fitter
# ---------------------------------------------------------------------------


class HawkesFitter:
    """
    Fits a BIVARIATE Hawkes process (leader-excited follower intensity).

    The headline output `alpha_mu_ratio` is now `α / μ`, where α is the
    cross-excitation strength FROM leader trades, not the follower's own
    self-excitation. The audit's "α/μ > 1 → confirmed follower" gate
    continues to work — with correct semantics.
    """

    async def fit_edge(self, leader_wallet: str, follower_wallet: str) -> dict | None:
        """
        Fetch both timestamp series, fit the bivariate Hawkes, return:

            {
              "mu":                  float,   # baseline follower intensity
              "alpha":               float,   # cross-excitation strength
              "beta":                float,   # exponential decay rate
              "alpha_mu_ratio":      float,   # α / μ (the causal score)
              "log_likelihood":      float,   # log L (NOT negative)
              "n_leader_events":     int,
              "n_follower_events":   int,
              "convergence":         str,     # converged|fallback_nelder_mead|degenerate
            }

        Returns None on hard failure (DB error, no follower events at all,
        no successful optimisation path). The "degenerate" path — zero
        leader events but enough follower events — returns a μ-only fit
        with α = 0 (Poisson model). This is intentional: it lets downstream
        consumers see "this edge has no causal coupling" rather than NULL.
        """
        lookback = timedelta(days=settings.HAWKES_LOOKBACK_DAYS)
        since = datetime.now(tz=timezone.utc) - lookback

        try:
            async with get_db() as conn:
                rows_l = await conn.fetch(
                    """
                    SELECT time FROM trades_observed
                    WHERE wallet_address = $1 AND time >= $2
                      AND source IS DISTINCT FROM 'onchain'
                    ORDER BY time
                    """,
                    leader_wallet,
                    since,
                )
                leader_times = np.array(
                    [r["time"].timestamp() for r in rows_l], dtype=float
                )

                rows_f = await conn.fetch(
                    """
                    SELECT time FROM trades_observed
                    WHERE wallet_address = $1 AND time >= $2
                      AND source IS DISTINCT FROM 'onchain'
                    ORDER BY time
                    """,
                    follower_wallet,
                    since,
                )
                follower_times = np.array(
                    [r["time"].timestamp() for r in rows_f], dtype=float
                )
        except Exception as e:
            logger.error(f"Failed to fetch timestamps for Hawkes fit: {e}")
            hawkes_fits_total.labels(result="failed").inc()
            return None

        # Need at least some follower events to even define a likelihood.
        if len(follower_times) < MIN_FOLLOWER_EVENTS:
            return None

        return self.fit_arrays(leader_times, follower_times)

    def fit_arrays(
        self,
        leader_times: np.ndarray,
        follower_times: np.ndarray,
    ) -> dict | None:
        """
        Pure-numpy entry point. Easy to unit-test without a DB.

        Performs sorting, rescaling to a common origin, and the MLE fit.
        """
        if len(follower_times) < MIN_FOLLOWER_EVENTS:
            return None

        leader_times = np.sort(np.asarray(leader_times, dtype=float))
        follower_times = np.sort(np.asarray(follower_times, dtype=float))

        # Anchor origin at min(both) so all times are ≥ 0. Window end is the
        # max of both — both processes were observed over the same window.
        if len(leader_times) > 0:
            t0 = float(min(leader_times[0], follower_times[0]))
            t_end_raw = float(max(leader_times[-1], follower_times[-1]))
        else:
            t0 = float(follower_times[0])
            t_end_raw = float(follower_times[-1])

        leader_norm = leader_times - t0
        follower_norm = follower_times - t0
        window_end = max(t_end_raw - t0, 1.0)

        start = _time.perf_counter()

        # Degenerate path: not enough leader events for cross-excitation to be
        # identifiable. Return a μ-only Poisson fit. α=0 → α/μ=0 → the
        # downstream confirmation gate cleanly rejects this edge.
        if len(leader_norm) < MIN_LEADER_EVENTS:
            mu_hat = float(len(follower_norm) / window_end)
            beta_hat = 1.0 / HAWKES_HALFLIFE_S
            elapsed = _time.perf_counter() - start
            hawkes_fit_duration_seconds.observe(elapsed)
            hawkes_fits_total.labels(result="degenerate").inc()
            hawkes_alpha_mu_ratio.observe(0.0)
            # log L for a homogeneous Poisson of rate μ:
            #   log L = -μT + n_F · log(μ)
            log_lik = -mu_hat * window_end + len(follower_norm) * np.log(max(mu_hat, _PARAM_FLOOR))
            return {
                "mu": mu_hat,
                "alpha": 0.0,
                "beta": beta_hat,
                "alpha_mu_ratio": 0.0,
                "log_likelihood": float(log_lik),
                "n_leader_events": int(len(leader_norm)),
                "n_follower_events": int(len(follower_norm)),
                "convergence": "degenerate",
            }

        fit_result, convergence = self._fit(leader_norm, follower_norm, window_end)
        elapsed = _time.perf_counter() - start
        hawkes_fit_duration_seconds.observe(elapsed)

        if fit_result is None:
            hawkes_fits_total.labels(result="failed").inc()
            return None

        mu, alpha, beta = fit_result
        nll = bivariate_hawkes_nll(
            np.array([mu, alpha, beta]), leader_norm, follower_norm, window_end
        )

        # Round 5 — BIC model selection against H0: α = 0.
        # Compute NLL of the μ-only Poisson model AT THE BIVARIATE μ
        # (so the comparison is against a properly-fit null, not
        # the empirical rate). β is irrelevant when α=0.
        null_nll = bivariate_hawkes_nll(
            np.array([mu, 0.0, beta]), leader_norm, follower_norm, window_end
        )
        nF = max(len(follower_norm), 1)
        # log(N) is the BIC unit; ×1 because bivariate adds exactly 1 free
        # parameter (α) over the H0 model (μ alone, β unidentified).
        bic_threshold = max(
            HAWKES_BIC_K_PENALTY * float(np.log(nF)),
            HAWKES_LRT_FLOOR,
        )
        if (
            np.isfinite(null_nll)
            and np.isfinite(nll)
            and null_nll < _INVALID_NLL / 2
            and nll < _INVALID_NLL / 2
        ):
            lrt_statistic = 2.0 * (null_nll - nll)
        else:
            lrt_statistic = 0.0

        if lrt_statistic < bic_threshold:
            # Not enough evidence to reject α=0 — return the μ-only fit.
            # This is the regularisation that prevents independent-Poisson
            # data from being labelled as a confirmed follower.
            alpha = 0.0
            nll = null_nll
            convergence = f"{convergence}_bic_rejected"

        log_lik = -nll if np.isfinite(nll) and nll < _INVALID_NLL / 2 else float("nan")
        alpha_mu = float(alpha / mu) if mu > _PARAM_FLOOR else 0.0

        hawkes_fits_total.labels(result=convergence).inc()
        hawkes_alpha_mu_ratio.observe(min(alpha_mu, 10.0))

        return {
            "mu": float(mu),
            "alpha": float(alpha),
            "beta": float(beta),
            "alpha_mu_ratio": alpha_mu,
            "log_likelihood": float(log_lik) if np.isfinite(log_lik) else 0.0,
            "lrt_statistic": float(lrt_statistic),
            "n_leader_events": int(len(leader_norm)),
            "n_follower_events": int(len(follower_norm)),
            "convergence": convergence,
        }

    # ------------------------------------------------------------------ #
    # Optimisation                                                        #
    # ------------------------------------------------------------------ #

    def _fit(
        self,
        leader_times: np.ndarray,
        follower_times: np.ndarray,
        window_end: float,
    ) -> tuple[tuple[float, float, float] | None, str]:
        """
        MLE via scipy.optimize. Try L-BFGS-B from a sensible prior +
        random restarts; on convergence failure fall back to Nelder-Mead.

        Returns ((mu, alpha, beta), convergence_label) where the label is
        one of "converged" | "fallback_nelder_mead" | "failed".
        """
        # ---- Prior seeds -------------------------------------------------
        empirical_mu = max(len(follower_times) / window_end, _PARAM_FLOOR * 10.0)
        seed_beta = 1.0 / HAWKES_HALFLIFE_S
        # Round 5: the H0 = no-coupling seed is included so the optimiser
        # has a fair shot at the null. If it lands here with strictly lower
        # NLL than any α > 0 seed, the LRT path will accept α = 0 cleanly.
        seeds = [
            np.array([empirical_mu, 0.0, seed_beta]),  # H0 seed (α=0)
            np.array([empirical_mu, 0.1 * empirical_mu, seed_beta]),
            np.array([empirical_mu * 0.5, 0.5 * empirical_mu, seed_beta]),
            np.array([empirical_mu * 2.0, 1.0 * empirical_mu, seed_beta * 2.0]),
            np.array([empirical_mu, 2.0 * empirical_mu, seed_beta * 0.5]),
        ]
        # Plus 1 random restart for ill-conditioned landscapes.
        rng = np.random.default_rng(seed=42)
        seeds.append(rng.uniform(_PARAM_FLOOR, max(empirical_mu * 3.0, 1.0), size=3))

        bounds = [
            (_PARAM_FLOOR, None),  # mu  > 0
            (0.0, None),           # alpha ≥ 0 (0 = no causal coupling)
            (_PARAM_FLOOR, None),  # beta > 0
        ]

        best_x: np.ndarray | None = None
        best_loss = float("inf")

        # ---- Try L-BFGS-B from each seed --------------------------------
        for x0 in seeds:
            try:
                res = minimize(
                    bivariate_hawkes_nll,
                    x0,
                    args=(leader_times, follower_times, window_end),
                    method="L-BFGS-B",
                    bounds=bounds,
                    options={"maxiter": 300, "ftol": 1e-9},
                )
                if res.success and np.isfinite(res.fun) and res.fun < best_loss:
                    best_loss = float(res.fun)
                    best_x = res.x
            except Exception as exc:  # pragma: no cover — scipy edge cases
                logger.debug(f"L-BFGS-B restart failed: {exc}")

        if best_x is not None and best_loss < _INVALID_NLL / 2:
            return (float(best_x[0]), float(best_x[1]), float(best_x[2])), "converged"

        # ---- Fallback: Nelder-Mead (derivative-free, more robust) -------
        try:
            res = minimize(
                bivariate_hawkes_nll,
                seeds[0],
                args=(leader_times, follower_times, window_end),
                method="Nelder-Mead",
                options={"maxiter": 1000, "xatol": 1e-6, "fatol": 1e-6},
            )
            if res.success and np.isfinite(res.fun) and res.fun < _INVALID_NLL / 2:
                # Clamp to non-negative — Nelder-Mead ignores bounds.
                x = res.x
                mu_c = max(float(x[0]), _PARAM_FLOOR)
                alpha_c = max(float(x[1]), 0.0)
                beta_c = max(float(x[2]), _PARAM_FLOOR)
                return (mu_c, alpha_c, beta_c), "fallback_nelder_mead"
        except Exception as exc:  # pragma: no cover
            logger.debug(f"Nelder-Mead fallback failed: {exc}")

        return None, "failed"

    # ------------------------------------------------------------------ #
    # Batch driver                                                        #
    # ------------------------------------------------------------------ #

    async def run_batch(self) -> int:
        """
        Fit Hawkes for every confirmed edge. Persists the new columns
        (mu, alpha, beta, log_likelihood, n_leader_events, fit_at) plus the
        legacy alpha_mu column for backward compat.
        """
        updated = 0
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT leader_wallet, follower_wallet
                    FROM follower_edges
                    WHERE co_occurrences >= $1
                    ORDER BY co_occurrences DESC
                    LIMIT $2
                    """,
                    settings.MIN_CO_OCCURRENCES,
                    settings.BATCH_HAWKES_LEADERS,
                )
        except Exception as e:
            logger.error(f"Failed to fetch edges for Hawkes batch: {e}")
            return 0

        for row in rows:
            result = await self.fit_edge(row["leader_wallet"], row["follower_wallet"])
            if result is None:
                continue
            try:
                async with get_db() as conn:
                    await conn.execute(
                        """
                        UPDATE follower_edges
                        SET hawkes_alpha_mu       = $1,
                            hawkes_alpha          = $2,
                            hawkes_mu             = $3,
                            hawkes_beta           = $4,
                            hawkes_log_likelihood = $5,
                            hawkes_n_leader_events = $6,
                            hawkes_fit_at         = NOW()
                        WHERE leader_wallet = $7 AND follower_wallet = $8
                        """,
                        round(result["alpha_mu_ratio"], 6),
                        round(result["alpha"], 6),
                        round(result["mu"], 6),
                        round(result["beta"], 6),
                        round(result["log_likelihood"], 4),
                        int(result["n_leader_events"]),
                        row["leader_wallet"],
                        row["follower_wallet"],
                    )
                    updated += 1
            except Exception as e:
                logger.warning(
                    f"Failed to update hawkes columns for "
                    f"{row['leader_wallet']}→{row['follower_wallet']}: {e}"
                )

        logger.info(f"Hawkes (bivariate) batch complete: {updated} edges updated")
        return updated
