from collections.abc import Iterable

from src.backtest.engine import LeaderSwingBacktester
from src.backtest.models import (
    BacktestBookSnapshot,
    BacktestCandle,
    BacktestMarket,
    BacktestRun,
    BacktestTrade,
)

REQUIRED_BASELINES = ("follow_all", "fade_all", "random_seeded", "liquid_markets_only")


def run_required_baselines(
    backtester: LeaderSwingBacktester,
    markets: Iterable[BacktestMarket],
    trades: Iterable[BacktestTrade],
    books: Iterable[BacktestBookSnapshot],
    candles: Iterable[BacktestCandle] = (),
) -> dict[str, BacktestRun]:
    market_rows = list(markets)
    trade_rows = list(trades)
    book_rows = list(books)
    candle_rows = list(candles)
    return {
        policy: backtester.run(
            markets=market_rows,
            trades=trade_rows,
            books=book_rows,
            candles=candle_rows,
            policy=policy,
        )
        for policy in REQUIRED_BASELINES
    }
