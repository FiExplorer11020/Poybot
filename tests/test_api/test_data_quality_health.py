from contextlib import asynccontextmanager

import pytest

from src.api import main


class _FakeConn:
    async def fetchval(self, _sql):
        return 12.5

    async def fetchrow(self, _sql):
        return {
            "total_markets": 10,
            "token_mapped_markets": 7,
            "mapped_tokens": 14,
            "legacy_fee_markets": 9,
        }


class _FakePool:
    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn()


class _FakeRedis:
    async def ping(self):
        return True

    async def get(self, key):
        values = {
            "ws:market:last_message_ts": None,
            "metrics:book_age_p95_s": "2.4",
            "metrics:fee_snapshot_coverage_pct": "98.0",
            "metrics:token_map_coverage_pct": "100.0",
        }
        return values.get(key)

    async def hgetall(self, key):
        if key == "signals:rejected:1h":
            return {"stale_book": "3", "missing_fee": "1"}
        return {}


class _FakeRedisWithoutDataQualityMetrics(_FakeRedis):
    async def get(self, key):
        values = {
            "ws:market:last_message_ts": None,
            "metrics:book_age_p95_s": None,
            "metrics:fee_snapshot_coverage_pct": None,
            "metrics:token_map_coverage_pct": None,
        }
        return values.get(key)


@pytest.mark.asyncio
async def test_health_exposes_v1_data_quality_without_static_websocket(monkeypatch):
    monkeypatch.setattr(main, "_pool", _FakePool())
    monkeypatch.setattr(main, "_redis", _FakeRedis())
    main._health_cache = {"data": None, "last_checked": 0.0}

    data = await main._health_checks(force=True)

    assert data["websocket_connected"] is False
    assert data["websocket"] is False
    assert data["last_message_age_s"] is None
    assert data["book_age_p95_s"] == 2.4
    assert data["fee_snapshot_coverage_pct"] == 98.0
    assert data["token_map_coverage_pct"] == 100.0
    assert data["rejected_signals_1h"] == {"stale_book": 3, "missing_fee": 1}


@pytest.mark.asyncio
async def test_health_falls_back_to_db_data_accumulation_coverage(monkeypatch):
    monkeypatch.setattr(main, "_pool", _FakePool())
    monkeypatch.setattr(main, "_redis", _FakeRedisWithoutDataQualityMetrics())
    main._health_cache = {"data": None, "last_checked": 0.0}

    data = await main._health_checks(force=True)

    assert data["fee_snapshot_coverage_pct"] == 90.0
    assert data["fee_snapshot_coverage_source"] == "markets.fee_rate_pct"
    assert data["token_map_coverage_pct"] == 70.0
    assert data["data_accumulation_counts"]["total_markets"] == 10
    assert data["data_accumulation_counts"]["token_mapped_markets"] == 7
