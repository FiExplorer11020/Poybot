"""
Unit tests for src/observer/trade_observer.py
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.trade_observer import REDIS_TRADES_CHANNEL, TradeObserver

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_redis_mock(duplicate: bool = False):
    """Return a mock redis client. If duplicate=True, .set returns None (already exists)."""
    r = AsyncMock()
    r.set = AsyncMock(return_value=None if duplicate else True)
    r.setex = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.publish = AsyncMock()
    return r


def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return conn


def _make_falcon_mock(trades=None):
    f = AsyncMock()
    f.query = AsyncMock(return_value=trades or [])
    return f


def _make_observer(
    leader_wallets=None,
    leader_markets=None,
    duplicate=False,
    trades=None,
):
    redis = _make_redis_mock(duplicate=duplicate)
    falcon = _make_falcon_mock(trades=trades)
    obs = TradeObserver(
        falcon_client=falcon,
        redis_client=redis,
        leader_wallets=leader_wallets or set(),
        leader_markets=leader_markets or set(),
    )
    return obs, redis, falcon


def _mock_get_db(conn_mock):
    """Return a patch context that replaces get_db with an async CM yielding conn_mock."""

    @asynccontextmanager
    async def fake_get_db():
        yield conn_mock

    return patch("src.observer.trade_observer.get_db", fake_get_db)


_TRADE_TIME = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_MARKET = "0xmarket1"
_TOKEN = "0xtoken1"
_WALLET = "0xwallet1"
_LEADER_WALLET = "0xleader1"


# ---------------------------------------------------------------------------
# 1. process_trade inserts to DB when not duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_inserts_to_db():
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()

    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=_LEADER_WALLET,
            side="BUY",
            price=Decimal("0.65"),
            size_usdc=Decimal("100.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    assert conn.execute.await_count >= 2
    first_sql = conn.execute.call_args_list[0].args[0]
    assert "INSERT INTO trades_observed" in first_sql
    assert obs.inserted_count == 1


# ---------------------------------------------------------------------------
# 2. process_trade does NOT insert when duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_deduplicates():
    obs, redis, _ = _make_observer(duplicate=True)
    conn = _make_conn()

    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=_WALLET,
            side="BUY",
            price=Decimal("0.65"),
            size_usdc=Decimal("100.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    conn.execute.assert_not_awaited()
    assert obs.inserted_count == 0


# ---------------------------------------------------------------------------
# 3. is_leader flag set correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_leader_flagged_correctly():
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()
    captured_args: list = []

    async def capture_execute(sql, *args):
        captured_args.extend(args)

    conn.execute = AsyncMock(side_effect=capture_execute)

    # Leader wallet
    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=_LEADER_WALLET,
            side="BUY",
            price=Decimal("0.65"),
            size_usdc=Decimal("100.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    # is_leader is the 9th positional arg ($9)
    assert captured_args[8] is True  # is_leader=True for leader wallet

    # Reset
    redis.set = AsyncMock(return_value=True)
    captured_args.clear()

    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address="0xunknown",
            side="SELL",
            price=Decimal("0.70"),
            size_usdc=Decimal("50.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    assert captured_args[8] is False  # is_leader=False for unknown wallet


# ---------------------------------------------------------------------------
# 4. process_trade publishes to Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_publishes_to_redis():
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()

    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=_LEADER_WALLET,
            side="BUY",
            price=Decimal("0.65"),
            size_usdc=Decimal("100.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.call_args[0]
    assert channel == REDIS_TRADES_CHANNEL
    event = json.loads(payload)
    assert event["wallet_address"] == _LEADER_WALLET
    assert event["is_leader"] is True
    assert event["source"] == "websocket"
    assert event["price"] == "0.65"


# ---------------------------------------------------------------------------
# 5. backfill_from_falcon calls _process_falcon_trade for each trade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_from_falcon():
    falcon_trades = [
        {
            "market_id": _MARKET,
            "token_id": _TOKEN,
            "side": "BUY",
            "price": "0.55",
            "size": "200",
            "timestamp": 1700000000000,
        },
        {
            "market_id": _MARKET,
            "token_id": _TOKEN,
            "side": "SELL",
            "price": "0.70",
            "size": "200",
            "timestamp": 1700003600000,
        },
    ]
    obs, redis, falcon = _make_observer(
        leader_wallets={_LEADER_WALLET},
        trades=falcon_trades,
    )
    conn = _make_conn()

    with _mock_get_db(conn):
        await obs._backfill_from_falcon()

    assert falcon.query.await_count == 1
    assert conn.execute.await_count >= 2


# ---------------------------------------------------------------------------
# 6. handle_ws_message ignores non-trade events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ws_message_ignores_non_trade():
    obs, redis, _ = _make_observer()
    conn = _make_conn()

    with _mock_get_db(conn):
        await obs._handle_ws_message({"event_type": "orderbook", "asset_id": _TOKEN})

    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. handle_ws_message parses a valid trade event and calls _process_trade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ws_message_parses_trade():
    obs, redis, _ = _make_observer(leader_wallets={_WALLET})
    conn = _make_conn()

    msg = {
        "event_type": "trade",
        "market": _MARKET,
        "asset_id": _TOKEN,
        "price": "0.65",
        "size": "500",
        "side": "BUY",
        "maker_address": _WALLET,
        "taker_address": "",
        "timestamp": "1700000000000",
    }

    with _mock_get_db(conn):
        await obs._handle_ws_message(msg)

    assert conn.execute.await_count >= 2


# ---------------------------------------------------------------------------
# 8. update_leaders updates internal sets and ws_client
# ---------------------------------------------------------------------------


def test_update_leaders_updates_sets():
    obs, _, _ = _make_observer(
        leader_wallets={"old_wallet"},
        leader_markets={"old_token"},
    )
    ws_mock = MagicMock()
    ws_mock.update_markets = MagicMock()
    obs._ws_client = ws_mock

    obs.update_leaders({"new_wallet1", "new_wallet2"}, {"new_token1"})

    assert obs._leader_wallets == {"new_wallet1", "new_wallet2"}
    assert obs._leader_markets == {"new_token1"}
    ws_mock.update_markets.assert_called_once_with({"new_token1"})


# ---------------------------------------------------------------------------
# 9. Falcon trade with millisecond timestamp (13 digits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_falcon_trade_ms_timestamp():
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()
    captured: list = []

    async def capture(sql, *args):
        captured.extend(args)

    conn.execute = AsyncMock(side_effect=capture)

    trade = {
        "market_id": _MARKET,
        "token_id": _TOKEN,
        "side": "BUY",
        "price": "0.60",
        "size": "300",
        "timestamp": 1700000000000,  # 13-digit ms timestamp
    }

    with _mock_get_db(conn):
        await obs._process_falcon_trade(trade, _LEADER_WALLET)

    assert conn.execute.await_count >= 2
    # First execute is the trades_observed insert; $1 is the first arg after SQL.
    trade_time: datetime = conn.execute.call_args_list[0].args[1]
    assert trade_time.tzinfo is not None
    # 1700000000000 ms = 1700000000 s → 2023-11-14T22:13:20Z
    assert trade_time.year == 2023


# ---------------------------------------------------------------------------
# 10. Falcon trade with seconds timestamp (10 digits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_falcon_trade_s_timestamp():
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()
    captured: list = []

    async def capture(sql, *args):
        captured.extend(args)

    conn.execute = AsyncMock(side_effect=capture)

    trade = {
        "market_id": _MARKET,
        "token_id": _TOKEN,
        "side": "BUY",
        "price": "0.60",
        "size": "300",
        "timestamp": 1700000000,  # 10-digit seconds timestamp
    }

    with _mock_get_db(conn):
        await obs._process_falcon_trade(trade, _LEADER_WALLET)

    assert conn.execute.await_count >= 2
    trade_time: datetime = conn.execute.call_args_list[0].args[1]
    assert trade_time.tzinfo is not None
    # 1700000000 s → 2023-11-14T22:13:20Z
    assert trade_time.year == 2023
