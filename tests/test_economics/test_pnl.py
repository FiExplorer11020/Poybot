from decimal import Decimal

import pytest

from src.economics.models import (
    ECONOMIC_MODEL_VERSION,
    LiquidityRole,
    OrderSide,
    StrategyTrack,
)
from src.economics.pnl import calculate_long_pnl, shares_from_notional


def test_required_enums_are_stable_strings():
    assert ECONOMIC_MODEL_VERSION == "v1.0.0"
    assert StrategyTrack.LEADER_SWING.value == "leader_swing"
    assert StrategyTrack.MICRO_REACTIVE.value == "micro_reactive"
    assert OrderSide.BUY.value == "BUY"
    assert OrderSide.SELL.value == "SELL"
    assert LiquidityRole.MAKER.value == "maker"
    assert LiquidityRole.TAKER.value == "taker"


def test_shares_from_notional_uses_entry_price():
    assert shares_from_notional(Decimal("200"), Decimal("0.50")) == Decimal("400.000000")


def test_long_yes_pnl_uses_shares_not_notional_as_multiplier():
    result = calculate_long_pnl(
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.60"),
        size_shares=Decimal("400"),
        entry_fee_usdc=Decimal("0"),
        exit_fee_usdc=Decimal("0"),
    )

    assert result.gross_pnl_usdc == Decimal("40.000000")
    assert result.net_pnl_usdc == Decimal("40.000000")


def test_long_pnl_subtracts_costs():
    result = calculate_long_pnl(
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.60"),
        size_shares=Decimal("400"),
        entry_fee_usdc=Decimal("1.00"),
        exit_fee_usdc=Decimal("1.20"),
        spread_cost_usdc=Decimal("0.50"),
        slippage_usdc=Decimal("0.30"),
    )

    assert result.gross_pnl_usdc == Decimal("40.000000")
    assert result.net_pnl_usdc == Decimal("37.000000")


def test_shares_from_notional_rejects_zero_price():
    with pytest.raises(ValueError):
        shares_from_notional(Decimal("200"), Decimal("0"))
