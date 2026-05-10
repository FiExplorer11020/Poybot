"""
Unit tests for src/observer/position_tracker.py
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.position_tracker import (
    REDIS_POSITIONS_CHANNEL,
    PositionTracker,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WALLET = "0xwallet1"
_MARKET = "0xmarket1"
_TOKEN_YES = "0xtoken_yes"
_TOKEN_NO = "0xtoken_no"

_T0 = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2024, 6, 1, 11, 0, 0, tzinfo=timezone.utc)  # T0 + 1 hour


# ---------------------------------------------------------------------------
# Phase 2 Task C: PositionTracker now writes to position_tracker_state too.
# Each `conn.execute(...)` no longer corresponds 1:1 to a positions_reconstructed
# INSERT — tests that introspect call args need to filter for the INSERT call
# specifically. These helpers keep the existing assertions readable.
# ---------------------------------------------------------------------------


_CLOSE_INSERT_SQL_FRAGMENT = "INSERT INTO positions_reconstructed"


def _close_insert_calls(conn):
    """Return only the execute() calls that wrote to positions_reconstructed."""
    return [
        c for c in conn.execute.call_args_list
        if c.args and isinstance(c.args[0], str)
        and _CLOSE_INSERT_SQL_FRAGMENT in c.args[0]
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis():
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


def _make_tracker(fee_rate=None):
    """Return a PositionTracker whose _get_fee_rate is stubbed to return fee_rate."""
    redis = _make_redis()
    tracker = PositionTracker(redis_client=redis)
    rate = Decimal(str(fee_rate)) if fee_rate is not None else Decimal("0")

    async def _stub_fee(market_id: str) -> Decimal:
        return rate

    tracker._get_fee_rate = _stub_fee
    return tracker, redis


def _make_conn(fee_row=None):
    """Return a mock asyncpg connection.

    `conn.transaction()` returns a no-op async context manager so production
    code calling `async with conn.transaction():` works under unit tests.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fee_row)

    @asynccontextmanager
    async def _tx():
        yield None

    # `transaction()` itself is sync (returns the Transaction object) — only
    # the returned object is an async CM. Hence MagicMock, not AsyncMock.
    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    return conn


def _mock_get_db(conn):
    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch("src.observer.position_tracker.get_db", fake_get_db)


def _buy_trade(
    wallet=_WALLET,
    market_id=_MARKET,
    token_id=_TOKEN_YES,
    price="0.60",
    size_usdc="600",
    size_shares="1000",
    time=None,
):
    return {
        "wallet_address": wallet,
        "market_id": market_id,
        "token_id": token_id,
        "side": "BUY",
        "price": price,
        "size_usdc": size_usdc,
        "size_shares": size_shares,
        "time": (time or _T0).isoformat(),
    }


def _sell_trade(
    wallet=_WALLET,
    market_id=_MARKET,
    token_id=_TOKEN_YES,
    price="0.70",
    size_usdc="600",
    size_shares="1000",
    time=None,
):
    return {
        "wallet_address": wallet,
        "market_id": market_id,
        "token_id": token_id,
        "side": "SELL",
        "price": price,
        "size_usdc": size_usdc,
        "size_shares": size_shares,
        "time": (time or _T1).isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. BUY opens a position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_opens_position():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade())

    key = (_WALLET, _MARKET, _TOKEN_YES)
    assert key in tracker._open_positions
    positions = tracker._open_positions[key]
    assert len(positions) == 1
    assert positions[0].entry_price == Decimal("0.60")
    assert positions[0].size_usdc == Decimal("600")
    assert positions[0].size_shares == Decimal("1000")
    assert positions[0].shares_remaining == Decimal("1000")


# ---------------------------------------------------------------------------
# 2. SELL fully closes an open position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_closes_position():
    tracker, redis = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade())
        await tracker.on_trade(_sell_trade())

    key = (_WALLET, _MARKET, _TOKEN_YES)
    assert key not in tracker._open_positions

    # Exactly one positions_reconstructed INSERT for the close (the OPEN
    # UPSERT and the same-tx state DELETE add extra execute() calls — see
    # Phase 2 Task C).
    inserts = _close_insert_calls(conn)
    assert len(inserts) == 1
    args = inserts[0].args
    assert args[13] == "sell"


