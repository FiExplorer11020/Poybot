"""
Tests for src/engine/order_manager.py.

The OrderManager talks to two collaborators: a CLOBClientWrapper (mocked)
and the DB (also mocked via the same `_make_pool` / `_make_conn` fixtures
used elsewhere in the suite). We never hit a real CLOB or database.

Coverage focuses on:
  * limit-price arithmetic (BUY adds slippage, SELL subtracts, clamped)
  * the "place -> wait -> reprice -> ..." loop, including:
      - first-attempt full fill
      - first-attempt timeout, second-attempt fill
      - max attempts exhausted
      - shadow path (dry_run on the wrapper) returns 'shadow' immediately
      - rejected place_order short-circuits without retrying
  * a DB row is written for every attempt, with the right state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine.clob_client_wrapper import (
    OrderStatus,
    PlaceOrderResult,
)
from src.engine.order_manager import OrderManager


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _make_clob(
    *,
    dry_run: bool = False,
    midpoint: float = 0.50,
    place_results=None,
    status_by_order_id: dict[str, "OrderStatus"] | None = None,
    fills=None,
):
    """A fake CLOBClientWrapper with the methods OrderManager uses.

    `status_by_order_id` maps a CLOB order id to the OrderStatus that
    every poll on that order should return — this is necessary because
    `_wait_for_fill` polls in a tight loop and a flat side_effect
    iterator runs out fast.
    """
    clob = MagicMock()
    clob.dry_run = dry_run
    clob.get_midpoint = AsyncMock(return_value=midpoint)
    if place_results is None:
        place_results = [PlaceOrderResult(success=True, clob_order_id="ord-1")]
    clob.place_limit_order = AsyncMock(side_effect=list(place_results))
    if status_by_order_id is None:
        status_by_order_id = {
            "ord-1": OrderStatus(clob_order_id="ord-1", state="filled",
                                 filled_size=20.0, remaining_size=0.0, avg_fill_price=0.5)
        }

    async def _status(order_id):  # AsyncMock side_effect can be async
        return status_by_order_id.get(order_id)

    clob.get_order_status = AsyncMock(side_effect=_status)
    clob.get_trades_for_order = AsyncMock(return_value=fills or [])
    clob.cancel_order = AsyncMock(return_value=True)
    return clob


def _patch_db(monkeypatch, fake_conn):
    """Patch src.engine.order_manager.get_db so the manager's INSERT/UPDATE
    paths go to a controllable AsyncMock connection."""
    @asynccontextmanager
    async def _get_db():
        yield fake_conn

    monkeypatch.setattr("src.engine.order_manager.get_db", _get_db)


def _make_conn(*, fetchval_returns=1):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=fetchval_returns)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


@pytest.fixture
def fast_polling(monkeypatch):
    """Make the wait loop tick instantly so tests don't actually sleep."""
    monkeypatch.setattr("src.engine.order_manager.settings.LIVE_FILL_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr("src.engine.order_manager.settings.LIVE_ORDER_TIMEOUT_S", 1)
    monkeypatch.setattr("src.engine.order_manager.settings.LIVE_ORDER_MAX_RETRIES", 3)
    monkeypatch.setattr("src.engine.order_manager.settings.LIVE_SLIPPAGE_BPS", 50)


# --------------------------------------------------------------------------- #
# Limit-price math                                                             #
# --------------------------------------------------------------------------- #


def test_compute_limit_price_buy_adds_slippage():
    p = OrderManager._compute_limit_price("BUY", mid=0.50, slippage_bps=100)
    # 0.50 + (0.50 * 0.01) = 0.505
    assert p == pytest.approx(0.5050)


def test_compute_limit_price_sell_subtracts_slippage():
    p = OrderManager._compute_limit_price("SELL", mid=0.50, slippage_bps=100)
    assert p == pytest.approx(0.4950)


def test_compute_limit_price_clamps_to_band():
    # Mid 0.999, slippage 1000 bps => would be > 1, must clamp
    assert OrderManager._compute_limit_price("BUY", mid=0.999, slippage_bps=1000) <= 0.999
    # Mid 0.001, slippage 1000 bps => would be ~0, must clamp
    assert OrderManager._compute_limit_price("SELL", mid=0.001, slippage_bps=1000) >= 0.001


# --------------------------------------------------------------------------- #
# Shadow path                                                                  #
# --------------------------------------------------------------------------- #


async def test_shadow_path_returns_shadow_state_no_retry(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=42)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(
        dry_run=True,
        place_results=[PlaceOrderResult(
            success=True, clob_order_id=None, shadow=True,
        )],
    )
    om = OrderManager(clob)
    outcome = await om.place_for_position(
        live_trade_id=42, token_id="t", side="BUY", size_usdc=100.0,
    )
    assert outcome.final_state == "shadow"
    assert outcome.filled is False
    # One attempt, no retries.
    clob.place_limit_order.assert_called_once()
    # Status polling never invoked.
    clob.get_order_status.assert_not_called()
    # Shadow row inserted in live_orders.
    fake_conn.fetchval.assert_called_once()


# --------------------------------------------------------------------------- #
# First-attempt full fill                                                      #
# --------------------------------------------------------------------------- #


async def test_first_attempt_full_fill(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=10)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(
        midpoint=0.50,
        place_results=[PlaceOrderResult(success=True, clob_order_id="ord-1")],
        status_by_order_id={
            "ord-1": OrderStatus(
                clob_order_id="ord-1", state="filled",
                filled_size=200.0, remaining_size=0.0, avg_fill_price=0.5025,
            ),
        },
    )
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=10, token_id="t", side="BUY", size_usdc=100.0,
    )
    assert out.filled is True
    assert out.final_state == "filled"
    assert out.attempts == 1
    assert out.last_clob_order_id == "ord-1"
    assert out.avg_fill_price == pytest.approx(0.5025)
    # No cancel needed when fully filled.
    clob.cancel_order.assert_not_called()


