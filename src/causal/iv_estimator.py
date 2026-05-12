"""Two-Stage Least Squares (2SLS) estimator for causal effect identification.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.2.

The 2SLS procedure:

    Stage 1: regress leader trade intensity L on instruments Z
        L_t = pi_0 + Z_t @ pi + e1_t
        -> L_hat_t (predicted L from instruments alone)

    Stage 2: regress follower intensity F on L_hat plus exogenous controls X
        F_t = b_0 + b_L * L_hat_t + X_t @ b_X + e2_t
        -> b_L is the causal Average Treatment Effect (ATE)

Diagnostics live in :mod:`src.causal.iv_diagnostics`:
    * First-stage F-statistic on the joint significance of Z in stage 1.
      Threshold: > 10 = strong instruments (Staiger-Stock 1997).
    * Wu-Hausman test: null = OLS == 2SLS, alternative = OLS biased
      (endogeneity present). Small p -> IV correction is doing real work.

Bootstrap: 1000-resample non-parametric percentile bootstrap for 95%
CI on b_L. Configurable via ``bootstrap_n`` (tests drop to 100 for
speed).

Implementation: pure numpy (statsmodels imported defensively when
available for cross-check helpers; production path stays numpy-only
so the estimator runs in any minimal env). Math is sound; the
**application** is the hard part — see the methodology audit gate
in spec § 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from loguru import logger

from src.causal.iv_diagnostics import (
    _add_intercept,
    _ols_fit,
    first_stage_f_stat,
    wu_hausman_test,
)

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class IVEstimate:
    """Output of one 2SLS fit. Mirrors spec § 3.2 contract."""

    ate: float
    """Causal effect estimate (b_L in stage 2)."""

    ci_low: float
    """Lower bound of 95% bootstrap CI on ATE."""

    ci_high: float
    """Upper bound of 95% bootstrap CI on ATE."""

    wu_hausman_p: float
    """Wu-Hausman test p-value. Null: OLS == 2SLS (no endogeneity)."""

    first_stage_f: float
    """First-stage F-statistic on the joint significance of Z."""

    instruments_used: list[str] = field(default_factory=list)
    """Names of the instruments active in this fit."""

    n_samples: int = 0
    """Number of observation rows used."""

    n_instruments: int = 0
    """Number of instruments (columns of Z)."""

    n_exogenous_controls: int = 0
    """Number of exogenous control columns in X (excluding constant)."""

    convergence: str = "converged"
    """'converged' | 'weak_instruments' | 'failed'."""

    bootstrap_n: int = 1000
    """How many bootstrap resamples produced the CI."""

    # Side-channel diagnostics. Useful in tests; not persisted to DB.
    ols_coef: Optional[float] = None
    """OLS coefficient on L (for Wu-Hausman comparison)."""

    stage1_coefficients: Optional[np.ndarray] = None
    """pi vector from stage 1 (used by bootstrap)."""


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------


class TwoStageLeastSquaresEstimator:
    """Two-Stage Least Squares for causal effect estimation.

    Public surface: ``fit(L, F, Z, X=None, instrument_names=None) -> IVEstimate``.

    The estimator is stateless beyond the constructor knobs; the caller
    constructs once per worker and calls ``fit`` per (leader, pool_class)
    pair.

    Parameters
    ----------
    bootstrap_n : int
        Number of bootstrap resamples for the 95% CI. Default 1000;
        test fixtures drop to 100 for ~10x speedup.
    weak_instrument_f_threshold : float
        First-stage F below this is flagged as 'weak_instruments' in
        ``IVEstimate.convergence``. Default 10 (Staiger-Stock 1997).
    rng : numpy.random.Generator | None
        Optional RNG for reproducible bootstrap. ``None`` = fresh default.
    """

    def __init__(
        self,
        bootstrap_n: int = 1000,
        weak_instrument_f_threshold: float = 10.0,
        rng: Optional[np.random.Generator] = None,
    ) -> None:
        if bootstrap_n < 10:
            raise ValueError(
                f"bootstrap_n must be >= 10, got {bootstrap_n}. "
                "Below 10 the CI is essentially noise."
            )
        if weak_instrument_f_threshold < 0:
            raise ValueError(
                f"weak_instrument_f_threshold must be >= 0, got "
                f"{weak_instrument_f_threshold}."
            )
        self.bootstrap_n = int(bootstrap_n)
        self.weak_instrument_f_threshold = float(weak_instrument_f_threshold)
        self._rng = rng if rng is not None else np.random.default_rng()

    # ------------------------------------------------------------------ #
    # Headline fit                                                       #
    # ------------------------------------------------------------------ #

    def fit(
        self,
        L: np.ndarray,
        F: np.ndarray,
        Z: np.ndarray,
        X: Optional[np.ndarray] = None,
        instrument_names: Optional[list[str]] = None,
    ) -> IVEstimate:
        """Fit 2SLS for the causal effect of L on F using instruments Z.

        Parameters
        ----------
        L : (n,) ndarray
            Endogenous regressor (leader trade intensity).
        F : (n,) ndarray
            Outcome (follower trade intensity).
        Z : (n, q) ndarray
            Instrument matrix. q must be >= 1; q == 1 = just-identified,
            q > 1 = over-identified (allows Hansen J test in future).
        X : (n, p) ndarray, optional
            Exogenous control columns (excluding constant; added
            internally). e.g. news_event indicator, market_state vector,
            time-of-day cyclical features.
        instrument_names : list[str], optional
            Names of the instruments (for IVEstimate.instruments_used).
            If None, generic ``z_0, z_1, ...`` names are used.

        Returns
        -------
        IVEstimate
        """
        L = np.asarray(L, dtype=float).flatten()
        F = np.asarray(F, dtype=float).flatten()
        Z = np.atleast_2d(np.asarray(Z, dtype=float))
        if Z.shape[0] != L.shape[0]:
            Z = Z.T
        if X is not None:
            X = np.atleast_2d(np.asarray(X, dtype=float))
            if X.shape[0] != L.shape[0]:
                X = X.T
        n = L.shape[0]
        q = Z.shape[1]
        p = X.shape[1] if X is not None else 0

        if instrument_names is None:
            instrument_names = [f"z_{i}" for i in range(q)]

        if F.shape[0] != n:
            raise ValueError(
                f"L and F must have same length, got {L.shape[0]} vs {F.shape[0]}"
            )
        if X is not None and X.shape[0] != n:
            raise ValueError(
                f"L and X must have same length, got {L.shape[0]} vs {X.shape[0]}"
            )

        # Wave-3 hardening: drop rows with NaN/Inf in any input column.
        # numpy.linalg.lstsq raises SVD-did-not-converge on NaN; the
        # production daemon's _load_streams could feed such rows from a
        # malformed timestamp. Fail-soft to 'failed' convergence instead
        # of bubbling LinAlgError out of the daemon.
        finite = np.isfinite(L) & np.isfinite(F) & np.all(np.isfinite(Z), axis=1)
        if X is not None:
            finite &= np.all(np.isfinite(X), axis=1)
        if not finite.all():
            L, F, Z = L[finite], F[finite], Z[finite]
            if X is not None:
                X = X[finite]
            n = int(finite.sum())

        # Need at least: 1 (intercept) + p (X) + 1 (L) + q (Z first stage)
        # rows of data. Tight lower bound is n >= q + p + 3 for any
        # variance estimate to be defined; we use that as the hard floor.
        if n < q + p + 3:
            logger.warning(
                f"TwoStageLeastSquaresEstimator: n={n} below minimum "
                f"q+p+3={q+p+3}; returning 'failed' convergence."
            )
            return IVEstimate(
                ate=float("nan"),
                ci_low=float("nan"),
                ci_high=float("nan"),
                wu_hausman_p=float("nan"),
                first_stage_f=float("nan"),
                instruments_used=instrument_names,
                n_samples=n,
                n_instruments=q,
                n_exogenous_controls=p,
                convergence="failed",
                bootstrap_n=self.bootstrap_n,
            )

        # ── First stage ────────────────────────────────────────────── #
        # L = c + X @ delta + Z @ pi + e1
        if X is not None:
            stage1_X = _add_intercept(np.column_stack([X, Z]))
        else:
            stage1_X = _add_intercept(Z)
        stage1_coefs, _ = _ols_fit(L, stage1_X)
        L_hat = stage1_X @ stage1_coefs

        f_stat = first_stage_f_stat(L, Z, X)

        # ── Second stage ───────────────────────────────────────────── #
        # F = c + b_L * L_hat + X @ b_X + e2
        if X is not None:
            stage2_X = _add_intercept(np.column_stack([L_hat, X]))
        else:
            stage2_X = _add_intercept(L_hat)
        stage2_coefs, stage2_resid = _ols_fit(F, stage2_X)
        # Coefficient on L_hat is at index 1 (after the intercept).
        ate = float(stage2_coefs[1])

        # Variance of the 2SLS coefficient. Use the conventional 2SLS
        # variance estimator: V_2SLS = sigma^2 * (X_hat' X_hat)^-1, where
        # X_hat = [1, L_hat, X] (the same matrix used in stage 2) and
        # sigma^2 is the second-stage residual variance.
        df_resid = max(1, n - stage2_X.shape[1])
        sigma2_2sls = float(np.dot(stage2_resid, stage2_resid) / df_resid)
        try:
            xtx_inv = np.linalg.inv(stage2_X.T @ stage2_X)
            tsls_var = float(sigma2_2sls * xtx_inv[1, 1])
        except np.linalg.LinAlgError:
            tsls_var = float("nan")

        # ── OLS for Wu-Hausman ─────────────────────────────────────── #
        if X is not None:
            ols_X = _add_intercept(np.column_stack([L, X]))
        else:
            ols_X = _add_intercept(L)
        ols_coefs, ols_resid = _ols_fit(F, ols_X)
        ols_coef = float(ols_coefs[1])
        ols_df = max(1, n - ols_X.shape[1])
        sigma2_ols = float(np.dot(ols_resid, ols_resid) / ols_df)
        try:
            ols_xtx_inv = np.linalg.inv(ols_X.T @ ols_X)
            ols_var = float(sigma2_ols * ols_xtx_inv[1, 1])
        except np.linalg.LinAlgError:
            ols_var = float("nan")

        wh_p = wu_hausman_test(ols_coef, ate, ols_var, tsls_var)

        # ── Bootstrap CI ───────────────────────────────────────────── #
        ci_low, ci_high = self._bootstrap_ci(L, F, Z, X)

        # ── Convergence flag ───────────────────────────────────────── #
        if f_stat < self.weak_instrument_f_threshold:
            convergence = "weak_instruments"
        else:
            convergence = "converged"

        return IVEstimate(
            ate=ate,
            ci_low=ci_low,
            ci_high=ci_high,
            wu_hausman_p=wh_p,
            first_stage_f=f_stat,
            instruments_used=list(instrument_names),
            n_samples=n,
            n_instruments=q,
            n_exogenous_controls=p,
            convergence=convergence,
            bootstrap_n=self.bootstrap_n,
            ols_coef=ols_coef,
            stage1_coefficients=stage1_coefs,
        )

    # ------------------------------------------------------------------ #
    # Bootstrap CI                                                       #
    # ------------------------------------------------------------------ #

    def _bootstrap_ci(
        self,
        L: np.ndarray,
        F: np.ndarray,
        Z: np.ndarray,
        X: Optional[np.ndarray],
    ) -> tuple[float, float]:
        """Non-parametric percentile bootstrap CI on the ATE.

        Resamples rows with replacement, re-fits 2SLS on each resample,
        returns the 2.5 / 97.5 percentile of the bootstrap distribution
        of b_L.
        """
        n = L.shape[0]
        ates = np.empty(self.bootstrap_n, dtype=float)
        ates[:] = np.nan

        for i in range(self.bootstrap_n):
            idx = self._rng.integers(0, n, size=n)
            try:
                ates[i] = self._fit_single_ate(
                    L[idx],
                    F[idx],
                    Z[idx],
                    X[idx] if X is not None else None,
                )
            except Exception:
                # Singular resample (e.g. all-ones bootstrap sample);
                # leave NaN. With 1000 resamples a handful of NaNs is
                # tolerable.
                continue

        valid = ates[~np.isnan(ates)]
        if valid.size < max(10, int(self.bootstrap_n * 0.05)):
            logger.warning(
                f"Bootstrap CI: only {valid.size}/{self.bootstrap_n} "
                "valid resamples; returning NaN CI."
            )
            return float("nan"), float("nan")
        lo = float(np.percentile(valid, 2.5))
        hi = float(np.percentile(valid, 97.5))
        return lo, hi

    @staticmethod
    def _fit_single_ate(
        L: np.ndarray,
        F: np.ndarray,
        Z: np.ndarray,
        X: Optional[np.ndarray],
    ) -> float:
        """Bare-bones 2SLS that returns just the ATE. Hot inner loop."""
        if X is not None:
            stage1_X = _add_intercept(np.column_stack([X, Z]))
        else:
            stage1_X = _add_intercept(Z)
        stage1_coefs, _ = _ols_fit(L, stage1_X)
        L_hat = stage1_X @ stage1_coefs
        if X is not None:
            stage2_X = _add_intercept(np.column_stack([L_hat, X]))
        else:
            stage2_X = _add_intercept(L_hat)
        stage2_coefs, _ = _ols_fit(F, stage2_X)
        return float(stage2_coefs[1])


__all__ = [
    "IVEstimate",
    "TwoStageLeastSquaresEstimator",
    "first_stage_f_stat",
    "wu_hausman_test",
]
