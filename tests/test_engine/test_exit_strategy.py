"""Plan 2026-05-19 P3 — tests for horizon-aware holding cap +
trailing-stop trigger."""
from __future__ import annotations

import pytest

from src.engine.exit_strategy import (
    HORIZON_HOLDER,
    HORIZON_SCALPER,
    HORIZON_SWING,
    check_trailing_stop,
    resolve_holding_cap_for_horizon,
)


# ──────────────────────────────────────────────────────────────────────
# resolve_holding_cap_for_horizon                                        #
# ──────────────────────────────────────────────────────────────────────


class TestResolveHoldingCapForHorizon:
    def test_scalper_horizon_returns_1h(self):
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=HORIZON_SCALPER,
            is_sport=False,
            sport_cap_s=1_800,
        )
        assert cap == 3_600

    def test_swing_horizon_returns_6h(self):
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=HORIZON_SWING,
            is_sport=False,
            sport_cap_s=1_800,
        )
        assert cap == 21_600

    def test_holder_horizon_returns_24h(self):
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=HORIZON_HOLDER,
            is_sport=False,
            sport_cap_s=1_800,
        )
        assert cap == 86_400

    def test_sport_always_wins(self):
        """Even a holder leader on a sport market uses the sport cap."""
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=HORIZON_HOLDER,
            is_sport=True,
            sport_cap_s=1_800,
        )
        assert cap == 1_800

    def test_unknown_horizon_falls_back_to_swing(self):
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=None,
            is_sport=False,
            sport_cap_s=1_800,
        )
        assert cap == 21_600  # swing default

    def test_garbage_horizon_falls_back_to_swing(self):
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon="nonsense_value",
            is_sport=False,
            sport_cap_s=1_800,
        )
        assert cap == 21_600

    def test_zero_sport_cap_uses_default(self):
        """Defense: a misconfigured sport_cap_s=0 falls back to default."""
        cap = resolve_holding_cap_for_horizon(
            default_cap_s=43_200,
            horizon=HORIZON_SCALPER,
            is_sport=True,
            sport_cap_s=0,
        )
        assert cap == 43_200


# ──────────────────────────────────────────────────────────────────────
# check_trailing_stop                                                    #
# ──────────────────────────────────────────────────────────────────────


class TestCheckTrailingStop:
    def test_below_activation_threshold_inactive(self):
        """PnL < +5% → trail not armed, no trigger."""
        result = check_trailing_stop(
            pnl_pct=0.02,
            peak_pnl_pct=None,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.active is False
        assert result.triggered is False

    def test_crossing_activation_arms_trail(self):
        """PnL just crossed +5% → trail armed, peak = current."""
        result = check_trailing_stop(
            pnl_pct=0.06,
            peak_pnl_pct=None,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.active is True
        assert result.new_peak == pytest.approx(0.06)
        assert result.triggered is False

    def test_swing_peak_then_retreat_triggers(self):
        """Swing trailing distance is 4%. Peak 0.15, retreat to 0.10
        → 0.10 < 0.15 - 0.04 = 0.11 → triggered."""
        result = check_trailing_stop(
            pnl_pct=0.10,
            peak_pnl_pct=0.15,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.active is True
        assert result.triggered is True
        assert "trailing_stop" in result.reason
        assert "h=swing" in result.reason

    def test_scalper_tighter_trail(self):
        """Scalper trail is 2%. Peak 0.15, retreat to 0.12 → 0.12 < 0.13
        → triggered. Same scenario on swing (0.04) would NOT trigger
        (0.12 > 0.11)."""
        scalper = check_trailing_stop(
            pnl_pct=0.12,
            peak_pnl_pct=0.15,
            horizon=HORIZON_SCALPER,
            is_sport=False,
        )
        swing = check_trailing_stop(
            pnl_pct=0.12,
            peak_pnl_pct=0.15,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert scalper.triggered is True
        assert swing.triggered is False

    def test_holder_loosest_trail(self):
        """Holder distance is 6%. Peak 0.15, retreat to 0.10 → 0.10 < 0.09
        → False (0.10 > 0.09)."""
        result = check_trailing_stop(
            pnl_pct=0.10,
            peak_pnl_pct=0.15,
            horizon=HORIZON_HOLDER,
            is_sport=False,
        )
        assert result.active is True
        assert result.triggered is False

    def test_peak_does_not_decrease(self):
        """If current PnL < prior peak, peak stays at prior."""
        result = check_trailing_stop(
            pnl_pct=0.08,
            peak_pnl_pct=0.12,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.new_peak == pytest.approx(0.12)

    def test_peak_updates_on_new_high(self):
        result = check_trailing_stop(
            pnl_pct=0.18,
            peak_pnl_pct=0.12,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.new_peak == pytest.approx(0.18)
        assert result.triggered is False  # 0.18 is the new peak, not a retreat

    def test_already_active_stays_active_even_on_retreat(self):
        """Once peak >= 5% the trail stays armed even if PnL retreats
        below the activation threshold."""
        result = check_trailing_stop(
            pnl_pct=0.03,
            peak_pnl_pct=0.10,
            horizon=HORIZON_SWING,
            is_sport=False,
        )
        assert result.active is True
        assert result.triggered is True  # 0.03 < 0.10 - 0.04 = 0.06
