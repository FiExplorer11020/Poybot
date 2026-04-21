from decimal import Decimal

import pytest

from src.economics.models import OrderSide, StrategyTrack
from src.execution.venue import ClobVenueDryRun, DryRunOrder


@pytest.mark.asyncio
async def test_clob_dry_run_never_submits_real_order():
    venue = ClobVenueDryRun()
    order = DryRunOrder(
        strategy_track=StrategyTrack.LEADER_SWING,
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        size_shares=Decimal("100"),
        limit_price=Decimal("0.55"),
        client_order_id="test-1",
        metadata={"reason": "unit-test", "signal_audit": {"accepted": True}},
    )

    receipt = await venue.submit_order(order)

    assert receipt.dry_run is True
    assert receipt.would_submit is False
    assert receipt.accepted is True
    assert receipt.order == order
    assert receipt.reason == "dry_run_only"


@pytest.mark.asyncio
async def test_clob_dry_run_rejects_order_without_accepted_signal_audit():
    venue = ClobVenueDryRun()
    order = DryRunOrder(
        strategy_track=StrategyTrack.LEADER_SWING,
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        size_shares=Decimal("100"),
        limit_price=Decimal("0.55"),
        client_order_id="test-2",
        metadata={},
    )

    receipt = await venue.submit_order(order)

    assert receipt.dry_run is True
    assert receipt.would_submit is False
    assert receipt.accepted is False
    assert receipt.reason == "missing_accepted_signal_audit"