# ---------------------------------------------------------------------------
# 3. Partial close splits position correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_close_splits_position():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade(size_usdc="600", size_shares="1000"))
        await tracker.on_trade(_sell_trade(size_usdc="280", size_shares="400"))

    key = (_WALLET, _MARKET, _TOKEN_YES)
    assert key in tracker._open_positions
    remaining_pos = tracker._open_positions[key][0]
    assert remaining_pos.shares_remaining == Decimal("600")

    # Exactly one positions_reconstructed INSERT for the partial close.
    inserts = _close_insert_calls(conn)
    assert len(inserts) == 1


# ---------------------------------------------------------------------------
# 4. PnL calculation — profit scenario (no fees)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_calculation_profit():
    tracker, _ = _make_tracker(fee_rate=0)
    # Capture (sql, args) tuples so we can pick out the close INSERT.
    captured: list[tuple[str, tuple]] = []

    conn = _make_conn()

    async def capture_execute(sql, *args):
        captured.append((sql, args))

    conn.execute = AsyncMock(side_effect=capture_execute)

    with _mock_get_db(conn):
        # BUY at 0.60 for 600 USDC
        await tracker.on_trade(_buy_trade(price="0.60", size_usdc="600"))
        # SELL at 0.70 for 600 USDC
        await tracker.on_trade(_sell_trade(price="0.70", size_usdc="600"))

    # Find the positions_reconstructed INSERT (Phase 2 Task C also UPSERTs
    # `position_tracker_state` and DELETEs it inside the same close tx).
    insert_args = next(
        a for s, a in captured if _CLOSE_INSERT_SQL_FRAGMENT in s
    )
    # pnl_usdc is the 10th positional arg (index 9, 0-based).
    pnl_usdc = insert_args[9]

    # shares=1000; gross = (0.70 - 0.60) * 1000 = 100 USDC
    assert abs(float(pnl_usdc) - 100.0) < 0.02


# ---------------------------------------------------------------------------
# 5. Fee deduction reduces PnL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_deduction_in_pnl():
    tracker, _ = _make_tracker(fee_rate=0.01)  # 1% fee as decimal rate
    captured: list[tuple[str, tuple]] = []

    conn = _make_conn()

    async def capture_execute(sql, *args):
        captured.append((sql, args))

    conn.execute = AsyncMock(side_effect=capture_execute)

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade(price="0.60", size_usdc="600"))
        await tracker.on_trade(_sell_trade(price="0.70", size_usdc="600"))

    insert_args = next(
        a for s, a in captured if _CLOSE_INSERT_SQL_FRAGMENT in s
    )
    pnl_usdc = insert_args[9]

    # gross = (0.70 - 0.60) * 1000 = 100
    # entry_fee = 1000 * 0.01 * 0.60 * 0.40 = 2.40
    # exit_fee = 1000 * 0.01 * 0.70 * 0.30 = 2.10
    # net = 100 - 2.40 - 2.10 = 95.50
    assert abs(float(pnl_usdc) - 95.50) < 0.02


# ---------------------------------------------------------------------------
# 6. Holding period calculated correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holding_period_calculated():
    tracker, _ = _make_tracker()
    captured: list[tuple[str, tuple]] = []

    conn = _make_conn()

    async def capture_execute(sql, *args):
        captured.append((sql, args))

    conn.execute = AsyncMock(side_effect=capture_execute)

    open_time = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    close_time = datetime(2024, 6, 1, 11, 0, 0, tzinfo=timezone.utc)  # +3600s

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade(time=open_time))
        await tracker.on_trade(_sell_trade(time=close_time))

    insert_args = next(
        a for s, a in captured if _CLOSE_INSERT_SQL_FRAGMENT in s
    )
    # holding_period_s is the 12th positional arg (index 11).
    holding_s = insert_args[11]
    assert holding_s == 3600


# ---------------------------------------------------------------------------
# 7. Market resolution closes all open positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_resolution_closes_all():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        # Open two positions on the same market, different tokens
        await tracker.on_trade(_buy_trade(token_id="0xtok_a", size_usdc="500"))
        await tracker.on_trade(_buy_trade(token_id="0xtok_b", size_usdc="300"))

        await tracker.close_market_positions(_MARKET, Decimal("1.0"))

    # Both positions should be closed and removed
    remaining = [k for k in tracker._open_positions if k[1] == _MARKET]
    assert remaining == []

    # Exactly two positions_reconstructed INSERTs (one per close), both
    # with close_method='resolution'. The OPEN UPSERTs and same-tx DELETEs
    # add extra execute() calls — see Phase 2 Task C.
    inserts = _close_insert_calls(conn)
    assert len(inserts) == 2
    for c in inserts:
        method = c.args[13]
        assert method == "resolution"


