from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.backtest.data_loader import HistoricalFalconLoader
from src.backtest.normalizers import (
    normalize_book_row,
    normalize_candle_row,
    normalize_market_row,
    normalize_trade_row,
)


def test_normalize_trade_row_from_falcon_556_payload():
    row = {
        "timestamp": "1713614400",
        "side": "BUY",
        "price": "0.42",
        "size": "100",
        "outcome": "Yes",
        "token_id": "yes1",
        "condition_id": "m1",
        "slug": "btc-target",
        "proxy_wallet": "0xleader",
        "tx_hash": "0xabc",
    }

    trade = normalize_trade_row(row)

    assert trade.leader_wallet == "0xleader"
    assert trade.market_id == "m1"
    assert trade.token_id == "yes1"
    assert trade.price == Decimal("0.42")
    assert trade.size_shares == Decimal("100")
    assert trade.event_ts == datetime(2024, 4, 20, 12, 0, tzinfo=timezone.utc)


def test_normalize_trade_row_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="missing required trade field"):
        normalize_trade_row({"timestamp": "1713614400", "price": "0.42"})


def test_normalize_market_row_from_falcon_574_payload():
    row = {
        "condition_id": "m1",
        "question": "Will BTC close above target?",
        "category": "crypto",
        "tokens": [{"outcome": "Yes", "token_id": "yes1"}, {"outcome": "No", "token_id": "no1"}],
        "volume_total": "123456.7",
        "feesEnabled": True,
        "fd": {"r": "4", "e": "2", "to": True},
        "timestamp": "1713614400",
    }

    market = normalize_market_row(row)

    assert market.market_id == "m1"
    assert market.yes_token_id == "yes1"
    assert market.no_token_id == "no1"
    assert market.volume_usdc == Decimal("123456.7")
    assert market.fee_snapshot.fee_rate == Decimal("0.04")


def test_normalize_market_row_from_falcon_574_side_a_side_b_payload():
    row = {
        "condition_id": "m1",
        "question": "Bitcoin Up or Down",
        "side_a_outcome": "Up",
        "side_a_token_id": "up1",
        "side_b_outcome": "Down",
        "side_b_token_id": "down1",
        "volume_total": "276.59",
        "timestamp": "2026-04-20T17:43:34Z",
    }

    market = normalize_market_row(row)

    assert market.yes_token_id == "up1"
    assert market.no_token_id == "down1"
    assert market.volume_usdc == Decimal("276.59")


def test_normalize_book_row_from_falcon_572_payload():
    row = {
        "condition_id": "m1",
        "token_id": "yes1",
        "timestamp": "1713614400",
        "best_bid": "0.41",
        "best_ask": "0.43",
    }

    book = normalize_book_row(row)

    assert book.market_id == "m1"
    assert book.token_id == "yes1"
    assert book.best_bid == Decimal("0.41")
    assert book.best_ask == Decimal("0.43")


def test_normalize_candle_row_from_falcon_568_payload():
    row = {
        "condition_id": "m1",
        "token_id": "yes1",
        "start_time": "1713614400",
        "end_time": "1713618000",
        "high": "0.58",
        "low": "0.50",
    }

    candle = normalize_candle_row(row)

    assert candle.market_id == "m1"
    assert candle.token_id == "yes1"
    assert candle.high == Decimal("0.58")
    assert candle.low == Decimal("0.50")
    assert candle.start_ts == datetime(2024, 4, 20, 12, 0, tzinfo=timezone.utc)
    assert candle.end_ts == datetime(2024, 4, 20, 13, 0, tzinfo=timezone.utc)


def test_normalize_candle_row_from_real_falcon_568_candle_time_payload():
    row = {
        "condition_id": "m1",
        "token_id": "yes1",
        "candle_time": "2026-04-16T04:00:00Z",
        "high": "0.7200",
        "low": "0.7000",
    }

    candle = normalize_candle_row(row)

    assert candle.start_ts == datetime(2026, 4, 16, 4, 0, tzinfo=timezone.utc)
    assert candle.end_ts == datetime(2026, 4, 16, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_loader_fetches_and_normalizes_with_cache(tmp_path):
    client = AsyncMock()
    client.query = AsyncMock(
        side_effect=[
            [
                {
                    "timestamp": "1713614400",
                    "side": "BUY",
                    "price": "0.42",
                    "size": "100",
                    "outcome": "Yes",
                    "token_id": "yes1",
                    "condition_id": "m1",
                    "proxy_wallet": "0xleader",
                    "tx_hash": "0xabc",
                }
            ],
            [
                {
                    "condition_id": "m1",
                    "question": "Will BTC close above target?",
                    "category": "crypto",
                    "tokens": [
                        {"outcome": "Yes", "token_id": "yes1"},
                        {"outcome": "No", "token_id": "no1"},
                    ],
                    "volume_total": "100000",
                    "feesEnabled": False,
                    "timestamp": "1713614400",
                }
            ],
            [
                {
                    "condition_id": "m1",
                    "token_id": "yes1",
                    "timestamp": "1713614400",
                    "best_bid": "0.41",
                    "best_ask": "0.43",
                }
            ],
            [
                {
                    "condition_id": "m1",
                    "token_id": "yes1",
                    "start_time": "1713614400",
                    "end_time": "1713618000",
                    "high": "0.58",
                    "low": "0.50",
                }
            ],
        ]
    )
    loader = HistoricalFalconLoader(client=client, cache_dir=tmp_path)

    dataset = await loader.load(
        wallets=["0xleader"],
        start=datetime(2024, 4, 20, tzinfo=timezone.utc),
        end=datetime(2024, 4, 21, tzinfo=timezone.utc),
    )

    assert len(dataset.trades) == 1
    assert len(dataset.markets) == 1
    assert len(dataset.books) == 1
    assert len(dataset.candles) == 1
    assert (tmp_path / "manifest" / "historical_load_done.json").exists()
    assert client.query.await_args_list[0].args[1]["proxy_wallet"] == "0xleader"


@pytest.mark.asyncio
async def test_loader_close_closes_underlying_client(tmp_path):
    client = AsyncMock()
    client.close = AsyncMock()
    loader = HistoricalFalconLoader(client=client, cache_dir=tmp_path)

    await loader.close()

    client.close.assert_awaited_once()
