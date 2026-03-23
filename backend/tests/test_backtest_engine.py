from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import stdev

import pytest

from app.services.backtest_engine import BacktestEngine, BacktestTrade


def test_build_result_computes_win_rate_and_sharpe_from_three_trades() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    trades = [
        BacktestTrade(
            market_id="m1",
            question="Trade 1",
            strategy="adaptive",
            side="BUY_YES",
            token_id="tok-1",
            entry_time=start,
            exit_time=start + timedelta(hours=4),
            entry_price=0.45,
            exit_price=0.60,
            size=100.0,
            notional=45.0,
            fees=0.1,
            pnl=100.0,
            pnl_pct=10.0,
            expected_edge=0.03,
            risk_pct=1.0,
            duration_h=4.0,
            resolved_outcome="YES",
            settlement="resolved",
        ),
        BacktestTrade(
            market_id="m2",
            question="Trade 2",
            strategy="adaptive",
            side="BUY_NO",
            token_id="tok-2",
            entry_time=start + timedelta(days=1),
            exit_time=start + timedelta(days=1, hours=3),
            entry_price=0.55,
            exit_price=0.30,
            size=90.0,
            notional=49.5,
            fees=0.1,
            pnl=-50.0,
            pnl_pct=-5.0,
            expected_edge=0.02,
            risk_pct=1.0,
            duration_h=3.0,
            resolved_outcome="YES",
            settlement="resolved",
        ),
        BacktestTrade(
            market_id="m3",
            question="Trade 3",
            strategy="adaptive",
            side="BUY_YES",
            token_id="tok-3",
            entry_time=start + timedelta(days=2),
            exit_time=start + timedelta(days=2, hours=2),
            entry_price=0.35,
            exit_price=0.80,
            size=120.0,
            notional=42.0,
            fees=0.1,
            pnl=150.0,
            pnl_pct=15.0,
            expected_edge=0.05,
            risk_pct=1.0,
            duration_h=2.0,
            resolved_outcome="YES",
            settlement="resolved",
        ),
    ]
    equity_curve = [
        {"timestamp": start, "equity": 1000.0, "drawdown_pct": 0.0},
        {"timestamp": start + timedelta(hours=23), "equity": 1100.0, "drawdown_pct": 0.0},
        {"timestamp": start + timedelta(days=1), "equity": 1100.0, "drawdown_pct": 0.0},
        {"timestamp": start + timedelta(days=1, hours=23), "equity": 990.0, "drawdown_pct": 10.0},
        {"timestamp": start + timedelta(days=2), "equity": 990.0, "drawdown_pct": 10.0},
        {"timestamp": start + timedelta(days=2, hours=23), "equity": 1188.0, "drawdown_pct": 0.0},
    ]

    result = BacktestEngine.build_result(
        trades=trades,
        equity_curve=equity_curve,
        initial_equity=1000.0,
    )

    daily_returns = [0.10, -0.10, 0.20]
    expected_sharpe = (sum(daily_returns) / len(daily_returns)) / stdev(daily_returns) * sqrt(252)

    assert result.total_trades == 3
    assert result.winning_trades == 2
    assert result.win_rate == pytest.approx(66.666667, rel=1e-6)
    assert result.sharpe_ratio == pytest.approx(expected_sharpe, rel=1e-6)