# --------------------------------------------------------------------------- #
# Timeout -> reprice -> success                                                #
# --------------------------------------------------------------------------- #


async def test_timeout_then_second_attempt_fills(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=11)
    _patch_db(monkeypatch, fake_conn)

    # First attempt: status returns 'placed' until we hit the timeout.
    # Second attempt: filled immediately.
    clob = _make_clob(
        place_results=[
            PlaceOrderResult(success=True, clob_order_id="ord-1"),
            PlaceOrderResult(success=True, clob_order_id="ord-2"),
        ],
        status_by_order_id={
            "ord-1": OrderStatus(
                clob_order_id="ord-1", state="placed",
                filled_size=0.0, remaining_size=10.0,
            ),
            "ord-2": OrderStatus(
                clob_order_id="ord-2", state="filled",
                filled_size=10.0, remaining_size=0.0, avg_fill_price=0.51,
            ),
        },
    )
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=11, token_id="t", side="BUY", size_usdc=5.0,
    )
    assert out.filled is True
    assert out.attempts == 2
    assert out.last_clob_order_id == "ord-2"
    # Cancel was issued for the timed-out first order.
    clob.cancel_order.assert_called_once_with("ord-1")


# --------------------------------------------------------------------------- #
# Partial fill on timeout is acceptable                                        #
# --------------------------------------------------------------------------- #


async def test_partial_fill_on_timeout_is_treated_as_done(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=12)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(
        place_results=[PlaceOrderResult(success=True, clob_order_id="ord-1")],
        status_by_order_id={
            "ord-1": OrderStatus(
                clob_order_id="ord-1", state="placed",
                filled_size=3.0, remaining_size=7.0, avg_fill_price=0.50,
            ),
        },
    )
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=12, token_id="t", side="BUY", size_usdc=5.0,
    )
    assert out.filled is True
    assert out.final_state == "partial"
    assert out.filled_size_shares == pytest.approx(3.0)
    # Cancel still issued so the rest of the order doesn't linger.
    clob.cancel_order.assert_called_once_with("ord-1")


# --------------------------------------------------------------------------- #
# Max attempts exhausted                                                       #
# --------------------------------------------------------------------------- #


async def test_max_attempts_exhausted_returns_canceled(fast_polling, monkeypatch):
    monkeypatch.setattr("src.engine.order_manager.settings.LIVE_ORDER_MAX_RETRIES", 2)
    fake_conn = _make_conn(fetchval_returns=13)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(
        place_results=[
            PlaceOrderResult(success=True, clob_order_id=f"ord-{i}") for i in range(2)
        ],
        status_by_order_id={
            "ord-0": OrderStatus(
                clob_order_id="ord-0", state="placed",
                filled_size=0.0, remaining_size=10.0,
            ),
            "ord-1": OrderStatus(
                clob_order_id="ord-1", state="placed",
                filled_size=0.0, remaining_size=10.0,
            ),
        },
    )
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=13, token_id="t", side="BUY", size_usdc=5.0,
    )
    assert out.filled is False
    assert out.attempts == 2
    assert out.final_state == "canceled"
    assert clob.cancel_order.await_count == 2


# --------------------------------------------------------------------------- #
# Rejected place short-circuits                                                #
# --------------------------------------------------------------------------- #


async def test_rejected_place_does_not_retry(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=14)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(
        place_results=[PlaceOrderResult(
            success=False, clob_order_id=None,
            error_message="insufficient_balance",
        )],
        # If we incorrectly retried, the second call would raise StopIteration.
        status_by_order_id={},
    )
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=14, token_id="t", side="BUY", size_usdc=5.0,
    )
    assert out.filled is False
    assert out.final_state == "rejected"
    assert out.attempts == 1
    assert "insufficient_balance" in (out.error_message or "")
    clob.place_limit_order.assert_called_once()


# --------------------------------------------------------------------------- #
# Bad midpoint guards                                                          #
# --------------------------------------------------------------------------- #


async def test_bad_midpoint_returns_rejected(fast_polling, monkeypatch):
    fake_conn = _make_conn(fetchval_returns=15)
    _patch_db(monkeypatch, fake_conn)

    clob = _make_clob(midpoint=0.0)  # degenerate
    om = OrderManager(clob)
    out = await om.place_for_position(
        live_trade_id=15, token_id="t", side="BUY", size_usdc=5.0,
    )
    assert out.filled is False
    assert out.final_state == "rejected"
    assert "bad_midpoint" in (out.error_message or "")
    clob.place_limit_order.assert_not_called()
