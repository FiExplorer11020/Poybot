"""Tests for InstrumentRegistry + Detector implementations.

Coverage:
  * FixtureNewsEventDetector reads a JSON fixture and emits InstrumentalEvents.
  * NewsEventDetector with no http_session returns []  (operator-deliverable).
  * OracleUpdateDetector with no rpc_client returns [].
  * OracleUpdateDetector with a mocked rpc_client decodes logs.
  * RelatedMarketResolver mocks get_db to return co-occurrence rows.
  * LeaderGasQuirkDetector mocks get_db to return wallet rows.
  * APIOutageWindowDetector mocks get_db to return outage windows.
  * InstrumentRegistry orchestrates multiple detectors and tolerates a
    detector that raises (the rest still run).
  * Registry persistence (_persist) writes events to instrumental_events.

All DB calls are mocked via patching ``src.causal.instruments.get_db``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.causal.instruments import (
    APIOutageWindowDetector,
    Detector,
    FixtureNewsEventDetector,
    InstrumentRegistry,
    InstrumentalEvent,
    LeaderGasQuirkDetector,
    NewsEventDetector,
    OracleUpdateDetector,
    RelatedMarketResolver,
)


# ---------------------------------------------------------------------------
# DB mocking helper
# ---------------------------------------------------------------------------


def _mock_get_db(fetch_rows=None, execute_mock=None, target="instruments"):
    """Patcher for get_db.

    ``target`` selects which module to patch:
      * 'instruments' (default) — News, Oracle, Registry persistence.
      * 'instruments_sql' — RelatedMarket, LeaderGasQuirk, APIOutage.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.execute = execute_mock if execute_mock is not None else AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    module_path = (
        "src.causal.instruments.get_db"
        if target == "instruments"
        else "src.causal.instruments_sql.get_db"
    )
    return patch(module_path, side_effect=_ctx), conn


# ---------------------------------------------------------------------------
# InstrumentalEvent dataclass
# ---------------------------------------------------------------------------


class TestInstrumentalEvent:
    def test_affected_csv_returns_none_when_empty(self):
        ev = InstrumentalEvent(
            event_type="news",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
        )
        assert ev.affected_csv() is None

    def test_affected_csv_joins_ids(self):
        ev = InstrumentalEvent(
            event_type="news",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
            affected_market_ids=["a", "b", "c"],
        )
        assert ev.affected_csv() == "a,b,c"

    def test_affected_csv_truncates_at_budget(self):
        ev = InstrumentalEvent(
            event_type="news",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
            affected_market_ids=["x" * 200 for _ in range(20)],
        )
        out = ev.affected_csv(max_chars=500)
        assert out is not None
        assert len(out) <= 500


# ---------------------------------------------------------------------------
# FixtureNewsEventDetector
# ---------------------------------------------------------------------------


class TestFixtureNewsEventDetector:
    @pytest.mark.asyncio
    async def test_returns_empty_when_fixture_missing(self, tmp_path):
        det = FixtureNewsEventDetector(tmp_path / "missing.json")
        out = await det.detect(datetime.now(tz=timezone.utc))
        assert out == []

    @pytest.mark.asyncio
    async def test_emits_events_from_fixture(self, tmp_path):
        path = tmp_path / "news.json"
        payload = [
            {
                "event_time": "2026-05-12T10:00:00+00:00",
                "headline": "X collapse",
                "affected_market_ids": ["mkt-1", "mkt-2"],
                "confidence": 0.9,
            },
            {
                "event_time": "2026-05-12T11:00:00+00:00",
                "headline": "Y approved",
                "affected_market_ids": ["mkt-3"],
                "confidence": 0.7,
            },
        ]
        path.write_text(json.dumps(payload))
        det = FixtureNewsEventDetector(path)
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        out = await det.detect(asof)
        assert len(out) == 2
        assert out[0].event_type == "news"
        assert out[0].source == "newsapi_fixture"
        assert out[0].confidence == 0.9
        assert out[0].affected_market_ids == ["mkt-1", "mkt-2"]

    @pytest.mark.asyncio
    async def test_skips_future_events(self, tmp_path):
        path = tmp_path / "news.json"
        payload = [
            {
                "event_time": "2027-01-01T00:00:00+00:00",
                "headline": "Future event",
                "affected_market_ids": ["mkt-1"],
            },
        ]
        path.write_text(json.dumps(payload))
        det = FixtureNewsEventDetector(path)
        asof = datetime(2026, 5, 12, tzinfo=timezone.utc)
        out = await det.detect(asof)
        assert out == []


# ---------------------------------------------------------------------------
# NewsEventDetector (real path, stubbed)
# ---------------------------------------------------------------------------


class TestNewsEventDetector:
    @pytest.mark.asyncio
    async def test_returns_empty_without_session(self):
        det = NewsEventDetector(http_session=None)
        out = await det.detect(datetime.now(tz=timezone.utc))
        assert out == []


# ---------------------------------------------------------------------------
# OracleUpdateDetector
# ---------------------------------------------------------------------------


class TestOracleUpdateDetector:
    @pytest.mark.asyncio
    async def test_returns_empty_without_rpc_client(self):
        det = OracleUpdateDetector(rpc_client=None, oracle_address="")
        out = await det.detect(datetime.now(tz=timezone.utc))
        assert out == []

    @pytest.mark.asyncio
    async def test_decodes_log_entries(self):
        rpc = MagicMock()
        rpc.call = AsyncMock(side_effect=[
            "0x1000",  # eth_blockNumber
            [
                {
                    "transactionHash": "0xabc",
                    "blockNumber": "0xfff",
                    "topics": ["0xdead", "0xbeef"],
                },
                {
                    "transactionHash": "0xdef",
                    "blockNumber": "0xfff",
                    "topics": ["0xdead"],
                },
            ],
        ])
        det = OracleUpdateDetector(rpc_client=rpc, oracle_address="0xORACLE")
        out = await det.detect(datetime.now(tz=timezone.utc))
        assert len(out) == 2
        assert out[0].event_type == "oracle_update"
        assert out[0].source == "oracle_logs"
        assert out[0].payload["tx_hash"] == "0xabc"


