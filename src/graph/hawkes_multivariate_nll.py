"""
Multivariate Hawkes NLL — numerical core for Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.1 + § 2.1.

This module hosts the bulk of the math for the multivariate Hawkes
fitter so that ``src/graph/hawkes_multivariate.py`` can focus on the
``MultivariateHawkesFitter`` API surface. Extracted here:

  * Block-sparse mask construction.
  * Negative log-likelihood (closed-form integral + vectorised
    sum-log-intensity).

Performance design notes for the NLL: the dominant cost is
``np.exp`` on per-pair time-difference arrays. We use two paths:

  * **Small streams** (≤ 64 events on either side) → pairwise
    broadcast `(n_i, n_j)` matrix. Fastest at small n.
  * **Large streams** → vectorised cumsum-with-shift trick. The shift
    is the max of source AND target times, chosen so SOURCE
    exponents land in [0, 1] (no overflow) and TARGET exponents
    land in [0, β·T]. Both stay within float64 range for T up to
    ~30 days at β ≈ 1/300.

The math derivation for the cumsum path:

    Σ_{u < t} exp(-β(t - u))
        = exp(-β·t) · Σ_{u < t} exp(β·u)
        = exp(-β·(t - M)) · Σ_{u < t} exp(β·(u - M))           (shift)
        = exp(β·(M - t)) · cum[idx_t]                         where
          cum[k] = Σ_{m < k} exp(β·(t_j[m] - M)).

Numerical safety: both `exp(-βT)` (source side) and `exp(βT)` (target
side) are clipped to avoid float overflow / underflow.
"""

from __future__ import annotations

import numpy as np


# Numerical floor and "invalid NLL" sentinel — same constants used by
# the R5 bivariate fitter so both modules share semantics.
_PARAM_FLOOR = 1e-9
_INVALID_NLL = 1e10


def build_default_mask(n: int) -> np.ndarray:
    """Build the default block-sparse mask for the R9 setup.

    Per spec § 2.2 Box diagram:
        - diagonal (self-excitation):   True
        - column 0, row i>0 (leader→pool): True
        - row 0, column j>0 (pool→leader): False
        - off-diagonal pool↔pool:        False
        - cell (0,0) (leader self-excite): True

    Args:
        n: number of processes (1 leader + K pools = K+1).

    Returns:
        Boolean mask of shape (n, n) with True for FREE α entries.
    """
    mask = np.zeros((n, n), dtype=bool)
    if n <= 0:
        return mask
    for i in range(n):
        mask[i, i] = True
    for i in range(1, n):
        mask[i, 0] = True
    return mask


def multivariate_hawkes_nll(
    params: np.ndarray,
    times_by_proc: list[np.ndarray],
    free_idx: list[tuple[int, int]],
    window_end: float,
) -> float:
    """Negative log-likelihood for a block-sparse multivariate Hawkes.

    Parameter vector layout:

        params = [mu_0, mu_1, ..., mu_{N-1}, alpha_{i_1, j_1}, ..., beta]

    where the α entries are listed in the order of ``free_idx`` (which
    determines the mask). All α entries not in ``free_idx`` are
    implicitly 0.

    The intensity of process i:

        λ_i(t) = μ_i + Σ_{(i,j) in free_idx} α_{i,j} · S_j(t)

    where S_j(t) = Σ_{u ∈ t_j, u < t} exp(-β(t - u)).

    Args:
        params: flat vector [mu (N), alpha_free (M), beta (1)].
        times_by_proc: list of N sorted timestamp arrays in [0, T].
        free_idx: list of (i, j) tuples for free α entries.
        window_end: T.

    Returns:
        Float NLL. Returns _INVALID_NLL on any numerical violation.
    """
    n_proc = len(times_by_proc)
    n_free = len(free_idx)
    expected_len = n_proc + n_free + 1
    if len(params) != expected_len:
        return _INVALID_NLL

    mu = params[:n_proc]
    alpha_free = params[n_proc : n_proc + n_free]
    beta = params[n_proc + n_free]

    # Bounds guards.
    if beta <= _PARAM_FLOOR:
        return _INVALID_NLL
    if np.any(mu < _PARAM_FLOOR):
        return _INVALID_NLL
    if np.any(alpha_free < 0.0):
        return _INVALID_NLL

    # Reconstruct sparse α matrix for fast lookup.
    alpha_mat = np.zeros((n_proc, n_proc), dtype=float)
    for k, (i, j) in enumerate(free_idx):
        alpha_mat[i, j] = alpha_free[k]

    # ---- Integral term ∫_0^T λ_i(s) ds ----
    integ_decay = np.zeros(n_proc, dtype=float)
    for j in range(n_proc):
        tj = times_by_proc[j]
        if tj.size == 0:
            continue
        gaps = np.clip(window_end - tj, 0.0, 700.0 / max(beta, _PARAM_FLOOR))
        integ_decay[j] = float(np.sum(1.0 - np.exp(-beta * gaps)))

    integral = float(np.sum(mu) * window_end)
    for (i, j) in free_idx:
        integral += (alpha_mat[i, j] / beta) * integ_decay[j]

    # ---- Sum-log-intensity term ----
    log_sum = 0.0

    for i in range(n_proc):
        t_i = times_by_proc[i]
        n_i = t_i.size
        if n_i == 0:
            continue

        S_per_pair = np.zeros((n_i,), dtype=float)
        for k, (ii, jj) in enumerate(free_idx):
            if ii != i:
                continue
            alpha_kij = alpha_free[k]
            if alpha_kij == 0.0:
                continue
            t_j = times_by_proc[jj]
            if t_j.size == 0:
                continue

            idx = np.searchsorted(t_j, t_i, side="left")

            if t_j.size <= 64 or n_i <= 64:
                # Small streams: pairwise broadcast.
                gaps = t_i[:, None] - t_j[None, :]
                mask = gaps > 0
                exps = np.where(
                    mask,
                    np.exp(
                        -beta
                        * np.clip(gaps, 0.0, 700.0 / max(beta, _PARAM_FLOOR))
                    ),
                    0.0,
                )
                S_j_at_target = exps.sum(axis=1)
            else:
                # Large streams: vectorised cumsum-with-shift.
                M = max(float(t_j[-1]), float(t_i[-1]))
                u_exp_shifted = np.exp(
                    np.clip(beta * (t_j - M), -700.0, 0.0)
                )
                cum = np.concatenate(([0.0], np.cumsum(u_exp_shifted)))
                prefactor = np.exp(
                    np.clip(beta * (M - t_i), 0.0, 700.0)
                )
                S_j_at_target = prefactor * cum[idx]
                S_j_at_target = np.where(idx > 0, S_j_at_target, 0.0)
                S_j_at_target = np.where(
                    np.isfinite(S_j_at_target), S_j_at_target, 0.0
                )

            S_per_pair += alpha_kij * S_j_at_target

        lam_i = mu[i] + S_per_pair
        if np.any(lam_i <= 0.0):
            return _INVALID_NLL
        log_sum += float(np.log(lam_i).sum())

    nll = integral - log_sum
    if not np.isfinite(nll):
        return _INVALID_NLL
    return float(nll)


__all__ = [
    "_PARAM_FLOOR",
    "_INVALID_NLL",
    "build_default_mask",
    "multivariate_hawkes_nll",
]
