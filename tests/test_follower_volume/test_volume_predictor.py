"""
Tests for FollowerVolumePredictor — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.3.

Coverage:
  1. Forecast shape (all required keys).
  2. by_pool sums to total_volume_usdc within float tolerance.
  3. time_distribution sums to 1.0.
  4. Strategy prior weights the per-pool contributions correctly.
  5. Empty pool list collapses gracefully (no crash, zero total).
  6. Hawkes modulator pulls up matched-label pools.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.follower_volume.kalman import FollowerPoolKalman, KalmanForecast
from src.follower_volume.volume_predictor import (
    FollowerVolumePredictor,
    _time_distribution,
)


def _fake_kalman_factory(volumes: dict[str, float]):
    """Build a kalman_factory that returns pre-warmed filters with the
    given expected volumes per pool."""

    def _factory(leader_wallet: str, pool_class: str) -> MagicMock:
        kf = MagicMock(spec=FollowerPoolKalman)
        v = float(volumes.get(pool_class, 0.0))
        kf.load_state = AsyncMock(return_value=False)
        kf.forecast = MagicMock(
            return_value=KalmanForecast(
                expected_volume_usdc=v,
                ci_low=max(0.0, v * 0.5),
                ci_high=v * 1.5,
                time_to_peak_s=0.0,
                half_life_s=600.0,
            )
        )
        return kf

    return _factory


# ---------------------------------------------------------------------------
# 1. Forecast shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forecast_returns_required_keys():
    """All spec § 3.3 keys must be present."""
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum"],
        kalman_factory=_fake_kalman_factory(
            {"directional": 5000.0, "momentum": 3000.0}
        ),
    )
    fc = await pred.forecast(leader_wallet="0xL", trade_size_usdc=200.0)
    for key in (
        "total_volume_usdc",
        "ci_low",
        "ci_high",
        "by_pool",
        "time_distribution",
        "confidence",
    ):
        assert key in fc, f"missing key {key!r}"


# ---------------------------------------------------------------------------
# 2. by_pool sums to total
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_pool_sums_to_total_volume():
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum", "social_driven"],
        kalman_factory=_fake_kalman_factory(
            {"directional": 5000.0, "momentum": 3000.0, "social_driven": 1000.0}
        ),
    )
    fc = await pred.forecast(leader_wallet="0xL", trade_size_usdc=100.0)
    total = sum(fc["by_pool"].values())
    assert total == pytest.approx(fc["total_volume_usdc"], rel=1e-6)


# ---------------------------------------------------------------------------
# 3. time_distribution CDF
# ---------------------------------------------------------------------------


def test_time_distribution_sums_to_one():
    """Time distribution buckets must sum to 1.0 (renormalised)."""
    td = _time_distribution(half_life_s=600.0)
    total = sum(td.values())
    assert total == pytest.approx(1.0, abs=1e-6)
    for k, v in td.items():
        assert 0.0 <= v <= 1.0


def test_time_distribution_handles_degenerate_zero_decay():
    """Half-life ~ infinite → kernel is flat → uniform fallback."""
    td = _time_distribution(half_life_s=1e18)
    total = sum(td.values())
    assert total == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. Strategy prior weighting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_prior_weights_pools_proportionally():
    """If strategy_prior heavily weights 'directional', that pool's
    contribution dominates by_pool."""
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum"],
        kalman_factory=_fake_kalman_factory(
            {"directional": 5000.0, "momentum": 5000.0}
        ),
    )
    fc = await pred.forecast(
        leader_wallet="0xL",
        trade_size_usdc=100.0,
        strategy_prior={"directional": 0.9, "momentum": 0.1},
    )
    directional = fc["by_pool"]["directional"]
    momentum = fc["by_pool"]["momentum"]
    # Same raw Kalman E[y], heavily skewed weights → directional > momentum.
    assert directional > momentum * 5


# ---------------------------------------------------------------------------
# 5. Graceful degrade — no pools provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pools_returns_zero_total_without_crash():
    """When pool_classes is empty AND no strategy_prior, the predictor
    collapses to the default 'all_followers' pool."""
    pred = FollowerVolumePredictor(
        pool_classes=[],
        kalman_factory=_fake_kalman_factory({}),  # default = 0
    )
    fc = await pred.forecast(leader_wallet="0xL")
    assert fc["total_volume_usdc"] == 0.0
    assert isinstance(fc["by_pool"], dict)


# ---------------------------------------------------------------------------
# 6. Hawkes modulator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hawkes_modulator_pulls_up_matched_label_pool():
    """When the Hawkes fit shows α_{1,0} > 0 for process_labels[1] =
    'directional', that pool's by_pool volume increases vs the
    no-hawkes baseline."""
    pred = FollowerVolumePredictor(
        pool_classes=["directional"],
        kalman_factory=_fake_kalman_factory({"directional": 1000.0}),
    )
    baseline = await pred.forecast(leader_wallet="0xL", trade_size_usdc=100.0)
    hawkes_fit = {
        "alpha_matrix": {(1, 0): 0.5},
        "mu_vector": {0: 0.001, 1: 0.001},
        "process_labels": ["leader", "directional"],
    }
    boosted = await pred.forecast(
        leader_wallet="0xL", trade_size_usdc=100.0, hawkes_fit=hawkes_fit
    )
    assert boosted["by_pool"]["directional"] > baseline["by_pool"]["directional"]
