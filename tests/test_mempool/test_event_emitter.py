"""Tests for :mod:`src.mempool.event_emitter` — Round 7 Wave-2.

Covers:
  * :meth:`LeaderIntentPublisher.publish` writes one entry to the
    target stream via the underlying :class:`StreamProducer`.
  * Payload shape: ``trace_id`` is set to ``intent.intent_id``,
    ``published_at_ms`` is injected by the producer, ``Decimal``
    fields are stringified, ``intent_received_at`` is replaced by
    ``intent_received_at_ms`` (epoch ms int).
  * Reconnect-safe by delegation: the publisher's stop+start cycle
    rebuilds the producer without raising.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.mempool.event_emitter import (
    MEMPOOL_LEADER_INTENT_STREAM,
    LeaderIntentPublisher,
    _intent_to_payload,
)
from src.mempool.tx_decoder import LeaderIntent


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _make_intent(*, intent_id: str = "intent-abc-123") -> LeaderIntent:
    return LeaderIntent(
        intent_id=intent_id,
        wallet="0x" + "ab" * 20,
        market_id="market-1",
        token_id="token-yes",
        side="buy",
        size_usdc=Decimal("1234.56"),
        price=Decimal("0.6234"),
        order_type="GTC",
        intent_received_at=datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
        expected_block=12345678,
        tx_hash="0xfeedface",
        nonce=42,
        replaces=None,
    )


# --------------------------------------------------------------------------- #
# 1. publish writes to the stream                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_writes_one_stream_entry(redis_client):
    publisher = LeaderIntentPublisher(redis_url="redis://ignored")
    await publisher.start(redis_client=redis_client)
    try:
        intent = _make_intent()
        entry_id = await publisher.publish(intent)
        assert isinstance(entry_id, str) and "-" in entry_id
        # Stream now has exactly one entry.
        assert await redis_client.xlen(MEMPOOL_LEADER_INTENT_STREAM) == 1
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_publish_payload_has_trace_and_published_ms(redis_client):
    """The payload must carry ``trace_id`` (= intent_id) and a numeric
    ``published_at_ms`` injected by the producer."""
    publisher = LeaderIntentPublisher(redis_url="redis://ignored")
    await publisher.start(redis_client=redis_client)
    try:
        intent = _make_intent(intent_id="trace-uuid-xyz")
        await publisher.publish(intent)
        entries = await redis_client.xrange(MEMPOOL_LEADER_INTENT_STREAM)
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        payload = json.loads(fields["data"])
        assert payload["trace_id"] == "trace-uuid-xyz"
        assert isinstance(payload["published_at_ms"], int)
        assert payload["published_at_ms"] > 0
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_publish_serialises_decimals_as_strings(redis_client):
    """``size_usdc`` and ``price`` are ``Decimal`` on the intent; the
    wire payload must stringify them for JSON portability."""
    publisher = LeaderIntentPublisher(redis_url="redis://ignored")
    await publisher.start(redis_client=redis_client)
    try:
        await publisher.publish(_make_intent())
        entries = await redis_client.xrange(MEMPOOL_LEADER_INTENT_STREAM)
        payload = json.loads(entries[0][1]["data"])
        assert payload["size_usdc"] == "1234.56"
        assert payload["price"] == "0.6234"
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_publish_converts_datetime_to_epoch_ms(redis_client):
    """``intent_received_at`` (datetime) → ``intent_received_at_ms``
    (int epoch ms)."""
    publisher = LeaderIntentPublisher(redis_url="redis://ignored")
    await publisher.start(redis_client=redis_client)
    try:
        await publisher.publish(_make_intent())
        entries = await redis_client.xrange(MEMPOOL_LEADER_INTENT_STREAM)
        payload = json.loads(entries[0][1]["data"])
        assert "intent_received_at" not in payload
        assert "intent_received_at_ms" in payload
        # 2026-05-12T10:00:00Z = 1778011200 epoch s
        expected_ms = int(
            datetime(
                2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc
            ).timestamp() * 1000
        )
        assert payload["intent_received_at_ms"] == expected_ms
    finally:
        await publisher.stop()


# --------------------------------------------------------------------------- #
# 2. Helper-function unit tests                                                #
# --------------------------------------------------------------------------- #


def test_intent_to_payload_shape():
    """The payload helper is the spec-of-record for the wire shape;
    pin it directly so changes are reviewed."""
    intent = _make_intent()
    payload = _intent_to_payload(intent)
    assert payload["wallet"] == intent.wallet
    assert payload["market_id"] == intent.market_id
    assert payload["token_id"] == intent.token_id
    assert payload["side"] == "buy"
    assert payload["size_usdc"] == "1234.56"
    assert payload["price"] == "0.6234"
    assert payload["order_type"] == "GTC"
    assert payload["tx_hash"] == intent.tx_hash
    assert payload["nonce"] == 42
    assert payload["expected_block"] == 12345678
    assert payload["replaces"] is None
    # The trace_id mirrors the intent_id.
    assert payload["trace_id"] == intent.intent_id
    # Timestamp field rename took effect.
    assert "intent_received_at" not in payload
    assert isinstance(payload["intent_received_at_ms"], int)


def test_intent_to_payload_preserves_replaces():
    intent = _make_intent()
    intent.replaces = "0xprev_tx"
    payload = _intent_to_payload(intent)
    assert payload["replaces"] == "0xprev_tx"


# --------------------------------------------------------------------------- #
# 3. Lifecycle: idempotent start/stop                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lifecycle_idempotent_and_reconnect_safe(redis_client):
    """start / stop / start / publish / stop must not raise — this
    exercises the underlying StreamProducer's reconnect-safe surface
    by re-entering the lifecycle."""
    publisher = LeaderIntentPublisher(redis_url="redis://ignored")
    await publisher.start(redis_client=redis_client)
    await publisher.start(redis_client=redis_client)  # idempotent
    await publisher.publish(_make_intent())
    await publisher.stop()
    await publisher.stop()  # idempotent
