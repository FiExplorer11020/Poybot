"""Round 12 feature_store readers — get_social_signals_asof,
get_cross_market_features_asof, get_cross_market_operator_resolution.

All DB calls are mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.profiler.feature_store import (
    get_cross_market_features_asof,
    get_cross_market_operator_resolution,
    get_social_signals_asof,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _make_conn(fetch_map: dict[str, list], fetchrow_map: dict[str, object | None] | None = None):
    """Build an AsyncMock conn whose fetch / fetchrow route by SQL
    keyword."""

    fetchrow_map = fetchrow_map or {}
    conn = AsyncMock()

    async def _fetch(query, *args):
        q = " ".join(query.split())
        for kw, rows in fetch_map.items():
            if kw in q:
                return rows
        return []

    async def _fetchrow(query, *args):
        q = " ".join(query.split())
        for kw, row in fetchrow_map.items():
            if kw in q:
                return row
        return None

    conn.fetch = _fetch
    conn.fetchrow = _fetchrow
    return conn


class TestGetSocialSignalsAsof:
    @pytest.mark.asyncio
    async def test_no_signals_returns_none(self, now):
        conn = _make_conn(fetch_map={"social_signals": []})
        out = await get_social_signals_asof(conn, "0xPM", now, lookback_days=30)
        assert out is None

    @pytest.mark.asyncio
    async def test_signals_aggregate(self, now):
        signals = [
            {
                "posted_at": now - timedelta(days=1),
                "intent": "entry_signal",
                "intent_confidence": 0.85,
                "parsed_market": "m1",
                "parsed_direction": "yes",
            },
            {
                "posted_at": now - timedelta(days=2),
                "intent": "noise",
                "intent_confidence": 0.6,
                "parsed_market": None,
                "parsed_direction": None,
            },
        ]
        conn = _make_conn(fetch_map={
            "social_signals": signals,
            "trades_observed": [],
        })
        out = await get_social_signals_asof(conn, "0xPM", now, lookback_days=30)
        assert out is not None
        assert "social_signal_density" in out
        assert out["social_signal_density"] == pytest.approx(2 / 30.0)


class TestGetCrossMarketFeaturesAsof:
    @pytest.mark.asyncio
    async def test_no_operator_returns_none(self, now):
        conn = _make_conn(
            fetch_map={},
            fetchrow_map={"cross_market_operators": None},
        )
        out = await get_cross_market_features_asof(
            conn, "0xPM", now, lookback_days=30,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_resolved_operator_returns_features(self, now):
        conn = _make_conn(
            fetch_map={
                "cross_market_positions": [
                    {
                        "venue": "kalshi", "market_id": "k1",
                        "side": "yes", "size_usdc": 100.0,
                        "opened_at": now - timedelta(days=1),
                        "closed_at": None,
                        "snapshot_at": now - timedelta(days=1),
                    },
                ],
                "trades_observed": [],
            },
            fetchrow_map={
                "cross_market_operators": {
                    "operator_id": 1,
                    "confidence": 0.95,
                    "resolution_source": "manual",
                },
            },
        )
        out = await get_cross_market_features_asof(
            conn, "0xPM", now, lookback_days=30,
        )
        assert out is not None
        assert "active_venue_count" in out
        assert out["active_venue_count"] >= 1


class TestGetCrossMarketOperatorResolution:
    @pytest.mark.asyncio
    async def test_no_row_returns_none(self):
        conn = _make_conn(
            fetch_map={},
            fetchrow_map={"cross_market_operators": None},
        )
        out = await get_cross_market_operator_resolution(conn, "0xPM")
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_latest_row(self):
        row = {
            "operator_id": 7,
            "polymarket_wallet": "0xPM",
            "kalshi_account": "k-7",
            "manifold_handle": None,
            "predictit_account": None,
            "x_handle": "alice",
            "resolution_source": "manual",
            "confidence": 1.0,
            "resolved_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "notes": "seed",
        }
        conn = _make_conn(
            fetch_map={},
            fetchrow_map={"cross_market_operators": row},
        )
        out = await get_cross_market_operator_resolution(conn, "0xPM")
        assert out is not None
        assert out["operator_id"] == 7
        assert out["kalshi_account"] == "k-7"
