"""
Tests for the Phase 3 Task D ``ingest:gap`` Telegram alert path.

Invariants:

* First ``ingest:gap`` message per source produces a formatted alert.
* Within ``INGEST_ALERT_COOLDOWN_S`` (per-source) the next alert for
  the SAME source is dropped (cooldown — does NOT touch the global
  outbound throttle).
* A different source within the same window IS allowed (the cooldown
  is per-source).
* The formatter renders the new payload shape correctly.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.telegram_bot import auth, formatters, notifier


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _allow_chat(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "111")
    auth.reload_allowlist()
    yield
    auth.reload_allowlist()


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.02) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Formatter                                                                    #
# --------------------------------------------------------------------------- #


def test_format_ingest_gap_warning():
    text = formatters.format_ingest_gap(
        {
            "source": "falcon_leaderboard",
            "duration_s": 2400.0,
            "severity": "warning",
            "threshold_s": 2100,
        }
    )
    assert "falcon_leaderboard" in text
    assert "warning" in text
    # Duration in minutes, 1 decimal.
    assert "40.0 min" in text


def test_format_ingest_gap_critical_uses_red_icon():
    text = formatters.format_ingest_gap(
        {
            "source": "ws_market_feed",
            "duration_s": 1800.0,
            "severity": "critical",
            "threshold_s": 60,
        }
    )
    assert "critical" in text
    assert "🚨" in text


# --------------------------------------------------------------------------- #
# Cooldown                                                                     #
# --------------------------------------------------------------------------- #


async def test_first_alert_sends(redis_client):
    """A single ingest_gap publish hits Telegram exactly once per chat."""
    send = AsyncMock()
    # Cooldown of 1s — keeps the test snappy.
    n = notifier.TelegramNotifier(
        redis_client=redis_client, send_fn=send, ingest_alert_cooldown_s=1
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_INGEST_GAP,
            json.dumps(
                {
                    "source": "falcon_leaderboard",
                    "duration_s": 2400.0,
                    "severity": "warning",
                    "threshold_s": 2100,
                }
            ),
        )
        ok = await _wait_for(lambda: send.await_count >= 1)
        assert ok
        # Authorized chat list has 1 entry; we sent 1 alert → 1 send.
        assert send.await_count == 1
    finally:
        await n.stop()


async def test_per_source_cooldown_drops_second_alert(redis_client):
    """Two alerts for the same source within cooldown → only the first sends."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        ingest_alert_cooldown_s=60,  # large — second should be dropped
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        payload = json.dumps(
            {
                "source": "falcon_wallet360",
                "duration_s": 8000.0,
                "severity": "warning",
                "threshold_s": 7200,
            }
        )
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        await _wait_for(lambda: send.await_count >= 1)
        # Second publish within cooldown.
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        # Give the dispatcher a moment.
        await asyncio.sleep(0.1)
        assert send.await_count == 1
    finally:
        await n.stop()


async def test_different_source_is_allowed_within_cooldown(redis_client):
    """Cooldown is per-source. Source A doesn't suppress source B."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        ingest_alert_cooldown_s=60,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            notifier.CHANNEL_INGEST_GAP,
            json.dumps(
                {
                    "source": "falcon_leaderboard",
                    "duration_s": 2400.0,
                    "severity": "warning",
                }
            ),
        )
        await _wait_for(lambda: send.await_count >= 1)
        # Different source — should NOT be suppressed.
        await redis_client.publish(
            notifier.CHANNEL_INGEST_GAP,
            json.dumps(
                {
                    "source": "ws_market_feed",
                    "duration_s": 120.0,
                    "severity": "warning",
                }
            ),
        )
        ok = await _wait_for(lambda: send.await_count >= 2, timeout=1.5)
        assert ok, f"expected 2 sends, got {send.await_count}"
    finally:
        await n.stop()


async def test_cooldown_zero_means_no_throttle(redis_client):
    """Setting cooldown to 0 disables the per-source gate."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        ingest_alert_cooldown_s=0,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        payload = json.dumps(
            {
                "source": "falcon_trades",
                "duration_s": 700.0,
                "severity": "warning",
            }
        )
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        ok = await _wait_for(lambda: send.await_count >= 2, timeout=1.5)
        assert ok, f"expected 2 sends, got {send.await_count}"
    finally:
        await n.stop()
