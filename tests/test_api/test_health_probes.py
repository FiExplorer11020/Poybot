"""
Tests for /healthz (liveness) and /health (readiness) probe endpoints.

These exist to give Docker HEALTHCHECK / Oracle Cloud LB / external watchdogs
a stable, well-documented place to ask "is the API alive and ready?".
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _make_pool(*, db_ok: bool = True, db_exc: type[BaseException] | None = None):
    """A fake asyncpg.Pool — the API only uses `pool.acquire()` then `fetchval`."""
    pool = MagicMock()
    conn = AsyncMock()
    if db_exc is not None:
        conn.fetchval = AsyncMock(side_effect=db_exc("simulated db failure"))
    else:
        conn.fetchval = AsyncMock(return_value=1 if db_ok else None)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


def _make_redis(*, redis_ok: bool = True, redis_exc: type[BaseException] | None = None):
    r = MagicMock()
    if redis_exc is not None:
        r.ping = AsyncMock(side_effect=redis_exc("simulated redis failure"))
    else:
        r.ping = AsyncMock(return_value=True if redis_ok else False)
    return r


@pytest.fixture
def patched_api(monkeypatch):
    """Patch _pool and _redis on the api.main module without booting lifespan."""
    import src.api.main as api_main

    def _apply(pool, redis):
        api_main._pool = pool
        api_main._redis = redis
        return api_main

    yield _apply

    api_main._pool = None
    api_main._redis = None


def _client(api_main):
    return TestClient(api_main.app, raise_server_exceptions=True)


# --------------------------------------------------------------------------- #
# /healthz — liveness                                                          #
# --------------------------------------------------------------------------- #


def test_healthz_returns_200_even_when_db_and_redis_down(patched_api):
    """
    Liveness must NEVER depend on external systems. If a transient DB or Redis
    blip flipped /healthz to 503, Docker / k8s would kill a perfectly healthy
    container — making the outage worse instead of better.
    """
    api_main = patched_api(
        _make_pool(db_exc=ConnectionRefusedError),
        _make_redis(redis_exc=ConnectionError),
    )
    resp = _client(api_main).get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "polymarket-bot-api"
    assert "uptime_s" in body
    assert "started_at" in body


def test_healthz_does_not_touch_db_or_redis(patched_api):
    """Concrete invariant: /healthz must call neither pool.acquire nor redis.ping."""
    pool = _make_pool()
    redis = _make_redis()
    pool.acquire = MagicMock(side_effect=AssertionError("pool.acquire called"))
    redis.ping = AsyncMock(side_effect=AssertionError("redis.ping called"))
    api_main = patched_api(pool, redis)

    resp = _client(api_main).get("/healthz")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# /health — readiness                                                          #
# --------------------------------------------------------------------------- #


def test_health_returns_200_when_db_and_redis_up(patched_api):
    api_main = patched_api(_make_pool(db_ok=True), _make_redis(redis_ok=True))
    resp = _client(api_main).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is True
    assert body["checks"]["db"]["error"] is None
    assert body["checks"]["redis"]["error"] is None


def test_health_returns_503_when_db_down(patched_api):
    api_main = patched_api(
        _make_pool(db_exc=ConnectionRefusedError),
        _make_redis(redis_ok=True),
    )
    resp = _client(api_main).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"]["ok"] is False
    assert "ConnectionRefusedError" in body["checks"]["db"]["error"]
    assert body["checks"]["redis"]["ok"] is True


def test_health_returns_503_when_redis_down(patched_api):
    api_main = patched_api(
        _make_pool(db_ok=True),
        _make_redis(redis_exc=ConnectionError),
    )
    resp = _client(api_main).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is False
    assert "ConnectionError" in body["checks"]["redis"]["error"]


def test_health_returns_503_when_pool_not_initialized(patched_api):
    """Boot race: lifespan hasn't yet built the pool. Must not 500 / NoneType."""
    api_main = patched_api(None, _make_redis(redis_ok=True))
    resp = _client(api_main).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["db"]["ok"] is False
    assert body["checks"]["db"]["error"] == "pool_not_initialized"


def test_health_alias_under_api_prefix_works(patched_api):
    """`/api/health` is the same endpoint, registered for callers scraping /api/."""
    api_main = patched_api(_make_pool(db_ok=True), _make_redis(redis_ok=True))
    resp = _client(api_main).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
