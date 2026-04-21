from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.backtest.baselines import run_required_baselines
from src.backtest.engine import LeaderSwingBacktester
from src.backtest.models import BacktestBookSnapshot, BacktestCandle, BacktestMarket, BacktestTrade
from src.backtest.report import build_gate_report
from src.economics.models import ECONOMIC_MODEL_VERSION, FeeSnapshot, StrategyTrack


def _fixture_data():
    t0 = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    market = BacktestMarket(
        market_id="m1",
        question="Will BTC close above target?",
        category="crypto",
        yes_token_id="yes1",
        no_token_id="no1",
        volume_usdc=Decimal("100000"),
        fee_snapshot=FeeSnapshot(
            market_id="m1",
            token_id="yes1",
            fee_enabled=True,
            fee_rate=Decimal("0.04"),
            source="unit",
            captured_at=t0,
        ),
    )
    trades = [
        BacktestTrade(
            leader_wallet="0xleader",
            market_id="m1",
            token_id="yes1",
            side="BUY",
            outcome="YES",
            price=Decimal("0.40"),
            size_shares=Decimal("100"),
            event_ts=t0,
            observed_ts=t0 + timedelta(seconds=10),
            tx_hash="entry",
        ),
        BacktestTrade(
            leader_wallet="0xleader",
            market_id="m1",
            token_id="yes1",
            side="SELL",
            outcome="YES",
            price=Decimal("0.55"),
            size_shares=Decimal("100"),
            event_ts=t0 + timedelta(hours=2),
            observed_ts=t0 + timedelta(hours=2, seconds=10),
            tx_hash="exit",
        ),
    ]
    books = [
        BacktestBookSnapshot("m1", "yes1", Decimal("0.39"), Decimal("0.41"), t0, "book"),
        BacktestBookSnapshot(
            "m1", "yes1", Decimal("0.54"), Decimal("0.56"), t0 + timedelta(hours=2), "book"
        ),
    ]
    return [market], trades, books


def _fixture_without_books():
    markets, trades, _ = _fixture_data()
    return markets, trades, []


def test_leader_swing_backtester_produces_versioned_net_fills():
    markets, trades, books = _fixture_data()
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)

    run = backtester.run(markets=markets, trades=trades, books=books, policy="follow_all")

    assert len(run.fills) == 1
    fill = run.fills[0]
    assert fill.strategy_track == StrategyTrack.LEADER_SWING
    assert fill.economic_model_version == ECONOMIC_MODEL_VERSION
    assert fill.signal_audit["accepted"] is True
    assert fill.net_pnl_usdc < fill.gross_pnl_usdc
    assert fill.net_pnl_usdc > Decimal("0")
    assert run.metrics["total_trades"] == 1
    assert run.metrics["net_pnl_usdc"] == fill.net_pnl_usdc


def test_required_baselines_return_comparable_metrics():
    markets, trades, books = _fixture_data()
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)

    baselines = run_required_baselines(
        backtester=backtester,
        markets=markets,
        trades=trades,
        books=books,
    )

    assert set(baselines) == {"follow_all", "fade_all", "random_seeded", "liquid_markets_only"}
    assert baselines["follow_all"].metrics["total_trades"] == 1
    assert baselines["fade_all"].metrics["net_pnl_usdc"] < Decimal("0")


def test_fade_uses_opposite_token_from_leader_token():
    markets, trades, books = _fixture_data()
    side_b_entry = type(trades[0])(
        **{
            **trades[0].__dict__,
            "token_id": "no1",
            "outcome": "NO",
        }
    )
    side_b_exit = type(trades[1])(
        **{
            **trades[1].__dict__,
            "token_id": "no1",
            "outcome": "NO",
        }
    )
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)

    run = backtester.run(
        markets=markets,
        trades=[side_b_entry, side_b_exit],
        books=books,
        policy="fade_all",
    )

    assert run.fills[0].token_id == "yes1"


def test_gate_report_requires_positive_net_sharpe_and_baseline_edge():
    markets, trades, books = _fixture_data()
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)
    primary = backtester.run(markets=markets, trades=trades, books=books, policy="follow_all")
    baselines = run_required_baselines(backtester, markets, trades, books)

    report = build_gate_report(primary, baselines)

    assert report["strategy_track"] == "leader_swing"
    assert report["economic_model_version"] == ECONOMIC_MODEL_VERSION
    assert report["gate_passed"] is True
    assert report["gate_status"] == "PASS"
    assert "baseline_comparison" in report
    assert report["markdown"].startswith("# Leader Swing Gate Report")


def test_gate_report_marks_data_insufficient_when_constant_fallback_dominates():
    markets, trades, books = _fixture_without_books()
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)
    primary = backtester.run(markets=markets, trades=trades, books=books, policy="follow_all")
    baselines = run_required_baselines(backtester, markets, trades, books)

    report = build_gate_report(primary, baselines)

    assert report["gate_passed"] is False
    assert report["gate_status"] == "DATA_INSUFFICIENT"
    assert report["data_quality"]["constant_fallback_fill_pct"] == Decimal("1")
    assert report["score_metrics"]["total_trades"] == 0


def test_gate_report_accepts_candle_cost_source_when_books_are_missing():
    markets, trades, _ = _fixture_data()
    t0 = trades[0].event_ts
    candles = [
        BacktestCandle(
            market_id="m1",
            token_id="yes1",
            start_ts=t0 - timedelta(minutes=5),
            end_ts=t0 + timedelta(minutes=5),
            high=Decimal("0.43"),
            low=Decimal("0.39"),
            source="falcon_568",
        ),
        BacktestCandle(
            market_id="m1",
            token_id="yes1",
            start_ts=trades[1].event_ts - timedelta(minutes=5),
            end_ts=trades[1].event_ts + timedelta(minutes=5),
            high=Decimal("0.57"),
            low=Decimal("0.53"),
            source="falcon_568",
        ),
    ]
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)

    primary = backtester.run(
        markets=markets,
        trades=trades,
        books=[],
        candles=candles,
        policy="follow_all",
    )
    baselines = run_required_baselines(backtester, markets, trades, [])
    report = build_gate_report(primary, baselines)

    assert primary.fills[0].cost_sources["entry_spread"] == "candle"
    assert report["data_quality"]["real_cost_source_fill_pct"] == Decimal("1")
    assert report["data_quality"]["constant_fallback_fill_pct"] == Decimal("0")


def test_gate_report_excludes_constant_fallback_fills_from_primary_score():
    markets, trades, books = _fixture_data()
    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)
    book_run = backtester.run(markets=markets, trades=trades, books=books, policy="follow_all")
    fallback_run = backtester.run(markets=markets, trades=trades, books=[], policy="follow_all")
    mixed_run = type(book_run)(
        strategy_track=book_run.strategy_track,
        policy=book_run.policy,
        fills=[book_run.fills[0], fallback_run.fills[0]],
        metrics=book_run.metrics | {"total_trades": 2},
        economic_model_version=book_run.economic_model_version,
    )
    baselines = run_required_baselines(backtester, markets, trades, books)

    report = build_gate_report(mixed_run, baselines)

    assert report["metrics"]["total_trades"] == 2
    assert report["score_metrics"]["total_trades"] == 1
    assert report["data_quality"]["constant_fallback_fill_pct"] == Decimal("0.5")