# ---------------------------------------------------------------------------
# 8. SELL without matching open position is ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_without_open_position_ignored():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_sell_trade())

    conn.execute.assert_not_awaited()
    assert tracker._open_positions == {}


# ---------------------------------------------------------------------------
# 9. Redis publish called on position close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_publish_on_close():
    tracker, redis = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade())
        await tracker.on_trade(_sell_trade())

    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.call_args[0]
    assert channel == REDIS_POSITIONS_CHANNEL
    event = json.loads(payload)
    assert event["wallet_address"] == _WALLET
    assert event["close_method"] == "sell"


# ---------------------------------------------------------------------------
# 10. on_trade ignores trade with missing wallet_address
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_exit_closes_sibling_position():
    """
    Buying the complementary token within MERGE_WINDOW_S with matching size
    should close the existing YES position as 'merge' at exit_price = 1 - 0.40.
    """
    tracker, redis = _make_tracker()
    # Prime the token cache so _sibling_token resolves without hitting the DB
    tracker._market_tokens[_MARKET] = (_TOKEN_YES, _TOKEN_NO)
    conn = _make_conn()

    captured: list[tuple[str, tuple]] = []

    async def capture_execute(sql, *args):
        captured.append((sql, args))

    conn.execute = AsyncMock(side_effect=capture_execute)

    with _mock_get_db(conn):
        # BUY YES at 0.60 for 1000 shares
        await tracker.on_trade(_buy_trade(token_id=_TOKEN_YES, price="0.60"))
        # BUY NO at 0.40 for matching 1000 shares 10 min later → merge exit
        await tracker.on_trade(
            _buy_trade(
                token_id=_TOKEN_NO,
                price="0.40",
                size_usdc="400",
                size_shares="1000",
                time=_T0.replace(minute=5),
            )
        )

    # Sibling YES position should be closed via merge; no fresh open from the
    # complementary BUY (sizes matched exactly).
    assert (_WALLET, _MARKET, _TOKEN_YES) not in tracker._open_positions
    assert (_WALLET, _MARKET, _TOKEN_NO) not in tracker._open_positions

    # Filter for the positions_reconstructed INSERT — Phase 2 Task C now
    # also UPSERTs `position_tracker_state` on the OPEN and DELETEs it
    # inside the close tx, so total execute() calls > 1.
    close_inserts = [
        a for s, a in captured if _CLOSE_INSERT_SQL_FRAGMENT in s
    ]
    assert len(close_inserts) == 1
    close_args = close_inserts[0]
    # close_method is $13 → index 12
    assert close_args[12] == "merge"
    # exit_price $8 → index 7 should be 1 - 0.40 = 0.60
    assert abs(float(close_args[7]) - 0.60) < 1e-6


@pytest.mark.asyncio
async def test_merge_skipped_outside_window():
    """A complementary buy outside MERGE_WINDOW_S should NOT merge."""
    from datetime import timedelta

    tracker, _ = _make_tracker()
    tracker._market_tokens[_MARKET] = (_TOKEN_YES, _TOKEN_NO)
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy_trade(token_id=_TOKEN_YES, price="0.60"))
        # 2 hours later — well outside the 10-minute MERGE_WINDOW_S
        late = _T0 + timedelta(hours=2)
        await tracker.on_trade(
            _buy_trade(
                token_id=_TOKEN_NO,
                price="0.40",
                size_usdc="400",
                size_shares="1000",
                time=late,
            )
        )

    # Both positions remain open (no merge)
    assert (_WALLET, _MARKET, _TOKEN_YES) in tracker._open_positions
    assert (_WALLET, _MARKET, _TOKEN_NO) in tracker._open_positions
    # No positions_reconstructed INSERT was issued — the OPEN UPSERTs into
    # `position_tracker_state` still fire (Phase 2 Task C) so we can't
    # claim `assert_not_awaited()` anymore. The contract here is "no close
    # was recorded".
    assert _close_insert_calls(conn) == []


@pytest.mark.asyncio
async def test_on_trade_ignores_missing_fields():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    bad_trade = {
        # wallet_address is missing
        "market_id": _MARKET,
        "token_id": _TOKEN_YES,
        "side": "BUY",
        "price": "0.60",
        "size_usdc": "100",
        "time": _T0.isoformat(),
    }

    with _mock_get_db(conn):
        await tracker.on_trade(bad_trade)

    conn.execute.assert_not_awaited()
    assert tracker._open_positions == {}
