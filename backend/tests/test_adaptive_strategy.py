from app.services.adaptive_strategy import AdaptiveStrategyEngine, PortfolioState


def test_probability_bounds_and_detection_shape() -> None:
    engine = AdaptiveStrategyEngine()
    out = engine.evaluate_market("m1", best_bid=-1, best_ask=5)
    assert 0.01 <= out["best_bid"] <= 0.99
    assert 0.01 <= out["best_ask"] <= 0.99
    assert out["best_ask"] >= out["best_bid"]
    assert "detected" in out


def test_position_sizing_respects_exposure_cap() -> None:
    engine = AdaptiveStrategyEngine()
    p = PortfolioState(equity=10_000, capital_in_trade=3_950, total_pnl=0)
    notional, risk_pct = engine.size_position(p, expected_edge=0.02)
    assert notional <= 50
    assert risk_pct <= 1
