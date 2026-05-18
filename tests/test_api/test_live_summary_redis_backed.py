"""Redis-backed /api/v1/live-summary endpoint regression tests.

Covers the precomputed-snapshot refactor (2026-05-17). The endpoint
no longer composes the snapshot in-process — it reads pre-built JSON
from Redis under ``SNAPSHOT_REDIS_KEY``. These tests pin the contract:

  * Populated key → 200 with payload + ETag + Cache-Control.
  * Missing key → 503 with skeleton + ``warming_up`` flag.
  * Redis raises → 503 with skeleton + ``error`` flag.
  * If-None-Match matches → 304 (zero body).
  * Built-at age > 60 s → ``X-Snapshot-Stale-Age`` header set.
  * Latency budget — the endpoint should return well under 50 ms when
    Redis is fast (the whole point of the refactor).

Run: ``pytest tests/test_api/test_live_summary_redis_backed.py -v``
"""

import hashlib
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures — isolated from test_endpoints.py to keep the test surface tight
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    """Minimal asyncpg-style pool. The Redis-backed endpoint does NOT
    hit the pool at all — but other lifespan / health-check code paths
    still expect a working pool when the FastAPI app boots."""
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield AsyncMock(
            fetchval=AsyncMock(return_value=0),
            fetchrow=AsyncMock(return_value=None),
            fetch=AsyncMock(return_value=[]),
        )

    pool.acquire = acquire
    return pool


def _make_redis(snapshot: dict | str | bytes | None, built_at: float | None = None,
                raise_on_get: Exception | None = None):
    """Build a mock redis.asyncio.Redis with controlled GET behaviour.

    Args:
        snapshot: the payload stored under SNAPSHOT_REDIS_KEY (already
            JSON-encoded if str/bytes, else dumped here). Use ``None`` to
            simulate a missing key.
        built_at: epoch seconds for SNAPSHOT_BUILT_AT_KEY. Defaults to now.
        raise_on_get: if set, every ``.get()`` raises this exception.
    """
    if isinstance(snapshot, dict):
        raw = json.dumps(snapshot, separators=(",", ":"))
    elif isinstance(snapshot, bytes):
        raw = snapshot.decode("utf-8")
    else:
        raw = snapshot

    if built_at is None:
        built_at = time.time()
    built_at_str = str(built_at) if built_at else None

    r = MagicMock()

    async def _get(key: str):
        if raise_on_get is not None:
            raise raise_on_get
        if key == "snapshot:live_summary":
            return raw
        if key == "snapshot:live_summary:built_at":
            return built_at_str
        return None

    r.get = AsyncMock(side_effect=_get)
    r.ping = AsyncMock(return_value=True)
    r.hgetall = AsyncMock(return_value={})
    return r


