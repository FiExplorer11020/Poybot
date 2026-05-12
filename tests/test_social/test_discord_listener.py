"""DiscordPublicChannelListener tests.

Coverage:
  * run_once polls the configured channel, publishes new messages,
    advances the per-channel cursor.
  * 429 response triggers a graceful pause (no crash).
  * 4xx error is logged but doesn't crash the loop.
  * Empty text messages are skipped.
  * Subsequent polls use 'after' cursor.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import fakeredis.aioredis as fakeredis_async
import pytest

from src.social.discord_listener import DiscordPublicChannelListener


@pytest.fixture
async def redis_client():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _fake_response(status: int, body: Any = None, headers: dict | None = None):
    """aiohttp-shaped async context manager around a JSON body."""

    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = headers or {}
            self._body = body

        async def text(self):
            return json.dumps(self._body) if self._body else ""

        async def json(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Resp


def _build_session(messages_by_call: list[list[dict]] | list[Any]):
    """Build a fake aiohttp session whose .get returns the listed
    responses in order. Each entry is either a list (interpreted as a
    200 JSON body) or an (status, body) tuple."""
    calls_left = list(messages_by_call)
    captured_calls: list[dict[str, Any]] = []

    class _Sess:
        def get(self, url, headers=None, params=None):
            captured_calls.append({"url": url, "params": dict(params or {})})
            if not calls_left:
                return _fake_response(200, [])()
            payload = calls_left.pop(0)
            if isinstance(payload, tuple):
                status, body = payload
                return _fake_response(status, body)()
            return _fake_response(200, payload)()

    sess = _Sess()
    return sess, captured_calls


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_publishes_messages(self, redis_client):
        msgs = [
            {
                "id": "1001",
                "content": "just entered YES",
                "author": {"username": "alice"},
                "timestamp": "2026-05-12T10:00:00.000000+00:00",
            },
            {
                "id": "1002",
                "content": "took profit",
                "author": {"username": "bob"},
                "timestamp": "2026-05-12T10:05:00.000000+00:00",
            },
        ]
        sess, _ = _build_session([msgs])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=["chan-1"],
        )
        n = await listener.run_once()
        assert n == 2
        entries = await redis_client.xrange(listener._stream_name)
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_cursor_advances_across_polls(self, redis_client):
        msgs = [
            {
                "id": "1001",
                "content": "hello",
                "author": {"username": "alice"},
                "timestamp": "2026-05-12T10:00:00.000000+00:00",
            },
        ]
        sess, captured = _build_session([msgs, []])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=["chan-1"],
        )
        await listener.run_once()
        # On the second call, 'after' should be set to the latest id.
        await listener.run_once()
        assert len(captured) >= 2
        assert captured[1]["params"].get("after") == "1001"

    @pytest.mark.asyncio
    async def test_empty_content_skipped(self, redis_client):
        msgs = [
            {
                "id": "1001",
                "content": "",
                "author": {"username": "alice"},
                "timestamp": "2026-05-12T10:00:00.000000+00:00",
            },
        ]
        sess, _ = _build_session([msgs])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=["chan-1"],
        )
        n = await listener.run_once()
        assert n == 0


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_429_pauses_gracefully(self, redis_client, monkeypatch):
        sess, _ = _build_session([(429, None)])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=["chan-1"],
            poll_interval_s=0.01,
        )
        import asyncio
        slept = []

        async def _sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _sleep)
        n = await listener.run_once()
        assert n == 0  # 429 → no posts published
        assert any(s >= 0.01 for s in slept)

    @pytest.mark.asyncio
    async def test_500_error_no_crash(self, redis_client):
        sess, _ = _build_session([(500, None)])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=["chan-1"],
        )
        # Should NOT raise.
        n = await listener.run_once()
        assert n == 0

    @pytest.mark.asyncio
    async def test_no_channels_no_calls(self, redis_client):
        sess, captured = _build_session([])
        listener = DiscordPublicChannelListener(
            redis_client,
            http_session=sess,
            bot_token="test-bot-token",
            channels=[],
        )
        n = await listener.run_once()
        assert n == 0
        assert captured == []
