"""
Unit tests for src/observer/trade_observer.py

Phase 1 Task O note: `_process_trade` is now a producer that enqueues onto
`obs._write_queue`; the actual DB insert happens in `_db_writer_loop`. Tests
that need to assert post-insert state must call `await obs._writer_run_once()`
to drain the queue synchronously through the writer's batch path.
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


def _make_conn(*, trade_insert_id: int | None = 1):
    """Build a fake asyncpg conn for the Phase 1 batched writer.

    The writer issues:
      - `executemany(markets stub upsert, [...])`
      - `fetch(SELECT category FROM markets WHERE market_id = ANY($1))`
      - `fetch(multi-row INSERT trades_observed ... RETURNING natural_key)`
      - `fetch(SELECT FROM leaders WHERE wallet_address = ANY($1))`
      - `fetchrow(SELECT FROM markets WHERE market_id=$1)` per inserted row
      - `execute(UPDATE trades_observed SET category)` per refined row

    `trade_insert_id` controls what the multi-row INSERT … RETURNING fetch
    yields back. `1` → every record in the batch maps to a row with id=1
    (i.e. inserted). `None` → empty result set, simulating a DB-level dupe
    that ON CONFLICT DO NOTHING swallowed.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.fetchval = AsyncMock(return_value=trade_insert_id)

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())

    # Smart `fetch`: dispatch based on the SQL string so tests can use this
    # one mock for all three fetch sites in the writer's atomic batch path.
    async def _fetch(sql: str, *args):
        if "FROM trades_observed" in sql or "INSERT INTO trades_observed" in sql:
            # Multi-row INSERT … RETURNING. Synthesize one returned row per
            # INSERT VALUES tuple in `args` so the writer thinks every
            # record was inserted (or none, if trade_insert_id is None).
            if trade_insert_id is None:
                return []
            # Each VALUES tuple has 10 positional params:
            # (time, market_id, token_id, wallet, side, price, size, source, is_leader, category)
            rows: list[dict] = []
            for i in range(0, len(args), 10):
                chunk = args[i : i + 10]
                if len(chunk) < 10:
                    break
                rows.append({
                    "id": trade_insert_id,
                    "wallet_address": chunk[3],
                    "market_id": chunk[1],
                    "time": chunk[0],
                    "side": chunk[4],
                    "price": chunk[5],
                    "size_usdc": chunk[6],
                })
            return rows
        if "NULLIF(category" in sql or "FROM markets" in sql:
            # Initial-category lookup: empty = "all unknown".
            return []
        if "FROM leaders" in sql:
            return []
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
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
    """Phase 1 Task O: `_process_trade` now enqueues. The actual INSERT
    happens in `_writer_run_once()`'s batched path via `conn.fetch` on a
    multi-row VALUES INSERT … RETURNING (asyncpg's `executemany` doesn't
    return rows). We assert on the SQL string emitted by that fetch.
    """
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
        # Drain the queue through the writer's batch path.
        await obs._writer_run_once()

    # Find the multi-row INSERT call in the fetch history.
    insert_calls = [
        c for c in conn.fetch.call_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert insert_calls, "writer did not run a trades_observed INSERT"
    insert_sql = insert_calls[0].args[0]
    assert "ON CONFLICT" in insert_sql
    assert "DO NOTHING" in insert_sql
    assert "RETURNING id" in insert_sql
    assert obs.inserted_count == 1


# ---------------------------------------------------------------------------
# 2. process_trade does NOT insert when duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_deduplicates():
    """Redis dedup hit must short-circuit BEFORE the trade is enqueued —
    the producer never touches the DB or the queue on a dedup hit.
    """
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

    # Producer is Redis-fast — no DB calls and nothing in the queue.
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.fetch.assert_not_awaited()
    assert obs._write_queue is None or obs._write_queue.qsize() == 0
    assert obs.inserted_count == 0


# ---------------------------------------------------------------------------
# 2b. DB-layer dedup catches what Redis missed (S1.3 safety net)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_db_layer_dedup_short_circuits():
    """Phase 1 Task O update: the writer's batched RETURNING query yields
    no rows (simulating ON CONFLICT DO NOTHING swallowing the trade).
    Expectations: no per-row enrichment, no leader fetch, no pub/sub
    publish, no inserted-counter bump. The markets stub upsert + initial-
    category fetch + the multi-row INSERT itself still run because they're
    batch-level setup that fires whether or not any individual row commits.
    """
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn(trade_insert_id=None)  # <-- DB says "dupe"

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
        await obs._writer_run_once()

    # Markets stub upsert runs as `executemany` (batched).
    assert conn.executemany.await_count >= 1
    stub_sql = conn.executemany.call_args_list[0].args[0]
    assert "INSERT INTO markets" in stub_sql
    assert "ON CONFLICT (market_id) DO NOTHING" in stub_sql

    # The multi-row INSERT into trades_observed ran (returned no rows).
    insert_calls = [
        c for c in conn.fetch.call_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert len(insert_calls) == 1

    # No per-row enrichment, no publish, no counter bump.
    conn.fetchrow.assert_not_awaited()
    redis.publish.assert_not_awaited()
    assert obs.inserted_count == 0


# ---------------------------------------------------------------------------
# 3. is_leader flag set correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_leader_flagged_correctly():
    """Phase 1 Task O: capture is_leader from the multi-row INSERT params
    that the writer sends to `conn.fetch`. Each VALUES tuple has 10
    positional params; is_leader is at index 8 (0-based) within each tuple.
    """
    obs, redis, _ = _make_observer(leader_wallets={_LEADER_WALLET})
    conn = _make_conn()

    captured: list = []

    async def capture_fetch(sql, *args):
        if "INSERT INTO trades_observed" in sql:
            captured.append(args)
            # Synthesize one returned row per VALUES tuple so the writer
            # still bumps `inserted_count` for downstream assertions.
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
        return []

    conn.fetch = AsyncMock(side_effect=capture_fetch)

    # Leader wallet — single-record batch.
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
        await obs._writer_run_once()

    # First captured INSERT call's args: (time, market_id, token_id,
    # wallet, side, price, size, source, is_leader, category) repeated
    # per VALUES tuple. Single-record batch → 10 args → is_leader at [8].
    assert captured, "writer never sent the trades_observed INSERT"
    assert captured[0][8] is True  # is_leader=True for leader wallet

    # Reset for non-leader trade.
    redis.set = AsyncMock(return_value=True)
    captured.clear()

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
        await obs._writer_run_once()

    assert captured, "writer never sent the trades_observed INSERT (non-leader)"
    assert captured[0][8] is False  # is_leader=False for unknown wallet


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
        await obs._writer_run_once()

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
        # Drain any enqueued records — the Falcon path enqueues 2 records
        # which fit in a single batch.
        await obs._writer_run_once()

    assert falcon.query.await_count == 1
    # The writer's batched INSERT runs once for both records.
    insert_calls = [
        c for c in conn.fetch.call_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert len(insert_calls) == 1
    # The single multi-row INSERT carries 2 records × 10 args each = 20.
    assert len(insert_calls[0].args) == 21  # +1 for the SQL string
    assert obs.inserted_count == 2


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


@pytest.mark.asyncio
async def test_handle_book_message_updates_book_age_metric():
    obs, redis, _ = _make_observer()

    with patch("src.observer.trade_observer.time.time", return_value=1_700_000_001.0):
        await obs._handle_ws_message(
            {
                "event_type": "book",
                "market": _MARKET,
                "asset_id": _TOKEN,
                "timestamp": "1700000000000",
                "bids": [{"price": "0.44", "size": "20"}],
                "asks": [{"price": "0.46", "size": "12"}],
            }
        )

    metric_calls = [call.args for call in redis.setex.await_args_list]
    assert ("metrics:book_age_p95_s", 300, "1.000") in metric_calls
    assert any(args[0] == f"book:last:{_MARKET}:{_TOKEN}" for args in metric_calls)


@pytest.mark.asyncio
async def test_handle_book_message_persists_book_quality_snapshot():
    obs, redis, _ = _make_observer()
    conn = _make_conn()

    with (
        _mock_get_db(conn),
        patch("src.observer.trade_observer.time.time", return_value=1_700_000_001.0),
    ):
        await obs._handle_ws_message(
            {
                "event_type": "book",
                "market": _MARKET,
                "asset_id": _TOKEN,
                "timestamp": "1700000000000",
                "bids": [{"price": "0.44", "size": "20"}],
                "asks": [{"price": "0.46", "size": "12"}],
            }
        )

    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO book_quality_snapshots" in sql for sql in sql_calls)
    snapshot_call = next(
        call for call in conn.execute.await_args_list if "INSERT INTO book_quality_snapshots" in call.args[0]
    )
    assert snapshot_call.args[1] == _MARKET
    assert snapshot_call.args[2] == _TOKEN
    assert snapshot_call.args[5] == Decimal("0.44")
    assert snapshot_call.args[6] == Decimal("0.46")


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
        await obs._writer_run_once()

    # Phase 1 Task O: trade insert is now `conn.fetch` on a multi-row
    # VALUES INSERT … RETURNING; markets stub is `conn.executemany`.
    insert_calls = [
        c for c in conn.fetch.call_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert insert_calls
    assert conn.executemany.await_count >= 1


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

    async def capture_fetch(sql, *args):
        if "INSERT INTO trades_observed" in sql:
            captured.extend(args)
            # Synthesize one returned row so the writer publishes downstream.
            return [{
                "id": 1,
                "wallet_address": args[3],
                "market_id": args[1],
                "time": args[0],
                "side": args[4],
                "price": args[5],
                "size_usdc": args[6],
            }]
        return []

    conn.fetch = AsyncMock(side_effect=capture_fetch)

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
        await obs._writer_run_once()

    assert captured, "writer never sent the INSERT"
    # captured[0] is trade_time (first $-arg of the VALUES tuple).
    trade_time: datetime = captured[0]
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

    async def capture_fetch(sql, *args):
        if "INSERT INTO trades_observed" in sql:
            captured.extend(args)
            return [{
                "id": 1,
                "wallet_address": args[3],
                "market_id": args[1],
                "time": args[0],
                "side": args[4],
                "price": args[5],
                "size_usdc": args[6],
            }]
        return []

    conn.fetch = AsyncMock(side_effect=capture_fetch)

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
        await obs._writer_run_once()

    assert captured
    trade_time: datetime = captured[0]
    assert trade_time.tzinfo is not None
    # 1700000000 s → 2023-11-14T22:13:20Z
    assert trade_time.year == 2023
