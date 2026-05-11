"""Phase 3 Task B — conditional-GET revalidation tests.

Concerns:
1. ETag header on response is stored in the Redis cache alongside the
   payload.
2. After the soft expiry window, the next call sends `If-None-Match`
   with the stored ETag.
3. A 304 response keeps the cached payload and counts as a savings hit.
4. Backward compat: legacy cache entries (bare-list JSON) still load
   and don't crash the new conditional-GET path.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.registry.falcon_client import FalconClient, _CacheEntry


def _make_client(redis=None) -> FalconClient:
    client = FalconClient(
        api_key="test-key",
        api_url="https://falcon.example.com",
        redis_client=redis,
        cache_ttl_s=300,
        max_rpm=0,
    )
    client._max_rpm = 0
    return client


def _mock_response(status: int, data, headers: dict | None = None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.raise_for_status = MagicMock()
    # Use a real dict for headers so `_coerce_header` doesn't see a Mock.
    resp.headers = headers or {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestCacheEntry:
    def test_to_json_and_from_json_roundtrip(self):
        entry = _CacheEntry(
            payload=[{"a": 1}],
            etag='"abc"',
            last_modified="Wed, 11 May 2026 00:00:00 GMT",
            cached_at=1234.5,
        )
        raw = entry.to_json()
        parsed = _CacheEntry.from_json(raw)
        assert parsed is not None
        assert parsed.payload == [{"a": 1}]
        assert parsed.etag == '"abc"'
        assert parsed.last_modified == "Wed, 11 May 2026 00:00:00 GMT"
        assert parsed.cached_at == 1234.5

    def test_from_json_accepts_legacy_bare_list(self):
        legacy = json.dumps([{"w": "0xabc"}])
        parsed = _CacheEntry.from_json(legacy)
        assert parsed is not None
        assert parsed.payload == [{"w": "0xabc"}]
        assert parsed.etag is None
        assert parsed.cached_at == 0.0

    def test_from_json_returns_none_on_garbage(self):
        assert _CacheEntry.from_json("not-json") is None
        assert _CacheEntry.from_json('{"no_payload_key": true}') is None


class TestEtagCapture:
    @pytest.mark.asyncio
    async def test_etag_stored_in_cache(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        client = _make_client(redis=redis)

        data = {"results": [{"w": "0xnew"}]}
        resp = _mock_response(200, data, headers={"ETag": '"v1"'})

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            await client.query(584, {}, limit=1)

        # The stored value should be a JSON dict (new format) with the ETag.
        redis.set.assert_awaited_once()
        stored_raw = redis.set.call_args[0][1]
        stored = json.loads(stored_raw)
        assert stored["etag"] == '"v1"'
        assert stored["payload"] == [{"w": "0xnew"}]
        await client.close()

    @pytest.mark.asyncio
    async def test_no_etag_header_still_caches_normally(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        client = _make_client(redis=redis)

        data = {"results": [{"w": "0xnew"}]}
        resp = _mock_response(200, data, headers={})

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            await client.query(584, {}, limit=1)

        redis.set.assert_awaited_once()
        stored = json.loads(redis.set.call_args[0][1])
        assert stored["etag"] is None
        assert stored["payload"] == [{"w": "0xnew"}]
        await client.close()


class TestConditionalRevalidation:
    @pytest.mark.asyncio
    async def test_soft_expired_call_sends_if_none_match(self):
        """Cache entry older than `_revalidate_after_s` with an ETag
        triggers a revalidating request."""
        redis = AsyncMock()
        cached = _CacheEntry(
            payload=[{"w": "0xcached"}],
            etag='"v1"',
            last_modified=None,
            cached_at=time.time() - 7200,  # 2h old > 1h default
        )
        redis.get = AsyncMock(return_value=cached.to_json())
        redis.set = AsyncMock()
        client = _make_client(redis=redis)
        client._revalidate_after_s = 3600  # 1h soft expiry

        # Simulate a 304 from the server.
        resp_304 = _mock_response(304, {}, headers={})

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp_304)
            mock_sess_fn.return_value = session
            result = await client.query(584, {}, limit=1)

        # Verify the If-None-Match header was sent.
        sent_headers = session.post.call_args.kwargs["headers"]
        assert sent_headers.get("If-None-Match") == '"v1"'
        # Cached payload is returned.
        assert result == [{"w": "0xcached"}]
        await client.close()

    @pytest.mark.asyncio
    async def test_304_response_reuses_cached_payload(self):
        redis = AsyncMock()
        cached = _CacheEntry(
            payload=[{"w": "0xcached"}],
            etag='"v1"',
            cached_at=time.time() - 7200,
        )
        redis.get = AsyncMock(return_value=cached.to_json())
        redis.set = AsyncMock()
        client = _make_client(redis=redis)
        client._revalidate_after_s = 3600

        resp_304 = _mock_response(304, {}, headers={})

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp_304)
            mock_sess_fn.return_value = session
            from src.monitoring.metrics import falcon_conditional_get_savings_total

            before = falcon_conditional_get_savings_total.labels(
                agent="584"
            )._value.get()
            result = await client.query(584, {}, limit=1)
            after = falcon_conditional_get_savings_total.labels(
                agent="584"
            )._value.get()

        assert result == [{"w": "0xcached"}]
        assert after == before + 1
        await client.close()

    @pytest.mark.asyncio
    async def test_fresh_cache_skips_revalidation(self):
        """Cache entries inside the soft-expiry window short-circuit
        without any HTTP traffic — same as the legacy hit path."""
        redis = AsyncMock()
        cached = _CacheEntry(
            payload=[{"w": "0xfresh"}],
            etag='"v1"',
            cached_at=time.time(),  # brand new
        )
        redis.get = AsyncMock(return_value=cached.to_json())
        redis.set = AsyncMock()
        client = _make_client(redis=redis)
        client._revalidate_after_s = 3600

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(
                side_effect=AssertionError("should not hit HTTP on fresh cache")
            )
            mock_sess_fn.return_value = session
            result = await client.query(584, {}, limit=1)

        assert result == [{"w": "0xfresh"}]
        await client.close()


class TestLegacyCacheCompat:
    @pytest.mark.asyncio
    async def test_legacy_bare_list_cache_is_returned(self):
        """A pre-Phase-3 cache entry (bare-list JSON) loads without
        revalidating: no validators, so we just return it directly."""
        redis = AsyncMock()
        legacy_raw = json.dumps([{"w": "0xlegacy"}])
        redis.get = AsyncMock(return_value=legacy_raw)
        redis.set = AsyncMock()
        client = _make_client(redis=redis)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(
                side_effect=AssertionError("legacy cache must not trigger HTTP")
            )
            mock_sess_fn.return_value = session
            result = await client.query(584, {}, limit=1)

        assert result == [{"w": "0xlegacy"}]
        await client.close()
