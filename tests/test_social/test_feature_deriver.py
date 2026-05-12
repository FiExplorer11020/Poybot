"""Per-wallet social feature derivation tests.

Coverage:
  * Empty input returns zeros / Nones (not crash).
  * Density + per-active-day computed correctly.
  * Tweet-to-trade lag has the correct sign (negative = tweet first).
  * Strategy concordance counts entry/buy + exit/sell as concordant.
  * Window cutoff is honored.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.social.feature_deriver import (
    SocialFeatures,
    derive_features,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


class TestEmptyInput:
    def test_empty_signals_returns_zero_density(self, now):
        feats = derive_features(
            signals=[], trades=[], asof_ts=now, lookback_days=30,
        )
        assert feats.social_signal_density == 0.0
        assert feats.tweets_per_active_day == 0.0
        assert feats.tweet_to_trade_lag_median_s is None
        assert feats.social_signal_strategy_concordance is None


class TestDensity:
    def test_density_counts_all_in_window(self, now):
        signals = [
            {
                "posted_at": now - timedelta(days=d),
                "intent": "noise",
                "intent_confidence": 0.6,
                "parsed_direction": None,
            }
            for d in range(5)
        ]
        feats = derive_features(
            signals=signals, trades=[], asof_ts=now, lookback_days=30,
        )
        # 5 tweets / 30 days = 0.166...
        assert feats.social_signal_density == pytest.approx(5 / 30.0)

    def test_window_cutoff_honored(self, now):
        signals = [
            # Inside window.
            {"posted_at": now - timedelta(days=1), "intent": "noise",
             "intent_confidence": 0.6, "parsed_direction": None},
            # Outside (40 days ago, lookback is 30).
            {"posted_at": now - timedelta(days=40), "intent": "noise",
             "intent_confidence": 0.6, "parsed_direction": None},
        ]
        feats = derive_features(
            signals=signals, trades=[], asof_ts=now, lookback_days=30,
        )
        assert feats.social_signal_density == pytest.approx(1 / 30.0)


class TestActiveDayRate:
    def test_per_active_day_uses_non_noise_count(self, now):
        signals = [
            {"posted_at": now - timedelta(days=1, hours=1),
             "intent": "entry_signal", "intent_confidence": 0.85,
             "parsed_direction": "yes"},
            {"posted_at": now - timedelta(days=1, hours=2),
             "intent": "entry_signal", "intent_confidence": 0.85,
             "parsed_direction": "yes"},
            {"posted_at": now - timedelta(days=3, hours=2),
             "intent": "exit_signal", "intent_confidence": 0.85,
             "parsed_direction": "no"},
            {"posted_at": now - timedelta(days=1, hours=3),
             "intent": "noise", "intent_confidence": 0.6,
             "parsed_direction": None},
        ]
        feats = derive_features(
            signals=signals, trades=[], asof_ts=now, lookback_days=30,
        )
        # Non-noise signals across 2 distinct days: 3 / 2 = 1.5.
        assert feats.tweets_per_active_day == pytest.approx(1.5)


class TestLagAndConcordance:
    def test_negative_lag_when_tweet_precedes_trade(self, now):
        # Tweet at t-10s, trade at t. Expect lag = -10s (tweet first).
        tweet_ts = now - timedelta(seconds=10)
        trade_ts = now
        signals = [
            {"posted_at": tweet_ts, "intent": "entry_signal",
             "intent_confidence": 0.85, "parsed_direction": "yes"},
        ]
        trades = [
            {"time": trade_ts, "market_id": "m1", "side": "buy",
             "token_id": "tok-yes"},
        ]
        feats = derive_features(
            signals=signals, trades=trades, asof_ts=now,
            lookback_days=30, concordance_window_s=3600,
        )
        # tweet_ts - trade_ts = -10 s.
        assert feats.tweet_to_trade_lag_median_s == pytest.approx(-10.0)

    def test_concordance_entry_signal_with_buy(self, now):
        tweet_ts = now - timedelta(seconds=30)
        signals = [
            {"posted_at": tweet_ts, "intent": "entry_signal",
             "intent_confidence": 0.85, "parsed_direction": "yes"},
        ]
        trades = [
            {"time": now, "market_id": "m1", "side": "buy",
             "token_id": "tok-yes"},
        ]
        feats = derive_features(
            signals=signals, trades=trades, asof_ts=now,
            lookback_days=30, concordance_window_s=3600,
        )
        assert feats.social_signal_strategy_concordance == pytest.approx(1.0)

    def test_concordance_exit_signal_with_sell(self, now):
        signals = [
            {"posted_at": now - timedelta(seconds=10),
             "intent": "exit_signal", "intent_confidence": 0.85,
             "parsed_direction": "no"},
        ]
        trades = [
            {"time": now, "market_id": "m1", "side": "sell",
             "token_id": "tok-no"},
        ]
        feats = derive_features(
            signals=signals, trades=trades, asof_ts=now,
            lookback_days=30, concordance_window_s=3600,
        )
        # exit_signal + sell = concordant.
        assert feats.social_signal_strategy_concordance == pytest.approx(1.0)

    def test_no_paired_trades_means_none(self, now):
        signals = [
            {"posted_at": now - timedelta(days=1),
             "intent": "entry_signal", "intent_confidence": 0.85,
             "parsed_direction": "yes"},
        ]
        feats = derive_features(
            signals=signals, trades=[], asof_ts=now,
            lookback_days=30, concordance_window_s=3600,
        )
        assert feats.tweet_to_trade_lag_median_s is None
        assert feats.social_signal_strategy_concordance is None

    def test_pair_outside_concordance_window_dropped(self, now):
        # Tweet 2 hours before trade; window is 1h → dropped.
        signals = [
            {"posted_at": now - timedelta(hours=2),
             "intent": "entry_signal", "intent_confidence": 0.85,
             "parsed_direction": "yes"},
        ]
        trades = [
            {"time": now, "market_id": "m1", "side": "buy",
             "token_id": "t1"},
        ]
        feats = derive_features(
            signals=signals, trades=trades, asof_ts=now,
            lookback_days=30, concordance_window_s=3600,
        )
        assert feats.tweet_to_trade_lag_median_s is None


class TestFeaturesAsDict:
    def test_as_dict_keys(self):
        feats = SocialFeatures(
            social_signal_density=1.0,
            tweets_per_active_day=2.0,
            tweet_to_trade_lag_median_s=-5.0,
            social_signal_strategy_concordance=0.8,
        )
        d = feats.as_dict()
        assert set(d.keys()) == {
            "social_signal_density",
            "tweets_per_active_day",
            "tweet_to_trade_lag_median_s",
            "social_signal_strategy_concordance",
        }
