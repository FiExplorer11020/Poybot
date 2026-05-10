"""
Tests for src/engine/clob_client_wrapper.py.

We never instantiate a real py-clob-client here — the SDK isn't installed
in CI and we don't want network calls. Instead we monkey-patch the
private `_get_client` to return a Mock with the SDK's surface, plus
toggle dry_run to exercise the shadow path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine.clob_client_wrapper import (
    CLOBClientWrapper,
    OrderbookSnapshot,
    PlaceOrderResult,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _wrapper(*, dry_run: bool = False, with_key: bool = True) -> CLOBClientWrapper:
    return CLOBClientWrapper(
        clob_url="https://clob.test",
        chain_id=137,
        private_key="0x" + "a" * 64 if with_key else "",
        funder_address="0xFunder",
        dry_run=dry_run,
    )


def _patch_client(wrapper: CLOBClientWrapper, fake_client) -> None:
    """Bypass lazy SDK build and inject a fake client."""
    wrapper._client = fake_client


# --------------------------------------------------------------------------- #
# dry_run flag derivation                                                      #
# --------------------------------------------------------------------------- #


def test_no_private_key_forces_dry_run_even_when_flag_false():
    w = _wrapper(dry_run=False, with_key=False)
    assert w.dry_run is True


def test_explicit_dry_run_true_overrides_key_presence():
    w = _wrapper(dry_run=True, with_key=True)
    assert w.dry_run is True


def test_explicit_dry_run_false_with_key_disables_dry_run():
    w = _wrapper(dry_run=False, with_key=True)
    assert w.dry_run is False


# --------------------------------------------------------------------------- #
# Read calls                                                                   #
# --------------------------------------------------------------------------- #


async def test_get_midpoint_returns_float_from_sdk_dict():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.get_midpoint = MagicMock(return_value={"mid": "0.523"})
    _patch_client(w, fake)
    assert await w.get_midpoint("token-1") == pytest.approx(0.523)
    fake.get_midpoint.assert_called_once_with("token-1")


async def test_get_orderbook_extracts_best_levels():
    w = _wrapper(dry_run=False, with_key=True)
    book = MagicMock()
    book.bids = [{"price": "0.50", "size": "100"}, {"price": "0.55", "size": "50"}]
    book.asks = [{"price": "0.56", "size": "30"}, {"price": "0.58", "size": "20"}]
    fake = MagicMock()
    fake.get_order_book = MagicMock(return_value=book)
    _patch_client(w, fake)

    snap: OrderbookSnapshot = await w.get_orderbook("token-1")
    assert snap.best_bid == pytest.approx(0.55)
    assert snap.bid_size == pytest.approx(50)
    assert snap.best_ask == pytest.approx(0.56)
    assert snap.ask_size == pytest.approx(30)
    assert snap.mid == pytest.approx(0.555)


async def test_get_orderbook_handles_empty_levels():
    w = _wrapper(dry_run=False, with_key=True)
    book = MagicMock()
    book.bids = []
    book.asks = []
    fake = MagicMock()
    fake.get_order_book = MagicMock(return_value=book)
    _patch_client(w, fake)

    snap = await w.get_orderbook("token-1")
    assert snap.best_bid == 0.0
    assert snap.best_ask == 0.0
    assert snap.mid == 0.0


# --------------------------------------------------------------------------- #
# place_limit_order — shadow path                                              #
# --------------------------------------------------------------------------- #


async def test_place_limit_order_shadow_returns_success_without_calling_sdk():
    w = _wrapper(dry_run=True, with_key=True)
    fake = MagicMock()
    fake.create_order = MagicMock(side_effect=AssertionError("must not be called"))
    fake.post_order = MagicMock(side_effect=AssertionError("must not be called"))
    _patch_client(w, fake)

    res: PlaceOrderResult = await w.place_limit_order(
        token_id="t", side="BUY", price=0.5, size=100,
    )
    assert res.success is True
    assert res.shadow is True
    assert res.clob_order_id is None


async def test_place_limit_order_validates_side():
    w = _wrapper(dry_run=False, with_key=True)
    res = await w.place_limit_order(token_id="t", side="HOLD", price=0.5, size=10)
    assert res.success is False
    assert "invalid side" in (res.error_message or "")


@pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.1])
async def test_place_limit_order_rejects_out_of_band_prices(price):
    w = _wrapper(dry_run=False, with_key=True)
    res = await w.place_limit_order(token_id="t", side="BUY", price=price, size=10)
    assert res.success is False
    assert "out of (0,1)" in (res.error_message or "")


async def test_place_limit_order_rejects_non_positive_size():
    w = _wrapper(dry_run=False, with_key=True)
    res = await w.place_limit_order(token_id="t", side="BUY", price=0.5, size=0)
    assert res.success is False
    assert "non-positive" in (res.error_message or "")


# --------------------------------------------------------------------------- #
# place_limit_order — real path (mocked SDK)                                   #
# --------------------------------------------------------------------------- #


async def test_place_limit_order_returns_clob_order_id_on_success(monkeypatch):
    w = _wrapper(dry_run=False, with_key=True)

    fake = MagicMock()
    fake.create_order = MagicMock(return_value={"signed": True})
    fake.post_order = MagicMock(return_value={"success": True, "orderID": "ord-42"})
    _patch_client(w, fake)

    # Patch the local imports inside the wrapper (OrderArgs, OrderType).
    fake_module = MagicMock()
    fake_module.OrderArgs = MagicMock(return_value="ARGS")
    fake_module.OrderType.GTC = "GTC"
    monkeypatch.setitem(__import__("sys").modules, "py_clob_client.clob_types", fake_module)

    res = await w.place_limit_order(token_id="t", side="BUY", price=0.5, size=10)
    assert res.success is True
    assert res.clob_order_id == "ord-42"
    fake.create_order.assert_called_once()
    fake.post_order.assert_called_once_with({"signed": True}, "GTC")


async def test_place_limit_order_returns_failure_on_clob_error(monkeypatch):
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.create_order = MagicMock(return_value={"signed": True})
    fake.post_order = MagicMock(return_value={"success": False, "errorMsg": "insufficient_balance"})
    _patch_client(w, fake)

    fake_module = MagicMock()
    fake_module.OrderArgs = MagicMock(return_value="ARGS")
    fake_module.OrderType.GTC = "GTC"
    monkeypatch.setitem(__import__("sys").modules, "py_clob_client.clob_types", fake_module)

    res = await w.place_limit_order(token_id="t", side="BUY", price=0.5, size=10)
    assert res.success is False
    assert "insufficient_balance" in (res.error_message or "")


async def test_place_limit_order_catches_sdk_exception(monkeypatch):
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.create_order = MagicMock(side_effect=RuntimeError("boom"))
    _patch_client(w, fake)

    fake_module = MagicMock()
    fake_module.OrderArgs = MagicMock(return_value="ARGS")
    fake_module.OrderType.GTC = "GTC"
    monkeypatch.setitem(__import__("sys").modules, "py_clob_client.clob_types", fake_module)

    res = await w.place_limit_order(token_id="t", side="BUY", price=0.5, size=10)
    assert res.success is False
    assert "boom" in (res.error_message or "")


# --------------------------------------------------------------------------- #
# cancel_order                                                                 #
# --------------------------------------------------------------------------- #


async def test_cancel_order_shadow_returns_true_without_sdk_call():
    w = _wrapper(dry_run=True, with_key=True)
    fake = MagicMock()
    fake.cancel = MagicMock(side_effect=AssertionError("must not be called"))
    _patch_client(w, fake)
    assert await w.cancel_order("ord-1") is True


async def test_cancel_order_real_returns_true_on_ack():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.cancel = MagicMock(return_value={"canceled": ["ord-1"], "not_canceled": {}})
    _patch_client(w, fake)
    assert await w.cancel_order("ord-1") is True


async def test_cancel_order_returns_false_when_not_canceled():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.cancel = MagicMock(return_value={"canceled": [], "not_canceled": {"ord-1": "already_filled"}})
    _patch_client(w, fake)
    assert await w.cancel_order("ord-1") is False


# --------------------------------------------------------------------------- #
# get_order_status — state vocabulary                                          #
# --------------------------------------------------------------------------- #


async def test_get_order_status_shadow_returns_none():
    w = _wrapper(dry_run=True, with_key=True)
    assert await w.get_order_status("ord-1") is None


async def test_get_order_status_filled_when_size_matches_original():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.get_order = MagicMock(return_value={
        "status": "MATCHED", "size_matched": "10.0", "original_size": "10.0", "price": "0.50",
    })
    _patch_client(w, fake)
    s = await w.get_order_status("ord-1")
    assert s is not None
    assert s.state == "filled"
    assert s.filled_size == pytest.approx(10.0)
    assert s.remaining_size == pytest.approx(0.0)


async def test_get_order_status_partial_when_some_matched():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.get_order = MagicMock(return_value={
        "status": "LIVE", "size_matched": "3.0", "original_size": "10.0", "price": "0.50",
    })
    _patch_client(w, fake)
    s = await w.get_order_status("ord-1")
    assert s.state == "partial"
    assert s.filled_size == pytest.approx(3.0)
    assert s.remaining_size == pytest.approx(7.0)


async def test_get_order_status_canceled_state():
    w = _wrapper(dry_run=False, with_key=True)
    fake = MagicMock()
    fake.get_order = MagicMock(return_value={
        "status": "CANCELED", "size_matched": "0", "original_size": "10",
    })
    _patch_client(w, fake)
    s = await w.get_order_status("ord-1")
    assert s.state == "canceled"
