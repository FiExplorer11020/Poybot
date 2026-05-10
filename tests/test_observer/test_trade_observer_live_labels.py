"""
Focused tests for live trade classification payloads published by TradeObserver.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.trade_observer import (
    REDIS_TRADES_CHANNEL,
    TradeObserver,
    _gamma_market_matches_request,
)


def _attach_transaction(conn) -> None:
    """Attach a no-op `conn.transaction()` async-CM so the production code
    that wraps multi-statement writes in `async with conn.transaction():`
    works under unit tests (the bare AsyncMock returns a coroutine, not a
    CM, which would raise TypeError).
    """

    @asynccontextmanager
    async def _tx():
        yield None

    # `transaction()` is sync (returns the Transaction object); the
    # returned object is the async CM. Hence MagicMock, not AsyncMock.
    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())


def _attach_writer_fetch(
    conn,
    *,
    leader_row: dict | None = None,
) -> None:
    """Phase 1 Task O: the batched writer issues three `conn.fetch` calls
    inside the transaction:
      1. initial-category SELECT (multi-row)
      2. multi-row INSERT … RETURNING natural_key
      3. leaders SELECT … WHERE wallet_address = ANY($1)

    This helper wires up a `side_effect` that dispatches by SQL substring
    so each test can keep its existing fetchrow-based market-data fixture
    while the new writer-side queries get sensible defaults.
    """

    async def _fetch(sql: str, *args):
        if "INSERT INTO trades_observed" in sql:
            # One returned row per VALUES tuple in `args` (10 params each).
            rows: list[dict] = []
            for i in range(0, len(args), 10):
                chunk = args[i : i + 10]
                if len(chunk) < 10:
                    break
                rows.append({
                    "id": 1,
                    "wallet_address": chunk[3],
                    "market_id": chunk[1],
                    "time": chunk[0],
                    "side": chunk[4],
                    "price": chunk[5],
                    "size_usdc": chunk[6],
                })
            return rows
        if "FROM leaders" in sql and "ANY(" in sql:
            if leader_row is None:
                return []
            # Caller supplied a single leader row; replay it for the leader
            # wallet referenced by the `ANY($1)` array.
            wallets = args[0] if args else []
            return [{"wallet_address": w, **leader_row} for w in wallets]
        if "NULLIF(category" in sql or "FROM markets" in sql:
            return []
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.executemany = AsyncMock()


@pytest.mark.asyncio
async def test_process_trade_publishes_live_market_and_wallet_labels():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.publish = AsyncMock()

    observer = TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=redis,
        leader_wallets={"0xleader"},
        leader_markets={"0xmkt"},
    )

    conn = AsyncMock()

    async def fetchrow(sql, *args):
        if "FROM markets" in sql:
            return {
                "question": "Will BTC finish April above $100k?",
                "category": "crypto",
            }
        if "FROM leaders" in sql:
            return {
                "classification_json": json.dumps(
                    {
                        "strategy": "directional",
                        "horizon": "intraday",
                        "influence": "whale",
                    }
                ),
                "excluded": False,
                "on_watchlist": True,
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    _attach_transaction(conn)
    _attach_writer_fetch(
        conn,
        leader_row={
            "classification_json": json.dumps(
                {
                    "strategy": "directional",
                    "horizon": "intraday",
                    "influence": "whale",
                }
            ),
            "excluded": False,
            "on_watchlist": True,
        },
    )

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    with patch("src.observer.trade_observer.get_db", fake_get_db):
        await observer._process_trade(
            market_id="0xmkt",
            token_id="0xtoken",
            wallet_address="0xleader",
            side="BUY",
            price=Decimal("0.63"),
            size_usdc=Decimal("125.00"),
            trade_time=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            source="data_api",
        )
        await observer._writer_run_once()

    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.call_args.args
    assert channel == REDIS_TRADES_CHANNEL
    event = json.loads(payload)
    assert event["market_question"] == "Will BTC finish April above $100k?"
    assert event["market_category"] == "crypto"
    assert event["market_type"] == "crypto"
    assert event["wallet_type"] == "leader"
    assert event["wallet_status"] == "active"
    assert event["wallet_strategy"] == "directional"
    assert event["wallet_horizon"] == "intraday"
    assert event["wallet_influence"] == "whale"


@pytest.mark.asyncio
async def test_process_trade_enriches_unknown_market_from_gamma():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.publish = AsyncMock()

    observer = TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=redis,
        leader_wallets={"0xleader"},
        leader_markets={"0xmkt"},
    )

    conn = AsyncMock()

    async def fetchrow(sql, *args):
        if "FROM markets" in sql:
            return {
                "question": "Market 0xmkt…",
                "category": "unknown",
            }
        if "FROM leaders" in sql:
            return {
                "classification_json": "{}",
                "excluded": False,
                "on_watchlist": True,
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    _attach_transaction(conn)
    _attach_writer_fetch(
        conn,
        leader_row={
            "classification_json": "{}",
            "excluded": False,
            "on_watchlist": True,
        },
    )

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    with (
        patch("src.observer.trade_observer.get_db", fake_get_db),
        patch.object(
            observer,
            "_fetch_market_metadata_from_gamma",
            new=AsyncMock(
                return_value={
                    "question": "Will ETH settle above $4k this week?",
                    "category": "crypto",
                    "token_yes": "yes",
                    "token_no": "no",
                    "end_date": None,
                    "volume_24h": 1000.0,
                    "liquidity_score": 0.9,
                    "fee_rate_pct": 0.02,
                }
            ),
        ),
    ):
        await observer._process_trade(
            market_id="0xmkt",
            token_id="0xtoken",
            wallet_address="0xleader",
            side="BUY",
            price=Decimal("0.63"),
            size_usdc=Decimal("125.00"),
            trade_time=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            source="data_api",
        )
        await observer._writer_run_once()

    channel, payload = redis.publish.call_args.args
    assert channel == REDIS_TRADES_CHANNEL
    event = json.loads(payload)
    assert event["market_question"] == "Will ETH settle above $4k this week?"
    assert event["market_category"] == "crypto"


@pytest.mark.asyncio
async def test_process_trade_repairs_stale_market_from_trade_title_hint():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.publish = AsyncMock()

    observer = TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=redis,
        leader_wallets={"0xleader"},
        leader_markets={"0xmkt"},
    )

    conn = AsyncMock()

    async def fetchrow(sql, *args):
        if "SELECT question, category, token_yes, token_no, end_date" in sql:
            return {
                "question": "Will Joe Biden get Coronavirus before the election?",
                "category": "US-current-affairs",
                "token_yes": "old-yes",
                "token_no": "old-no",
                "end_date": datetime(2020, 11, 3, 23, 0, tzinfo=timezone.utc),
            }
        if "FROM leaders" in sql:
            return {
                "classification_json": "{}",
                "excluded": False,
                "on_watchlist": True,
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    _attach_transaction(conn)
    _attach_writer_fetch(
        conn,
        leader_row={
            "classification_json": "{}",
            "excluded": False,
            "on_watchlist": True,
        },
    )

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    with patch("src.observer.trade_observer.get_db", fake_get_db):
        await observer._process_trade(
            market_id="0xmkt",
            token_id="0xyes",
            wallet_address="0xleader",
            side="BUY",
            price=Decimal("0.63"),
            size_usdc=Decimal("125.00"),
            trade_time=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            source="data_api",
            market_question_hint="Bitcoin Up or Down - April 2, 2:10PM-2:15PM ET",
            market_slug_hint="btc-updown-5m-1775153400",
            outcome_hint="Up",
            outcome_index=0,
        )
        await observer._writer_run_once()

    channel, payload = redis.publish.call_args.args
    assert channel == REDIS_TRADES_CHANNEL
    event = json.loads(payload)
    assert event["market_question"] == "Bitcoin Up or Down - April 2, 2:10PM-2:15PM ET"
    assert event["market_category"] == "crypto"
    assert event["market_type"] == "crypto"


def test_gamma_market_match_rejects_mismatched_condition_id():
    market = {
        "conditionId": "0xother",
        "clobTokenIds": json.dumps(["yes", "no"]),
    }

    assert _gamma_market_matches_request(market, "0xrequested", "yes") is False


def test_gamma_market_match_rejects_missing_requested_token():
    market = {
        "conditionId": "0xrequested",
        "clobTokenIds": json.dumps(["yes", "no"]),
    }

    assert _gamma_market_matches_request(market, "0xrequested", "different-token") is False
