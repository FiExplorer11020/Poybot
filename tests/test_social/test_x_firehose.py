"""Tests for FixtureXSubscriber + XFirehoseSubscriber.

Coverage:
  * FixtureXSubscriber replays a JSON fixture and publishes to a
    Redis Stream (fakeredis-backed).
  * SocialPost.to_stream_fields() round-trips via decode_stream_fields.
  * XFirehoseSubscriber rule payload chunks handles correctly.
  * XFirehoseSubscriber gracefully handles a 429 by pausing.
  * sync_rules without API key returns False.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fakeredis_async
import pytest

from src.social.x_firehose import (
    FixtureXSubscriber,
    SocialPost,
    XFirehoseSubscriber,
    decode_stream_fields,
)


@pytest.fixture
async def redis_client():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _fake_response(status: int, body: str = "", headers: dict | None = None):
    """Helper that builds an async-context-manager mimicking an
    aiohttp response — supports .json(), .text(), .headers, .status,
    plus async iteration over .content for line-delimited streams."""

    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = headers or {}
            self._body = body

        async def text(self):
            return self._body

        async def json(self):
            return json.loads(self._body) if self._body else None

        @property
        def content(self):
            # Yield each non-empty line as bytes.
            async def _iter():
                for line in (self._body or "").split("\n"):
                    if line.strip():
                        yield (line + "\n").encode("utf-8")
            return _iter()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Resp


class TestSocialPostSerialization:
    def test_stream_fields_round_trip(self):
        post = SocialPost(
            source="x",
            author_handle="alice",
            text="just entered YES at 0.42",
            posted_at=datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
            market_urls=["https://polymarket.com/event/foo"],
            raw_payload={"id": "12345"},
        )
        fields = post.to_stream_fields()
        assert "data" in fields
        decoded = decode_stream_fields(fields)
        assert decoded is not None
        assert decoded.author_handle == "alice"
        assert decoded.text == "just entered YES at 0.42"
        assert decoded.market_urls == ["https://polymarket.com/event/foo"]

    def test_decode_handles_malformed_payload(self):
        assert decode_stream_fields({"data": "not-json"}) is None
        assert decode_stream_fields({}) is None


class TestFixtureXSubscriber:
    @pytest.mark.asyncio
    async def test_replays_fixture(self, redis_client, tmp_path):
        fixture = [
            {
                "author_handle": "alice",
                "text": "just entered YES",
                "posted_at": "2026-05-12T10:00:00+00:00",
                "market_urls": [],
            },
            {
                "author_handle": "bob",
                "text": "took profit",
                "posted_at": "2026-05-12T10:05:00+00:00",
                "market_urls": [],
            },
        ]
        path = tmp_path / "tweets.json"
        path.write_text(json.dumps(fixture))
        sub = FixtureXSubscriber(redis_client, path)
        n = await sub.run_once()
        assert n == 2
        # Stream should hold both entries.
        entries = await redis_client.xrange(sub._stream_name)
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_exhausts_after_one_pass(self, redis_client, tmp_path):
        path = tmp_path / "tweets.json"
        path.write_text(json.dumps([{
            "author_handle": "alice", "text": "hi",
            "posted_at": "2026-05-12T10:00:00+00:00",
        }]))
        sub = FixtureXSubscriber(redis_client, path)
        assert await sub.run_once() == 1
        assert await sub.run_once() == 0

    @pytest.mark.asyncio
    async def test_missing_file_yields_zero(self, redis_client, tmp_path):
        sub = FixtureXSubscriber(redis_client, tmp_path / "missing.json")
        assert await sub.run_once() == 0


class TestXFirehoseRuleManagement:
    @pytest.mark.asyncio
    async def test_rule_payload_chunks_handles(self, redis_client):
        # Build a session that captures the POSTed payload.
        captured = {}
        post_resp = _fake_response(200, "")

        class _Sess:
            def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                return post_resp()

            def get(self, *a, **kw):  # pragma: no cover
                raise NotImplementedError

        handles = [f"leader_{i}" for i in range(200)]
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=_Sess(),
            api_key="test-key",
            tracked_handles=handles,
        )
        ok = await sub.sync_rules()
        assert ok is True
        rules = captured["json"]["add"]
        # At least one rule per chunked-handles batch + one URL rule.
        assert len(rules) >= 2
        # No single rule string > 500 chars.
        for r in rules:
            assert len(r["value"]) < 500

    @pytest.mark.asyncio
    async def test_sync_rules_without_api_key_returns_false(self, redis_client):
        sub = XFirehoseSubscriber(
            redis_client, http_session=MagicMock(), api_key="",
            tracked_handles=["alice"],
        )
        assert await sub.sync_rules() is False


class TestXFirehoseRateLimit:
    @pytest.mark.asyncio
    async def test_429_pauses_gracefully(self, redis_client, monkeypatch):
        # Build a session whose .get returns 429.
        get_resp = _fake_response(429, "", headers={"x-rate-limit-remaining": "0"})

        class _Sess:
            def post(self, *a, **kw):
                return _fake_response(200, "")()

            def get(self, *a, **kw):
                return get_resp()

        sub = XFirehoseSubscriber(
            redis_client,
            http_session=_Sess(),
            api_key="test-key",
            tracked_handles=["alice"],
            rate_limit_pause_s=0.01,  # fast for tests
        )
        # Patch sleep to a no-op so 429 doesn't actually delay.
        import asyncio
        slept = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        n = await sub.run_once()
        # 429 path → no posts published, no crash.
        assert n == 0
        # The 429 handler triggered a sleep.
        assert any(s >= 0.01 for s in slept)


class TestXFirehoseDecode:
    @pytest.mark.asyncio
    async def test_decodes_streaming_tweets(self, redis_client):
        # One JSON object per line.
        body_lines = [
            json.dumps({
                "data": {
                    "id": "1",
                    "author_id": "100",
                    "text": "just entered YES",
                    "created_at": "2026-05-12T10:00:00.000Z",
                },
                "includes": {
                    "users": [{"id": "100", "username": "alice"}],
                },
            }),
            json.dumps({
                "data": {
                    "id": "2",
                    "author_id": "101",
                    "text": "took profit",
                    "created_at": "2026-05-12T10:05:00.000Z",
                },
                "includes": {
                    "users": [{"id": "101", "username": "bob"}],
                },
            }),
        ]
        body = "\n".join(body_lines)
        get_resp = _fake_response(200, body, headers={"x-rate-limit-remaining": "1000"})

        class _Sess:
            def post(self, *a, **kw):
                return _fake_response(200, "")()

            def get(self, *a, **kw):
                return get_resp()

        sub = XFirehoseSubscriber(
            redis_client,
            http_session=_Sess(),
            api_key="test-key",
            tracked_handles=["alice", "bob"],
        )
        n = await sub.run_once()
        assert n == 2
        entries = await redis_client.xrange(sub._stream_name)
        assert len(entries) == 2
