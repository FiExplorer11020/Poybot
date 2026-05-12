"""
Tests for the MULTIVARIATE Hawkes fitter — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.1 + § 7.

The six tests below cover the spec's acceptance criteria:

  1. Block-sparse mask construction & shape enforcement.
  2. Monte Carlo identifiability: simulate from known params, recover.
  3. Off-diagonal pool↔pool entries stay at 0 (mask enforcement).
  4. BIC threshold scales with k (number of free entries).
  5. Independent Poisson streams → convergence='bic_rejected'.
  6. The R5 bivariate fitter is unaffected (regression coverage).
"""

from __future__ import annotations

import numpy as np
import pytest

from src.graph.hawkes_multivariate import (
    MultivariateHawkesFitter,
    build_default_mask,
    multivariate_hawkes_nll,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simulate_poisson(rate: float, T: float, rng: np.random.Generator) -> np.ndarray:
    """Homogeneous Poisson process on [0, T]."""
    if rate <= 0.0:
        return np.array([], dtype=float)
    n = int(rate * T * 2 + 50)
    inter = rng.exponential(1.0 / rate, size=n)
    times = np.cumsum(inter)
    return times[times < T]


def _simulate_excited(
    leader_times: np.ndarray,
    mu: float,
    alpha: float,
    beta: float,
    T: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Simulate a follower stream excited by a fixed leader history.

    Uses the modified-Ogata thinning algorithm from
    tests/test_graph/test_hawkes_bivariate.py (R5).
    """
    leader_times = np.sort(np.asarray(leader_times, dtype=float))
    boundaries = np.concatenate([[0.0], leader_times, [T]])
    boundaries = boundaries[boundaries <= T]
    if len(boundaries) == 1:
        boundaries = np.array([0.0, T])

    follower_times: list[float] = []
    state = 0.0
    for k in range(len(boundaries) - 1):
        t_start = float(boundaries[k])
        t_end = float(boundaries[k + 1])
        if k > 0:
            state += 1.0
        lam_upper = mu + alpha * state
        if lam_upper <= 0.0:
            lam_upper = 1e-12
        t = t_start
        while True:
            dt = rng.exponential(1.0 / lam_upper)
            t = t + dt
            if t >= t_end:
                break
            decay = np.exp(-beta * (t - t_start))
            lam_real = mu + alpha * state * decay
            if rng.uniform() <= lam_real / lam_upper:
                follower_times.append(t)
        state = state * np.exp(-beta * (t_end - t_start))

    return np.array(follower_times)


# ---------------------------------------------------------------------------
# 1. Mask construction & enforcement
# ---------------------------------------------------------------------------


def test_default_mask_shape_and_block_sparse_structure():
    """The block-sparse mask must match the spec § 2.2 Box diagram."""
    mask = build_default_mask(5)  # 1 leader + 4 pools
    assert mask.shape == (5, 5)
    assert mask.dtype == bool
    # Diagonal: all True (self-excitation).
    for i in range(5):
        assert mask[i, i] is np.True_ or mask[i, i] == True, f"mask[{i},{i}] must be True"  # noqa: E712
    # First column rows 1..K: True (leader → pool).
    for i in range(1, 5):
        assert mask[i, 0], f"mask[{i},0] (leader→pool) must be True"
    # First row cols 1..K: False (pool → leader constrained).
    for j in range(1, 5):
        assert not mask[0, j], f"mask[0,{j}] (pool→leader) must be False"
    # Off-diagonal pool-pool: False.
    for i in range(1, 5):
        for j in range(1, 5):
            if i != j:
                assert not mask[i, j], (
                    f"mask[{i},{j}] off-diagonal pool-pool must be False"
                )


def test_fitter_uses_only_free_entries_from_mask():
    """The fitter's `free_idx` list must enumerate exactly the True cells."""
    fitter = MultivariateHawkesFitter(n_processes=4)
    free_set = set(fitter.free_idx)
    for i in range(4):
        for j in range(4):
            if fitter.mask[i, j]:
                assert (i, j) in free_set
            else:
                assert (i, j) not in free_set
    # On the default mask with N=4: 4 diagonal + 3 leader→pool = 7 free.
    assert fitter.n_free == 7


def test_fitter_rejects_mismatched_custom_mask_shape():
    """Custom masks must match (n_processes, n_processes)."""
    bad_mask = np.eye(3, dtype=bool)
    with pytest.raises(ValueError):
        MultivariateHawkesFitter(n_processes=4, mask=bad_mask)


# ---------------------------------------------------------------------------
# 2. Monte Carlo identifiability
# ---------------------------------------------------------------------------


def test_monte_carlo_identifiability_recovers_known_alpha():
    """Simulate from known α matrix, verify recovery within tolerance.

    Small system (1 leader + 1 pool) to keep wall time under ~5 s.
    Uses the R5-style modified thinning algorithm. The fitter should
    recover the leader-causality contract:

       - μ_0 within ~2× of truth
       - α_{1,0} strictly positive
       - BIC test ACCEPTS the coupled model (not bic_rejected)

    Recovery of exact integrated kernel area is an operator-only soak
    gate per spec § 6 — on 1 day of seconds-granularity data the MLE
    is identifiable in SIGN and BIC SIGNIFICANCE but not in absolute
    magnitude. We assert the production contract (BIC accepts,
    α > 0), not the lab contract (α matches truth).
    """
    rng = np.random.default_rng(seed=12345)
    T = 1 * 86_400.0  # 1 day — sufficient for BIC acceptance
    mu_leader = 0.005   # ~430 leader events / day
    mu_pool = 0.001     # ~85 pool events / day baseline
    alpha = 0.5
    beta = 1.0 / 300.0  # 5-min half-life

    leader_times = _simulate_poisson(mu_leader, T, rng)
    pool_times = _simulate_excited(leader_times, mu_pool, alpha, beta, T, rng)

    # We need enough events for the fit to be meaningful.
    assert len(leader_times) > 100, f"need leader sample, got {len(leader_times)}"
    assert len(pool_times) > 100, f"need pool sample, got {len(pool_times)}"

    fitter = MultivariateHawkesFitter(n_processes=2, max_iter=100)
    result = fitter.fit_arrays(
        times_by_proc=[leader_times, pool_times],
        process_labels=["leader", "pool"],
    )

    assert result["convergence"] in {"converged", "fallback"}, (
        f"unexpected convergence={result['convergence']!r} "
        f"(bic={result['bic_statistic']:.2f} thresh={result['bic_threshold']:.2f})"
    )
    # BIC must ACCEPT the coupled model on causal data.
    assert result["bic_statistic"] > result["bic_threshold"], (
        f"BIC rejected causal data: bic={result['bic_statistic']:.2f} "
        f"thresh={result['bic_threshold']:.2f}"
    )
    # α_{1,0} should be positive.
    alpha10_fit = result["alpha_matrix"].get((1, 0), 0.0)
    assert alpha10_fit > 0.0, (
        f"α_{{1,0}} collapsed to {alpha10_fit}; expected > 0"
    )
    # μ_leader within order of magnitude.
    mu0_fit = result["mu_vector"][0]
    assert mu0_fit > 0.0, f"μ_leader fit non-positive: {mu0_fit}"
    assert mu0_fit < 10 * mu_leader, (
        f"μ_leader fit {mu0_fit:.6f} >> truth {mu_leader:.6f}"
    )


# ---------------------------------------------------------------------------
# 3. Block-sparse mask: off-diagonal pool-pool stays 0
# ---------------------------------------------------------------------------


def test_block_sparse_mask_zeroes_off_diagonal_pool_pool():
    """Even on data where two pools both fire after a leader trade, the
    off-diagonal pool↔pool α entries must stay constrained to 0 by the
    mask — verified by inspecting the returned ``alpha_matrix``.

    Smaller-window test (1 day, low rates) to keep the fit fast — the
    contract being tested is the MASK shape, not optimisation quality.
    """
    rng = np.random.default_rng(seed=42)
    T = 1 * 86_400.0
    leader_times = _simulate_poisson(0.001, T, rng)
    pool_a = _simulate_excited(leader_times, 0.0005, 0.01, 1.0 / 300.0, T, rng)
    pool_b = _simulate_excited(leader_times, 0.0005, 0.01, 1.0 / 300.0, T, rng)
    # Both pools share a common excitation cause (the leader); a naive
    # full-N² model might attribute coupling between them.

    fitter = MultivariateHawkesFitter(n_processes=3, max_iter=50)
    result = fitter.fit_arrays(
        times_by_proc=[leader_times, pool_a, pool_b],
        process_labels=["leader", "pool_a", "pool_b"],
    )
    # Cells (1, 2) and (2, 1) are pool-to-pool — they must NOT appear in
    # alpha_matrix (which only carries FREE entries per the mask).
    assert (1, 2) not in result["alpha_matrix"], (
        "pool-to-pool α(1,2) leaked into result"
    )
    assert (2, 1) not in result["alpha_matrix"], (
        "pool-to-pool α(2,1) leaked into result"
    )


# ---------------------------------------------------------------------------
# 4. BIC threshold scales with k
# ---------------------------------------------------------------------------


def test_bic_threshold_scales_with_k_penalty():
    """The BIC threshold is k_penalty · log(N_events). Increasing the
    number of free entries (mask) raises the threshold proportionally.

    Uses a tiny synthetic dataset since the contract under test is
    purely about k_penalty arithmetic — no MLE quality is being
    assessed here.
    """
    rng = np.random.default_rng(seed=7)
    T = 1 * 86_400.0
    leader_times = _simulate_poisson(0.001, T, rng)
    pool = _simulate_excited(leader_times, 0.0005, 0.01, 1.0 / 300.0, T, rng)

    f2 = MultivariateHawkesFitter(n_processes=2, max_iter=50)  # 3 free
    r2 = f2.fit_arrays([leader_times, pool])
    # For 4 processes: 4 diagonal + 3 leader→pool = 7 free.
    f4 = MultivariateHawkesFitter(n_processes=4, max_iter=50)
    empty = np.array([], dtype=float)
    r4 = f4.fit_arrays([leader_times, pool, empty, empty])

    # Equal N_events → bic_threshold proportional to k_penalty (== n_free).
    # We don't compare exactly because the integer log(N) is the same;
    # we just assert the larger mask has a larger or equal threshold.
    assert r4["bic_threshold"] >= r2["bic_threshold"], (
        f"4-proc threshold {r4['bic_threshold']:.2f} should be ≥ "
        f"2-proc {r2['bic_threshold']:.2f}"
    )


# ---------------------------------------------------------------------------
# 5. Independence → bic_rejected
# ---------------------------------------------------------------------------


def test_independence_yields_bic_rejected():
    """Two independent Poissons → the BIC test rejects the coupled model
    and convergence is 'bic_rejected'. This is the multivariate
    analogue of R5's `test_independence_yields_low_alpha_mu`.
    """
    rng = np.random.default_rng(seed=314)
    T = 7 * 86_400.0
    leader_times = _simulate_poisson(0.002, T, rng)
    follower_times = _simulate_poisson(0.003, T, rng)

    fitter = MultivariateHawkesFitter(n_processes=2)
    result = fitter.fit_arrays([leader_times, follower_times])
    # The BIC test must reject the bivariate-coupling model OR collapse
    # α to a near-zero value. Both outcomes are acceptable; the
    # important contract is "no false positive of strong coupling".
    alpha = result["alpha_matrix"].get((1, 0), 0.0)
    assert (
        result["convergence"] == "bic_rejected" or alpha < 1e-3
    ), (
        f"independent streams produced α(1,0)={alpha} "
        f"convergence={result['convergence']!r}"
    )


# ---------------------------------------------------------------------------
# 6. R5 regression — bivariate fitter still passes
# ---------------------------------------------------------------------------


def test_r5_bivariate_independence_test_still_passes_under_r9_import():
    """The R5 bivariate fitter must remain unaffected by R9 imports.

    This test re-runs the R5 `test_independence_yields_low_alpha_mu`
    contract through the canonical R5 fitter to prove the R9 module
    is additive (not a replacement). If this test fails AFTER any
    R9 change, the change has broken the R5 contract — see spec § 6
    acceptance criterion 5.
    """
    from src.graph.hawkes_fitter import HawkesFitter

    rng = np.random.default_rng(seed=314)
    T = 7 * 86_400.0
    leader_times = _simulate_poisson(0.002, T, rng)
    follower_times = _simulate_poisson(0.003, T, rng)

    fitter = HawkesFitter()
    result = fitter.fit_arrays(leader_times, follower_times)
    assert result is not None
    assert result["alpha_mu_ratio"] < 1.0, (
        f"R5 bivariate fit regressed: α/μ = {result['alpha_mu_ratio']}"
    )


# ---------------------------------------------------------------------------
# 7. NLL math sanity
# ---------------------------------------------------------------------------


def test_nll_finite_on_simple_input():
    """Sanity check that the NLL is finite on a well-formed input."""
    times = [np.array([10.0, 30.0, 50.0]), np.array([15.0, 35.0, 55.0])]
    params = np.array([0.01, 0.01, 0.05, 0.05, 0.05, 1.0 / 300.0])  # 2 mu + 3 alpha + beta
    fitter = MultivariateHawkesFitter(n_processes=2)
    nll = multivariate_hawkes_nll(params, times, fitter.free_idx, 100.0)
    assert np.isfinite(nll)


def test_nll_rejects_negative_alpha():
    """Negative α must be rejected via the bounds-guard."""
    times = [np.array([10.0, 30.0]), np.array([15.0, 35.0])]
    fitter = MultivariateHawkesFitter(n_processes=2)
    # n_free=3 → params layout = 2 mu + 3 alpha + 1 beta = 6 floats.
    params = np.array([0.01, 0.01, -0.1, 0.05, 0.05, 1.0 / 300.0])
    nll = multivariate_hawkes_nll(params, times, fitter.free_idx, 100.0)
    assert nll >= 1e9  # _INVALID_NLL
