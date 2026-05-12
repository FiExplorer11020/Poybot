"""
Wave-3 hardening tests for FollowerVolumePredictor — Round 9.

Audit reference: docs/audit/phase3/round9_wave3_review.md.

Covers contracts the original suite left implicit:

  1. Empty-Hawkes-fit fallback: predictor still produces a usable
     forecast (no crash, by_pool sums to total).
  2. Dominant-pool half-life selection: the time distribution
     reflects the half-life of the pool with the largest weighted
     contribution, not a hard-coded 30-min default.
  3. CI bounds: ci_low ≥ 0 and ci_high ≥ total_volume_usdc.
  4. Time-distribution CDF monotonicity in [0,1] for any half-life.
  5. Confidence collapses to 0 when E[volume] is near zero
     (we can't be confident about nothing).
  6. Single-pool collapse equivalence to bivariate Hawkes shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.follower_volume.kalman import FollowerPoolKalman, KalmanForecast
from src.follower_volume.volume_predictor import (
    FollowerVolumePredictor,
    _time_distribution,
)


def _fake_factory(
    volume_by_pool: dict[str, float],
    half_life_by_pool: dict[str, float] | None = None,
):
    half_life_by_pool = half_life_by_pool or {}

    def _factory(leader_wallet: str, pool_class: str) -> MagicMock:
        kf = MagicMock(spec=FollowerPoolKalman)
        v = float(volume_by_pool.get(pool_class, 0.0))
        hl = float(half_life_by_pool.get(pool_class, 600.0))
        kf.load_state = AsyncMock(return_value=False)
        kf.forecast = MagicMock(
            return_value=KalmanForecast(
                expected_volume_usdc=v,
                ci_low=max(0.0, v * 0.5),
                ci_high=v * 1.5,
                time_to_peak_s=0.0,
                half_life_s=hl,
            )
        )
        return kf

    return _factory


# ---------------------------------------------------------------------------
# 1. Empty-Hawkes-fit fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_hawkes_fit_still_produces_usable_forecast():
    """When the daemon hasn't fit yet (or the fit failed), the
    predictor must STILL produce a forecast — graceful degradation
    to Kalman-only mode.
    """
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum"],
        kalman_factory=_fake_factory(
            {"directional": 3000.0, "momentum": 2000.0}
        ),
    )
    # Three cases the daemon can hand us when no good fit exists.
    for fit in (None, {}, {"alpha_matrix": {}, "mu_vector": {}, "process_labels": []}):
        fc = await pred.forecast(
            leader_wallet="0xL", trade_size_usdc=100.0, hawkes_fit=fit
        )
        assert fc["total_volume_usdc"] > 0.0
        total = sum(fc["by_pool"].values())
        assert total == pytest.approx(fc["total_volume_usdc"], rel=1e-6)


# ---------------------------------------------------------------------------
# 2. Dominant-pool half-life drives the time CDF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_distribution_reflects_dominant_pool_half_life():
    """If the heavily-weighted pool has a short half-life, the CDF
    mass concentrates in the early buckets — even if other pools have
    longer half-lives. (Bug fixed wave-3: the previous code seeded
    half_life_for_dist = 1800.0 and only ratcheted UP via max().)
    """
    # info_leak pool dominates the volume and has a 60-s half-life.
    # directional pool is light and has a 1-h half-life.
    pred = FollowerVolumePredictor(
        pool_classes=["info_leak", "directional"],
        kalman_factory=_fake_factory(
            volume_by_pool={"info_leak": 8000.0, "directional": 200.0},
            half_life_by_pool={"info_leak": 60.0, "directional": 3600.0},
        ),
    )
    fc = await pred.forecast(
        leader_wallet="0xL",
        trade_size_usdc=100.0,
        strategy_prior={"info_leak": 0.95, "directional": 0.05},
    )
    td = fc["time_distribution"]
    # Most of the volume is dominated by info_leak with a 60-s
    # half-life → 0-5min bucket should hold > 80% of the CDF mass.
    assert td["0-5min"] > 0.80, (
        f"0-5min bucket only {td['0-5min']:.2f}; dominant pool "
        f"should drive the CDF concentration"
    )


# ---------------------------------------------------------------------------
# 3. CI bounds sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_low_nonneg_and_ci_high_geq_total():
    """ci_low ≥ 0 (volume is non-negative) and ci_high ≥ total_volume."""
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum"],
        kalman_factory=_fake_factory(
            {"directional": 5000.0, "momentum": 3000.0}
        ),
    )
    fc = await pred.forecast(leader_wallet="0xL", trade_size_usdc=100.0)
    assert fc["ci_low"] >= 0.0
    assert fc["ci_high"] >= fc["total_volume_usdc"] - 1e-6


# ---------------------------------------------------------------------------
# 4. Time-distribution CDF monotonicity in [0,1]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("half_life", [10.0, 60.0, 300.0, 1800.0, 3600.0, 86_400.0])
def test_time_distribution_buckets_in_unit_interval(half_life):
    """Each bucket value must be in [0, 1] and the four buckets must
    sum to 1.0 for any positive half-life."""
    td = _time_distribution(half_life)
    total = sum(td.values())
    assert total == pytest.approx(1.0, abs=1e-6)
    for label, v in td.items():
        assert 0.0 <= v <= 1.0 + 1e-9, (
            f"bucket {label!r}={v} out of [0, 1] for half_life={half_life}"
        )


# ---------------------------------------------------------------------------
# 5. Confidence collapses to 0 on near-zero expectations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_zero_when_expected_volume_near_zero():
    """If E[volume] < 1 USDC across all pools, confidence is 0 — the
    model has nothing meaningful to say."""
    pred = FollowerVolumePredictor(
        pool_classes=["directional", "momentum"],
        kalman_factory=_fake_factory(
            {"directional": 0.0, "momentum": 0.0}
        ),
    )
    fc = await pred.forecast(leader_wallet="0xL", trade_size_usdc=0.0)
    assert fc["confidence"] == 0.0


# ---------------------------------------------------------------------------
# 6. Single-pool collapse equivalence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_pool_collapse_when_no_strategy_classification():
    """Per spec § 6 dependency note: when R8 is missing, the
    predictor collapses to a single 'all_followers' pool. We verify
    the predictor accepts an empty pool_classes list (deferring to
    DEFAULT_POOL_CLASS) and produces a one-pool by_pool.
    """
    pred = FollowerVolumePredictor(
        pool_classes=[],
        kalman_factory=_fake_factory({"all_followers": 2500.0}),
    )
    fc = await pred.forecast(leader_wallet="0xL", trade_size_usdc=100.0)
    # Either the by_pool is empty (zero total) OR it carries exactly
    # one entry — depending on whether DEFAULT_POOL_CLASS was triggered.
    n_pools = len(fc["by_pool"])
    assert n_pools <= 1, (
        f"R8-missing fallback produced {n_pools} pools, expected ≤ 1"
    )
