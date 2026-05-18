"""Unit tests for PaperTrader.force_close_all_positions + _on_halt_message
(PLAN-UIA-001 / ADR-PMK-014.4).

The halt path is the most operationally critical surface in the dashboard
— if the operator clicks EMERGENCY HALT during a phantom-trade incident
and nothing happens, that's the worst possible failure mode. These tests
exercise:

  * happy path: all positions close at oracle price
  * oracle returns None: fall back to entry_price + tag close_reason
  * individual close_trade raises: continue with the others, increment failed
  * no open positions: return zero counts, no errors
  * halt subscriber: receives Redis payload + triggers force_close
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine.paper_trader import PaperTrader, OpenPaperTrade
from src.control.price_oracle import PriceQuote


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _make_open_trade(
    *, trade_id: int, market_id: str = "MKT", token_id: str = "TOK",
    direction: str = "yes", entry_price: float = 0.50, size_usdc: float = 100.0,
) -> OpenPaperTrade:
    """Minimal OpenPaperTrade dataclass — we only set the fields the halt
    path reads (id, market_id, token_id, entry_price, direction)."""
    return OpenPaperTrade(
        id=trade_id,
        market_id=market_id,
        token_id=token_id,
        direction=direction,
        entry_price=entry_price,
        size_usdc=size_usdc,
        size_shares=size_usdc / entry_price,
        strategy="follow",
        leader_wallet="0xabc",
        opened_at=datetime.now(tz=timezone.utc),
        confidence=0.8,
        fee_rate_pct=0.0,
        entry_fee_usdc=0.0,
    )


def _make_trader_with_mocks(
    open_trades: list[OpenPaperTrade] | None = None,
    oracle_quote: PriceQuote | None = None,
    oracle_raises: Exception | None = None,
    close_raises_for: set[int] | None = None,
) -> PaperTrader:
    """Build a PaperTrader with the minimum mocks the halt path touches.

    We bypass __init__ to avoid the Subscriber() constructor reaching for
    Redis. The halt method only reads ``self._open_trades``,
    ``self._price_oracle``, calls ``self.close_trade``, and (optionally)
    publishes to ``self._redis``.
    """
    trader = PaperTrader.__new__(PaperTrader)
    trader._open_trades = open_trades or []
    trader._redis = None  # default — halt path tolerates None

    # PriceOracle mock
    oracle = MagicMock()
    if oracle_raises is not None:
        oracle.get_close_price = AsyncMock(side_effect=oracle_raises)
    else:
        oracle.get_close_price = AsyncMock(return_value=oracle_quote)
    trader._price_oracle = oracle

    # close_trade mock — succeeds unless trade_id in close_raises_for.
    fail_ids = close_raises_for or set()
    async def fake_close_trade(trade_id, *, exit_price, close_reason, price_quote=None):
        if trade_id in fail_ids:
            raise RuntimeError(f"simulated close failure for #{trade_id}")
        return True
    trader.close_trade = AsyncMock(side_effect=fake_close_trade)
    return trader


def _fresh_quote(price: float = 0.60) -> PriceQuote:
    return PriceQuote(price=price, source="book", observed_ts=1234567890.0)


# --------------------------------------------------------------------------- #
# force_close_all_positions                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_close_returns_correct_counts_happy_path():
    """3 open trades, oracle returns a fresh quote for each → all close."""
    trades = [_make_open_trade(trade_id=i) for i in (1, 2, 3)]
    trader = _make_trader_with_mocks(
        open_trades=trades,
        oracle_quote=_fresh_quote(0.55),
    )
    result = await trader.force_close_all_positions(reason="test_halt")
    assert result["closed_count"] == 3
    assert result["failed_count"] == 0
    assert result["no_price_count"] == 0
    assert result["trade_ids_closed"] == [1, 2, 3]
    assert result["trade_ids_failed"] == []
    assert result["reason"] == "test_halt"
    assert result["duration_s"] >= 0.0
    # close_trade was called for each, with the oracle exit_price
    assert trader.close_trade.await_count == 3
    for call in trader.close_trade.await_args_list:
        kwargs = call.kwargs
        assert kwargs["exit_price"] == 0.55
        assert kwargs["close_reason"] == "test_halt"


@pytest.mark.asyncio
async def test_force_close_no_oracle_quote_falls_back_to_entry_price():
    """Oracle returns a PriceQuote with price=None → fall back to entry_price."""
    trades = [_make_open_trade(trade_id=10, entry_price=0.42)]
    trader = _make_trader_with_mocks(
        open_trades=trades,
        oracle_quote=PriceQuote(price=None, source="fail", observed_ts=0.0),
    )
    result = await trader.force_close_all_positions()
    assert result["closed_count"] == 1
    assert result["no_price_count"] == 1
    # close_trade got entry_price as exit_price + tagged close_reason
    call_kwargs = trader.close_trade.await_args.kwargs
    assert call_kwargs["exit_price"] == 0.42
    assert call_kwargs["close_reason"].endswith("_no_price")


@pytest.mark.asyncio
async def test_force_close_oracle_raises_falls_back_safely():
    """Oracle raises an exception → fall back to entry_price, don't propagate."""
    trades = [_make_open_trade(trade_id=20, entry_price=0.30)]
    trader = _make_trader_with_mocks(
        open_trades=trades,
        oracle_raises=ConnectionError("oracle down"),
    )
    result = await trader.force_close_all_positions(reason="oracle_outage")
    assert result["closed_count"] == 1
    assert result["no_price_count"] == 1
    assert result["failed_count"] == 0
    call_kwargs = trader.close_trade.await_args.kwargs
    assert call_kwargs["exit_price"] == 0.30
    assert call_kwargs["close_reason"] == "oracle_outage_no_price"


