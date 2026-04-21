import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.backtest.cache import BacktestCache
from src.backtest.models import BacktestBookSnapshot, BacktestCandle, BacktestMarket, BacktestTrade
from src.backtest.normalizers import (
    normalize_book_row,
    normalize_candle_row,
    normalize_market_row,
    normalize_trade_row,
)
from src.registry.falcon_client import FalconClient


@dataclass(frozen=True)
class HistoricalBacktestDataset:
    markets: list[BacktestMarket]
    trades: list[BacktestTrade]
    books: list[BacktestBookSnapshot]
    candles: list[BacktestCandle]


class HistoricalFalconLoader:
    def __init__(
        self,
        *,
        client: FalconClient,
        cache_dir: str | Path = "data_cache",
        page_limit: int = 200,
        max_pages: int = 20,
        max_markets: int | None = None,
        max_tokens: int | None = None,
        refresh: bool = False,
    ) -> None:
        self.client = client
        self.cache = BacktestCache(cache_dir)
        self.cache_dir = Path(cache_dir)
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.max_markets = max_markets
        self.max_tokens = max_tokens
        self.refresh = refresh

    async def load(
        self,
        *,
        wallets: list[str],
        start: datetime,
        end: datetime,
    ) -> HistoricalBacktestDataset:
        trade_rows = await self.fetch_trade_rows(wallets=wallets, start=start, end=end)
        trades = [normalize_trade_row(row) for row in trade_rows]
        market_ids = sorted({trade.market_id for trade in trades})
        if self.max_markets is not None:
            market_ids = market_ids[: self.max_markets]
            allowed_markets = set(market_ids)
            trades = [trade for trade in trades if trade.market_id in allowed_markets]
        token_ids = sorted({trade.token_id for trade in trades})
        if self.max_tokens is not None:
            token_ids = token_ids[: self.max_tokens]
        market_rows = await self.fetch_market_rows(market_ids)
        markets = [normalize_market_row(row) for row in market_rows]
        token_to_market = {
            token_id: market.market_id
            for market in markets
            for token_id in (market.yes_token_id, market.no_token_id)
        }

        book_rows = await self.fetch_book_rows(token_ids=token_ids, start=start, end=end)
        book_rows = [self._with_market_id(row, token_to_market) for row in book_rows]
        books = [normalize_book_row(row) for row in book_rows]

        candle_rows = await self.fetch_candle_rows(token_ids=token_ids, start=start, end=end)
        candle_rows = [self._with_market_id(row, token_to_market) for row in candle_rows]
        candles = [normalize_candle_row(row) for row in candle_rows]

        self.cache.write_records(
            "falcon_556_trades",
            "historical",
            trade_rows,
            dedupe_keys=("tx_hash", "transaction_hash", "id"),
        )
        self.cache.write_records(
            "falcon_574_markets", "historical", market_rows, dedupe_keys=("condition_id",)
        )
        self.cache.write_records(
            "falcon_572_books", "historical", book_rows, dedupe_keys=("token_id", "timestamp")
        )
        self.cache.write_records(
            "falcon_568_candles",
            "historical",
            candle_rows,
            dedupe_keys=("token_id", "start_time", "candle_time", "end_time"),
        )
        self._write_manifest(wallets=wallets, start=start, end=end)
        return HistoricalBacktestDataset(
            markets=markets,
            trades=trades,
            books=books,
            candles=candles,
        )

    async def fetch_trade_rows(
        self,
        *,
        wallets: list[str],
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        rows: list[dict] = []
        for wallet in wallets:
            for window_start, window_end in self._window_ranges(start, end):
                rows.extend(
                    await self._query_all_pages(
                        556,
                        {
                            "proxy_wallet": wallet,
                            "start_time": str(int(window_start.timestamp())),
                            "end_time": str(int(window_end.timestamp())),
                        },
                    )
                )
        return rows

    async def fetch_market_rows(self, market_ids: list[str]) -> list[dict]:
        rows: list[dict] = []
        for market_id in market_ids:
            rows.extend(
                await self.client.query(
                    574,
                    {"condition_id": market_id},
                    limit=1,
                    offset=0,
                )
            )
        return rows

    async def fetch_book_rows(
        self,
        *,
        token_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        rows: list[dict] = []
        for token_id in token_ids:
            for window_start, window_end in self._window_ranges(start, end):
                rows.extend(
                    await self._query_all_pages(
                        572,
                        {
                            "token_id": token_id,
                            "start_time": str(int(window_start.timestamp())),
                            "end_time": str(int(window_end.timestamp())),
                        },
                    )
                )
        return rows

    async def fetch_candle_rows(
        self,
        *,
        token_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        rows: list[dict] = []
        for token_id in token_ids:
            for window_start, window_end in self._window_ranges(start, end):
                rows.extend(
                    await self._query_all_pages(
                        568,
                        {
                            "token_id": token_id,
                            "start_time": str(int(window_start.timestamp())),
                            "end_time": str(int(window_end.timestamp())),
                            "interval": "1h",
                        },
                    )
                )
        return rows

    async def _query_all_pages(self, agent_id: int, params: dict) -> list[dict]:
        rows: list[dict] = []
        for page in range(self.max_pages):
            page_rows = await self.client.query(
                agent_id,
                params,
                limit=self.page_limit,
                offset=page * self.page_limit,
            )
            rows.extend(page_rows)
            if len(page_rows) < self.page_limit:
                break
        return rows

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()

    def _window_ranges(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        if start >= end:
            return []
        ranges: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=7), end)
            ranges.append((cursor, window_end))
            cursor = window_end
        return ranges

    def _with_market_id(self, row: dict, token_to_market: dict[str, str]) -> dict:
        if row.get("condition_id") or row.get("market_id"):
            return row
        token_id = row.get("token_id") or row.get("asset_id")
        market_id = token_to_market.get(str(token_id))
        if not market_id:
            return row
        enriched = dict(row)
        enriched["condition_id"] = market_id
        return enriched

    def _write_manifest(self, *, wallets: list[str], start: datetime, end: datetime) -> None:
        manifest_dir = self.cache_dir / "manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "historical_load_done.json").write_text(
            json.dumps(
                {
                    "wallets": wallets,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
        )
