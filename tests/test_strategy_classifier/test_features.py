"""Unit tests for LeaderFeatureExtractor.

Cover:

* The vector shape is exactly FEATURE_COUNT (=45 after R12; was 42 pre-R12).
* asof_ts is honored — trades AFTER asof are not included.
* Microstructure / social / cross-market slots are np.nan when upstream
  sources are missing; the extractor doesn't crash.
* PENDING_FEATURE_NAMES is preserved (R10/R11/R12 wiring is additive).
* H. SOCIAL slots (35-38) populate from get_social_signals_asof.
* J. CROSS_MARKET slots (42-44) populate from
  get_cross_market_features_asof.
"""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from src.strategy_classifier.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    PENDING_FEATURE_NAMES,
    LeaderFeatureExtractor,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mock_conn_with_trades_positions(trades, positions, edges):
    """Returns a context-manager factory yielding a mock asyncpg conn."""

    conn = AsyncMock()

    async def _fetch(query, *args):
        q = " ".join(query.split())
        if "FROM trades_observed" in q:
            return trades
        if "FROM positions_reconstructed" in q:
            return positions
        if "FROM follower_edges" in q:
            return edges
        return []

    conn.fetch = _fetch

    async def _fetchrow(query, *args):
        return None

    conn.fetchrow = _fetchrow

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestFeatureSchema:
    def test_feature_count_is_45(self):
        # 42 (R8) + 3 (R12 J. CROSS_MARKET) = 45.
        assert FEATURE_COUNT == 45

    def test_feature_names_unique(self):
        assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))

    def test_pending_features_are_subset(self):
        for name in PENDING_FEATURE_NAMES:
            assert name in FEATURE_NAMES

    def test_feature_categories_cover_a_through_j(self):
        # R12 appends J. CROSS_MARKET; the prefix set widens.
        prefixes = set(name.split("_", 1)[0] for name in FEATURE_NAMES)
        assert prefixes == {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j"}

    def test_cross_market_slots_present(self):
        for name in (
            "j_active_venue_count",
            "j_cross_venue_correlation",
            "j_cross_venue_lag_s",
        ):
            assert name in FEATURE_NAMES


class TestExtractShape:
    @pytest.mark.asyncio
    async def test_empty_wallet_returns_full_shape(self, now):
        """No trades, no positions — still returns 42 values, mostly nan."""
        ctx, _ = _mock_conn_with_trades_positions([], [], [])
        with patch("src.strategy_classifier.features.get_db", side_effect=ctx):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        assert fv.values.shape == (FEATURE_COUNT,)
        # `missing` recorded any structural slot we never populated.
        assert len(fv.missing) > 0

    @pytest.mark.asyncio
    async def test_basic_velocity_features(self, now):
        """Synthetic trades produce sane velocity features."""
        trades = [
            {
                "time": now - timedelta(days=d, hours=h),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
            for d in range(5) for h in range(2)
        ]
        positions = []
        edges = []
        ctx, _ = _mock_conn_with_trades_positions(trades, positions, edges)
        with patch("src.strategy_classifier.features.get_db", side_effect=ctx):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        # a_trades_per_day: 10 trades / 30-day lookback ≈ 0.33
        assert fv.values[0] == pytest.approx(10 / 30.0)
        # a_active_day_fraction: 5 unique days / 30
        assert fv.values[4] == pytest.approx(5 / 30.0)

    @pytest.mark.asyncio
    async def test_holding_period_features(self, now):
        positions = [
            {
                "open_time": now - timedelta(days=2),
                "close_time": now - timedelta(days=1),
                "entry_price": 0.5,
                "exit_price": 0.6,
                "size_usdc": 100.0,
                "holding_period_s": 86400,
                "close_method": "sell",
                "pnl_usdc": 20.0,
                "market_id": "m1",
                "token_id": "t1",
            }
            for _ in range(3)
        ]
        ctx, _ = _mock_conn_with_trades_positions([], positions, [])
        with patch("src.strategy_classifier.features.get_db", side_effect=ctx):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        # b_holding_period_median_s
        assert fv.values[5] == pytest.approx(86400.0)
        # b_close_method_sell_share == 1.0
        assert fv.values[8] == pytest.approx(1.0)
        # fraction_closed_within_1h == 0
        assert fv.values[9] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_microstructure_features_missing_when_no_orderbook(self, now):
        """Microstructure (E) and Social (H) slots are nan when upstream
        sources aren't wired. This is the 'structural slot is preserved'
        contract."""
        trades = [
            {
                "time": now - timedelta(days=1),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
        ]
        ctx, _ = _mock_conn_with_trades_positions(trades, [], [])

        async def _no_orderbook(*args, **kwargs):
            return None

        with patch("src.strategy_classifier.features.get_db", side_effect=ctx), \
             patch(
                 "src.strategy_classifier.features.get_orderbook_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_microstructure_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_wallet_microstructure_signature_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_social_signals_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_cross_market_features_asof",
                 side_effect=_no_orderbook,
             ):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        # E-category slots all nan (R11)
        for name in (
            "e_microprice_deviation_at_entry_median",
            "e_spread_bps_at_entry_median",
            "e_depth_imbalance_at_entry_median",
        ):
            i = FEATURE_NAMES.index(name)
            assert math.isnan(fv.values[i])
            assert name in fv.missing
        # H-category social slots all nan (R12)
        for name in (
            "h_social_signal_density",
            "h_tweets_per_active_day",
            "h_tweet_to_trade_lag_median_s",
            "h_social_signal_strategy_concordance",
        ):
            i = FEATURE_NAMES.index(name)
            assert math.isnan(fv.values[i])
        # J-category cross-market slots all nan (R12)
        for name in (
            "j_active_venue_count",
            "j_cross_venue_correlation",
            "j_cross_venue_lag_s",
        ):
            i = FEATURE_NAMES.index(name)
            assert math.isnan(fv.values[i])

    @pytest.mark.asyncio
    async def test_microstructure_wallet_signature_populates_slots(self, now):
        """When the R11 wallet signature is populated, slots 25
        (e_cancel_to_fill_ratio_30d) and 26 (e_takes_vs_makes_ratio)
        carry real numbers — not np.nan. This is the headline R11
        acceptance criterion (3pp R8 accuracy gate depends on these
        new dimensions). Slot SHAPE is preserved; only values change.
        """
        trades = [
            {
                "time": now - timedelta(days=1),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
        ]
        ctx, _ = _mock_conn_with_trades_positions(trades, [], [])

        async def _orderbook(*args, **kwargs):
            return None

        async def _microstructure(*args, **kwargs):
            return None

        async def _wallet_sig(*args, **kwargs):
            return {
                "rollup_at": now - timedelta(hours=1),
                "cancel_to_fill_ratio_30d": 2.5,
                "iceberg_score_30d": 0.1,
                "spoof_score_30d": 0.05,
                "place_to_fill_seconds_p50": 30.0,
                "place_to_fill_seconds_p99": 600.0,
                "n_orders_30d": 200,
                "n_fills_30d": 80,
            }

        with patch("src.strategy_classifier.features.get_db", side_effect=ctx), \
             patch(
                 "src.strategy_classifier.features.get_orderbook_features_asof",
                 side_effect=_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_microstructure_features_asof",
                 side_effect=_microstructure,
             ), patch(
                 "src.strategy_classifier.features.get_wallet_microstructure_signature_asof",
                 side_effect=_wallet_sig,
             ):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        # Slot 25 (e_cancel_to_fill_ratio_30d) → real number, not nan.
        i_c2f = FEATURE_NAMES.index("e_cancel_to_fill_ratio_30d")
        assert not math.isnan(fv.values[i_c2f])
        assert fv.values[i_c2f] == pytest.approx(2.5)
        assert "e_cancel_to_fill_ratio_30d" not in fv.missing
        # Slot 26 (e_takes_vs_makes_ratio) → 80/200 = 0.4.
        i_tvm = FEATURE_NAMES.index("e_takes_vs_makes_ratio")
        assert not math.isnan(fv.values[i_tvm])
        assert fv.values[i_tvm] == pytest.approx(0.4)
        # Vector shape is preserved.
        assert fv.values.shape == (FEATURE_COUNT,)

    @pytest.mark.asyncio
    async def test_microstructure_per_token_rollup_populates_book_age(self, now):
        """When the R11 microstructure rollup returns features-via-
        orderbook (depth/spread/microprice), slot 24
        (e_book_age_ms_at_entry_median) is populated from the rollup
        age — values change from nan to a real number. Slot shape
        preserved."""
        trades = [
            {
                "time": now - timedelta(days=1),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
        ]
        ctx, _ = _mock_conn_with_trades_positions(trades, [], [])

        async def _orderbook(*args, **kwargs):
            return {
                "bucket_ts": now - timedelta(days=1),
                "depth_imbalance_mean": 0.1,
                "spread_bps_mean": 5.0,
                "microprice_deviation_mean": 0.002,
                "feature_age_s": 12.0,
            }

        async def _microstructure(*args, **kwargs):
            return None

        async def _wallet_sig(*args, **kwargs):
            return None

        with patch("src.strategy_classifier.features.get_db", side_effect=ctx), \
             patch(
                 "src.strategy_classifier.features.get_orderbook_features_asof",
                 side_effect=_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_microstructure_features_asof",
                 side_effect=_microstructure,
             ), patch(
                 "src.strategy_classifier.features.get_wallet_microstructure_signature_asof",
                 side_effect=_wallet_sig,
             ):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        i_book_age = FEATURE_NAMES.index("e_book_age_ms_at_entry_median")
        assert not math.isnan(fv.values[i_book_age])
        # 12 s of age → 12000 ms.
        assert fv.values[i_book_age] == pytest.approx(12000.0)
        assert fv.values.shape == (FEATURE_COUNT,)

    @pytest.mark.asyncio
    async def test_social_slots_populate_when_signals_present(self, now):
        """R12 wiring: when get_social_signals_asof returns a populated
        dict, the H. SOCIAL slots (35-38) carry real values."""
        trades = [
            {
                "time": now - timedelta(days=1),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
        ]
        ctx, _ = _mock_conn_with_trades_positions(trades, [], [])

        async def _no_orderbook(*args, **kwargs):
            return None

        async def _social(*args, **kwargs):
            return {
                "social_signal_density": 0.5,
                "tweets_per_active_day": 1.2,
                "tweet_to_trade_lag_median_s": -25.0,
                "social_signal_strategy_concordance": 0.8,
            }

        async def _cross_market(*args, **kwargs):
            return None

        with patch("src.strategy_classifier.features.get_db", side_effect=ctx), \
             patch(
                 "src.strategy_classifier.features.get_orderbook_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_microstructure_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_wallet_microstructure_signature_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_social_signals_asof",
                 side_effect=_social,
             ), patch(
                 "src.strategy_classifier.features.get_cross_market_features_asof",
                 side_effect=_cross_market,
             ):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        # All four H. SOCIAL slots carry real values, none in missing.
        i_d = FEATURE_NAMES.index("h_social_signal_density")
        i_tpd = FEATURE_NAMES.index("h_tweets_per_active_day")
        i_lag = FEATURE_NAMES.index("h_tweet_to_trade_lag_median_s")
        i_conc = FEATURE_NAMES.index("h_social_signal_strategy_concordance")
        assert fv.values[i_d] == pytest.approx(0.5)
        assert fv.values[i_tpd] == pytest.approx(1.2)
        assert fv.values[i_lag] == pytest.approx(-25.0)
        assert fv.values[i_conc] == pytest.approx(0.8)
        for name in (
            "h_social_signal_density",
            "h_tweets_per_active_day",
            "h_tweet_to_trade_lag_median_s",
            "h_social_signal_strategy_concordance",
        ):
            assert name not in fv.missing

    @pytest.mark.asyncio
    async def test_cross_market_slots_populate_when_present(self, now):
        """R12 wiring: J. CROSS_MARKET slots (42-44) carry real values
        when the reader returns a populated dict."""
        trades = [
            {
                "time": now - timedelta(days=1),
                "market_id": "m1",
                "token_id": "t1",
                "side": "buy",
                "price": 0.5,
                "size_usdc": 100.0,
                "category": "crypto",
            }
        ]
        ctx, _ = _mock_conn_with_trades_positions(trades, [], [])

        async def _no_orderbook(*args, **kwargs):
            return None

        async def _social(*args, **kwargs):
            return None

        async def _cross_market(*args, **kwargs):
            return {
                "active_venue_count": 3,
                "cross_venue_correlation": 0.75,
                "cross_venue_lag_s": -12.5,
            }

        with patch("src.strategy_classifier.features.get_db", side_effect=ctx), \
             patch(
                 "src.strategy_classifier.features.get_orderbook_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_microstructure_features_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_wallet_microstructure_signature_asof",
                 side_effect=_no_orderbook,
             ), patch(
                 "src.strategy_classifier.features.get_social_signals_asof",
                 side_effect=_social,
             ), patch(
                 "src.strategy_classifier.features.get_cross_market_features_asof",
                 side_effect=_cross_market,
             ):
            ext = LeaderFeatureExtractor()
            fv = await ext.extract("0xabc", now)
        i_avc = FEATURE_NAMES.index("j_active_venue_count")
        i_corr = FEATURE_NAMES.index("j_cross_venue_correlation")
        i_lag = FEATURE_NAMES.index("j_cross_venue_lag_s")
        assert fv.values[i_avc] == pytest.approx(3.0)
        assert fv.values[i_corr] == pytest.approx(0.75)
        assert fv.values[i_lag] == pytest.approx(-12.5)
        for name in (
            "j_active_venue_count",
            "j_cross_venue_correlation",
            "j_cross_venue_lag_s",
        ):
            assert name not in fv.missing

    @pytest.mark.asyncio
    async def test_asof_in_past_is_used_in_query(self, now):
        """The query MUST cap at asof_ts. We mock the fetch and assert
        the args contain (floor, asof) — not (floor, now).
        """
        captured_args: list = []

        conn = AsyncMock()

        async def _fetch(query, *args):
            captured_args.append((query, args))
            return []

        conn.fetch = _fetch
        conn.fetchrow = AsyncMock(return_value=None)

        @asynccontextmanager
        async def _ctx():
            yield conn

        past_asof = now - timedelta(days=10)
        with patch("src.strategy_classifier.features.get_db", side_effect=_ctx):
            ext = LeaderFeatureExtractor()
            await ext.extract("0xabc", past_asof)

        # At least the trades query should have captured (wallet, floor, asof)
        # where asof == past_asof, not now.
        found_trades_query = False
        for q, args in captured_args:
            if "trades_observed" in q:
                found_trades_query = True
                # args is (wallet, floor, asof)
                assert args[2] == past_asof
        assert found_trades_query, "Expected at least one trades_observed fetch"
