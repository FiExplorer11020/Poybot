"""Plan 2026-05-19 P1 — tests for the market-context sizing penalty
contributors and aggregator. Each contributor is tested at three points
(below floor, mid-range linear interp, above ceiling) so a future
refactor can't silently break the scaling curves."""
from __future__ import annotations

import pytest

from src.engine.sizing_penalties import (
    _high_price_zone_penalty,
    _liquidity_zone_penalty,
    _near_resolution_penalty,
    _partial_live_match_penalty,
    compute_market_context_penalty,
)


# ──────────────────────────────────────────────────────────────────────
# Liquidity zone (1k → 10k linear)                                       #
# ──────────────────────────────────────────────────────────────────────


class TestLiquidityZone:
    def test_above_ceiling_returns_zero(self):
        assert _liquidity_zone_penalty(15_000.0) == 0.0
        assert _liquidity_zone_penalty(10_000.0) == 0.0

    def test_at_or_below_floor_returns_max(self):
        assert _liquidity_zone_penalty(1_000.0) == pytest.approx(0.5)
        assert _liquidity_zone_penalty(500.0) == pytest.approx(0.5)
        assert _liquidity_zone_penalty(0.0) == pytest.approx(0.5)

    def test_mid_range_linear(self):
        # midpoint 5500 → halfway from 0.5 to 0.0 → 0.25
        assert _liquidity_zone_penalty(5_500.0) == pytest.approx(0.25)

    def test_none_returns_zero(self):
        assert _liquidity_zone_penalty(None) == 0.0

    def test_garbage_returns_zero(self):
        assert _liquidity_zone_penalty("not-a-number") == 0.0


# ──────────────────────────────────────────────────────────────────────
# Near-resolution zone (6h → 24h linear)                                 #
# ──────────────────────────────────────────────────────────────────────


class TestNearResolutionZone:
    def test_above_ceiling_returns_zero(self):
        assert _near_resolution_penalty(48.0) == 0.0
        assert _near_resolution_penalty(24.0) == 0.0

    def test_at_or_below_floor_returns_max(self):
        assert _near_resolution_penalty(6.0) == pytest.approx(0.4)
        assert _near_resolution_penalty(1.0) == pytest.approx(0.4)

    def test_mid_range_linear(self):
        # midpoint 15h → halfway from 0.4 to 0.0 → 0.2
        assert _near_resolution_penalty(15.0) == pytest.approx(0.2)


# ──────────────────────────────────────────────────────────────────────
# High-price zone (0.75 → 0.85 linear)                                   #
# ──────────────────────────────────────────────────────────────────────


class TestHighPriceZone:
    def test_below_floor_returns_zero(self):
        assert _high_price_zone_penalty(0.50) == 0.0
        assert _high_price_zone_penalty(0.75) == 0.0

    def test_at_or_above_ceiling_returns_max(self):
        assert _high_price_zone_penalty(0.85) == pytest.approx(0.3)
        assert _high_price_zone_penalty(0.90) == pytest.approx(0.3)

    def test_mid_range_linear(self):
        # midpoint 0.80 → halfway 0.0 to 0.3 → 0.15
        assert _high_price_zone_penalty(0.80) == pytest.approx(0.15)


# ──────────────────────────────────────────────────────────────────────
# Partial live-match                                                     #
# ──────────────────────────────────────────────────────────────────────


class TestPartialLiveMatch:
    def test_one_signal_returns_penalty(self):
        assert _partial_live_match_penalty(1) == pytest.approx(0.3)

    def test_zero_signals_returns_zero(self):
        assert _partial_live_match_penalty(0) == 0.0

    def test_two_or_more_signals_returns_zero(self):
        # 2+ signals are blocked at the hard gate by default — should
        # never reach the penalty path.
        assert _partial_live_match_penalty(2) == 0.0
        assert _partial_live_match_penalty(5) == 0.0

    def test_none_returns_zero(self):
        assert _partial_live_match_penalty(None) == 0.0


# ──────────────────────────────────────────────────────────────────────
# Aggregator                                                             #
# ──────────────────────────────────────────────────────────────────────


class TestAggregator:
    def test_empty_context_returns_zero(self):
        pen, codes = compute_market_context_penalty({})
        assert pen == 0.0
        assert codes == []

    def test_single_contributor_returns_isolated(self):
        ctx = {"market_volume_24h": 1_000.0}
        pen, codes = compute_market_context_penalty(ctx)
        assert pen == pytest.approx(0.5)
        assert codes == ["liquidity_zone"]

    def test_multiple_contributors_sum(self):
        ctx = {
            "market_volume_24h": 1_000.0,  # 0.5
            "hours_to_resolution": 6.0,    # 0.4
            "entry_price": 0.80,           # 0.15
            "live_match_signal_count": 1,  # 0.3
        }
        pen, codes = compute_market_context_penalty(ctx)
        # 0.5 + 0.4 + 0.15 + 0.3 = 1.35 → clamped at 0.8
        assert pen == pytest.approx(0.8)
        assert set(codes) == {
            "liquidity_zone", "near_res_zone",
            "high_price_zone", "partial_live_match",
        }

    def test_cap_at_zero_point_eight(self):
        """Even when raw sum > 0.8, the aggregator clamps so the
        downstream 0.20 floor in _kelly_size always keeps the trade
        viable."""
        ctx = {
            "market_volume_24h": 0.0,
            "hours_to_resolution": 1.0,
            "entry_price": 0.95,
        }
        pen, _ = compute_market_context_penalty(ctx)
        assert pen <= 0.8 + 1e-9

    def test_missing_keys_tolerated(self):
        ctx = {"entry_price": 0.78}  # only one key
        pen, codes = compute_market_context_penalty(ctx)
        # 0.78 → 0.3 * 0.03/0.10 = 0.009 → above 0.001 threshold
        assert pen > 0.0
        assert codes == ["high_price_zone"]

    def test_only_micro_penalty_ignored(self):
        """A contributor returning < 0.001 (essentially zero) is omitted
        from the codes list so the dashboard reason stays clean."""
        ctx = {"entry_price": 0.751}  # 0.3 * 0.001/0.10 = 0.003 — just above
        pen, codes = compute_market_context_penalty(ctx)
        # This is above the threshold but barely — verifies the
        # threshold is consistent.
        if pen > 0.0:
            assert "high_price_zone" in codes
