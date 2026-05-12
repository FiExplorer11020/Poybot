"""Unit tests for :mod:`src.microstructure.daemon` — Round 11 § 3.2.

The daemon is mostly plumbing on top of well-tested components; we
verify:
  * It starts cleanly with a fakeredis client (consumer group created,
    no exceptions).
  * ``stop()`` is graceful — no orphaned tasks.
  * ``run_once()`` processes a Redis Stream entry and feeds it to the
    deriver.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.microstructure.daemon import MicrostructureDaemon, _decode_stream_entry
from src.microstructure.rollup import MicrostructureRollup


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_decode_stream_entry_round_trips():
    """The on-the-wire payload uses ``{'data': json}`` — reverse of
    BookEvent.to_stream_payload."""
    payload = {
        "event_time": "2026-05-12T10:00:00+00:00",
        "market_id": "m1",
        "token_id": "t1",
        "event_type": "placed",
        "side": "buy",
        "price": "0.5",
        "size_delta": "100",
        "order_hash": "h1",
        "wallet_address": None,
        "source": "ws",
    }
    event = _decode_stream_entry({"data": json.dumps(payload)})
    assert event is not None
    assert event.event_type == "placed"
    assert event.market_id == "m1"
    assert event.price == Decimal("0.5")


@pytest.mark.asyncio
async def test_decode_stream_entry_handles_garbage():
    assert _decode_stream_entry({"data": "not-json"}) is None
    assert _decode_stream_entry({}) is None


@pytest.mark.asyncio
async def test_start_creates_consumer_group(redis_client):
    daemon = MicrostructureDaemon(
        redis_client=redis_client,
        stream_name="book:events:stream:test",
        bucket_s=60,
    )
    await daemon.start()
    # The group exists — calling start twice doesn't raise.
    await daemon.start()
    await daemon.stop()


@pytest.mark.asyncio
async def test_run_once_processes_stream_entry(redis_client):
    stream = "book:events:stream:test"
    # Pre-create the stream and a single event entry. We use approximate
    # MAXLEN so a fresh stream doesn't trip the xreadgroup ID logic.
    payload = {
        "event_time": "2026-05-12T10:00:01+00:00",
        "market_id": "m1",
        "token_id": "t1",
        "event_type": "placed",
        "side": "buy",
        "price": "0.5",
        "size_delta": "100",
        "order_hash": "h1",
        "wallet_address": None,
        "source": "ws",
    }
    # Stub the rollup so we don't try to hit a real DB.
    rollup = MicrostructureRollup(bucket_s=60)
    rollup.flush = AsyncMock(return_value=0)

    daemon = MicrostructureDaemon(
        redis_client=redis_client,
        stream_name=stream,
        bucket_s=60,
        rollup=rollup,
    )
    await daemon.start()
    # Use last_id="0" semantics by overriding the group's start to read
    # everything in the stream from the beginning. (We adjusted the
    # daemon to use ">" which means new-since-last-read; so writing
    # AFTER start should be picked up.)
    await redis_client.xadd(stream, {"data": json.dumps(payload)})
    n = await daemon.run_once()
    assert n == 1
    assert daemon.events_processed == 1
    await daemon.stop()


@pytest.mark.asyncio
async def test_stop_is_graceful(redis_client):
    daemon = MicrostructureDaemon(
        redis_client=redis_client,
        stream_name="book:events:stream:test_stop",
        bucket_s=60,
    )
    await daemon.start()
    # Should not raise even if start was called once.
    await daemon.stop()
    # Second stop is also a no-op.
    await daemon.stop()
