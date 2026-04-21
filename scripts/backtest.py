#!/usr/bin/env python
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.backtest.baselines import run_required_baselines
from src.backtest.data_loader import HistoricalFalconLoader
from src.backtest.engine import LeaderSwingBacktester
from src.backtest.models import BacktestBookSnapshot, BacktestCandle, BacktestMarket, BacktestTrade
from src.backtest.report import build_gate_report, write_gate_report
from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.economics.models import FeeSnapshot
from src.registry.falcon_client import FalconClient


def _mock_fixture():
    t0 = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    markets = [
        BacktestMarket(
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
                source="mock",
                captured_at=t0,
            ),
        )
    ]
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
        BacktestBookSnapshot("m1", "yes1", Decimal("0.39"), Decimal("0.41"), t0, "mock"),
        BacktestBookSnapshot(
            "m1",
            "yes1",
            Decimal("0.54"),
            Decimal("0.56"),
            t0 + timedelta(hours=2),
            "mock",
        ),
    ]
    return markets, trades, books


def _parse_date(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _make_loader(
    cache_dir: str,
    *,
    refresh: bool = False,
    page_limit: int = 200,
    max_pages: int = 20,
    max_markets: int | None = None,
    max_tokens: int | None = None,
) -> HistoricalFalconLoader:
    return HistoricalFalconLoader(
        client=FalconClient(max_rpm=60),
        cache_dir=cache_dir,
        page_limit=page_limit,
        max_pages=max_pages,
        max_markets=max_markets,
        max_tokens=max_tokens,
        refresh=refresh,
    )


async def _load_top_leader_wallets(limit: int) -> list[str]:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=1,
        max_size=2,
    )
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT wallet_address
                FROM leaders
                WHERE COALESCE(on_watchlist, TRUE) IS TRUE
                  AND COALESCE(excluded, FALSE) IS FALSE
                ORDER BY falcon_score DESC NULLS LAST, last_refresh DESC NULLS LAST
                LIMIT $1
                """,
                int(limit),
            )
            return [row["wallet_address"] for row in rows]
    finally:
        await close_pool()


async def _load_historical(
    args,
) -> tuple[
    list[BacktestMarket],
    list[BacktestTrade],
    list[BacktestBookSnapshot],
    list[BacktestCandle],
]:
    start, end = _resolve_date_range(args)
    wallets = _wallets_from_args(args)
    if not wallets and args.top_leaders:
        wallets = await _load_top_leader_wallets(int(args.top_leaders))
    if args.max_wallets:
        wallets = wallets[: int(args.max_wallets)]
    if not wallets:
        raise ValueError("historical mode requires --wallets or --top-leaders")

    loader = _make_loader(
        args.cache_dir,
        refresh=args.refresh,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        max_markets=args.max_markets,
        max_tokens=args.max_tokens,
    )
    try:
        dataset = await loader.load(
            wallets=wallets,
            start=start,
            end=end,
        )
    finally:
        await loader.close()
    return dataset.markets, dataset.trades, dataset.books, getattr(dataset, "candles", [])


def _wallets_from_args(args) -> list[str]:
    if not args.wallets:
        return []
    return [wallet.strip() for wallet in args.wallets.split(",") if wallet.strip()]


def _resolve_date_range(args) -> tuple[datetime, datetime]:
    if args.start and args.end:
        return _parse_date(args.start), _parse_date(args.end)
    if args.smoke_days:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=int(args.smoke_days))
        return start, end
    raise ValueError("historical mode requires --start/--end or --smoke-days")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run leader_swing Gate A backtest.")
    parser.add_argument("--fixture", choices=["mock", "none"], default="none")
    parser.add_argument("--start", help="Historical start date, YYYY-MM-DD.")
    parser.add_argument("--end", help="Historical end date, YYYY-MM-DD.")
    parser.add_argument("--wallets", help="Comma-separated leader wallet list.")
    parser.add_argument("--top-leaders", type=int, help="Load top N leaders from the local DB.")
    parser.add_argument("--max-wallets", type=int, help="Cap the number of wallets used.")
    parser.add_argument("--smoke-days", type=int, help="Use the last N days as historical range.")
    parser.add_argument("--page-limit", type=int, default=200, help="Falcon page size.")
    parser.add_argument("--max-pages", type=int, default=20, help="Max Falcon pages per query.")
    parser.add_argument("--max-markets", type=int, help="Cap unique markets for diagnostics.")
    parser.add_argument("--max-tokens", type=int, help="Cap unique tokens for diagnostics.")
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--refresh", action="store_true", help="Refresh historical Falcon cache.")
    parser.add_argument("--out", default="reports/leader_swing_gate_report.json")
    args = parser.parse_args(argv)

    if args.fixture == "mock":
        markets, trades, books = _mock_fixture()
        candles: list[BacktestCandle] = []
    else:
        try:
            markets, trades, books, candles = asyncio.run(_load_historical(args))
        except ValueError as exc:
            parser.error(str(exc))

    backtester = LeaderSwingBacktester(size_usdc=Decimal("40"), observation_lag_s=0)
    primary = backtester.run(
        markets=markets,
        trades=trades,
        books=books,
        candles=candles,
        policy="follow_all",
    )
    baselines = run_required_baselines(backtester, markets, trades, books, candles)
    report = build_gate_report(primary, baselines)
    report["input_counts"] = {
        "markets": len(markets),
        "trades": len(trades),
        "books": len(books),
        "candles": len(candles),
    }
    output = write_gate_report(report, Path(args.out))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
