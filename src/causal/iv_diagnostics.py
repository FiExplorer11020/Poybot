"""Diagnostic helpers for the 2SLS estimator.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.2.

Split out of ``src/causal/iv_estimator.py`` so the main estimator
file stays under the 500-LOC project limit. Two public functions:

  * :func:`first_stage_f_stat` — Joint F-test for instrument
    significance in the first-stage regression.
  * :func:`wu_hausman_test`    — p-value for OLS vs 2SLS difference
    under the null of no endogeneity.

Plus two internal helpers (``_ols_fit``, ``_add_intercept``) reused
by both the diagnostics and the estimator itself.
"""

from __future__ import annotations

from math import erfc, sqrt
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Internal linear-algebra helpers
# ---------------------------------------------------------------------------


def _ols_fit(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """OLS via numpy.linalg.lstsq.

    Returns
    -------
    coefs : (k,) ndarray
    residuals : (n,) ndarray
    """
    # lstsq solves min ||X @ coefs - y||_2; uses SVD so it's numerically
    # robust even when X has near-rank-deficiency.
    coefs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    residuals = y - X @ coefs
    return coefs, residuals


def _add_intercept(X: np.ndarray) -> np.ndarray:
    """Prepend a column of ones to X."""
    n = X.shape[0]
    return np.column_stack([np.ones(n), X])


# ---------------------------------------------------------------------------
# Public diagnostics
# ---------------------------------------------------------------------------


def first_stage_f_stat(
    L: np.ndarray,
    Z: np.ndarray,
    X: Optional[np.ndarray] = None,
) -> float:
    """First-stage F-stat: joint significance of Z in regressing L on (X, Z).

    Tests H0: all coefficients on Z are zero.

    Parameters
    ----------
    L : (n,) ndarray
        Endogenous regressor (leader trade intensity).
    Z : (n, q) ndarray
        Instrument matrix (q instruments).
    X : (n, p) ndarray, optional
        Exogenous controls (excluding constant; added internally).
        Pass ``None`` if no exogenous controls.

    Returns
    -------
    F : float
        Joint F-statistic. > 10 = strong instruments (Staiger-Stock 1997).
    """
    n = L.shape[0]
    Z = np.atleast_2d(Z)
    if Z.shape[0] != n:
        Z = Z.T
    q = Z.shape[1]
    if X is not None:
        X = np.atleast_2d(X)
        if X.shape[0] != n:
            X = X.T
        full = _add_intercept(np.column_stack([X, Z]))
        restricted = _add_intercept(X)
    else:
        full = _add_intercept(Z)
        restricted = np.ones((n, 1))

    _, full_resid = _ols_fit(L, full)
    _, rest_resid = _ols_fit(L, restricted)

    rss_full = float(np.dot(full_resid, full_resid))
    rss_rest = float(np.dot(rest_resid, rest_resid))

    # F = ((RSS_r - RSS_u) / q) / (RSS_u / (n - k))
    k = full.shape[1]
    df_resid = max(1, n - k)
    if rss_full <= 0:
        return float("inf")
    num = (rss_rest - rss_full) / max(1, q)
    den = rss_full / df_resid
    if den <= 0:
        return float("inf")
    return float(num / den)


def wu_hausman_test(
    ols_coef: float,
    tsls_coef: float,
    ols_var: float,
    tsls_var: float,
) -> float:
    """Wu-Hausman test p-value.

    H0: OLS and 2SLS are both consistent (no endogeneity, OLS is
    efficient). H1: OLS is inconsistent (endogeneity present, 2SLS is
    consistent but inefficient).

    The Hausman statistic:
        H = (b_OLS - b_2SLS)^2 / (V_2SLS - V_OLS)

    Under H0, H ~ chi2(1). The variance difference must be positive
    (2SLS is less efficient under H0); when V_2SLS <= V_OLS due to
    finite-sample noise we return p=1.0 (cannot reject H0).

    Parameters
    ----------
    ols_coef : float
        OLS coefficient on the endogenous regressor.
    tsls_coef : float
        2SLS coefficient on the same regressor.
    ols_var : float
        Variance of the OLS coefficient.
    tsls_var : float
        Variance of the 2SLS coefficient.

    Returns
    -------
    p_value : float in [0, 1]
    """
    diff = ols_coef - tsls_coef
    var_diff = tsls_var - ols_var
    if var_diff <= 0:
        return 1.0
    h_stat = (diff * diff) / var_diff
    # Survival function of chi2(1) at h_stat.
    # chi2(1).sf(x) = erfc(sqrt(x/2)) for x >= 0.
    if h_stat < 0:
        return 1.0
    return float(erfc(sqrt(h_stat / 2.0)))


__all__ = [
    "_ols_fit",
    "_add_intercept",
    "first_stage_f_stat",
    "wu_hausman_test",
]
