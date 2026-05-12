"""
Wave-3 hardening tests for FollowerPoolKalman — Round 9.

Audit reference: docs/audit/phase3/round9_wave3_review.md.

Covers the math contracts the original suite left implicit:

  1. Joseph-form covariance update preserves symmetry under repeated
     observations (numerical stability test).
  2. P stays positive-semi-definite after many updates.
  3. Innovation magnitude tracks model-mismatch for divergence
     detection.
  4. The forecast variance grows when the prior covariance grows
     (sanity).
  5. The Kalman gain shrinks as more observations accumulate (filter
     converges, not diverges).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from src.follower_volume.kalman import (
    DEFAULT_F,
    DEFAULT_P0,
    DEFAULT_Q,
    DEFAULT_R,
    DEFAULT_X0,
    FollowerPoolKalman,
)


def _kf(**kw) -> FollowerPoolKalman:
    return FollowerPoolKalman(
        leader_wallet=kw.pop("leader", "0xL"),
        pool_class=kw.pop("pool", "d"),
        **kw,
    )


def _mock_get_db():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


# ---------------------------------------------------------------------------
# 1. Joseph form preserves symmetry across many updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_joseph_form_keeps_covariance_symmetric():
    """The Joseph-form covariance update (introduced wave-3) preserves
    symmetry P = P^T even under repeated updates. The textbook form
    `(I - K H) P_pred` can drift away from symmetry over many updates
    due to floating point — Joseph form does not.
    """
    rng = np.random.default_rng(seed=2026_05_12)
    kf = _kf()
    factory, _ = _mock_get_db()

    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        for _ in range(100):
            y = max(0.0, 10_000.0 * 0.15 + rng.normal(0.0, 300.0))
            await kf.update(y_observed=y, persist=False)
            # Symmetry contract: max element-wise asymmetry < 1e-9.
            asym = float(np.max(np.abs(kf.P - kf.P.T)))
            assert asym < 1e-9, (
                f"P lost symmetry: max|P - P^T|={asym}"
            )


# ---------------------------------------------------------------------------
# 2. P stays positive semi-definite under many updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_covariance_stays_psd_under_many_updates():
    """All eigenvalues of P must remain ≥ 0 (within float tolerance)
    after many noisy observations. Loss of PSD is the canonical
    numerical-instability failure mode of the textbook (I-KH)P form.
    """
    rng = np.random.default_rng(seed=2026_05_13)
    kf = _kf()
    factory, _ = _mock_get_db()
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        for _ in range(150):
            y = max(0.0, 5_000.0 * 0.20 + rng.normal(0.0, 250.0))
            await kf.update(y_observed=y, persist=False)
    eigs = np.linalg.eigvalsh(kf.P)
    assert float(np.min(eigs)) >= -1e-6, (
        f"P lost PSD: min eigenvalue={np.min(eigs)}"
    )


# ---------------------------------------------------------------------------
# 3. Innovation magnitude tracks model mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_innovation_magnitude_detects_regime_shift():
    """When the underlying truth shifts (a "regime change"), the
    innovation magnitude should JUMP before the filter readjusts.
    This is the load-bearing signal for the operator's
    `polybot_kalman_innovation_magnitude` divergence alert.
    """
    rng = np.random.default_rng(seed=2026_05_14)
    kf = _kf()
    factory, _ = _mock_get_db()

    pre_shift_innovations: list[float] = []
    shift_innovations: list[float] = []

    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        # Calibrate on regime A (low volume).
        for _ in range(30):
            y = max(0.0, 1_000.0 * 0.10 + rng.normal(0.0, 50.0))
            res = await kf.update(y_observed=y, persist=False)
            pre_shift_innovations.append(abs(float(res["innovation"])))
        # Sudden regime shift to 10× volume — filter sees a step.
        for _ in range(5):
            y = max(0.0, 1_000.0 * 1.0 + rng.normal(0.0, 100.0))
            res = await kf.update(y_observed=y, persist=False)
            shift_innovations.append(abs(float(res["innovation"])))

    pre_med = float(np.median(pre_shift_innovations[-10:]))
    shift_max = float(np.max(shift_innovations))
    # The first shifted observation should be FAR outside the
    # pre-shift noise envelope. We require ≥ 5× the pre-shift median.
    assert shift_max > 5.0 * pre_med, (
        f"shift innovation {shift_max:.2f} not >> "
        f"pre-shift median {pre_med:.2f} (no divergence signal)"
    )


# ---------------------------------------------------------------------------
# 4. Forecast variance grows with prior covariance
# ---------------------------------------------------------------------------


def test_forecast_variance_scales_with_state_uncertainty():
    """A diffuse prior P → wider forecast CI. A tight P → narrower CI.
    This is the monotone-monotone sanity contract: more state
    uncertainty must yield more forecast uncertainty.
    """
    kf = _kf()
    kf.x = np.array([10_000.0, 0.10, 1.0 / 1800.0])
    kf.P = np.diag([1.0e6, 1.0e-4, 1.0e-10])  # tight
    fc_tight = kf.forecast()

    kf2 = _kf()
    kf2.x = np.array([10_000.0, 0.10, 1.0 / 1800.0])
    kf2.P = np.diag([1.0e10, 1.0e-2, 1.0e-6])  # diffuse
    fc_diffuse = kf2.forecast()

    width_tight = fc_tight.ci_high - fc_tight.ci_low
    width_diffuse = fc_diffuse.ci_high - fc_diffuse.ci_low
    assert width_diffuse > width_tight, (
        f"diffuse-prior CI width {width_diffuse:.2f} should exceed "
        f"tight-prior width {width_tight:.2f}"
    )


# ---------------------------------------------------------------------------
# 5. Kalman gain shrinks under repeated agreeing observations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kalman_gain_shrinks_as_filter_learns():
    """When the filter sees consistent observations, the Kalman gain
    norm shrinks (the filter trusts its state more). If the gain
    GROWS over time, the filter is diverging — a numerical bug.
    """
    rng = np.random.default_rng(seed=2026_05_15)
    kf = _kf()
    factory, _ = _mock_get_db()

    gains: list[float] = []
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        for _ in range(50):
            y = max(0.0, 8_000.0 * 0.18 + rng.normal(0.0, 100.0))
            res = await kf.update(y_observed=y, persist=False)
            gains.append(float(np.linalg.norm(res["K"])))

    # First gain should exceed the LATER gains (filter learns).
    # Compare initial (first 5 mean) vs late (last 5 mean).
    early = float(np.mean(gains[:5]))
    late = float(np.mean(gains[-5:]))
    assert late <= early, (
        f"Kalman gain grew over time: early={early:.4f} "
        f"late={late:.4f} (filter diverging?)"
    )


# ---------------------------------------------------------------------------
# 6. CI coverage on a longer run (stricter than the smoke gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_coverage_high_after_long_burn_in():
    """After a long burn-in, the 95% CI should bracket ≥ 0.80 of
    fresh observations. This is stricter than the existing smoke
    gate (0.70). Strict 0.95 ± 0.03 is still operator-only soak.
    """
    rng = np.random.default_rng(seed=2026_05_16)
    true_pool = 8_000.0
    true_response = 0.15
    obs_noise = 200.0
    factory, _ = _mock_get_db()

    kf = _kf()
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        # Burn-in.
        for _ in range(80):
            y = max(0.0, true_pool * true_response + rng.normal(0.0, obs_noise))
            await kf.update(y_observed=y, persist=False)

        bracketed = 0
        n_runs = 100
        for _ in range(n_runs):
            fc = kf.forecast()
            y = max(0.0, true_pool * true_response + rng.normal(0.0, obs_noise))
            if fc.ci_low <= y <= fc.ci_high:
                bracketed += 1
            await kf.update(y_observed=y, persist=False)

    coverage = bracketed / n_runs
    assert coverage >= 0.80, (
        f"long-burn CI coverage too low: {coverage:.2f}"
    )
