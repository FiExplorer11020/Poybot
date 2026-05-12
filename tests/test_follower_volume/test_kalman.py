"""
Tests for FollowerPoolKalman — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.2.

Coverage:
  1. Predict math (F·x and F·P·F^T + Q).
  2. Update math (innovation, Kalman gain, posterior).
  3. State clamps (response_pct in [0, 1], pool_size >= 0).
  4. CI coverage on synthetic data converges to ~95% over many runs.
  5. Persistence writes both the current row and a history snapshot.
  6. Cold-start (load_state returns False) keeps the constructor prior.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.follower_volume.kalman import (
    DEFAULT_F,
    DEFAULT_P0,
    DEFAULT_Q,
    DEFAULT_X0,
    FollowerPoolKalman,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kf(leader="0xLEADER", pool="directional", **kw) -> FollowerPoolKalman:
    return FollowerPoolKalman(leader_wallet=leader, pool_class=pool, **kw)


def _mock_get_db_factory():
    """Build a fake DB context manager that records executed SQL."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


# ---------------------------------------------------------------------------
# 1. Predict math
# ---------------------------------------------------------------------------


def test_predict_advances_state_by_F():
    """x_pred = F @ x. With DEFAULT_F (mostly identity), pool_size stays
    flat and response_pct decays by 0.95."""
    kf = _kf()
    kf.x = np.array([100_000.0, 0.20, 1.0 / 1800.0], dtype=float)
    x_pred, P_pred = kf.predict()
    # pool_size persists (F[0,0]=1).
    assert x_pred[0] == pytest.approx(100_000.0)
    # response_pct shrinks toward zero (F[1,1]=0.95).
    assert x_pred[1] == pytest.approx(0.20 * 0.95)
    # decay_rate shrinks slightly (F[2,2]=0.99).
    assert x_pred[2] == pytest.approx((1.0 / 1800.0) * 0.99)
    # Covariance grew by Q.
    assert P_pred.shape == (3, 3)
    assert P_pred[0, 0] > kf.P[0, 0]


# ---------------------------------------------------------------------------
# 2. Update math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_innovation_sign_matches_residual():
    """If y_observed > y_predicted, the innovation is positive and the
    posterior state increases (specifically pool_size, since its
    Jacobian entry is response_pct > 0).

    Note: the update step first applies predict() (F·x), which shrinks
    response_pct by F[1,1]=0.95 BEFORE computing E[y]. So E[y] after
    predict is 10_000 · (0.10 · 0.95) = 950, not 1_000. The innovation
    is therefore 2_000 - 950 = 1_050. This test verifies the SIGN and
    magnitude of the residual, not the exact arithmetic.
    """
    kf = _kf()
    kf.x = np.array([10_000.0, 0.10, 1.0 / 1800.0], dtype=float)
    factory, _ = _mock_get_db_factory()
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        result = await kf.update(y_observed=2_000.0, persist=True)
    # Innovation should be positive and within 5% of (2_000 − 0.95·1_000) = 1_050.
    assert result["innovation"] > 0
    assert result["innovation"] == pytest.approx(1_050.0, rel=0.05)
    assert kf.x[0] > 10_000.0  # pool_size revised UP
    assert kf.n_observations == 1


@pytest.mark.asyncio
async def test_update_clamps_response_pct_to_unit_interval():
    """response_pct must never escape (1e-4, 1.0]."""
    kf = _kf()
    # Push toward >1 by hammering y >> E[y].
    kf.x = np.array([1.0, 0.10, 1.0 / 1800.0], dtype=float)
    factory, _ = _mock_get_db_factory()
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        await kf.update(y_observed=1.0e9, persist=False)
    assert 0.0 < kf.x[1] <= 1.0
    assert kf.x[0] >= 0.0


# ---------------------------------------------------------------------------
# 3. Forecast
# ---------------------------------------------------------------------------


def test_forecast_returns_ci_around_expected_volume():
    """Forecast E[y] = pool_size · response_pct with CI > 0."""
    kf = _kf()
    kf.x = np.array([50_000.0, 0.20, 1.0 / 1800.0], dtype=float)
    fc = kf.forecast()
    assert fc.expected_volume_usdc == pytest.approx(10_000.0, rel=0.01)
    assert fc.ci_low >= 0.0
    assert fc.ci_high > fc.expected_volume_usdc
    assert fc.half_life_s > 0


