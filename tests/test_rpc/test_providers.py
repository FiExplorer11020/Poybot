"""Tests for src.rpc.providers.ProviderPool.

Covers:
  * 2-provider pool respects priority order
  * Priority-0 unavailable -> priority-1 selected
  * All unavailable -> raises NoRPCProviderAvailable after timeout
  * Unset provider URL -> stays UNHEALTHY without crashing
  * stats() returns per-provider counters
  * Breaker auto-record on success / failure exit paths
"""

import asyncio

import pytest

from src.rpc.circuit_breaker import CircuitBreaker, CircuitState
from src.rpc.providers import (
    NoRPCProviderAvailable,
    ProviderPool,
    ProviderState,
    RPCProvider,
)
from src.rpc.rate_limiter import AdaptiveTokenBucket


def _make_provider(name: str, priority: int, url: str = "http://x") -> RPCProvider:
    return RPCProvider(
        name=name,
        url=url,
        priority=priority,
        bucket=AdaptiveTokenBucket(name, capacity=5, refill_per_sec=5.0),
        breaker=CircuitBreaker(name, failure_threshold=3, cooldown_s=0.05),
    )


@pytest.mark.asyncio
async def test_priority_zero_chosen_first():
    """With both providers healthy, the lowest-priority number wins."""
    p0 = _make_provider("local", 0)
    p1 = _make_provider("alchemy", 1)
    pool = ProviderPool([p0, p1])
    async with pool.acquire() as picked:
        assert picked.name == "local"


@pytest.mark.asyncio
async def test_priority_one_used_when_zero_is_open():
    """If priority-0's breaker is OPEN, the pool falls through to 1."""
    p0 = _make_provider("local", 0)
    p1 = _make_provider("alchemy", 1)
    pool = ProviderPool([p0, p1])
    p0.breaker.open()
    async with pool.acquire() as picked:
        assert picked.name == "alchemy"


@pytest.mark.asyncio
async def test_priority_one_used_when_zero_bucket_drained():
    """If priority-0's bucket is empty (and slow to refill), the pool
    falls through to priority-1 rather than blocking."""
    p0 = RPCProvider(
        name="local",
        url="http://local",
        priority=0,
        bucket=AdaptiveTokenBucket("local", capacity=1, refill_per_sec=0.001),
        breaker=CircuitBreaker("local"),
    )
    p1 = _make_provider("alchemy", 1)
    pool = ProviderPool([p0, p1])
    # Drain priority-0.
    assert p0.bucket.try_acquire() is True
    async with pool.acquire() as picked:
        assert picked.name == "alchemy"


@pytest.mark.asyncio
async def test_all_unavailable_raises_after_timeout():
    """All providers UNHEALTHY -> NoRPCProviderAvailable after timeout."""
    p0 = _make_provider("local", 0)
    p1 = _make_provider("alchemy", 1)
    p0.state = ProviderState.UNHEALTHY
    p1.state = ProviderState.UNHEALTHY
    pool = ProviderPool([p0, p1], acquire_timeout_s=0.1)
    with pytest.raises(NoRPCProviderAvailable):
        async with pool.acquire():
            pass


@pytest.mark.asyncio
async def test_unset_url_keeps_provider_open_circuit():
    """An empty url string marks the provider UNHEALTHY but does NOT
    crash construction or acquire()."""
    p0 = RPCProvider(
        name="local_erigon",
        url="",  # unset -> stays out of rotation
        priority=0,
        bucket=AdaptiveTokenBucket("local_erigon", capacity=1, refill_per_sec=1.0),
        breaker=CircuitBreaker("local_erigon"),
    )
    p1 = _make_provider("alchemy", 1)
    pool = ProviderPool([p0, p1])
    assert p0.state == ProviderState.UNHEALTHY
    async with pool.acquire() as picked:
        assert picked.name == "alchemy"


def test_stats_returns_per_provider_counters():
    """stats() returns one dict per provider with the expected keys."""
    p0 = _make_provider("local", 0)
    p1 = _make_provider("alchemy", 1)
    pool = ProviderPool([p0, p1])
    snapshot = pool.stats()
    assert len(snapshot) == 2
    names = {entry["name"] for entry in snapshot}
    assert names == {"local", "alchemy"}
    for entry in snapshot:
        assert "counters" in entry
        assert set(entry["counters"].keys()) >= {
            "acquisitions",
            "successes",
            "failures",
            "fallbacks",
        }
        assert "bucket" in entry
        assert "breaker" in entry


@pytest.mark.asyncio
async def test_failure_records_on_breaker():
    """An exception inside the acquire() context records a failure on
    the picked provider's breaker."""
    p0 = _make_provider("local", 0)
    pool = ProviderPool([p0])
    assert p0.breaker.state == CircuitState.CLOSED
    with pytest.raises(RuntimeError):
        async with pool.acquire() as _p:
            raise RuntimeError("simulated transport failure")
    # 1 failure, threshold=3 -> still CLOSED.
    assert p0.breaker.state == CircuitState.CLOSED
    # Trip it.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            async with pool.acquire() as _p:
                raise RuntimeError("again")
    assert p0.breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_success_records_on_breaker():
    """A clean exit records success; breaker stays CLOSED."""
    p0 = _make_provider("local", 0)
    pool = ProviderPool([p0])
    async with pool.acquire():
        pass
    assert p0.breaker.state == CircuitState.CLOSED
    s = pool.stats()[0]
    assert s["counters"]["successes"] == 1


@pytest.mark.asyncio
async def test_fallback_metric_increments_when_skipping_priority_zero():
    """When we skip priority-0 due to an open breaker, the fallback
    counter increments."""
    from src.monitoring.metrics import rpc_fallback_total

    p0 = _make_provider("local-fallback", 0)
    p1 = _make_provider("alchemy-fallback", 1)
    pool = ProviderPool([p0, p1])
    p0.breaker.open()
    # Read counter pre/post; metric defines explicit labels so the
    # counter is created lazily on first .inc().
    counter = rpc_fallback_total.labels(
        from_provider="local-fallback", to_provider="alchemy-fallback"
    )
    before = counter._value.get()
    async with pool.acquire() as picked:
        assert picked.name == "alchemy-fallback"
    after = counter._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_pool_handles_empty_provider_list_gracefully():
    """An empty pool raises NoRPCProviderAvailable rather than hanging."""
    pool = ProviderPool([], acquire_timeout_s=0.05)
    with pytest.raises(NoRPCProviderAvailable):
        async with pool.acquire():
            pass


@pytest.mark.asyncio
async def test_report_429_penalises_named_provider_bucket():
    """report_429() forwards to the named bucket's penalise()."""
    p0 = _make_provider("local", 0)
    pool = ProviderPool([p0])
    base = p0.bucket.refill
    pool.report_429("local")
    assert p0.bucket.refill < base
    assert p0.bucket.penalty_active is True
    # Unknown name is a silent no-op (defensive).
    pool.report_429("nonexistent")
    await asyncio.sleep(0)  # yield to keep the test loop fair