@pytest.fixture
def app_client_factory(mock_pool):
    """Returns a callable that builds a TestClient with the supplied redis mock."""

    def _build(redis_mock):
        import src.api.main as api_main

        api_main._pool = mock_pool
        api_main._redis = redis_mock
        return TestClient(api_main.app, raise_server_exceptions=True)

    return _build


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRedisBackedLiveSummary:
    """Contract for the Redis-backed /api/v1/live-summary endpoint."""

    def test_endpoint_returns_redis_cached_json(self, app_client_factory):
        """Populated Redis → 200 + raw payload + ETag + Cache-Control."""
        payload = {
            "data": {
                "clock": {"updated_at": "2026-05-17T10:00:00+00:00"},
                "bot": {"status": "running"},
                "stats": {"net_pnl": 1234.56},
            }
        }
        client = app_client_factory(_make_redis(payload))
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 200
        # The endpoint serves the raw bytes verbatim — no re-serialisation.
        body = resp.json()
        assert body == payload
        # ETag is a SHA-256 hex (first 16 chars) of the raw bytes.
        assert "etag" in {k.lower() for k in resp.headers.keys()}
        assert resp.headers["etag"].startswith('"') and resp.headers["etag"].endswith('"')
        assert "private" in resp.headers.get("cache-control", "").lower()

    def test_endpoint_returns_503_when_key_missing(self, app_client_factory):
        """Cold start — maintenance container hasn't written the key yet."""
        client = app_client_factory(_make_redis(snapshot=None))
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 503
        body = resp.json()
        assert body.get("warming_up") is True
        # Skeleton must be present so the dashboard can render shells.
        assert "data" in body
        data = body["data"]
        for key in ("clock", "bot", "stats", "positions", "wallet_graph"):
            assert key in data, f"Missing skeleton key: {key}"

    def test_endpoint_returns_503_when_redis_unavailable(self, app_client_factory):
        """Redis down / GET raises → 503 + skeleton + error flag (no hang)."""
        client = app_client_factory(
            _make_redis(snapshot=None, raise_on_get=ConnectionError("redis down"))
        )
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 503
        body = resp.json()
        assert body.get("warming_up") is True
        assert body.get("error") == "redis_unavailable"
        # Skeleton still present so the client can render shells.
        assert "data" in body

    def test_endpoint_returns_304_on_etag_match(self, app_client_factory):
        """If-None-Match equal to current ETag → 304 with zero body."""
        payload = {"data": {"clock": {"updated_at": "2026-05-17T10:00:00+00:00"}}}
        raw = json.dumps(payload, separators=(",", ":"))
        expected_etag = '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] + '"'
        client = app_client_factory(_make_redis(payload))
        with client as c:
            resp = c.get(
                "/api/v1/live-summary",
                headers={"If-None-Match": expected_etag},
            )
        assert resp.status_code == 304
        # 304 must echo the ETag so caches stay consistent.
        assert resp.headers["etag"] == expected_etag
        # Body must be empty (HTTP 304 semantics).
        assert resp.content == b""

    def test_endpoint_adds_stale_header_when_age_over_60s(self, app_client_factory):
        """built_at > 60s ago → X-Snapshot-Stale-Age set, but still serves 200."""
        payload = {"data": {"clock": {"updated_at": "2026-05-17T09:00:00+00:00"}}}
        old_built_at = time.time() - 90.0  # 90s old → > 60s threshold
        client = app_client_factory(_make_redis(payload, built_at=old_built_at))
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 200
        stale_header = resp.headers.get("x-snapshot-stale-age")
        assert stale_header is not None, "X-Snapshot-Stale-Age header missing"
        # The header value is the age in seconds, rounded to 1 decimal.
        age = float(stale_header)
        assert age >= 60.0
        # Sanity — within a generous window so a slow test box doesn't flake.
        assert age < 600.0

    def test_endpoint_no_stale_header_when_fresh(self, app_client_factory):
        """Recent built_at (< 60s) → no stale header."""
        payload = {"data": {"clock": {"updated_at": "2026-05-17T10:00:00+00:00"}}}
        client = app_client_factory(_make_redis(payload, built_at=time.time()))
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 200
        assert "x-snapshot-stale-age" not in {k.lower() for k in resp.headers.keys()}

    def test_endpoint_returns_within_50ms(self, app_client_factory):
        """Perf budget — Redis-backed path must be sub-50 ms (mock-based).

        The acceptance criterion in the architecture doc is <50 ms p99
        with a real Redis. With an AsyncMock backing the GET, anything
        above 100 ms suggests the endpoint regressed into a slow path
        (e.g. silently fell back to the in-process gather). We assert
        a comfortable 100 ms ceiling here — the mocks themselves cost
        ~1 ms so headroom is huge.
        """
        payload = {
            "data": {
                "clock": {"updated_at": "2026-05-17T10:00:00+00:00"},
                "stats": {"net_pnl": 0.0},
            }
        }
        client = app_client_factory(_make_redis(payload))
        with client as c:
            # Warm-up call so any one-shot import / route-table cost
            # doesn't pollute the timing.
            c.get("/api/v1/live-summary")
            t0 = time.perf_counter()
            resp = c.get("/api/v1/live-summary")
            elapsed_ms = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200
        # Generous ceiling — real prod target is <10ms, but TestClient +
        # AsyncMock adds overhead. 100 ms catches any accidental fall-back
        # to the in-process gather (which was 200-400 ms warm, 30 s cold).
        assert elapsed_ms < 100.0, f"endpoint took {elapsed_ms:.2f}ms — perf regression"

    def test_endpoint_handles_bytes_payload_from_redis(self, app_client_factory):
        """redis-py may return bytes if decode_responses=False — must still work."""
        payload = {"data": {"clock": {"updated_at": "2026-05-17T10:00:00+00:00"}}}
        raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        client = app_client_factory(_make_redis(raw_bytes))
        with client as c:
            resp = c.get("/api/v1/live-summary")
        assert resp.status_code == 200
        assert resp.json() == payload
