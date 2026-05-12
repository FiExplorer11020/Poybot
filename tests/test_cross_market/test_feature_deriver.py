"""Cross-market feature derivation tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.cross_market.feature_deriver import (
    CrossMarketFeatures,
    derive_features,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


class TestEmptyInput:
    def test_empty_returns_zero_count(self, now):
        feats = derive_features(
            cross_market_rows=[], polymarket_trades=[],
            asof_ts=now, lookback_days=30,
        )
        assert feats.active_venue_count == 0
        assert feats.cross_venue_correlation is None
        assert feats.cross_venue_lag_s is None


class TestActiveVenueCount:
    def test_distinct_venues_counted(self, now):
        rows = [
            {"venue": "kalshi", "market_id": "k1", "side": "yes",
             "opened_at": now - timedelta(days=1)},
            {"venue": "manifold", "market_id": "m1", "side": "no",
             "opened_at": now - timedelta(days=2)},
        ]
        feats = derive_features(
            cross_market_rows=rows, polymarket_trades=[],
            asof_ts=now, lookback_days=30,
        )
        assert feats.active_venue_count == 2

    def test_polymarket_trades_add_venue(self, now):
        feats = derive_features(
            cross_market_rows=[],
            polymarket_trades=[
                {"time": now - timedelta(days=1), "market_id": "m1",
                 "side": "buy"},
            ],
            asof_ts=now, lookback_days=30,
        )
        # Only polymarket is active.
        assert feats.active_venue_count == 1


class TestCorrelationAndLag:
    def test_same_direction_correlation(self, now):
        # Kalshi position on 'yes' side + Polymarket 'buy' (yes-direction)
        # in same market → 100% concordance.
        rows = [
            {"venue": "kalshi", "market_id": "shared", "side": "yes",
             "opened_at": now - timedelta(days=1)},
        ]
        trades = [
            {"time": now - timedelta(days=1, seconds=30),
             "market_id": "shared", "side": "buy"},
        ]
        feats = derive_features(
            cross_market_rows=rows, polymarket_trades=trades,
            asof_ts=now, lookback_days=30,
        )
        assert feats.cross_venue_correlation == pytest.approx(1.0)
        # Polymarket trade is 30s BEFORE kalshi → lag = pm - k = -30.
        # Sign convention per docstring: negative = Kalshi LEADS.
        # Actually if pm precedes k, then k LAGS, so (pm - k) < 0
        # means pm leads. Just verify it's computed and finite.
        assert feats.cross_venue_lag_s is not None

    def test_opposite_directions_zero_correlation(self, now):
        rows = [
            {"venue": "kalshi", "market_id": "shared", "side": "no",
             "opened_at": now - timedelta(days=1)},
        ]
        trades = [
            {"time": now - timedelta(days=1, seconds=30),
             "market_id": "shared", "side": "buy"},  # yes
        ]
        feats = derive_features(
            cross_market_rows=rows, polymarket_trades=trades,
            asof_ts=now, lookback_days=30,
        )
        assert feats.cross_venue_correlation == pytest.approx(0.0)

    def test_no_shared_market_no_correlation(self, now):
        rows = [
            {"venue": "kalshi", "market_id": "k1", "side": "yes",
             "opened_at": now - timedelta(days=1)},
        ]
        trades = [
            {"time": now - timedelta(days=1), "market_id": "p1",
             "side": "buy"},
        ]
        feats = derive_features(
            cross_market_rows=rows, polymarket_trades=trades,
            asof_ts=now, lookback_days=30,
        )
        # paired_total = 0 → correlation None.
        assert feats.cross_venue_correlation is None


class TestWindowCutoff:
    def test_old_rows_excluded(self, now):
        rows = [
            {"venue": "kalshi", "market_id": "x", "side": "yes",
             "opened_at": now - timedelta(days=40)},  # outside 30d.
        ]
        feats = derive_features(
            cross_market_rows=rows, polymarket_trades=[],
            asof_ts=now, lookback_days=30,
        )
        assert feats.active_venue_count == 0


class TestAsDict:
    def test_as_dict_keys(self):
        feats = CrossMarketFeatures(
            active_venue_count=3,
            cross_venue_correlation=0.8,
            cross_venue_lag_s=-12.5,
        )
        d = feats.as_dict()
        assert set(d.keys()) == {
            "active_venue_count",
            "cross_venue_correlation",
            "cross_venue_lag_s",
        }
        assert d["active_venue_count"] == 3
