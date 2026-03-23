import pytest

from app.ingestion.universe import MarketUniverse
from app.services.latency_arb_engine import (
    LatencyArbEngine,
    SpotPriceCache,
    SpotPriceData,
    TopOfBookData,
)


def test_evaluate_detects_buy_yes_when_spot_is_well_above_strike() -> None:
    now_ts = 1_710_000_000.0
    engine = LatencyArbEngine(now_fn=lambda: now_ts)
    market = MarketUniverse(
        market_id="m1",
        market_title="BTC above $105,000 by 3pm?",
        token_ids=["yes", "no"],
        expiry_ts=now_ts + (3 * 3600),
    )
    poly_book = TopOfBookData(best_bid=0.30, best_ask=0.34, updated_ts=now_ts - 0.4)
    spot_cache = SpotPriceCache(
        {"BTCUSDT": SpotPriceData(symbol="BTCUSDT", mid=112_000.0, updated_ts=now_ts - 0.05)}
    )

    signal = engine.evaluate(market, poly_book, spot_cache)

    assert signal is not None
    assert signal.direction == "BUY_YES"
    assert signal.market_id == "m1"
    assert signal.strike == 105_000.0
    assert signal.poly_mid == 0.32
    assert signal.fair_prob > signal.poly_mid
    assert signal.edge >= engine.cfg.min_edge
    assert signal.lag_ms == pytest.approx(350.0)
    assert 0.0 < signal.confidence <= 1.0


def test_evaluate_returns_none_when_spread_is_too_wide() -> None:
    now_ts = 1_710_000_000.0
    engine = LatencyArbEngine(now_fn=lambda: now_ts)
    market = MarketUniverse(
        market_id="m1",
        market_title="BTC above $105,000 by 3pm?",
        token_ids=["yes", "no"],
        expiry_ts=now_ts + (2 * 3600),
    )
    poly_book = TopOfBookData(best_bid=0.28, best_ask=0.37, updated_ts=now_ts - 0.4)
    spot_cache = SpotPriceCache(
        {"BTCUSDT": SpotPriceData(symbol="BTCUSDT", mid=112_000.0, updated_ts=now_ts - 0.05)}
    )

    assert engine.evaluate(market, poly_book, spot_cache) is None


def test_extract_strike_and_norm_cdf_guardrails() -> None:
    assert LatencyArbEngine.extract_strike("BTC above EUR?") is None
    assert LatencyArbEngine.extract_strike("BTC above €98,500 by 15h?") == 98_500.0
    assert LatencyArbEngine.norm_cdf(0.0) == pytest.approx(0.5, abs=1e-7)
    assert LatencyArbEngine.norm_cdf(1.0) == pytest.approx(0.841344746, abs=1e-6)
