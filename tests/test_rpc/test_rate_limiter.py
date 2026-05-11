"""Tests for src.rpc.rate_limiter.AdaptiveTokenBucket.

Covers:
  * Burst capacity (fresh bucket allows ``capacity`` acquisitions)
  * Refill rate after sleep
  * 429 penalty halves refill for the penalty window
  * Penalty restoration after window elapses
  * ``unlimited=True`` short-circuit
  * Non-blocking try_acquire / stats snapshot
"""

import asyncio
import time

import pytest

from src.rpc.rate_limiter import AdaptiveTokenBucket


def test_burst_capacity_allows_n_acquisitions():
    """A fresh bucket starts with ``capacity`` tokens — all available."""
    bucket = AdaptiveTokenBucket("alchemy", capacity=5, refill_per_sec=1.0)
    for _ in range(5):
        assert bucket.try_acquire() is True
    # The 6th must fail — bucket is empty and not enough time elapsed.
    assert bucket.try_acquire() is False


def test_refill_replenishes_tokens_over_time():
    """After 1/refill seconds, one token is available again."""
    bucket = AdaptiveTokenBucket("alchemy", capacity=2, refill_per_sec=100.0)
    # Drain it.
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    # 1 / 100.0 = 0.01s; sleep a bit longer to be safe.
    time.sleep(0.05)
    assert bucket.try_acquire() is True


def test_penalise_halves_refill_rate():
    """penalise() halves refill_per_sec for backoff_s seconds."""
    bucket = AdaptiveTokenBucket(
        "alchemy", capacity=1, refill_per_sec=10.0, backoff_s=5.0
    )
    assert bucket.refill == pytest.approx(10.0)
    bucket.penalise()
    assert bucket.refill == pytest.approx(5.0)
    assert bucket.penalty_active is True


def test_penalty_restoration_after_cooldown():
    """After the backoff_s window, refill returns to base rate."""
    bucket = AdaptiveTokenBucket(
        "alchemy", capacity=1, refill_per_sec=10.0, backoff_s=0.05
    )
    bucket.penalise()
    assert bucket.refill == pytest.approx(5.0)
    # Wait out the penalty window.
    time.sleep(0.1)
    # Any refill-triggering read restores the rate.
    _ = bucket.tokens_available
    assert bucket.refill == pytest.approx(10.0)
    assert bucket.penalty_active is False


def test_unlimited_fast_path():
    """unlimited=True skips the lock entirely and never blocks."""
    bucket = AdaptiveTokenBucket(
        "local_erigon", capacity=1, refill_per_sec=1.0, unlimited=True
    )
    # Even though capacity is 1, an "unlimited" bucket never says no.
    for _ in range(100):
        assert bucket.try_acquire() is True


@pytest.mark.asyncio
async def test_acquire_unlimited_returns_immediately():
    """unlimited acquire() returns instantly with no sleep."""
    bucket = AdaptiveTokenBucket(
        "local_erigon", capacity=1, refill_per_sec=1.0, unlimited=True
    )
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_acquire_blocks_when_drained():
    """acquire() waits up to ~ 1/refill seconds when the bucket is dry."""
    bucket = AdaptiveTokenBucket("alchemy", capacity=1, refill_per_sec=20.0)
    # Drain.
    assert bucket.try_acquire() is True
    start = time.monotonic()
    await asyncio.wait_for(bucket.acquire(), timeout=1.0)
    elapsed = time.monotonic() - start
    # Refill at 20/s -> ~50ms for the next token. Allow some slack.
    assert 0.02 < elapsed < 0.4


def test_stats_snapshot_shape():
    """stats() returns the documented shape."""
    bucket = AdaptiveTokenBucket("alchemy", capacity=3, refill_per_sec=2.0)
    s = bucket.stats()
    assert s["provider"] == "alchemy"
    assert s["capacity"] == pytest.approx(3.0)
    assert s["refill_per_sec"] == pytest.approx(2.0)
    assert s["penalty_active"] is False
    assert "tokens" in s
    assert "unlimited" in s
