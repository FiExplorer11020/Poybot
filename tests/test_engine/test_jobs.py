"""
Tests for the scheduled jobs (S3.10).

Each job has a tiny surface — we mostly assert that:
    * the factory returns a coroutine factory,
    * the factory invocation does the expected I/O (Redis writes,
      method calls on injected services),
    * exceptions inside dependencies are swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.engine.jobs import (
    make_killswitch_sync_job,
    make_redis_cleanup_job,
    make_refresh_markets_job,
)
from src.engine.jobs import refresh_markets as refresh_markets_module
from src.engine.watchdog import REDIS_HEARTBEAT_PREFIX


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


# --------------------------------------------------------------------------- #
# killswitch_sync                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class _StubKillswitchState:
    execution_enabled: bool = True
    real_execution_enabled: bool = False


class _StubKillswitch:
    def __init__(self) -> None:
        self.invalidated = 0
        self.read = 0

    async def _invalidate_cache(self) -> None:
        self.invalidated += 1

    async def get_state(self) -> _StubKillswitchState:
        self.read += 1
        return _StubKillswitchState()


async def test_killswitch_sync_invalidates_then_reads():
    ks = _StubKillswitch()
    job = make_killswitch_sync_job(ks)
    await job()
    assert ks.invalidated == 1
    assert ks.read == 1


async def test_killswitch_sync_swallows_exceptions():
    class _Bad:
        async def _invalidate_cache(self):
            raise RuntimeError("redis down")

        async def get_state(self):
            return _StubKillswitchState()

    job = make_killswitch_sync_job(_Bad())
    # Must not raise.
    await job()


# --------------------------------------------------------------------------- #
# refresh_markets                                                              #
# --------------------------------------------------------------------------- #


async def test_refresh_markets_writes_set(monkeypatch, redis_client):
    async def fake_fetch(session, limit):
        return {"tok_a", "tok_b", "tok_c"}

    monkeypatch.setattr(
        refresh_markets_module, "_fetch_active_market_tokens", fake_fetch
    )

    job = make_refresh_markets_job(redis_client, limit=50)
    await job()
    members = await redis_client.smembers(refresh_markets_module.REDIS_ACTIVE_MARKETS_KEY)
    assert members == {"tok_a", "tok_b", "tok_c"}


async def test_refresh_markets_skips_when_empty(monkeypatch, redis_client):
    """If the upstream returns nothing (network error etc.), don't blow
    away the existing set."""
    # Pre-populate
    await redis_client.sadd(refresh_markets_module.REDIS_ACTIVE_MARKETS_KEY, "old1", "old2")

    async def fake_fetch(session, limit):
        return set()

    monkeypatch.setattr(
        refresh_markets_module, "_fetch_active_market_tokens", fake_fetch
    )
    job = make_refresh_markets_job(redis_client, limit=50)
    await job()
    members = await redis_client.smembers(refresh_markets_module.REDIS_ACTIVE_MARKETS_KEY)
    assert members == {"old1", "old2"}


async def test_refresh_markets_replaces_existing(monkeypatch, redis_client):
    """A successful fetch should replace, not merge."""
    await redis_client.sadd(refresh_markets_module.REDIS_ACTIVE_MARKETS_KEY, "stale1")

    async def fake_fetch(session, limit):
        return {"new1", "new2"}

    monkeypatch.setattr(
        refresh_markets_module, "_fetch_active_market_tokens", fake_fetch
    )
    job = make_refresh_markets_job(redis_client, limit=50)
    await job()
    members = await redis_client.smembers(refresh_markets_module.REDIS_ACTIVE_MARKETS_KEY)
    assert "stale1" not in members
    assert members == {"new1", "new2"}


# --------------------------------------------------------------------------- #
# redis_cleanup                                                                #
# --------------------------------------------------------------------------- #


async def test_redis_cleanup_purges_orphan_heartbeats(redis_client):
    """Heartbeat keys without TTL should be deleted; keys WITH TTL should
    survive."""
    # Orphan: no TTL
    await redis_client.set(f"{REDIS_HEARTBEAT_PREFIX}orphan", "123")
    # Healthy: has TTL
    await redis_client.set(f"{REDIS_HEARTBEAT_PREFIX}healthy", "456", ex=60)
    # Unrelated key — must NOT be touched.
    await redis_client.set("other:key", "x")

    job = make_redis_cleanup_job(redis_client)
    await job()

    assert await redis_client.exists(f"{REDIS_HEARTBEAT_PREFIX}orphan") == 0
    assert await redis_client.exists(f"{REDIS_HEARTBEAT_PREFIX}healthy") == 1
    assert await redis_client.exists("other:key") == 1


async def test_redis_cleanup_no_heartbeats_does_nothing(redis_client):
    """No-op when there's nothing to clean."""
    job = make_redis_cleanup_job(redis_client)
    await job()  # must not raise


# --------------------------------------------------------------------------- #
# nightly_batch                                                                #
# --------------------------------------------------------------------------- #


async def test_nightly_batch_swallows_import_error(monkeypatch):
    """If batch_runner can't import (missing optional deps), the job
    must not raise."""
    import sys

    # Force a fresh import that fails
    if "scripts.batch_runner" in sys.modules:
        monkeypatch.setitem(sys.modules, "scripts.batch_runner", None)

    from src.engine.jobs import make_nightly_batch_job

    job = make_nightly_batch_job()
    # Should log and return, not raise.
    await job()
