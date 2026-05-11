"""Phase 3 Task B — request coalescing tests.

Concerns:
1. Two concurrent identical calls share one HTTP request: the second
   `await`s the first's future rather than issuing a new POST.
2. Different params do NOT share — each gets its own HTTP request.
3. Cache-window TTL: a second call within FALCON_COALESCE_TTL_S of the
   first's completion returns the cached future's result; after TTL,
   a new request is issued.
4. Exceptions propagate to all waiters: if the owner's call raises, every
   `await fut` sees the same exception.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.registry.falcon_client import FalconClient


def _make_client() -> FalconClient:
    client = FalconClient(
        api_key="test-key",
        api_url="https://falcon.example.com",
        redis_client=None,
        cache_ttl_s=300,
        max_rpm=0,
    )
    client._max_rpm = 0  # neutralise the legacy throttle
    return client


def _mock_response(status: int, data):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.raise_for_status = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestInflightDedup:
    @pytest.mark.asyncio
    async def test_two_concurrent_identical_calls_share_one_http_request(self):
        client = _make_client()
        call_count = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        @asynccontextmanager
        async def fake_post(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            first_started.set()
            await release.wait()
            yield _mock_response(200, {"results": [{"w": "0xabc"}]})

        session = MagicMock()
        session.post = fake_post

        with patch.object(client, "_session_or_new", return_value=session):
            t1 = asyncio.create_task(client.query(584, {"q": 1}, limit=1))
            # Wait for the first call to enter the HTTP layer.
            await asyncio.wait_for(first_started.wait(), timeout=2.0)
            # Now issue an identical call — it must NOT enter `fake_post`.
            t2 = asyncio.create_task(client.query(584, {"q": 1}, limit=1))
            # Give the second task a chance to enter coalescing.
            await asyncio.sleep(0.05)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)

        assert call_count == 1, f"expected 1 HTTP call, got {call_count}"
        assert r1 == r2 == [{"w": "0xabc"}]
        await client.close()

    @pytest.mark.asyncio
    async def test_different_params_do_not_share(self):
        client = _make_client()
        call_count = 0

        @asynccontextmanager
        async def fake_post(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            yield _mock_response(200, {"results": []})

        session = MagicMock()
        session.post = fake_post

        with patch.object(client, "_session_or_new", return_value=session):
            await asyncio.gather(
                client.query(584, {"q": 1}, limit=1),
                client.query(584, {"q": 2}, limit=1),
                client.query(581, {"q": 1}, limit=1),
            )

        assert call_count == 3
        await client.close()


class TestCoalesceTTL:
    @pytest.mark.asyncio
    async def test_call_within_ttl_returns_cached_future_result(self):
        client = _make_client()
        client._coalesce_ttl_s = 5.0  # generous TTL for the test
        call_count = 0

        @asynccontextmanager
        async def fake_post(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            yield _mock_response(200, {"results": [{"w": "0xabc"}]})

        session = MagicMock()
        session.post = fake_post

        with patch.object(client, "_session_or_new", return_value=session):
            r1 = await client.query(584, {"q": 1}, limit=1)
            # Immediate re-issue within TTL — should hit the cached future.
            r2 = await client.query(584, {"q": 1}, limit=1)

        # The second call MUST not have hit `fake_post` again.
        assert call_count == 1
        assert r1 == r2 == [{"w": "0xabc"}]
        await client.close()

    @pytest.mark.asyncio
    async def test_call_after_ttl_issues_new_request(self):
        client = _make_client()
        client._coalesce_ttl_s = 0.05  # very short TTL
        call_count = 0

        @asynccontextmanager
        async def fake_post(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            yield _mock_response(200, {"results": [{"w": "0xabc"}]})

        session = MagicMock()
        session.post = fake_post

        with patch.object(client, "_session_or_new", return_value=session):
            await client.query(584, {"q": 1}, limit=1)
            await asyncio.sleep(0.15)  # exceed TTL
            await client.query(584, {"q": 1}, limit=1)

        assert call_count == 2
        await client.close()


class TestExceptionPropagation:
    @pytest.mark.asyncio
    async def test_owner_exception_propagates_to_all_waiters(self):
        client = _make_client()
        first_started = asyncio.Event()
        release = asyncio.Event()

        @asynccontextmanager
        async def fake_post(*_args, **_kwargs):
            first_started.set()
            await release.wait()
            # Mimic Falcon 400: client raises FalconAPIError.
            resp = AsyncMock()
            resp.status = 400
            resp.text = AsyncMock(return_value="bad request")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            yield resp

        session = MagicMock()
        session.post = fake_post

        with patch.object(client, "_session_or_new", return_value=session):
            t1 = asyncio.create_task(client.query(584, {"q": "fail"}, limit=1))
            await asyncio.wait_for(first_started.wait(), timeout=2.0)
            t2 = asyncio.create_task(client.query(584, {"q": "fail"}, limit=1))
            await asyncio.sleep(0.05)
            release.set()

            with pytest.raises(Exception):
                await t1
            with pytest.raises(Exception):
                await t2

        await client.close()
