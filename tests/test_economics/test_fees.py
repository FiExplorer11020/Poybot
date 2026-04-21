from decimal import Decimal

import pytest

from src.economics.fees import calculate_polymarket_fee
from src.economics.models import LiquidityRole


def test_taker_fee_uses_polymarket_binary_fee_formula():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=True,
    )

    assert fee == Decimal("2.40000")


def test_maker_fee_is_zero_by_default():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.MAKER,
        fees_enabled=True,
    )

    assert fee == Decimal("0.00000")


def test_fee_disabled_returns_zero():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=False,
    )

    assert fee == Decimal("0.00000")


@pytest.mark.parametrize("bad_price", [Decimal("-0.01"), Decimal("1.01")])
def test_fee_rejects_invalid_binary_prices(bad_price):
    with pytest.raises(ValueError):
        calculate_polymarket_fee(
            shares=Decimal("1000"),
            price=bad_price,
            fee_rate=Decimal("0.01"),
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=True,
        )
