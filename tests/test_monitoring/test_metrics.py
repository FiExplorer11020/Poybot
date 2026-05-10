"""
Phase 1 Task M — smoke tests for the Prometheus metrics foundation.

These tests are deliberately small. They exist to guarantee that:

1. The metrics module imports cleanly (no name collisions on the default
   REGISTRY, no missing labels) — the contract Phase 1 Tasks O and F depend on.
2. ``export_latest()`` emits a non-empty Prometheus text payload that contains
   the polybot_ prefix.
3. The FastAPI ``/metrics`` route is wired and serves the standard
   Prometheus content-type so a vanilla Prometheus scrape works.

Mirror the patching style from ``tests/test_api/test_health_probes.py`` so we
don't have to boot the FastAPI lifespan (no real DB / Redis required).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Module-level smoke tests                                                    #
# --------------------------------------------------------------------------- #


def test_metrics_module_imports_cleanly():
    """The contract: every named metric exists and is the right type."""
    from prometheus_client import Counter, Gauge, Histogram

    from src.monitoring import metrics as m

    # Counters
    assert isinstance(m.trades_ingested_total, Counter)
    assert isinstance(m.ws_disconnects_total, Counter)
    assert isinstance(m.observer_queue_drops_total, Counter)
    assert isinstance(m.falcon_calls_total, Counter)
    assert isinstance(m.redis_publishes_total, Counter)
    assert isinstance(m.killswitch_strict_path_total, Counter)

    # Histograms
    assert isinstance(m.trade_ingestion_latency_seconds, Histogram)
    assert isinstance(m.db_write_batch_size, Histogram)
    assert isinstance(m.db_write_latency_seconds, Histogram)
    assert isinstance(m.falcon_call_latency_seconds, Histogram)

    # Gauges
    assert isinstance(m.observer_queue_depth, Gauge)
    assert isinstance(m.falcon_concurrency, Gauge)


def test_export_latest_returns_non_empty_payload_with_polybot_prefix():
    from src.monitoring.metrics import (
        export_latest,
        falcon_calls_total,
        trades_ingested_total,
    )

    # Touch a couple of metrics so the registry has something to serialize. We
    # use labels that match the contract so this also smoke-tests label arity.
    trades_ingested_total.labels(source="ws", result="inserted").inc()
    falcon_calls_total.labels(agent="574", result="ok").inc()

    payload, content_type = export_latest()

    assert isinstance(payload, (bytes, bytearray))
    assert len(payload) > 0
    assert content_type.startswith("text/plain")

    body = payload.decode("utf-8")
    assert "polybot_" in body
    # The two metrics we just touched must be in the output.
    assert "polybot_trades_ingested_total" in body
    assert "polybot_falcon_calls_total" in body


# --------------------------------------------------------------------------- #
# FastAPI /metrics route                                                      #
# --------------------------------------------------------------------------- #


def _make_pool():
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


def _make_redis():
    r = MagicMock()
    r.ping = AsyncMock(return_value=True)
    return r


@pytest.fixture
def patched_api(monkeypatch):
    """Mirror tests/test_api/test_health_probes.py: bypass lifespan + I/O."""
    import src.api.main as api_main

    api_main._pool = _make_pool()
    api_main._redis = _make_redis()
    yield api_main
    api_main._pool = None
    api_main._redis = None


def test_metrics_endpoint_returns_200_and_prometheus_content_type(patched_api):
    """
    /metrics must return HTTP 200 with the standard Prometheus content-type so
    a vanilla scrape (Prometheus, Grafana Agent, vector.dev) works without any
    extra config. The exact content-type comes from prometheus_client and is
    locked at ``text/plain; version=0.0.4; charset=utf-8``.
    """
    client = TestClient(patched_api.app, raise_server_exceptions=True)
    resp = client.get("/metrics")

    assert resp.status_code == 200
    # Match the full Prometheus content-type string. If prometheus-client ever
    # bumps this, we want the test to fail loudly so dashboards / scrapers can
    # be re-validated.
    assert resp.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "polybot_" in resp.text


def test_metrics_endpoint_does_not_require_db_or_redis(patched_api):
    """
    Concrete invariant: a Prometheus scrape must NOT touch DB or Redis.
    If it did, a transient backend blip would silently kill metrics scraping
    (and therefore alerting) right when we need it most.
    """
    patched_api._pool.acquire = MagicMock(side_effect=AssertionError("pool.acquire called"))
    patched_api._redis.ping = AsyncMock(side_effect=AssertionError("redis.ping called"))

    client = TestClient(patched_api.app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    assert resp.status_code == 200
