from app.services.adaptive_strategy import AdaptiveStrategyEngine, MarketRegime, PortfolioState


def _feed_market(
    engine: AdaptiveStrategyEngine, market_id: str, mids: list[float], spread: float
) -> dict:
    out = {}
    half_spread = spread / 2
    for mid in mids:
        out = engine.evaluate_market(
            market_id,
            best_bid=mid - half_spread,
            best_ask=mid + half_spread,
        )
    return out


def test_probability_bounds_and_detection_shape() -> None:
    engine = AdaptiveStrategyEngine()
    out = engine.evaluate_market("m1", best_bid=-1, best_ask=5)
    assert 0.01 <= out["best_bid"] <= 0.99
    assert 0.01 <= out["best_ask"] <= 0.99
    assert out["best_ask"] >= out["best_bid"]
    assert "detected" in out
    assert out["observations"] == 1
    assert out["detected"] is False


def test_detection_requires_multiple_samples() -> None:
    engine = AdaptiveStrategyEngine()
    outputs = [
        engine.evaluate_market("m1", best_bid=0.48 + idx * 0.002, best_ask=0.49 + idx * 0.002)
        for idx in range(7)
    ]
    assert all(out["detected"] is False for out in outputs)


def test_position_sizing_respects_exposure_cap() -> None:
    engine = AdaptiveStrategyEngine()
    p = PortfolioState(equity=10_000, capital_in_trade=3_950, total_pnl=0)
    notional, risk_pct = engine.size_position(p, expected_edge=0.02)
    assert notional <= 50
    assert risk_pct <= 1


def test_low_vol_regime_reduces_entry_threshold() -> None:
    engine = AdaptiveStrategyEngine()
    out = _feed_market(engine, "calm", [0.5000, 0.5004, 0.5008, 0.5012], spread=0.01)

    volatility = engine._rolling_volatility(engine._series["calm"])
    trading_cost = out["spread"] + (engine.cfg.fee_bps / 10_000)
    base_threshold = max(engine.cfg.base_entry_threshold, trading_cost * 1.1 + (volatility * 0.3))

    assert engine.classify_regime("calm") == MarketRegime.LOW_VOL
    assert MarketRegime(out["regime"]) == MarketRegime.LOW_VOL
    assert out["entry_threshold"] == round(base_threshold * 0.7, 6)
    assert out["entry_threshold"] < round(base_threshold, 6)


def test_crisis_regime_blocks_detection() -> None:
    engine = AdaptiveStrategyEngine()
    out = _feed_market(engine, "panic", [0.50, 0.56, 0.48, 0.57, 0.47, 0.60], spread=0.002)

    assert engine.classify_regime("panic") == MarketRegime.CRISIS
    assert MarketRegime(out["regime"]) == MarketRegime.CRISIS
    assert out["observations"] >= engine.cfg.min_observations
    assert out["signal_strength"] > engine.cfg.min_signal_strength
    assert out["detected"] is False
    assert out["direction"] == "HOLD"


def test_portfolio_regime_returns_most_severe_market() -> None:
    engine = AdaptiveStrategyEngine()
    _feed_market(engine, "calm", [0.5000, 0.5004, 0.5008, 0.5012], spread=0.01)
    _feed_market(engine, "steady", [0.50, 0.506, 0.499, 0.508, 0.500], spread=0.01)
    _feed_market(engine, "panic", [0.50, 0.56, 0.48, 0.57, 0.47, 0.60], spread=0.002)

    assert engine.portfolio_regime(["calm", "steady", "panic"]) == MarketRegime.CRISIS