# ---------------------------------------------------------------------------
# RelatedMarketResolver
# ---------------------------------------------------------------------------


class TestRelatedMarketResolver:
    @pytest.mark.asyncio
    async def test_emits_event_per_pair(self):
        rows = [
            {"market_a": "m1", "market_b": "m2", "co_count": 50},
            {"market_a": "m1", "market_b": "m3", "co_count": 25},
        ]
        patcher, _ = _mock_get_db(fetch_rows=rows, target="instruments_sql")
        with patcher:
            det = RelatedMarketResolver(lookback_days=30)
            out = await det.detect(datetime.now(tz=timezone.utc))
        assert len(out) == 2
        assert all(e.event_type == "news" for e in out)
        assert all(e.source == "related_market" for e in out)
        assert out[0].affected_market_ids == ["m1", "m2"]
        assert out[0].confidence == 0.5  # 50/100 clamp


# ---------------------------------------------------------------------------
# LeaderGasQuirkDetector
# ---------------------------------------------------------------------------


class TestLeaderGasQuirkDetector:
    @pytest.mark.asyncio
    async def test_emits_one_per_wallet(self):
        rows = [
            {"wallet_address": "0xA", "n_intents": 100, "replacement_count": 20},
            {"wallet_address": "0xB", "n_intents": 50, "replacement_count": 0},
        ]
        patcher, _ = _mock_get_db(fetch_rows=rows, target="instruments_sql")
        with patcher:
            det = LeaderGasQuirkDetector()
            out = await det.detect(datetime.now(tz=timezone.utc))
        assert len(out) == 2
        assert all(e.event_type == "gas_quirk" for e in out)
        assert all(e.source == "mempool_observations" for e in out)
        # Wallet B: 0 replacement => confidence = 1.0
        b_event = next(e for e in out if e.payload["wallet_address"] == "0xB")
        assert b_event.confidence == 1.0


# ---------------------------------------------------------------------------
# APIOutageWindowDetector
# ---------------------------------------------------------------------------


class TestAPIOutageWindowDetector:
    @pytest.mark.asyncio
    async def test_emits_event_for_outage_window(self):
        rows = [
            {
                "window_start": datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
                "n_onchain": 100,
                "n_api": 50,   # ratio 0.5 < threshold 0.95
            },
            {
                "window_start": datetime(2026, 5, 1, 13, tzinfo=timezone.utc),
                "n_onchain": 100,
                "n_api": 98,   # ratio 0.98 >= 0.95
            },
        ]
        patcher, _ = _mock_get_db(fetch_rows=rows, target="instruments_sql")
        with patcher:
            det = APIOutageWindowDetector(coverage_threshold=0.95)
            out = await det.detect(datetime.now(tz=timezone.utc))
        # Only the first row crosses the threshold downward.
        assert len(out) == 1
        assert out[0].event_type == "api_outage"
        assert out[0].source == "coverage_reconciler"


# ---------------------------------------------------------------------------
# Registry orchestration
# ---------------------------------------------------------------------------


class _SyntheticDetector(Detector):
    name = "synthetic"
    event_type = "news"

    def __init__(self, events: list[InstrumentalEvent], raise_exc: bool = False):
        self._events = events
        self._raise = raise_exc

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        if self._raise:
            raise RuntimeError("synthetic boom")
        return self._events


class TestRegistry:
    @pytest.mark.asyncio
    async def test_registry_runs_all_detectors(self):
        ev1 = InstrumentalEvent(
            event_type="news",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
        )
        ev2 = InstrumentalEvent(
            event_type="oracle_update",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
        )
        d1 = _SyntheticDetector([ev1])
        d2 = _SyntheticDetector([ev2])
        execute_mock = AsyncMock()
        patcher, _ = _mock_get_db(fetch_rows=[], execute_mock=execute_mock)
        with patcher:
            reg = InstrumentRegistry([d1, d2])
            summary = await reg.run_one_pass()
        assert summary["by_detector"]["synthetic"]["events_detected"] >= 1
        # _persist invokes conn.execute per event; both events should land.
        assert execute_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_registry_tolerates_failing_detector(self):
        d_ok = _SyntheticDetector([InstrumentalEvent(
            event_type="news",
            event_time=datetime.now(tz=timezone.utc),
            source="test",
        )])
        d_bad = _SyntheticDetector([], raise_exc=True)
        execute_mock = AsyncMock()
        patcher, _ = _mock_get_db(fetch_rows=[], execute_mock=execute_mock)
        with patcher:
            reg = InstrumentRegistry([d_ok, d_bad])
            summary = await reg.run_one_pass()
        # The failing detector's entry has an error string.
        # _SyntheticDetector instances share the same name attribute,
        # so summary["by_detector"]["synthetic"] reflects the LAST run
        # (d_bad). We assert the failing one logged an error and the
        # passing one persisted at least one event.
        assert summary["by_detector"]["synthetic"]["error"] is not None
        assert execute_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_register_adds_detector(self):
        reg = InstrumentRegistry()
        det = _SyntheticDetector([])
        reg.register(det)
        assert det in reg.detectors

    @pytest.mark.asyncio
    async def test_empty_registry_yields_empty_summary(self):
        reg = InstrumentRegistry()
        summary = await reg.run_one_pass()
        assert summary["by_detector"] == {}
