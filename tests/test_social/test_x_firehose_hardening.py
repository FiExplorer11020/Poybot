"""Wave-3 hardening tests for the X firehose subscriber.

Coverage beyond the architect's pre-merge suite:

  * 429 with ``Retry-After`` header → pause length honours the header
    (clamped to [rate_limit_pause_s, 900s]).
  * 429 with ``x-rate-limit-reset`` epoch header → pause length computed
    correctly + clamped.
  * 429 with a malformed Retry-After (non-numeric) falls back to the
    constructor floor — never parks the daemon forever.
  * 4xx response other than 429 (e.g. 401 invalid key) doesn't crash and
    yields zero posts on the iteration.
  * Truly malformed streaming line (non-JSON) is silently skipped — the
    iterator keeps reading subsequent valid lines.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import fakeredis.aioredis as fakeredis_async
import pytest

from src.social.x_firehose import XFirehoseSubscriber


@pytest.fixture
async def redis_client():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _fake_response(status: int, body: str = "", headers: dict | None = None):
    class _Resp:
        def __init__(self) -> None:
            self.status = status
            self.headers = headers or {}
            self._body = body

        async def text(self) -> str:
            return self._body

        async def json(self) -> Any:
            return json.loads(self._body) if self._body else None

        @property
        def content(self):
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


def _build_get_session(resp_factory):
    class _Sess:
        def post(self, *a, **kw):
            return _fake_response(200, "")()

        def get(self, *a, **kw):
            return resp_factory()

    return _Sess()


class TestRetryAfterHonoured:
    @pytest.mark.asyncio
    async def test_retry_after_seconds_header_used(
        self, redis_client, monkeypatch
    ):
        sess = _build_get_session(
            _fake_response(429, "", headers={"retry-after": "120"})
        )
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
            rate_limit_pause_s=5.0,
        )
        slept: list[float] = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        n = await sub.run_once()
        assert n == 0
        # 120 > floor (5) → 120 used (clamped to <= 900).
        assert any(abs(s - 120.0) < 1e-6 for s in slept)

    @pytest.mark.asyncio
    async def test_retry_after_clamped_to_15min_ceiling(
        self, redis_client, monkeypatch
    ):
        # A misbehaving header of 10_000 s must not park the daemon
        # forever — implementation clamps to 15 min.
        sess = _build_get_session(
            _fake_response(429, "", headers={"retry-after": "10000"})
        )
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
            rate_limit_pause_s=5.0,
        )
        slept: list[float] = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        await sub.run_once()
        # Largest sleep should be the 429 sleep, clamped to 900.
        assert max(slept) <= 900.0
        # Floor respected.
        assert all(s >= 5.0 or s == 0 for s in slept)

    @pytest.mark.asyncio
    async def test_x_rate_limit_reset_epoch_header_used(
        self, redis_client, monkeypatch
    ):
        # X sends epoch seconds for the reset time. We feed `now + 60`.
        future_epoch = int(time.time()) + 60
        sess = _build_get_session(
            _fake_response(
                429, "",
                headers={"x-rate-limit-reset": str(future_epoch)},
            )
        )
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
            rate_limit_pause_s=5.0,
        )
        slept: list[float] = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        await sub.run_once()
        # Should be ~60s, clamped to [5, 900].
        big = [s for s in slept if s >= 5.0]
        assert big, f"expected a 429-derived sleep; got {slept}"
        assert all(5.0 <= s <= 900.0 for s in big)

    @pytest.mark.asyncio
    async def test_malformed_retry_after_falls_back_to_floor(
        self, redis_client, monkeypatch
    ):
        # Non-numeric Retry-After → fall back to constructor floor.
        sess = _build_get_session(
            _fake_response(429, "", headers={"retry-after": "soon-ish"})
        )
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
            rate_limit_pause_s=7.0,
        )
        slept: list[float] = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        await sub.run_once()
        # Floor honoured — no header-derived sleep used.
        assert 7.0 in slept or any(s == 7.0 for s in slept)


class TestNon429Errors:
    @pytest.mark.asyncio
    async def test_401_does_not_crash_and_yields_zero(self, redis_client):
        sess = _build_get_session(_fake_response(401, ""))
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="bad-key",
            tracked_handles=["alice"],
        )
        n = await sub.run_once()
        assert n == 0

    @pytest.mark.asyncio
    async def test_502_does_not_crash_and_yields_zero(self, redis_client):
        sess = _build_get_session(_fake_response(502, ""))
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
        )
        n = await sub.run_once()
        assert n == 0


class TestMalformedLines:
    @pytest.mark.asyncio
    async def test_garbage_line_skipped_valid_line_kept(self, redis_client):
        # First line non-JSON, second line valid streaming payload.
        good = json.dumps(
            {
                "data": {
                    "id": "42",
                    "author_id": "100",
                    "text": "just entered YES",
                    "created_at": "2026-05-12T10:00:00.000Z",
                },
                "includes": {
                    "users": [{"id": "100", "username": "alice"}],
                },
            }
        )
        body = "this-is-not-json\n" + good + "\n"
        sess = _build_get_session(_fake_response(200, body))
        sub = XFirehoseSubscriber(
            redis_client,
            http_session=sess,
            api_key="test-key",
            tracked_handles=["alice"],
        )
        n = await sub.run_once()
        # The garbage line skipped; the valid one published.
        assert n == 1
