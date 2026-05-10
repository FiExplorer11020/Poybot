"""
Verifies RiskManager.check_can_trade() consults the killswitch FIRST.

The killswitch must short-circuit before any drawdown / position / loss check
runs — we don't want to query the DB for circuit-breaker state if we already
know execution is forbidden globally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.control import killswitch as ks_mod
from src.engine.risk_manager import RiskManager


@pytest.fixture
def reset_singleton():
    """Reset the killswitch singleton between tests for isolation."""
    ks_mod._singleton = None
    yield
    ks_mod._singleton = None


async def test_killswitch_off_short_circuits_check_can_trade(monkeypatch, reset_singleton):
    """When killswitch is OFF, check_can_trade returns False immediately."""

    fake_service = MagicMock()
    fake_service.is_execution_enabled = AsyncMock(return_value=False)
    ks_mod._singleton = fake_service

    rm = RiskManager()
    # Spy on the private DB-querying methods — none should fire.
    rm._count_recent_losses = AsyncMock(return_value=0)
    rm._count_open_positions = AsyncMock(return_value=0)
    rm._market_exposure = AsyncMock(return_value=0.0)

    ok = await rm.check_can_trade({"market_id": "m1"}, current_capital=10_000)

    assert ok is False
    fake_service.is_execution_enabled.assert_awaited_once()
    rm._count_recent_losses.assert_not_awaited()
    rm._count_open_positions.assert_not_awaited()
    rm._market_exposure.assert_not_awaited()


async def test_killswitch_on_proceeds_to_other_checks(monkeypatch, reset_singleton):
    """When killswitch is ON, the remaining circuit breakers run as before."""

    fake_service = MagicMock()
    fake_service.is_execution_enabled = AsyncMock(return_value=True)
    ks_mod._singleton = fake_service

    rm = RiskManager()
    rm._count_recent_losses = AsyncMock(return_value=0)
    rm._count_open_positions = AsyncMock(return_value=0)
    rm._market_exposure = AsyncMock(return_value=0.0)

    ok = await rm.check_can_trade({"market_id": "m1"}, current_capital=10_000)

    assert ok is True
    fake_service.is_execution_enabled.assert_awaited_once()
    # Subsequent checks did fire.
    rm._count_recent_losses.assert_awaited_once()
    rm._count_open_positions.assert_awaited_once()
    rm._market_exposure.assert_awaited_once()


async def test_killswitch_read_failure_refuses_trade(monkeypatch, reset_singleton):
    """If the killswitch read raises, we fail SAFE — refuse the trade."""

    fake_service = MagicMock()
    fake_service.is_execution_enabled = AsyncMock(side_effect=ConnectionError("redis+db down"))
    ks_mod._singleton = fake_service

    rm = RiskManager()
    rm._count_recent_losses = AsyncMock(return_value=0)
    rm._count_open_positions = AsyncMock(return_value=0)
    rm._market_exposure = AsyncMock(return_value=0.0)

    ok = await rm.check_can_trade({"market_id": "m1"}, current_capital=10_000)

    assert ok is False
    rm._count_recent_losses.assert_not_awaited()
