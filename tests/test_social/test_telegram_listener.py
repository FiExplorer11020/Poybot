"""TelegramPublicChannelListener tests.

Coverage:
  * process_update extracts text + channel handle and publishes.
  * Channel allowlist filters out off-list messages.
  * Updates without text are skipped.
  * No token / no library — listener no-ops gracefully.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis.aioredis as fakeredis_async
import pytest

from src.social.telegram_listener import TelegramPublicChannelListener


@pytest.fixture
async def redis_client():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _fake_update(*, channel: str, text: str | None, message_id: int = 1):
    """Build a duck-typed object that mimics a python-telegram-bot Update.

    The listener reads: update.channel_post / update.message;
    msg.text, msg.chat.username/id, msg.date, msg.message_id.
    """
    chat = SimpleNamespace(username=channel, id=12345)
    msg = SimpleNamespace(
        text=text,
        chat=chat,
        date=datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
        message_id=message_id,
    )
    return SimpleNamespace(channel_post=msg, message=None)


class TestProcessUpdate:
    @pytest.mark.asyncio
    async def test_publishes_message_to_stream(self, redis_client):
        listener = TelegramPublicChannelListener(
            redis_client,
            bot_token="test-token",
            channels=["alphachan"],
        )
        update = _fake_update(channel="alphachan", text="just entered YES")
        n = await listener.process_update(update)
        assert n == 1
        entries = await redis_client.xrange(listener._stream_name)
        assert len(entries) == 1
        # Verify the entry carries the right author.
        import json
        fields = entries[0][1]
        payload = json.loads(fields["data"])
        assert payload["author_handle"] == "alphachan"
        assert payload["text"] == "just entered YES"

    @pytest.mark.asyncio
    async def test_off_channel_message_is_dropped(self, redis_client):
        listener = TelegramPublicChannelListener(
            redis_client,
            bot_token="test-token",
            channels=["alphachan"],
        )
        update = _fake_update(channel="randomchat", text="not interesting")
        n = await listener.process_update(update)
        assert n == 0

    @pytest.mark.asyncio
    async def test_empty_text_is_dropped(self, redis_client):
        listener = TelegramPublicChannelListener(
            redis_client,
            bot_token="test-token",
            channels=["alphachan"],
        )
        update = _fake_update(channel="alphachan", text=None)
        n = await listener.process_update(update)
        assert n == 0


class TestStartFallback:
    @pytest.mark.asyncio
    async def test_no_library_means_no_op_start(self, redis_client, monkeypatch):
        # Patch _maybe_import_ptb to return None — simulates absent dep.
        from src.social import telegram_listener as mod

        monkeypatch.setattr(mod, "_maybe_import_ptb", lambda: None)
        listener = TelegramPublicChannelListener(
            redis_client,
            bot_token="test-token",
            channels=["alphachan"],
        )
        # Should not raise.
        await listener.start()
        assert listener._running is False

    @pytest.mark.asyncio
    async def test_application_injection_works(self, redis_client):
        # Inject a fully mocked application — start() should call its
        # async lifecycle hooks.
        app = MagicMock()
        from unittest.mock import AsyncMock
        app.initialize = AsyncMock()
        app.start = AsyncMock()
        app.stop = AsyncMock()
        app.shutdown = AsyncMock()
        listener = TelegramPublicChannelListener(
            redis_client,
            bot_token="test-token",
            channels=["alphachan"],
            application=app,
        )
        await listener.start()
        app.initialize.assert_awaited()
        app.start.assert_awaited()
        await listener.stop()
        app.stop.assert_awaited()
