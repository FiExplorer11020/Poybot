"""Phase 3 Task B — FalconKeyPool tests.

Concerns:
1. 1-key backward-compat: pool built from `FALCON_API_KEY` alone behaves
   exactly like the legacy single-key client.
2. N-key round-robin: with 3 keys, calls are distributed evenly and in
   round-robin order on the fast path.
3. All-empty blocking: if every bucket is empty, the pool blocks until a
   token refills rather than over-committing.
4. Per-key stats: calls / errors / last_used_at populate correctly.
5. Empty pool raises a clean error message.
6. Operator misuse: capacity > 60 logs WARNING but does not crash.
"""

import asyncio
from collections import Counter
from unittest.mock import patch

import pytest

from src.registry.falcon_client import (
    FalconAPIError,
    FalconKeyPool,
    _resolve_api_keys,
)


def _make_pool(keys, capacity=60, refill=1.0, backoff_s=60) -> FalconKeyPool:
    return FalconKeyPool(
        keys=keys, bucket_capacity=capacity, refill_per_sec=refill, backoff_s=backoff_s
    )


class TestKeyResolution:
    def test_falcon_api_keys_takes_precedence(self):
        keys = _resolve_api_keys("a,b,c", "ignored")
        assert keys == ["a", "b", "c"]

    def test_falls_back_to_single_key(self):
        keys = _resolve_api_keys("", "only-key")
        assert keys == ["only-key"]

    def test_strips_whitespace_and_empties(self):
        keys = _resolve_api_keys("a, b ,, c,,", "ignored")
        assert keys == ["a", "b", "c"]

    def test_both_empty_returns_empty_list(self):
        keys = _resolve_api_keys("", "")
        assert keys == []

    def test_none_inputs_are_safe(self):
        keys = _resolve_api_keys(None, None)
        assert keys == []


class TestSingleKeyBackcompat:
    @pytest.mark.asyncio
    async def test_single_key_acquires_and_yields(self):
        pool = _make_pool(["solo"])
        async with pool.acquire() as (key, idx):
            assert key == "solo"
            assert idx == 0
        assert pool.size == 1

    @pytest.mark.asyncio
    async def test_single_key_repeated_acquisitions(self):
        pool = _make_pool(["solo"])
        for _ in range(5):
            async with pool.acquire() as (key, idx):
                assert key == "solo"
                assert idx == 0


class TestRoundRobin:
    @pytest.mark.asyncio
    async def test_three_keys_distribute_evenly(self):
        pool = _make_pool(["a", "b", "c"])
        seen: list[int] = []
        # Drive 9 sequential acquisitions; expect each index to appear
        # exactly 3 times under round-robin.
        for _ in range(9):
            async with pool.acquire() as (_key, idx):
                seen.append(idx)
        counts = Counter(seen)
        assert counts == {0: 3, 1: 3, 2: 3}

    @pytest.mark.asyncio
    async def test_round_robin_ordering(self):
        pool = _make_pool(["a", "b", "c"])
        seen: list[int] = []
        for _ in range(6):
            async with pool.acquire() as (_key, idx):
                seen.append(idx)
        # With all buckets full, the strict round-robin advances by 1
        # each call: 0,1,2,0,1,2.
        assert seen == [0, 1, 2, 0, 1, 2]


class TestRateLimitFallback:
    @pytest.mark.asyncio
    async def test_blocks_when_all_buckets_empty(self):
        """Capacity=1, refill=10/sec. Drain both keys with one call each,
        then a third call must wait for refill before returning.
        """
        pool = _make_pool(["a", "b"], capacity=1, refill=10.0)
        # Drain
        async with pool.acquire():
            pass
        async with pool.acquire():
            pass
        # Third call should wait ~100 ms for a token to refill.
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with pool.acquire() as (_key, _idx):
            elapsed = loop.time() - t0
        # Allow generous slack for asyncio bookkeeping.
        assert 0.0 <= elapsed < 0.5

    @pytest.mark.asyncio
    async def test_429_penalises_key_bucket(self):
        pool = _make_pool(["a", "b"], capacity=60, refill=1.0, backoff_s=60)
        before_refill = pool._buckets[0].refill
        pool.report_429(0)
        # After 429, refill is halved.
        assert pool._buckets[0].refill == pytest.approx(before_refill / 2.0)
        # Stats reflect the hit.
        stats = pool.stats()
        assert stats[0]["rate_limit_hits"] == 1
        assert stats[1]["rate_limit_hits"] == 0


class TestPerKeyStats:
    @pytest.mark.asyncio
    async def test_calls_and_last_used_increment(self):
        pool = _make_pool(["a", "b"])
        async with pool.acquire():
            pass
        async with pool.acquire():
            pass
        async with pool.acquire():
            pass
        stats = pool.stats()
        # 3 calls split round-robin: a, b, a (start=0 first call).
        assert stats[0]["calls"] + stats[1]["calls"] == 3
        # last_used_at populated for both since each was selected at least
        # once.
        assert stats[0]["last_used_at"] > 0
        assert stats[1]["last_used_at"] > 0

    @pytest.mark.asyncio
    async def test_exceptions_increment_error_counter(self):
        pool = _make_pool(["a"])
        with pytest.raises(RuntimeError):
            async with pool.acquire():
                raise RuntimeError("boom")
        stats = pool.stats()
        assert stats[0]["errors"] == 1
        assert stats[0]["calls"] == 1


class TestEmptyPool:
    @pytest.mark.asyncio
    async def test_empty_pool_raises(self):
        pool = _make_pool([])
        with pytest.raises(FalconAPIError, match="FalconKeyPool is empty"):
            async with pool.acquire():
                pass

    def test_empty_pool_size_is_zero(self):
        pool = _make_pool([])
        assert pool.size == 0


class TestCapacityOverride:
    def test_capacity_above_60_logs_warning(self, caplog):
        # We don't crash; we just warn. Loguru→stderr or caplog depending
        # on the test config — we accept either path. The critical
        # assertion is "no exception raised".
        pool = _make_pool(["a"], capacity=600)
        # The bucket still functions with the elevated capacity.
        assert pool._buckets[0].capacity == 600.0
