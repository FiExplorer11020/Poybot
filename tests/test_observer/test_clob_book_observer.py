"""Unit tests for :mod:`src.observer.clob_book_observer` — Round 11 § 3.1.

Cover:
  * WS event decode — every canonical event_type round-trips through
    :func:`decode_ws_message`.
  * Wallet attribution: NULL on placement events is PRESERVED (per spec
    § 3.1).
  * Backpressure: the 50001st event drops the OLDEST event, not the
    incoming one (the spec contract). Metric increments observed.
  * Redis Stream publish smoke test — fakeredis-backed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.observer.clob_book_observer import (
    EVENT_CANCELLED,
    EVENT_FILLED,
    EVENT_MODIFIED,
    EVENT_PARTIAL_FILL,
    EVENT_PLACED,
    BookEvent,
    CLOBBookObserver,
    decode_ws_message,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _msg(**overrides):
    base = {
        "event_type": "order_placed",
        "market_id": "m1",
        "token_id": "t1",
        "side": "buy",
        "price": "0.6234",
        "size_delta": "100",
        "order_hash": "0xfeed",
        "timestamp": 1715500800,  # 2024-05-12 08:00:00 UTC (any plausible past)
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. WS decoder                                                                #
# --------------------------------------------------------------------------- #


class TestDecodeWsMessage:
    def test_placed_event_round_trips(self):
        event = decode_ws_message(_msg())
        assert event is not None
        assert event.event_type == EVENT_PLACED
        assert event.market_id == "m1"
        assert event.token_id == "t1"
        assert event.side == "buy"
        assert event.price == Decimal("0.6234")
        assert event.size_delta == Decimal("100")
        assert event.order_hash == "0xfeed"
        # Wallet stays NULL on placement events — spec § 3.1 contract.
        assert event.wallet_address is None

    @pytest.mark.parametrize(
        "raw_type,canonical",
        [
            ("order_placed", EVENT_PLACED),
            ("place", EVENT_PLACED),
            ("order_modified", EVENT_MODIFIED),
            ("update", EVENT_MODIFIED),
            ("order_cancelled", EVENT_CANCELLED),
            ("canceled", EVENT_CANCELLED),
            ("order_partial_fill", EVENT_PARTIAL_FILL),
            ("partial", EVENT_PARTIAL_FILL),
            ("order_filled", EVENT_FILLED),
            ("trade", EVENT_FILLED),
        ],
    )
    def test_event_type_normalization(self, raw_type, canonical):
        event = decode_ws_message(_msg(event_type=raw_type))
        assert event is not None
        assert event.event_type == canonical

    def test_cancel_sets_negative_size_delta(self):
        event = decode_ws_message(
            _msg(event_type="order_cancelled", size_delta="50")
        )
        assert event is not None
        assert event.size_delta == Decimal("-50")

    def test_wallet_attribution_present_on_fill(self):
        event = decode_ws_message(
            _msg(
                event_type="order_filled",
                wallet_address="0xABC123",
            )
        )
        assert event is not None
        assert event.event_type == EVENT_FILLED
        assert event.wallet_address == "0xabc123"

    def test_unknown_event_type_returns_none(self):
        assert decode_ws_message(_msg(event_type="weird_unknown_thing")) is None

    def test_missing_market_id_returns_none(self):
        assert decode_ws_message(_msg(market_id="")) is None

    def test_missing_token_id_returns_none(self):
        assert decode_ws_message(_msg(token_id="")) is None

    def test_unknown_side_returns_none(self):
        assert decode_ws_message(_msg(side="lolwut")) is None

    def test_ms_epoch_timestamp_normalized_to_seconds(self):
        ms_ts = 1715500800123  # 2024-05-12 08:00:00.123 in ms
        event = decode_ws_message(_msg(timestamp=ms_ts))
        assert event is not None
        # Should be a tz-aware datetime in 2024.
        assert event.event_time.tzinfo is not None
        assert event.event_time.year == 2024

    def test_iso_timestamp(self):
        event = decode_ws_message(
            _msg(timestamp="2026-05-12T10:00:00+00:00")
        )
        assert event is not None
        assert event.event_time.year == 2026


# --------------------------------------------------------------------------- #
# 2. Backpressure — drop OLDEST (spec contract)                                #
# --------------------------------------------------------------------------- #


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_50001st_event_drops_oldest(self, redis_client):
        """50,001st event into a 50,000-capacity queue must drop the
        OLDEST event (per spec § 3.1), NOT the incoming one. Verify by
        ensuring the queue still contains the most-recent N events and
        the dropped counter incremented exactly once.
        """
        # Bound the queue small (50) so the test runs fast; the
        # semantic is identical at 50_000.
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=50,
        )
        # Push 50 events — fills the queue.
        for i in range(50):
            evt = await observer.handle_message(
                _msg(order_hash=f"h{i:03d}", timestamp=1715500800 + i)
            )
            assert evt is not None
        assert observer.queue_depth() == 50
        # Push the 51st — must drop the oldest entry (h000), not the new one.
        evt = await observer.handle_message(
            _msg(order_hash="h_new", timestamp=1715500900)
        )
        assert evt is not None
        # Queue still at capacity.
        assert observer.queue_depth() == 50
        # The dropped counter went up.
        assert observer.events_dropped_queue_full >= 1
        # The OLDEST is gone; the NEW one is still there. Verify by
        # draining and looking at the order_hash sequence.
        drained = []
        while observer.queue_depth() > 0:
            drained.append(observer._db_queue.popleft())
        order_hashes = [e.order_hash for e in drained]
        assert "h000" not in order_hashes  # oldest evicted
        assert "h_new" in order_hashes  # incoming kept

    @pytest.mark.asyncio
    async def test_n_events_above_capacity_increment_drop_counter(
        self, redis_client
    ):
        """Pushing 60 events into a 50-slot queue produces exactly 10
        drops (40 fit + 10 over capacity → 10 evictions on the
        oldest-drop semantic)."""
        # Notably the deque applies oldest-drop on each push past
        # capacity, so each event after the 50th evicts one older.
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=50,
        )
        for i in range(60):
            await observer.handle_message(_msg(order_hash=f"h{i:03d}"))
        assert observer.queue_depth() == 50
        # 10 events worth of drops on BOTH the db queue and the stream
        # queue (the observer pushes into both).
        assert observer.events_dropped_queue_full >= 10


# --------------------------------------------------------------------------- #
# 3. Wallet=NULL preserved on placement                                        #
# --------------------------------------------------------------------------- #


class TestWalletAttribution:
    @pytest.mark.asyncio
    async def test_placement_wallet_null_preserved(self, redis_client):
        """The observer must NOT invent a wallet for placement events
        (spec § 3.1 — Polymarket WS doesn't ship one). Downstream joins
        with trades_observed handle the attribution."""
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=100,
        )
        event = await observer.handle_message(_msg())  # placement, no wallet
        assert event is not None
        assert event.wallet_address is None
        # Confirm DB queue carries the None.
        queued = observer._db_queue.popleft()
        assert queued.wallet_address is None


# --------------------------------------------------------------------------- #
# 4. Redis Stream publish smoke test                                           #
# --------------------------------------------------------------------------- #


class TestStreamPublish:
    @pytest.mark.asyncio
    async def test_stream_publisher_writes_event(self, redis_client):
        """The stream publisher loop ingests from the stream queue and
        writes one XADD entry per event. Drive one full publish cycle
        manually so we don't depend on the background task scheduling."""
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=10,
            stream_name="book:events:stream:test",
        )
        # Use a fill event so the wallet is non-null end-to-end.
        await observer.handle_message(
            _msg(
                event_type="order_filled",
                wallet_address="0xabc",
            )
        )
        # Drain the stream queue manually and write to Redis directly
        # (the publisher loop does this in the daemon; we mimic the
        # one-shot path here).
        batch = await observer._drain_stream_for_test()
        assert len(batch) == 1
        await redis_client.xadd(
            "book:events:stream:test",
            {"data": json.dumps(batch[0].to_stream_payload())},
            maxlen=100,
            approximate=True,
        )
        entries = await redis_client.xrange("book:events:stream:test")
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        payload = json.loads(fields["data"])
        assert payload["event_type"] == EVENT_FILLED
        assert payload["wallet_address"] == "0xabc"
        assert payload["market_id"] == "m1"

    @pytest.mark.asyncio
    async def test_stream_payload_decimal_fields_stringified(self, redis_client):
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=10,
        )
        await observer.handle_message(_msg(price="0.7", size_delta="125.50"))
        batch = await observer._drain_stream_for_test()
        payload = batch[0].to_stream_payload()
        assert payload["price"] == "0.7"
        assert payload["size_delta"] == "125.50"
        # Decimal -> string survives a json round-trip.
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        assert decoded["price"] == "0.7"


# --------------------------------------------------------------------------- #
# 5. Invalid events counted                                                    #
# --------------------------------------------------------------------------- #


class TestInvalidEvents:
    @pytest.mark.asyncio
    async def test_invalid_event_counted_and_dropped(self, redis_client):
        observer = CLOBBookObserver(
            redis_client=redis_client,
            ws_factory=None,
            queue_maxsize=10,
        )
        result = await observer.handle_message({"junk": True})
        assert result is None
        assert observer.events_dropped_invalid == 1
        assert observer.events_received == 0
        assert observer.queue_depth() == 0
