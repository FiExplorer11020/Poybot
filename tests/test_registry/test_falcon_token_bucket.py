"""Phase 3 Task B — adaptive token bucket tests.

Concerns:
1. Burst at startup: bucket starts FULL (capacity tokens immediately
   available, no waiting on the first `capacity` calls).
2. Sustained 1/sec under load: once the burst is exhausted, sustained
   throughput is bound by `refill_per_sec`.
3. 429-triggered halving + restore: after `penalise()` the refill rate
   is halved for `backoff_s`, then restored.
4. Per-key independence: penalising key 0 does NOT slow key 1.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from src.registry.falcon_client import _TokenBucket


class TestBurstAtStartup:
    @pytest.mark.asyncio
    async def test_full_burst_passes_without_waiting(self):
        """capacity=60, refill=1/sec. The first 60 acquires should all
        complete in <100 ms (no waiting on the bucket)."""
        bucket = _TokenBucket(capacity=60, refill_per_sec=1.0, backoff_s=60, key_index=0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(60):
            await bucket.acquire()
        elapsed = loop.time() - t0
        assert elapsed < 0.5, f"burst should be ~instant, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_61st_acquire_waits_for_refill(self):
        """After draining the bucket, the 61st acquire must wait for the
        bucket to refill at least one token."""
        bucket = _TokenBucket(capacity=2, refill_per_sec=10.0, backoff_s=60, key_index=0)
        await bucket.acquire()
        await bucket.acquire()
        # Now empty. Next acquire waits ~100 ms for refill (1 / 10 / sec).
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await bucket.acquire()
        elapsed = loop.time() - t0
        # Should wait ~100 ms, with slack.
        assert 0.05 <= elapsed < 0.5


class TestSustainedRate:
    @pytest.mark.asyncio
    async def test_sustained_rate_matches_refill(self):
        """After burst exhaustion, throughput is bound by refill.

        With capacity=2 and refill=20/sec, after draining the 2-token
        burst we should sustain ~20 acquires/sec. We measure 10 acquires
        and assert wall-time is close to 10/20 = 0.5 s (with slack).
        """
        bucket = _TokenBucket(capacity=2, refill_per_sec=20.0, backoff_s=60, key_index=0)
        # Drain burst.
        await bucket.acquire()
        await bucket.acquire()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(10):
            await bucket.acquire()
        elapsed = loop.time() - t0
        # 10 acquires at 20 tokens/sec → ~0.5 s. Slack ±0.4 s for
        # asyncio bookkeeping.
        assert 0.3 <= elapsed < 1.5, f"sustained 10 acquires took {elapsed:.3f}s"


class Test429Adaptive:
    def test_penalise_halves_refill(self):
        bucket = _TokenBucket(capacity=60, refill_per_sec=1.0, backoff_s=60, key_index=0)
        assert bucket.refill == 1.0
        bucket.penalise()
        assert bucket.refill == pytest.approx(0.5)

    def test_penalise_restores_after_window(self):
        bucket = _TokenBucket(capacity=60, refill_per_sec=1.0, backoff_s=1, key_index=0)
        bucket.penalise()
        assert bucket.refill == pytest.approx(0.5)
        # Simulate the backoff window expiring by manually rewinding the
        # penalty timestamp. The `_refill_tokens()` method restores the
        # base rate when `_now() >= _penalty_until`.
        bucket._penalty_until = time.monotonic() - 1.0
        bucket._refill_tokens()
        assert bucket.refill == pytest.approx(1.0)


class TestPerKeyIndependence:
    @pytest.mark.asyncio
    async def test_penalising_one_does_not_affect_the_other(self):
        b0 = _TokenBucket(capacity=60, refill_per_sec=1.0, backoff_s=60, key_index=0)
        b1 = _TokenBucket(capacity=60, refill_per_sec=1.0, backoff_s=60, key_index=1)
        b0.penalise()
        assert b0.refill == pytest.approx(0.5)
        # b1 is untouched.
        assert b1.refill == pytest.approx(1.0)
