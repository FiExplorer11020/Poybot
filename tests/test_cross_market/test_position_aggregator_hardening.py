"""Wave-3 hardening for the cross-market position aggregator.

Coverage beyond the pre-merge suite:

  * Operator with `kalshi_account=None` and `manifold_handle=None` →
    aggregator iterates produced 0 rows + run_once still returns a
    well-shaped summary.
  * One operator with both Kalshi + Manifold → rows from both venues
    are persisted in the same cycle (mixed-venue per-operator).
  * `_load_operators` filter respects the confidence floor — operators
    below floor are silently skipped.
  * Mid-cycle persist failure on one row doesn't abort the remaining
    inserts in the same cycle.
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
    failing_indices: set[int] | None = None,
):
    conn = AsyncMock()

    async def _fetch(query, *args):
        return operators_rows or []

    conn.fetch = _fetch
    captured = execute_capture if execute_capture is not None else []
    call_idx = {"n": 0}
    failing = failing_indices or set()

    async def _execute(query, *args):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx in failing:
            raise RuntimeError(f"simulated DB write failure at call {idx}")
        captured.append(args)

    conn.execute = _execute

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn, captured


class _FakeKalshi:
    venue = "kalshi"

    def __init__(self, positions):
        self._positions = positions

    async def fetch_wallet_positions(self, account):
        return list(self._positions)


class _FakeManifold:
    venue = "manifold"

    def __init__(self, bets):
        self._bets = bets

    async def fetch_wallet_positions(self, handle):
        return list(self._bets)


class TestNoResolvedVenues:
    @pytest.mark.asyncio
    async def test_operator_with_no_resolved_venues_yields_zero_rows(self):
        # Operator row has neither kalshi_account nor manifold_handle —
        # so even with both clients plumbed, no rows can be derived.
        operators = [{
            "operator_id": 7, "polymarket_wallet": "0xPM",
            "kalshi_account": None, "manifold_handle": None,
            "predictit_account": None, "x_handle": None,
            "resolution_source": "manual", "confidence": 1.0,
        }]
        ctx, _, captured = _mock_get_db(operators_rows=operators)
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=_FakeKalshi([{"ticker": "X", "position": 1,
                                     "market_exposure": 10.0}]),
                manifold=_FakeManifold([{"contractId": "x",
                                          "outcome": "YES",
                                          "amount": 10}]),
            )
            summary = await agg.run_once()
        # 1 operator loaded, 0 rows written.
        assert summary["n_operators"] == 1
        assert summary["n_rows_written"] == 0
        assert captured == []


class TestMixedVenueOperator:
    @pytest.mark.asyncio
    async def test_kalshi_and_manifold_rows_in_one_cycle(self):
        operators = [{
            "operator_id": 5, "polymarket_wallet": "0xPM",
            "kalshi_account": "K-5", "manifold_handle": "alice",
            "predictit_account": None, "x_handle": None,
            "resolution_source": "manual", "confidence": 1.0,
        }]
        positions = [
            {"ticker": "FED-RATE", "position": 50, "market_exposure": 200.0,
             "created_time": "2026-05-12T10:00:00+00:00"},
        ]
        bets = [
            {"contractId": "m1", "outcome": "NO", "amount": 75,
             "createdTime": 1747044000000},
        ]
        ctx, _, captured = _mock_get_db(operators_rows=operators)
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=_FakeKalshi(positions),
                manifold=_FakeManifold(bets),
            )
            summary = await agg.run_once()
        assert summary["n_rows_written"] == 2
        venues_written = {row[1] for row in captured}
        assert venues_written == {"kalshi", "manifold"}


class TestConfidenceFloor:
    @pytest.mark.asyncio
    async def test_load_operators_uses_threshold_in_query(self):
        # Capture the SQL args passed to conn.fetch — the aggregator
        # must pass its configured min_confidence as $1.
        captured_args: list[tuple[Any, ...]] = []

        conn = AsyncMock()

        async def _fetch(query, *args):
            captured_args.append(args)
            return []

        conn.fetch = _fetch
        conn.execute = AsyncMock()

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=_ctx
        ):
            agg = CrossMarketPositionAggregator(min_confidence=0.95)
            await agg.run_once()
        assert captured_args, "expected at least one fetch call"
        # First arg passed to the query is the threshold.
        assert captured_args[0][0] == pytest.approx(0.95)


class TestPartialFailureTolerance:
    @pytest.mark.asyncio
    async def test_one_failing_insert_does_not_abort_cycle(self):
        operators = [{
            "operator_id": 9, "polymarket_wallet": "0xPM",
            "kalshi_account": "K-9", "manifold_handle": None,
            "predictit_account": None, "x_handle": None,
            "resolution_source": "manual", "confidence": 1.0,
        }]
        positions = [
            {"ticker": "A", "position": 1, "market_exposure": 1.0,
             "created_time": "2026-05-12T10:00:00+00:00"},
            {"ticker": "B", "position": 1, "market_exposure": 2.0,
             "created_time": "2026-05-12T10:00:00+00:00"},
            {"ticker": "C", "position": 1, "market_exposure": 3.0,
             "created_time": "2026-05-12T10:00:00+00:00"},
        ]
        # Fail the second insert (index 1 in the executed calls).
        ctx, _, captured = _mock_get_db(
            operators_rows=operators,
            failing_indices={1},
        )
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            agg = CrossMarketPositionAggregator(
                kalshi=_FakeKalshi(positions),
            )
            summary = await agg.run_once()
        # 2 of 3 rows persisted; cycle did not abort.
        assert summary["n_rows_written"] == 2
        assert len(captured) == 2
