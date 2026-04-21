"""
Focused tests for live trade classification payloads published by TradeObserver.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.observer.trade_observer import (
    REDIS_TRADES_CHANNEL,
    TradeObserver,
    _gamma_market_matches_request,
)


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
