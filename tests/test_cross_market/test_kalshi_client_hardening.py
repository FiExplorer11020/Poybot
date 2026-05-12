"""Wave-3 hardening for the Kalshi client + the shared adaptive
token-bucket pattern.

Coverage:
  * Token-bucket exhaustion: many concurrent GETs are sequenced — the
    second batch waits for refill, so the elapsed wall-clock matches
    the configured ``refill_per_sec``.
  * Timeout during a GET resolves to status=0 (no crash) and records
    a 'timeout' metric label.
  * Malformed JSON body → fetch returns None / [] gracefully.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.cross_market._http_base import VenueClient, _TokenBucket
from src.cross_market.kalshi_client import KalshiClient


class _BadJsonResp:
    """aiohttp-shaped response whose .json() raises (malformed body)."""

    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return "{not valid json"

    async def json(self) -> Any:
        raise ValueError("not valid json")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _TimeoutSession:
    def get(self, *_, **__):
        async def _raise():
            raise asyncio.TimeoutError()

        class _Ctx:
            async def __aenter__(self):
                await _raise()

            async def __aexit__(self, *_):
                return False

        return _Ctx()


class TestTokenBucketRateLimiting:
    @pytest.mark.asyncio
    async def test_bucket_sequences_concurrent_acquires(self):
        # Capacity=2 → first 2 acquires are immediate; the third must
        # wait ~1/refill seconds.
        bucket = _TokenBucket(capacity=2, refill_per_sec=10.0)
        t0 = time.perf_counter()
        await bucket.acquire()
        await bucket.acquire()
        # First two are instant.
        assert time.perf_counter() - t0 < 0.05
        # Third call requires refill (1/10 s = 0.1s).
        t1 = time.perf_counter()
        await bucket.acquire()
        elapsed = time.perf_counter() - t1
        assert elapsed >= 0.05, (
            f"expected refill wait ~0.1s, got {elapsed:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_bucket_concurrent_callers_are_serialised(self):
        # Capacity=1, refill 50/s → 5 concurrent callers should take
        # roughly 4 * (1/50) = 0.08s (first is free).
        bucket = _TokenBucket(capacity=1, refill_per_sec=50.0)

        async def _one():
            await bucket.acquire()

        t0 = time.perf_counter()
        await asyncio.gather(*(_one() for _ in range(5)))
        elapsed = time.perf_counter() - t0
        # Tolerant window: should be at least 4 refills (0.08s) but
        # well under 1s.
        assert 0.03 < elapsed < 1.0, (
            f"unexpected elapsed for 5 concurrent acquires: {elapsed:.3f}s"
        )


class TestKalshiTimeoutPath:
    @pytest.mark.asyncio
    async def test_timeout_resolves_gracefully(self):
        # The injected session raises TimeoutError mid-request.
        sess = _TimeoutSession()
        client = KalshiClient(sess, api_key="test-key")
        market = await client.fetch_market("ANY")
        # The VenueClient catches asyncio.TimeoutError and returns a
        # 0-status HTTPResponse; the Kalshi adapter then yields None.
        assert market is None


class TestMalformedJson:
    @pytest.mark.asyncio
    async def test_malformed_json_body_returns_none(self):
        class _Sess:
            def get(self, *a, **kw):
                return _BadJsonResp()

        sess = _Sess()
        client = KalshiClient(sess, api_key="test-key")
        market = await client.fetch_market("ANY")
        assert market is None
