"""
Tests for the TelegramNotifier (S3.9).

Drives a real fakeredis pub/sub instance and asserts that:
  * each of the 6 channels routes to a properly formatted message,
  * the broadcast fans out to every authorized chat_id,
  * the sliding-window rate limit drops messages over the cap,
  * a bad chat_id (send_fn raising) doesn't stop fanout.

We use the same fakeredis pattern as test_dual_routing_integration —
the notifier doesn't care whether it's pub/sub'ing on Redis or fake
Redis, only that publish/subscribe semantics hold.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.telegram_bot import auth, notifier


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _allow_two_chats(monkeypatch):
    """Two chat_ids in the allowlist — broadcast must hit both."""
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "111,222")
    auth.reload_allowlist()
    yield
    auth.reload_allowlist()


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.02) -> bool:
    """Poll the predicate until it returns truthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Channel routing                                                              #
# --------------------------------------------------------------------------- #


async def test_paper_opened_routes_to_formatter(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)  # let pubsub.subscribe land
        await redis_client.publish(
            notifier.CHANNEL_PAPER_OPENED,
            json.dumps(
                {
                    "trade_id": 7,
                    "market_id": "0xabc",
                    "strategy": "follow",
                    "direction": "yes",
                    "size_usdc": 50.0,
                    "entry_price": 0.5,
                }
            ),
        )
        ok = await _wait_for(lambda: send.await_count >= 2)
        assert ok, f"expected 2 sends (one per chat_id), got {send.await_count}"
    finally:
        await n.stop()
    # Both chats received the same text
    chats = sorted(call.args[0] for call in send.await_args_list)
    assert chats == [111, 222]
    text = send.await_args_list[0].args[1]
    assert "PAPER OPEN" in text
    assert "FOLLOW" in text
    assert "#7" in text


async def test_paper_closed_routes_correctly(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_PAPER_CLOSED,
            json.dumps({"trade_id": 1, "pnl_usdc": 5.0, "close_reason": "tp"}),
        )
        ok = await _wait_for(lambda: send.await_count >= 2)
        assert ok
    finally:
        await n.stop()
    text = send.await_args_list[0].args[1]
    assert "PAPER CLOSE" in text
    assert "+5.00$" in text


async def test_live_opened_and_closed_route_correctly(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_LIVE_OPENED,
            json.dumps({"trade_id": 9, "strategy": "fade", "direction": "no"}),
        )
        await redis_client.publish(
            notifier.CHANNEL_LIVE_CLOSED,
            json.dumps({"trade_id": 9, "pnl_usdc": -1.0, "close_reason": "stop"}),
        )
        # 2 events x 2 chats = 4 sends
        ok = await _wait_for(lambda: send.await_count >= 4)
        assert ok, f"got {send.await_count} sends"
    finally:
        await n.stop()
    texts = [c.args[1] for c in send.await_args_list]
    assert any("LIVE OPEN" in t for t in texts)
    assert any("LIVE CLOSE" in t for t in texts)


async def test_killswitch_channel_routes_correctly(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_KILLSWITCH,
            json.dumps(
                {
                    "execution_enabled": False,
                    "real_execution_enabled": False,
                    "updated_by": "test",
                    "paused_reason": "drill",
                }
            ),
        )
        ok = await _wait_for(lambda: send.await_count >= 2)
        assert ok
    finally:
        await n.stop()
    text = send.await_args_list[0].args[1]
    assert "KILLSWITCH FLIP" in text
    assert "drill" in text


async def test_engine_crash_routes_correctly(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_ENGINE_CRASH,
            json.dumps({"component": "engine", "error_type": "RuntimeError", "error": "boom"}),
        )
        ok = await _wait_for(lambda: send.await_count >= 2)
        assert ok
    finally:
        await n.stop()
    text = send.await_args_list[0].args[1]
    assert "CRITICAL" in text
    assert "boom" in text


# --------------------------------------------------------------------------- #
# Robustness                                                                   #
# --------------------------------------------------------------------------- #


async def test_bad_json_does_not_kill_subscriber(redis_client):
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        # Garbage payload — must be skipped, not crash the loop
        await redis_client.publish(notifier.CHANNEL_PAPER_OPENED, "{not json")
        # Then a good one — proves the loop is still alive
        await redis_client.publish(
            notifier.CHANNEL_PAPER_OPENED,
            json.dumps({"trade_id": 1, "market_id": "0x", "strategy": "follow"}),
        )
        ok = await _wait_for(lambda: send.await_count >= 2)
        assert ok
    finally:
        await n.stop()


async def test_one_failing_chat_does_not_starve_others(redis_client):
    """If chat 111 raises, chat 222 still receives the message."""
    sent_to: list[int] = []

    async def flaky_send(chat_id: int, text: str) -> None:
        if chat_id == 111:
            raise RuntimeError("simulated send failure")
        sent_to.append(chat_id)

    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=flaky_send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_PAPER_OPENED,
            json.dumps({"trade_id": 1, "market_id": "0x", "strategy": "follow"}),
        )
        ok = await _wait_for(lambda: 222 in sent_to)
        assert ok, "chat 222 must receive message even if 111 fails"
    finally:
        await n.stop()


async def test_rate_limit_drops_excess(redis_client, monkeypatch):
    # cap of 2/min — third event in the same window must be dropped
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client, send_fn=send, max_per_minute=2
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        for i in range(5):
            await redis_client.publish(
                notifier.CHANNEL_PAPER_OPENED,
                json.dumps({"trade_id": i, "market_id": "0x", "strategy": "follow"}),
            )
        # Wait long enough that anything that was going to be sent has been
        await asyncio.sleep(0.3)
    finally:
        await n.stop()
    # 2 allowed broadcasts × 2 chats = 4 sends max
    assert send.await_count <= 4
    # And at least 2 (the first broadcast must always go through)
    assert send.await_count >= 2


async def test_disabled_when_no_chats(redis_client, monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "")
    auth.reload_allowlist()
    send = AsyncMock()
    n = notifier.TelegramNotifier(redis_client=redis_client, send_fn=send)
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_PAPER_OPENED,
            json.dumps({"trade_id": 1, "market_id": "0x", "strategy": "follow"}),
        )
        await asyncio.sleep(0.1)
    finally:
        await n.stop()
    assert send.await_count == 0
