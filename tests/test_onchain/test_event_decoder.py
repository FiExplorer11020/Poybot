"""Unit tests for :class:`src.onchain.event_decoder.EventDecoder`.

We craft canonical raw-log dicts from the ABI itself (via ``eth_abi.encode``)
so the tests don't rely on a hand-written fixture that could drift from
the contract. The decoder is pure — no DB / no Redis — so the tests are
straight-line: encode → call → assert.
"""

from __future__ import annotations

import pytest
from eth_abi import encode as abi_encode

from src.onchain.clob_abi import EVENT_TOPICS
from src.onchain.event_decoder import EventDecoder
from src.onchain.models import (
    FeeRateUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrdersMatchedEvent,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic raw logs whose bytes match what the contract
# would emit. Indexed inputs go into log.topics[1:], non-indexed inputs
# get ABI-encoded into log.data.
# ---------------------------------------------------------------------------


def _addr_topic(address_hex: str) -> str:
    """Pad a 20-byte address to a 32-byte topic. Lowercase, 0x-prefixed."""
    raw = address_hex.lower().replace("0x", "")
    return "0x" + raw.rjust(64, "0")


def _bytes32_topic(hex_str: str) -> str:
    raw = hex_str.lower().replace("0x", "")
    if len(raw) != 64:
        raise ValueError("bytes32 must be exactly 32 bytes")
    return "0x" + raw


def _uint_topic(value: int) -> str:
    return "0x" + format(value, "064x")


def _build_order_filled_log(
    *,
    order_hash: str = "1" * 64,
    maker: str = "0x" + "ab" * 20,
    taker: str = "0x" + "cd" * 20,
    maker_asset_id: int = 0,
    taker_asset_id: int = 12345,
    maker_amount_filled: int = 1_000_000_000,
    taker_amount_filled: int = 1_500_000,
    fee: int = 1234,
    block_number: int = 0x100,
    tx_hash: str = "0x" + "ee" * 32,
    log_index: int = 7,
    block_time: int = 1_700_000_000,
) -> dict:
    data = abi_encode(
        ["uint256", "uint256", "uint256", "uint256", "uint256"],
        [
            maker_asset_id,
            taker_asset_id,
            maker_amount_filled,
            taker_amount_filled,
            fee,
        ],
    )
    return {
        "topics": [
            EVENT_TOPICS["OrderFilled"],
            _bytes32_topic(order_hash),
            _addr_topic(maker),
            _addr_topic(taker),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block_number),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockTimestamp": block_time,
    }


def _build_orders_matched_log(
    *,
    taker_order_hash: str = "2" * 64,
    taker_order_maker: str = "0x" + "11" * 20,
    maker_asset_id: int = 999,
    taker_asset_id: int = 888,
    maker_amount_filled: int = 500_000_000,
    taker_amount_filled: int = 750_000,
) -> dict:
    data = abi_encode(
        ["uint256", "uint256", "uint256", "uint256"],
        [
            maker_asset_id,
            taker_asset_id,
            maker_amount_filled,
            taker_amount_filled,
        ],
    )
    return {
        "topics": [
            EVENT_TOPICS["OrdersMatched"],
            _bytes32_topic(taker_order_hash),
            _addr_topic(taker_order_maker),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": "0x200",
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": "0x5",
        "blockTimestamp": 1_700_001_000,
    }


def _build_order_cancelled_log(
    *,
    order_hash: str = "3" * 64,
) -> dict:
    return {
        "topics": [
            EVENT_TOPICS["OrderCancelled"],
            _bytes32_topic(order_hash),
        ],
        "data": "0x",
        "blockNumber": 12345,
        "transactionHash": "0x" + "fe" * 32,
        "logIndex": 9,
        "blockTimestamp": 1_700_002_000,
    }


def _build_fee_rate_updated_log(*, new_fee: int = 250) -> dict:
    data = abi_encode(["uint256"], [new_fee])
    return {
        "topics": [
            EVENT_TOPICS["FeeRateUpdated"],
        ],
        "data": "0x" + data.hex(),
        "blockNumber": "0x300",
        "transactionHash": "0x" + "12" * 32,
        "logIndex": 0,
    }


# ---------------------------------------------------------------------------
# Happy path: one test per event type
# ---------------------------------------------------------------------------


def test_decode_order_filled_happy_path():
    """Synthetic OrderFilled log → fully populated dataclass."""
    decoder = EventDecoder()
    log = _build_order_filled_log()
    event = decoder.decode_order_filled(log)
    assert isinstance(event, OrderFilledEvent)
    assert event.event_type == "OrderFilled"
    # Indexed (topics)
    assert event.order_hash == "0x" + "1" * 64
    assert event.maker == "0x" + "ab" * 20
    assert event.taker == "0x" + "cd" * 20
    # Non-indexed (data blob)
    assert event.maker_asset_id == 0
    assert event.taker_asset_id == 12345
    assert event.maker_amount_filled == 1_000_000_000
    assert event.taker_amount_filled == 1_500_000
    assert event.fee == 1234
    # Tx context
    assert event.block_number == 0x100
    assert event.tx_hash == "0x" + "ee" * 32
    assert event.log_index == 7
    assert event.block_time == 1_700_000_000.0


def test_decode_orders_matched_happy_path():
    decoder = EventDecoder()
    log = _build_orders_matched_log()
    event = decoder.decode_orders_matched(log)
    assert isinstance(event, OrdersMatchedEvent)
    assert event.event_type == "OrdersMatched"
    assert event.taker_order_hash == "0x" + "2" * 64
    assert event.taker_order_maker == "0x" + "11" * 20
    assert event.maker_asset_id == 999
    assert event.taker_asset_id == 888
    assert event.maker_amount_filled == 500_000_000
    assert event.taker_amount_filled == 750_000
    assert event.block_number == 0x200
    assert event.log_index == 5


def test_decode_order_cancelled_happy_path():
    decoder = EventDecoder()
    log = _build_order_cancelled_log()
    event = decoder.decode_order_cancelled(log)
    assert isinstance(event, OrderCancelledEvent)
    assert event.event_type == "OrderCancelled"
    assert event.order_hash == "0x" + "3" * 64
    assert event.block_number == 12345
    assert event.log_index == 9


def test_decode_fee_rate_updated_happy_path():
    decoder = EventDecoder()
    log = _build_fee_rate_updated_log(new_fee=400)
    event = decoder.decode_fee_rate_updated(log)
    assert isinstance(event, FeeRateUpdatedEvent)
    assert event.event_type == "FeeRateUpdated"
    assert event.new_fee_rate_bps == 400


# ---------------------------------------------------------------------------
# Mismatch path — wrong topic-0 returns None (no exception, no metric inc).
# Each per-event decoder must skip if the topic isn't its own.
# ---------------------------------------------------------------------------


def test_decode_order_filled_wrong_topic_returns_none():
    decoder = EventDecoder()
    log = _build_orders_matched_log()  # different event
    assert decoder.decode_order_filled(log) is None


def test_decode_orders_matched_wrong_topic_returns_none():
    decoder = EventDecoder()
    log = _build_order_filled_log()
    assert decoder.decode_orders_matched(log) is None


def test_decode_order_cancelled_wrong_topic_returns_none():
    decoder = EventDecoder()
    log = _build_order_filled_log()
    assert decoder.decode_order_cancelled(log) is None


# ---------------------------------------------------------------------------
# Dispatch (decode_any) correctness
# ---------------------------------------------------------------------------


def test_decode_any_routes_order_filled():
    decoder = EventDecoder()
    log = _build_order_filled_log()
    event = decoder.decode_any(log)
    assert isinstance(event, OrderFilledEvent)


def test_decode_any_routes_orders_matched():
    decoder = EventDecoder()
    log = _build_orders_matched_log()
    event = decoder.decode_any(log)
    assert isinstance(event, OrdersMatchedEvent)


def test_decode_any_routes_order_cancelled():
    decoder = EventDecoder()
    log = _build_order_cancelled_log()
    event = decoder.decode_any(log)
    assert isinstance(event, OrderCancelledEvent)


def test_decode_any_unknown_topic_returns_none():
    """Unknown topic-0 (random hash) must produce None silently —
    the listener subscription may receive non-CLOB logs if the filter
    is mis-scoped and we don't want to log-spam.
    """
    decoder = EventDecoder()
    log = {
        "topics": ["0x" + "ff" * 32],
        "data": "0x",
        "blockNumber": "0x1",
        "transactionHash": "0xdead",
        "logIndex": "0x0",
    }
    assert decoder.decode_any(log) is None


def test_decode_any_empty_topics_returns_none():
    decoder = EventDecoder()
    log = {"topics": [], "data": "0x", "blockNumber": 0, "transactionHash": "0x0", "logIndex": 0}
    assert decoder.decode_any(log) is None


# ---------------------------------------------------------------------------
# Malformed payload — returns None, increments failed-decode metric.
# Bad ABI data must not propagate as an exception (a single bad log
# would otherwise kill the subscription loop).
# ---------------------------------------------------------------------------


def test_decode_malformed_data_returns_none_and_bumps_metric(monkeypatch):
    """A truncated data blob makes eth_abi raise. The decoder must:
      1. Catch the exception.
      2. Return None.
      3. Increment polybot_chain_events_failed_decode_total{reason="malformed"}.
    """
    decoder = EventDecoder()
    log = _build_order_filled_log()
    # Truncate the data so eth_abi.decode raises.
    log["data"] = "0x" + log["data"][2:-32]  # drop 16 bytes — alignment breaks

    increments: list[tuple[str, str]] = []

    class _FakeMetric:
        def labels(self, event_type, reason):  # noqa: D401
            return self

        def inc(self):
            increments.append(("decoder", "malformed"))

    # Patch the decoder's metric reference.
    import src.onchain.event_decoder as _ed

    monkeypatch.setattr(_ed, "chain_events_failed_decode_total", _FakeMetric())

    result = decoder.decode_order_filled(log)
    assert result is None
    assert increments == [("decoder", "malformed")]


def test_decode_any_handles_malformed_without_raise(monkeypatch):
    """The dispatch entry point must also swallow malformed payloads."""
    decoder = EventDecoder()
    log = _build_orders_matched_log()
    log["data"] = "0xdeadbeef"  # too short

    class _FakeMetric:
        def labels(self, **_kw):
            return self

        def inc(self):
            pass

    import src.onchain.event_decoder as _ed

    monkeypatch.setattr(_ed, "chain_events_failed_decode_total", _FakeMetric())

    # Must not raise; must return None.
    assert decoder.decode_any(log) is None


# ---------------------------------------------------------------------------
# Tx context decoding — providers may return ints or hex strings; we
# must accept both.
# ---------------------------------------------------------------------------


def test_decode_accepts_int_block_number_and_log_index():
    decoder = EventDecoder()
    log = _build_order_cancelled_log()  # uses int block_number directly
    event = decoder.decode_order_cancelled(log)
    assert event is not None
    assert event.block_number == 12345
    assert event.log_index == 9
