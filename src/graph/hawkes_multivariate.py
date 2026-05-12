"""
Multivariate Hawkes Process Fitter — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.1.

Generalises the R5 bivariate fitter to N processes with a block-sparse
mask (process 0 = leader, processes 1..K = strategy-clustered pools).
Per spec § 2.2 the mask is: diagonal + first column for i > 0 (the
leader→pool entries). The NLL math is in
``src/graph/hawkes_multivariate_nll.py``; this file owns the fitter
API surface. R5 (``src/graph/hawkes_fitter.py``) is left untouched.

References:
    Daley, D. J. & Vere-Jones, D. (2003). An Introduction to the Theory
    of Point Processes, vol. I, ch. 7.
    Bacry, E., Mastromatteo, I., Muzy, J.-F. (2015). Hawkes processes
    in finance. Market Microstructure and Liquidity 1(1).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np
from loguru import logger
from scipy.optimize import minimize

from src.graph.hawkes_multivariate_nll import (
    _INVALID_NLL,
    _PARAM_FLOOR,
    build_default_mask,
    multivariate_hawkes_nll,
)


# ---------------------------------------------------------------------------
# Tuning constants. Module-level so tests can monkey-patch.
# ---------------------------------------------------------------------------

# Default exponential-decay half-life seed (5 min, matches the R5 fitter
# and FOLLOWER_WINDOW_S so the kernel actually peaks inside the audit's
# causal window). Optimiser is free to walk away from this seed.
DEFAULT_HALFLIFE_S = 300.0

# Minimum events per process to make the fit defined. Below this we
# return a degenerate Poisson μ-only fit per the spec § 3.1 fallback
# semantics.
MIN_EVENTS_PER_PROCESS = 5

# Minimum total events to attempt a meaningful BIC test. Below this
# the log(N) penalty is tiny enough that null+full are indistinguishable.
MIN_TOTAL_EVENTS_FOR_BIC = 20


@dataclass
class MultivariateHawkesResult:
    """Structured result of one multivariate fit.

    The headline dict returned by ``MultivariateHawkesFitter.fit_arrays``
    is a plain dict mirroring this shape so the JSON persistence path
    in the daemon can ``json.dumps(...)`` without dataclass-aware code.
    """

    alpha_matrix: dict[tuple[int, int], float]
    mu_vector: dict[int, float]
    beta: float
    log_likelihood: float
    bic_threshold: float
    bic_statistic: float
    accepted_couplings: dict[tuple[int, int], bool]
    convergence: str
    n_events_total: int = 0
    process_labels: list[str] = field(default_factory=list)


class MultivariateHawkesFitter:
    """N-dim Hawkes MLE with block-sparse priors and BIC model selection.

    Construction:

        fitter = MultivariateHawkesFitter(
            n_processes=5,                  # 1 leader + 4 pools
            mask=None,                      # None = default block-sparse
            k_penalty=None,                 # None = count free entries
            halflife_s=300.0,
        )

    Fit interface:

        result = fitter.fit_arrays(
            times_by_proc=[leader_t, pool0_t, pool1_t, ...],
            window=T,
            process_labels=["leader", "directional", ...],
        )

    Returns a dict (NOT MultivariateHawkesResult — JSON-friendly):

        {
          'alpha_matrix':       {(i, j): float},   # only free entries
          'mu_vector':          {i: float},
          'beta':               float,
          'log_likelihood':     float,             # full model
          'bic_threshold':      float,
          'bic_statistic':      float,             # 2·(NLL_null - NLL_full)
          'accepted_couplings': {(i, j): bool},
          'convergence':        'converged' | 'fallback' | 'bic_rejected',
          'n_events_total':     int,
          'process_labels':     list[str],
        }

    Design notes:
        - The optimiser is L-BFGS-B with bounded constraints (β capped
          at 1.0 s^-1 to rule out the degenerate kernel-collapses-to-
          delta branch). Fallback is Nelder-Mead.
        - Seeds: H0 (all α=0), all-on, plus targeted leader→pool
          seeds at moderate and strong magnitudes.
        - The R5 univariate machinery is untouched. This class
          NEVER imports or wraps src.graph.hawkes_fitter — the NLL
          lives in ``src.graph.hawkes_multivariate_nll`` for separation.
    """

    def __init__(
        self,
        n_processes: int,
        mask: Optional[np.ndarray] = None,
        k_penalty: Optional[int] = None,
        halflife_s: float = DEFAULT_HALFLIFE_S,
        max_iter: int = 300,
    ) -> None:
        if n_processes < 1:
            raise ValueError(f"n_processes must be >= 1, got {n_processes}")
        self.n_processes = int(n_processes)

        if mask is None:
            mask = build_default_mask(n_processes)
        else:
            mask = np.asarray(mask, dtype=bool)
            if mask.shape != (n_processes, n_processes):
                raise ValueError(
                    f"mask shape {mask.shape} != ({n_processes}, {n_processes})"
                )
        self.mask = mask

        # Free entries in (i, j) order — canonical parameter layout.
        self.free_idx: list[tuple[int, int]] = [
            (i, j)
            for i in range(n_processes)
            for j in range(n_processes)
            if mask[i, j]
        ]
        self.n_free = len(self.free_idx)

        self.k_penalty = int(k_penalty if k_penalty is not None else self.n_free)
        self.halflife_s = float(halflife_s)
        self.max_iter = int(max_iter)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def fit_arrays(
        self,
        times_by_proc: Iterable[np.ndarray],
        window: Optional[float] = None,
        process_labels: Optional[list[str]] = None,
    ) -> dict:
        """Fit the multivariate Hawkes on the provided event streams.

        Args:
            times_by_proc: iterable of N float arrays, sorted-or-sortable.
            window: observation horizon T. If None, set to max event time.
            process_labels: optional human-readable labels.

        Returns:
            Result dict per the class docstring.
        """
        proc_list: list[np.ndarray] = [
            np.sort(np.asarray(t, dtype=float)) for t in times_by_proc
        ]
        if len(proc_list) != self.n_processes:
            raise ValueError(
                f"times_by_proc has {len(proc_list)} streams, expected "
                f"{self.n_processes}"
            )

        # Normalise: anchor origin at min, compute window if missing.
        all_times = (
            np.concatenate([t for t in proc_list if t.size > 0])
            if any(t.size > 0 for t in proc_list)
            else np.array([0.0])
        )
        t0 = float(all_times.min())
        t_end_raw = float(all_times.max())
        proc_norm = [t - t0 if t.size > 0 else t for t in proc_list]
        T = float(window) if window is not None else max(t_end_raw - t0, 1.0)
        if T <= 0.0:
            T = 1.0

        n_events_total = int(sum(t.size for t in proc_norm))
        process_labels = process_labels or [
            f"proc_{i}" for i in range(self.n_processes)
        ]

        # Degenerate guards.
        if all(t.size < MIN_EVENTS_PER_PROCESS for t in proc_norm):
            return self._degenerate_result(
                proc_norm, T, process_labels, "fallback", n_events_total
            )

        # ---- Fit ----
        start = _time.perf_counter()
        full_x, full_nll, full_label = self._fit(proc_norm, T)
        elapsed = _time.perf_counter() - start

        # ---- BIC vs null model (all α = 0) ----
        null_x, null_nll = self._fit_null(proc_norm, T)
        bic_threshold = (
            self.k_penalty * float(np.log(max(n_events_total, 2)))
            if n_events_total >= MIN_TOTAL_EVENTS_FOR_BIC
            else float("inf")
        )

        if (
            np.isfinite(full_nll)
            and np.isfinite(null_nll)
            and full_nll < _INVALID_NLL / 2
            and null_nll < _INVALID_NLL / 2
        ):
            bic_statistic = 2.0 * (null_nll - full_nll)
        else:
            bic_statistic = 0.0

        if full_x is None or not np.isfinite(full_nll):
            return self._degenerate_result(
                proc_norm, T, process_labels, "failed", n_events_total
            )

        if (
            bic_statistic < bic_threshold
            and n_events_total >= MIN_TOTAL_EVENTS_FOR_BIC
        ):
            chosen_x = null_x if null_x is not None else full_x
            chosen_nll = null_nll
            convergence = "bic_rejected"
            chosen_alpha = {ij: 0.0 for ij in self.free_idx}
            accepted = {ij: False for ij in self.free_idx}
        else:
            chosen_x = full_x
            chosen_nll = full_nll
            convergence = full_label
            chosen_alpha = {
                ij: float(chosen_x[self.n_processes + k])
                for k, ij in enumerate(self.free_idx)
            }
            # Per-spec § 2.3 accepted-couplings semantics — see audit
            # doc § 6 for the trade-off we make here.
            accepted = {ij: chosen_alpha[ij] > 1e-6 for ij in self.free_idx}

        mu_vec = {i: float(chosen_x[i]) for i in range(self.n_processes)}
        beta_out = (
            float(chosen_x[-1])
            if chosen_x is not None
            else 1.0 / self.halflife_s
        )

        log_lik = -chosen_nll if np.isfinite(chosen_nll) else float("nan")

        logger.debug(
            f"MultivariateHawkesFitter: fit n={self.n_processes} "
            f"events={n_events_total} bic={bic_statistic:.2f} "
            f"thresh={bic_threshold:.2f} conv={convergence} "
            f"elapsed={elapsed:.2f}s"
        )

        return {
            "alpha_matrix": chosen_alpha,
            "mu_vector": mu_vec,
            "beta": beta_out,
            "log_likelihood": float(log_lik) if np.isfinite(log_lik) else 0.0,
            "bic_threshold": float(bic_threshold),
            "bic_statistic": float(bic_statistic),
            "accepted_couplings": accepted,
            "convergence": convergence,
            "n_events_total": n_events_total,
            "process_labels": list(process_labels),
        }

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _fit(
        self,
        proc_norm: list[np.ndarray],
        T: float,
    ) -> tuple[Optional[np.ndarray], float, str]:
        """Optimise the full multivariate NLL.

        Returns (best_params, best_nll, convergence_label).
        """
        seed_beta = 1.0 / max(self.halflife_s, 1.0)
        emp_mu = np.array(
            [
                max(float(t.size) / T, _PARAM_FLOOR * 10.0)
                for t in proc_norm
            ],
            dtype=float,
        )

        seeds: list[np.ndarray] = []

        def _make_seed(alpha_init: np.ndarray) -> np.ndarray:
            return np.concatenate(
                [emp_mu.copy(), alpha_init.copy(), [seed_beta]]
            )

        # H0 seed: all α = 0.
        seeds.append(_make_seed(np.zeros(self.n_free)))
        # All-on seed: each α = 0.1·μ_diag.
        seeds.append(
            _make_seed(
                np.array(
                    [
                        0.1 * emp_mu[ij[0]]
                        if emp_mu[ij[0]] > 0
                        else 0.01
                        for ij in self.free_idx
                    ]
                )
            )
        )
        # Two targeted seeds per leader→pool entry (moderate + strong
        # coupling), so the optimiser explores both NLL basins.
        for k, (i, j) in enumerate(self.free_idx):
            if j == 0 and i > 0:  # leader → pool
                base = max(emp_mu[i], 0.001)
                a_mod = np.zeros(self.n_free)
                a_mod[k] = 0.5 * base
                seeds.append(_make_seed(a_mod))
                a_strong = np.zeros(self.n_free)
                a_strong[k] = 5.0 * base
                seeds.append(_make_seed(a_strong))

        # Bounds: μ > 0, α >= 0, β > 0, β <= 1.0 (s^-1).
        # The β upper bound rules out the degenerate "kernel collapses
        # to a delta at zero gap" branch. 1 s^-1 corresponds to a 1-s
        # half-life which is faster than any realistic coupling.
        bounds = (
            [(_PARAM_FLOOR, None)] * self.n_processes
            + [(0.0, None)] * self.n_free
            + [(_PARAM_FLOOR, 1.0)]
        )

        best_x: Optional[np.ndarray] = None
        best_nll: float = float("inf")

        for x0 in seeds:
            try:
                res = minimize(
                    multivariate_hawkes_nll,
                    x0,
                    args=(proc_norm, self.free_idx, T),
                    method="L-BFGS-B",
                    bounds=bounds,
                    options={"maxiter": self.max_iter, "ftol": 1e-9},
                )
                if (
                    res.success
                    and np.isfinite(res.fun)
                    and res.fun < best_nll
                ):
                    best_nll = float(res.fun)
                    best_x = np.asarray(res.x, dtype=float)
            except Exception as exc:  # pragma: no cover — scipy edge cases
                logger.debug(f"L-BFGS-B restart failed: {exc}")

        if best_x is not None and best_nll < _INVALID_NLL / 2:
            return best_x, best_nll, "converged"

        # Fallback: Nelder-Mead.
        try:
            res = minimize(
                multivariate_hawkes_nll,
                seeds[0],
                args=(proc_norm, self.free_idx, T),
                method="Nelder-Mead",
                options={"maxiter": 2000, "xatol": 1e-6, "fatol": 1e-6},
            )
            if (
                res.success
                and np.isfinite(res.fun)
                and res.fun < _INVALID_NLL / 2
            ):
                x = np.asarray(res.x, dtype=float)
                # Clamp to bounds (Nelder-Mead ignores them).
                x[: self.n_processes] = np.clip(
                    x[: self.n_processes], _PARAM_FLOOR, None
                )
                x[self.n_processes : self.n_processes + self.n_free] = np.clip(
                    x[self.n_processes : self.n_processes + self.n_free],
                    0.0,
                    None,
                )
                x[-1] = max(min(x[-1], 1.0), _PARAM_FLOOR)
                return x, float(res.fun), "fallback"
        except Exception as exc:  # pragma: no cover
            logger.debug(f"Nelder-Mead fallback failed: {exc}")

        return None, float("inf"), "failed"

    def _fit_null(
        self,
        proc_norm: list[np.ndarray],
        T: float,
    ) -> tuple[Optional[np.ndarray], float]:
        """Fit the null model (all α = 0).

        Closed form: μ_i = N_i / T. We evaluate the NLL through the same
        shared kernel so the BIC test compares like-for-like.
        """
        seed_beta = 1.0 / max(self.halflife_s, 1.0)
        emp_mu = np.array(
            [
                max(float(t.size) / T, _PARAM_FLOOR * 10.0)
                for t in proc_norm
            ],
            dtype=float,
        )
        x = np.concatenate([emp_mu, np.zeros(self.n_free), [seed_beta]])
        nll = multivariate_hawkes_nll(x, proc_norm, self.free_idx, T)
        if not np.isfinite(nll):
            return None, float("inf")
        return x, float(nll)

    def _degenerate_result(
        self,
        proc_norm: list[np.ndarray],
        T: float,
        process_labels: list[str],
        convergence: str,
        n_events_total: int,
    ) -> dict:
        """Build a Poisson-only result when the fit can't proceed."""
        emp_mu = {
            i: max(float(t.size) / T, _PARAM_FLOOR)
            for i, t in enumerate(proc_norm)
        }
        log_lik = 0.0
        for i, mu_i in emp_mu.items():
            n_i = proc_norm[i].size
            log_lik += -mu_i * T + n_i * np.log(max(mu_i, _PARAM_FLOOR))
        return {
            "alpha_matrix": {ij: 0.0 for ij in self.free_idx},
            "mu_vector": emp_mu,
            "beta": 1.0 / max(self.halflife_s, 1.0),
            "log_likelihood": float(log_lik) if np.isfinite(log_lik) else 0.0,
            "bic_threshold": 0.0,
            "bic_statistic": 0.0,
            "accepted_couplings": {ij: False for ij in self.free_idx},
            "convergence": convergence,
            "n_events_total": int(n_events_total),
            "process_labels": list(process_labels),
        }


__all__ = [
    "MultivariateHawkesFitter",
    "MultivariateHawkesResult",
    "build_default_mask",
    "multivariate_hawkes_nll",
    "MIN_EVENTS_PER_PROCESS",
    "MIN_TOTAL_EVENTS_FOR_BIC",
    "DEFAULT_HALFLIFE_S",
]