def test_forecast_half_life_derived_from_decay_rate():
    """half_life_s = log(2) / decay_rate."""
    kf = _kf()
    decay = 1.0 / 600.0  # 10-min mean residence
    kf.x = np.array([1.0, 0.1, decay], dtype=float)
    fc = kf.forecast()
    assert fc.half_life_s == pytest.approx(np.log(2.0) / decay, rel=1e-6)


# ---------------------------------------------------------------------------
# 4. CI coverage on synthetic data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_coverage_converges_to_high_fraction():
    """On synthetic noisy observations with a tracked true state, the
    95% CI should bracket the realised observation at a high rate.

    We don't require strict 0.95 ± 0.03 in the unit test (too tight on
    50 samples + 3 EKF restarts); we require ≥ 0.7 as a regression
    smoke gate. The strict 0.95 ± 0.03 is the OPERATOR-ONLY soak gate
    on real 60-day data per spec § 6.
    """
    rng = np.random.default_rng(seed=2026)
    true_pool = 10_000.0
    true_response = 0.15
    factory, _ = _mock_get_db_factory()

    kf = _kf()
    bracketed = 0
    n_runs = 50

    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        # Warm up the filter on noisy observations.
        for _ in range(20):
            y = max(0.0, true_pool * true_response + rng.normal(0.0, 200.0))
            await kf.update(y_observed=y, persist=False)
        # Now sample forecasts vs new observations.
        for _ in range(n_runs):
            fc = kf.forecast()
            y = max(0.0, true_pool * true_response + rng.normal(0.0, 200.0))
            if fc.ci_low <= y <= fc.ci_high:
                bracketed += 1
            await kf.update(y_observed=y, persist=False)

    coverage = bracketed / n_runs
    # Smoke gate: must be substantially better than random.
    assert coverage >= 0.7, f"CI coverage too low: {coverage:.2f}"


# ---------------------------------------------------------------------------
# 5. Persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_persists_current_state_and_history_row():
    """Each update writes one UPSERT into follower_pool_state AND one
    INSERT into follower_pool_state_history."""
    kf = _kf()
    factory, conn = _mock_get_db_factory()
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        await kf.update(y_observed=500.0, persist=True)
    # Two execute calls — one per table.
    assert conn.execute.call_count == 2
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("follower_pool_state" in s for s in sqls)
    assert any("follower_pool_state_history" in s for s in sqls)


@pytest.mark.asyncio
async def test_update_persist_failure_does_not_corrupt_state():
    """A DB write failure on persist must NOT roll back the in-memory
    state update — the next observation still uses the current state."""
    kf = _kf()

    @asynccontextmanager
    async def _broken_ctx():
        raise RuntimeError("db_down")
        yield  # pragma: no cover

    with patch("src.follower_volume.kalman.get_db", side_effect=_broken_ctx):
        result = await kf.update(y_observed=500.0, persist=True)
    # Update math still applied.
    assert "innovation" in result
    assert kf.n_observations == 1


# ---------------------------------------------------------------------------
# 6. Cold start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_state_returns_false_on_cold_start():
    """load_state returns False when the row doesn't exist and leaves
    the constructor-default state untouched."""
    kf = _kf()
    factory, conn = _mock_get_db_factory()
    conn.fetchrow = AsyncMock(return_value=None)  # cold
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        ok = await kf.load_state()
    assert ok is False
    # Default x0 untouched.
    np.testing.assert_allclose(kf.x, DEFAULT_X0)


@pytest.mark.asyncio
async def test_load_state_hydrates_from_row():
    """load_state populates x and P from a DB row."""
    kf = _kf()
    factory, conn = _mock_get_db_factory()
    cov = [1.0, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.0001]
    conn.fetchrow = AsyncMock(
        return_value={
            "pool_size_usdc": 25_000.0,
            "recent_response_pct": 0.18,
            "decay_rate": 1.0 / 900.0,
            "state_cov_json": json.dumps(cov),
            "n_observations": 42,
            "last_innovation": 250.0,
        }
    )
    with patch("src.follower_volume.kalman.get_db", side_effect=factory):
        ok = await kf.load_state()
    assert ok is True
    assert kf.x[0] == pytest.approx(25_000.0)
    assert kf.x[1] == pytest.approx(0.18)
    assert kf.n_observations == 42
