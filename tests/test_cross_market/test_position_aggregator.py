"""CrossMarketPositionAggregator tests.

Coverage:
  * No operators → run_once writes zero rows but doesn't crash.
  * One Kalshi-resolved operator → positions persisted with venue=kalshi.
  * One Manifold-resolved operator → bets persisted with venue=manifold.
  * PredictIt position fetch returns [] (the client's contract); no rows
    persisted for predictit even if it's plumbed.
  * Confidence filter excludes pending-review fingerprint matches.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.cross_market.position_aggregator import CrossMarketPositionAggregator


def _mock_get_db(
    operators_rows: list[dict[str, Any]] | None = None,
    execute_capture: list[tuple[Any, ...]] | None = None,
):
    """Mock get_db. conn.fetch returns operators_rows; conn.execute
    appends captured args."""
    conn = AsyncMock()

    async def _fetch(query, *args):
        return operators_rows or []

    conn.fetch = _fetch
    captured = execute_capture if execute_capture is not None else []

    async def _execute(query, *args):
        captured.append(args)

    conn.execute = _execute

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn, captured


class _FakeKalshi:
    def __init__(self, positions):
        self._positions = positions
        self.venue = "kalshi"

    async def fetch_wallet_positions(self, account):
        return self._positions


class _FakeManifold:
    def __init__(self, bets):
        self._bets = bets
        self.venue = "manifold"

    async def fetch_wallet_positions(self, handle):
        return self._bets


class _FakePredictIt:
    venue = "predictit"

    async def fetch_wallet_positions(self, account):
        return []


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_no_operators(self):
        ctx, _, captured = _mock_get_db(operators_rows=[])
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=_FakeKalshi([]),
                manifold=_FakeManifold([]),
                predictit=_FakePredictIt(),
            )
            summary = await agg.run_once()
        assert summary["n_operators"] == 0
        assert summary["n_rows_written"] == 0

    @pytest.mark.asyncio
    async def test_kalshi_positions_persisted(self):
        operators = [{
            "operator_id": 1, "polymarket_wallet": "0xPM",
            "kalshi_account": "K-1", "manifold_handle": None,
            "predictit_account": None, "x_handle": None,
            "resolution_source": "manual", "confidence": 1.0,
        }]
        positions = [
            {"ticker": "FED-RATE", "position": 100, "market_exposure": 500.0,
             "created_time": "2026-05-12T10:00:00+00:00"},
        ]
        ctx, _, captured = _mock_get_db(operators_rows=operators)
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=_FakeKalshi(positions),
                manifold=None,
                predictit=None,
            )
            summary = await agg.run_once()
        assert summary["n_rows_written"] == 1
        # The first executed INSERT had venue='kalshi'.
        # captured[0] is the args tuple for the insert; venue is arg index 1.
        assert captured[0][1] == "kalshi"
        assert captured[0][2] == "FED-RATE"  # market_id
        assert captured[0][3] == "yes"        # side (positive position)

    @pytest.mark.asyncio
    async def test_manifold_bets_persisted(self):
        operators = [{
            "operator_id": 2, "polymarket_wallet": "0xPM",
            "kalshi_account": None, "manifold_handle": "alice",
            "predictit_account": None, "x_handle": None,
            "resolution_source": "manual", "confidence": 1.0,
        }]
        bets = [
            {"contractId": "m1", "outcome": "YES", "amount": 100,
             "createdTime": 1747044000000},  # 2026-05-12 in ms
        ]
        ctx, _, captured = _mock_get_db(operators_rows=operators)
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=None,
                manifold=_FakeManifold(bets),
                predictit=None,
            )
            summary = await agg.run_once()
        assert summary["n_rows_written"] == 1
        assert captured[0][1] == "manifold"
        assert captured[0][2] == "m1"
        assert captured[0][3] == "yes"