@pytest.mark.asyncio
async def test_force_close_handles_individual_close_failure():
    """One trade fails to close → continue with the others, increment failed."""
    trades = [_make_open_trade(trade_id=i) for i in (100, 101, 102)]
    trader = _make_trader_with_mocks(
        open_trades=trades,
        oracle_quote=_fresh_quote(0.50),
        close_raises_for={101},
    )
    result = await trader.force_close_all_positions()
    assert result["closed_count"] == 2
    assert result["failed_count"] == 1
    assert 101 in result["trade_ids_failed"]
    assert sorted(result["trade_ids_closed"]) == [100, 102]


@pytest.mark.asyncio
async def test_force_close_with_no_open_positions():
    """Empty list → zero counts, no errors, returns the timing fields."""
    trader = _make_trader_with_mocks(open_trades=[])
    result = await trader.force_close_all_positions()
    assert result == {
        "closed_count": 0, "failed_count": 0, "no_price_count": 0,
        "trade_ids_closed": [], "trade_ids_failed": [],
        "started_at_iso": result["started_at_iso"],
        "completed_at_iso": result["completed_at_iso"],
        "duration_s": 0.0,
        "reason": "emergency_halt",
    }
    # The completed_at == started_at (no trades to process)
    assert result["started_at_iso"] == result["completed_at_iso"]


@pytest.mark.asyncio
async def test_force_close_no_oracle_attribute_set_falls_back():
    """When PaperTrader has no price_oracle (older test path), fall back."""
    trades = [_make_open_trade(trade_id=200, entry_price=0.99)]
    trader = _make_trader_with_mocks(open_trades=trades)
    trader._price_oracle = None  # explicit
    result = await trader.force_close_all_positions(reason="test")
    assert result["closed_count"] == 1
    assert result["no_price_count"] == 1
    call_kwargs = trader.close_trade.await_args.kwargs
    assert call_kwargs["exit_price"] == 0.99
    assert call_kwargs["close_reason"] == "test_no_oracle"


@pytest.mark.asyncio
async def test_force_close_close_trade_returns_false_marks_failed():
    """close_trade returning False (vs raising) is also treated as failure."""
    trades = [_make_open_trade(trade_id=300)]
    trader = _make_trader_with_mocks(
        open_trades=trades,
        oracle_quote=_fresh_quote(0.50),
    )
    # Override close_trade to return False
    trader.close_trade = AsyncMock(return_value=False)
    result = await trader.force_close_all_positions()
    assert result["closed_count"] == 0
    assert result["failed_count"] == 1
    assert result["trade_ids_failed"] == [300]


# --------------------------------------------------------------------------- #
# _on_halt_message — Redis pubsub handler                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_on_halt_message_invokes_force_close_with_payload_reason():
    trader = _make_trader_with_mocks(open_trades=[])
    trader.force_close_all_positions = AsyncMock(
        return_value={"closed_count": 0, "failed_count": 0}
    )
    await trader._on_halt_message(
        {"reason": "panic_button", "actor": "dashboard"},
        "control:halt",
    )
    trader.force_close_all_positions.assert_awaited_once_with(reason="panic_button")


@pytest.mark.asyncio
async def test_on_halt_message_defaults_when_payload_missing_keys():
    trader = _make_trader_with_mocks(open_trades=[])
    trader.force_close_all_positions = AsyncMock(return_value={})
    await trader._on_halt_message({}, "control:halt")
    trader.force_close_all_positions.assert_awaited_once_with(reason="emergency_halt")


@pytest.mark.asyncio
async def test_on_halt_message_swallows_force_close_exception():
    """A bug in force_close should not crash the subscriber loop."""
    trader = _make_trader_with_mocks(open_trades=[])
    trader.force_close_all_positions = AsyncMock(
        side_effect=RuntimeError("kaboom")
    )
    # Should NOT raise
    await trader._on_halt_message({"reason": "x"}, "control:halt")
    trader.force_close_all_positions.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Result publish — control:halt:completed                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_close_publishes_completed_event_when_redis_present():
    trader = _make_trader_with_mocks(
        open_trades=[_make_open_trade(trade_id=1)],
        oracle_quote=_fresh_quote(),
    )
    redis = MagicMock()
    publish_mock = AsyncMock()
    redis.publish = publish_mock
    trader._redis = redis

    await trader.force_close_all_positions(reason="published_event_test")
    publish_mock.assert_awaited_once()
    channel = publish_mock.await_args.args[0]
    assert channel == "control:halt:completed"
