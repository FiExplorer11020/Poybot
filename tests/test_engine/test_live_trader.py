"""
Tests for src/engine/live_trader.py.

LiveTrader orchestrates: pre-flight vetos -> insert live_trades row ->
delegate execution to OrderManager -> persist outcome. We mock
OrderManager + the DB connection + the CLOB client so no real order or
SQL fires.

Coverage:
  * Pre-flight vetos (size below min, missing market_id, conflict).
  * Shadow path inserts live_trades.status='shadow' and never updates to 'open'.
  * Filled path updates status to 'open' and adds to in-memory list.
  * Failed/canceled path updates status to 'failed' / 'canceled'.
  * close_trade computes pnl, fee, updates DB, removes from memory.
  * Rehydration on start() loads existing 'open' trades from DB.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine.live_trader import LiveTrader, OpenLiveTrade
from src.engine.order_manager import OrderOutcome


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _make_conn(*, fetchval_returns=1, fetch_returns=None):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=fetchval_returns)
    conn.fetch = AsyncMock(return_value=fetch_returns or [])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


def _patch_db(monkeypatch, fake_conn):
    @asynccontextmanager
    async def _get_db():
        yield fake_conn

    monkeypatch.setattr("src.engine.live_trader.get_db", _get_db)


def _make_clob(*, dry_run: bool = False, midpoint: float = 0.50):
    clob = MagicMock()
    clob.dry_run = dry_run
    clob.get_midpoint = AsyncMock(return_value=midpoint)
    return clob


def _make_order_manager(outcome: OrderOutcome):
    om = MagicMock()
    om.place_for_position = AsyncMock(return_value=outcome)
    return om


def _make_redis():
    r = MagicMock()
    r.publish = AsyncMock(return_value=1)
    return r


@pytest.fixture(autouse=True)
def _stub_killswitch(monkeypatch):
    """Stub the strict-path killswitch lookup that ``LiveTrader.open_trade``
    now performs before insertion (audit F-05 fix). Tests that need to
    exercise the killswitch veto override this fixture explicitly.

    Default: real-execution allowed, so existing assertions still hold.
    """
    fake = MagicMock()
    fake.is_real_execution_enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.engine.live_trader.get_killswitch", lambda: fake
    )
    return fake


def _decision(**overrides):
    base = {
        "market_id": "0xMarket",
        "token_id": "tok-1",
        "action": "follow",
        "size_usdc": 100.0,
        "confidence": 0.8,
        "leader_wallet": "0xLeader",
        "trade_context": {"foo": "bar"},
        "direction": "yes",
    }
    base.update(overrides)
    return base


def _trader(*, conn, dry_run=False, om_outcome=None):
    if om_outcome is None:
        om_outcome = OrderOutcome(
            filled=True, filled_size_shares=200.0, avg_fill_price=0.50,
            fee_paid_usdc=1.0, last_clob_order_id="ord-1", attempts=1,
            final_state="filled",
        )
    return LiveTrader(
        redis_client=_make_redis(),
        clob_client=_make_clob(dry_run=dry_run),
        order_manager=_make_order_manager(om_outcome),
    )


# --------------------------------------------------------------------------- #
# Pre-flight vetos                                                             #
# --------------------------------------------------------------------------- #


async def test_open_trade_vetos_size_below_min(monkeypatch):
    conn = _make_conn()
    _patch_db(monkeypatch, conn)
    monkeypatch.setattr("src.engine.live_trader.settings.MIN_POSITION_USDC", 50.0)

    trader = _trader(conn=conn)
    res = await trader.open_trade(_decision(size_usdc=10.0))
    assert res is None
    # No DB insert happened.
    conn.fetchval.assert_not_called()


async def test_open_trade_vetos_missing_market_id(monkeypatch):
    conn = _make_conn()
    _patch_db(monkeypatch, conn)
    trader = _trader(conn=conn)
    res = await trader.open_trade(_decision(market_id=""))
    assert res is None
    conn.fetchval.assert_not_called()


async def test_open_trade_vetos_unknown_action(monkeypatch):
    conn = _make_conn()
    _patch_db(monkeypatch, conn)
    trader = _trader(conn=conn)
    res = await trader.open_trade(_decision(action="hold"))
    assert res is None


async def test_open_trade_vetos_existing_conflict(monkeypatch):
    """A live_trades row already exists for this (market, leader, strategy)."""
    # First call to fetchval returns 1 (= conflict count > 0).
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)
    trader = _trader(conn=conn)
    res = await trader.open_trade(_decision())
    assert res is None
    # Only the conflict-check fetchval was called, no INSERT.
    assert conn.fetchval.await_count == 1


# --------------------------------------------------------------------------- #
# Shadow path                                                                  #
# --------------------------------------------------------------------------- #


async def test_open_trade_shadow_inserts_row_and_returns_id(monkeypatch):
    # Conflict-check returns 0, then INSERT returns id 77.
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 77])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
        fee_paid_usdc=0.0, last_clob_order_id=None, attempts=1,
        final_state="shadow",
    )
    trader = _trader(conn=conn, dry_run=True, om_outcome=om_outcome)
    res = await trader.open_trade(_decision())
    assert res == 77
    # Status='shadow' was used in the INSERT (5th positional arg pattern depends
    # on SQL — easier to just verify INSERT was made with correct status arg).
    insert_call = conn.fetchval.await_args_list[1]
    # args[0] is the SQL query; SQL params start at args[1].
    # Order: market_id, token_id, direction, size_usdc, action,
    # leader_wallet, leader_context, confidence, initial_status, ...
    args = insert_call.args
    assert args[9] == "shadow"
    # Position not added to in-memory open list (shadow is not 'open').
    assert trader.open_trades == []


# --------------------------------------------------------------------------- #
# Filled path                                                                  #
# --------------------------------------------------------------------------- #


async def test_open_trade_filled_updates_status_open_and_tracks_position(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 88])  # no conflict, INSERT id=88
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=True, filled_size_shares=200.0, avg_fill_price=0.5025,
        fee_paid_usdc=0.50, last_clob_order_id="ord-77", attempts=2,
        final_state="filled",
    )
    trader = _trader(conn=conn, dry_run=False, om_outcome=om_outcome)
    res = await trader.open_trade(_decision())
    assert res == 88

    # UPDATE to 'open' was issued.
    update_calls = [c for c in conn.execute.await_args_list]
    assert len(update_calls) >= 1
    update_args = update_calls[0].args
    # args[0] is the SQL string. Then: live_trade_id, avg_fill_price,
    # filled_usdc, fee_paid_usdc, clob_order_id, attempts.
    assert update_args[1] == 88
    assert update_args[2] == pytest.approx(0.5025)
    assert update_args[4] == pytest.approx(0.50)  # fee_paid

    # Position recorded in memory.
    assert len(trader.open_trades) == 1
    pos = trader.open_trades[0]
    assert pos.id == 88
    assert pos.entry_price == pytest.approx(0.5025)
    assert pos.size_shares == pytest.approx(200.0)


async def test_open_trade_publishes_redis_event_on_fill(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 99])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    trader = _trader(conn=conn)
    await trader.open_trade(_decision())
    trader._redis.publish.assert_awaited()  # at least one publish (positions:live_opened)
    channel, payload = trader._redis.publish.await_args.args
    assert channel == "positions:live_opened"
    body = json.loads(payload)
    assert body["trade_id"] == 99


# --------------------------------------------------------------------------- #
# Failed paths                                                                 #
# --------------------------------------------------------------------------- #


async def test_open_trade_rejected_marks_failed(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 200])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
        fee_paid_usdc=0.0, last_clob_order_id=None, attempts=1,
        final_state="rejected", error_message="insufficient_balance",
    )
    trader = _trader(conn=conn, om_outcome=om_outcome)
    res = await trader.open_trade(_decision())
    assert res is None

    update_args = conn.execute.await_args.args
    # args[0] is SQL; then: live_trade_id, status, close_reason, attempts.
    assert update_args[1] == 200
    assert update_args[2] == "failed"
    # close_reason is the error message, truncated to 50.
    assert update_args[3].startswith("insufficient_balance")
    assert trader.open_trades == []


async def test_open_trade_canceled_marks_canceled(monkeypatch):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 201])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
        fee_paid_usdc=0.0, last_clob_order_id="ord-1", attempts=3,
        final_state="canceled", error_message="timeout_no_fill_in_30s",
    )
    trader = _trader(conn=conn, om_outcome=om_outcome)
    res = await trader.open_trade(_decision())
    assert res is None
    update_args = conn.execute.await_args.args
    # args[0] is SQL; args[2] is the new status.
    assert update_args[2] == "canceled"


# --------------------------------------------------------------------------- #
# close_trade                                                                  #
# --------------------------------------------------------------------------- #


async def test_close_trade_filled_path_updates_db_and_clears_memory(monkeypatch):
    conn = _make_conn()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=True, filled_size_shares=200.0, avg_fill_price=0.55,
        fee_paid_usdc=0.40, last_clob_order_id="exit-1", attempts=1,
        final_state="filled",
    )
    trader = _trader(conn=conn, om_outcome=om_outcome)
    # Seed an open position by hand.
    from datetime import datetime, timezone
    trader._open_trades.append(OpenLiveTrade(
        id=300, market_id="0xM", token_id="tok", direction="yes",
        strategy="follow", entry_price=0.50, size_usdc=100.0,
        leader_wallet="0xL", confidence=0.8, fee_paid_usdc=0.5,
        size_shares=200.0, opened_at=datetime.now(timezone.utc),
    ))

    ok = await trader.close_trade(300, exit_price=0.55, close_reason="take_profit")
    assert ok is True
    assert trader.open_trades == []  # removed from memory

    # args[0] is SQL; then: id, status, exit_price, pnl_usdc, fee_paid_usdc,
    # close_reason, exit_clob_order_id.
    update_args = conn.execute.await_args.args
    assert update_args[1] == 300
    assert update_args[2] == "closed"
    assert update_args[3] == pytest.approx(0.55)
    # PnL = (0.55 - 0.50) * 200 - exit_fee. Should be positive.
    assert update_args[4] > 0


async def test_close_trade_rejects_unknown_id():
    trader = LiveTrader(
        redis_client=_make_redis(),
        clob_client=_make_clob(),
        order_manager=_make_order_manager(OrderOutcome(
            filled=False, filled_size_shares=0, avg_fill_price=0,
            fee_paid_usdc=0, last_clob_order_id=None, attempts=0,
            final_state="rejected",
        )),
    )
    ok = await trader.close_trade(9999, exit_price=0.55, close_reason="manual")
    assert ok is False


async def test_close_trade_shadow_path_collapses_position(monkeypatch):
    conn = _make_conn()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
        fee_paid_usdc=0.0, last_clob_order_id=None, attempts=1,
        final_state="shadow",
    )
    trader = _trader(conn=conn, dry_run=True, om_outcome=om_outcome)
    from datetime import datetime, timezone
    trader._open_trades.append(OpenLiveTrade(
        id=301, market_id="0xM", token_id="tok", direction="yes",
        strategy="follow", entry_price=0.50, size_usdc=100.0,
        leader_wallet="0xL", confidence=0.8, fee_paid_usdc=0.0,
        size_shares=200.0, opened_at=datetime.now(timezone.utc),
    ))

    ok = await trader.close_trade(301, exit_price=0.60, close_reason="take_profit")
    assert ok is True
    assert trader.open_trades == []


# --------------------------------------------------------------------------- #
# Rehydration                                                                  #
# --------------------------------------------------------------------------- #


async def test_reload_open_trades_rehydrates_from_db(monkeypatch):
    from datetime import datetime, timezone
    rows = [
        {
            "id": 1, "market_id": "0xA", "token_id": "tok-A",
            "direction": "yes", "strategy": "follow",
            "entry_price": 0.50, "size_usdc": 100.0, "leader_wallet": "0xL",
            "confidence": 0.8, "fee_paid_usdc": 0.5,
            "opened_at": datetime.now(timezone.utc),
            "leader_context": json.dumps({"k": "v"}),
        },
        {
            "id": 2, "market_id": "0xB", "token_id": "tok-B",
            "direction": "no", "strategy": "fade",
            "entry_price": 0.40, "size_usdc": 50.0, "leader_wallet": "0xL2",
            "confidence": 0.7, "fee_paid_usdc": 0.3,
            "opened_at": datetime.now(timezone.utc),
            "leader_context": None,
        },
    ]
    conn = _make_conn(fetch_returns=rows)
    _patch_db(monkeypatch, conn)

    trader = _trader(conn=conn)
    await trader._reload_open_trades()
    assert len(trader.open_trades) == 2
    ids = sorted(t.id for t in trader.open_trades)
    assert ids == [1, 2]
    # leader_context decoded from JSON
    a = next(t for t in trader.open_trades if t.id == 1)
    assert a.leader_context == {"k": "v"}
    b = next(t for t in trader.open_trades if t.id == 2)
    assert b.leader_context == {}


# --------------------------------------------------------------------------- #
# Strict-path killswitch gate (audit F-05)                                     #
# --------------------------------------------------------------------------- #


async def test_open_trade_vetoes_when_killswitch_real_off(monkeypatch, _stub_killswitch):
    """When the killswitch reports real_execution_enabled=False on the
    strict path, LiveTrader must refuse the order BEFORE inserting any
    DB row or calling OrderManager. This closes the F-05 2s leak window
    where a stale Redis cache could otherwise let a trade slip through.
    """
    _stub_killswitch.is_real_execution_enabled = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    trader = _trader(conn=conn, dry_run=False)
    res = await trader.open_trade(_decision())

    assert res is None, "must refuse the live order"
    # The killswitch must have been consulted on the STRICT path.
    _stub_killswitch.is_real_execution_enabled.assert_awaited_once_with(
        bypass_cache=True
    )
    # No DB write happened (no INSERT, no UPDATE).
    conn.fetchval.assert_not_called()
    conn.execute.assert_not_called()
    # And no order was placed.
    trader._order_manager.place_for_position.assert_not_called()
    assert trader.open_trades == []


async def test_open_trade_strict_path_used_not_cached(monkeypatch, _stub_killswitch):
    """LiveTrader must call is_real_execution_enabled with bypass_cache=True
    (not the cached fast path). Documents the API contract."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 1])  # no conflict, INSERT id=1
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    trader = _trader(conn=conn, dry_run=False)
    await trader.open_trade(_decision())

    _stub_killswitch.is_real_execution_enabled.assert_awaited_once_with(
        bypass_cache=True
    )


async def test_open_trade_skips_killswitch_check_in_dry_run(monkeypatch, _stub_killswitch):
    """In dry_run mode no real order goes out, so the strict-path
    killswitch check is a no-op (we preserve shadow-row behavior for
    benchmark comparisons). Tested explicitly to lock this contract."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[0, 42])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    om_outcome = OrderOutcome(
        filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
        fee_paid_usdc=0.0, last_clob_order_id=None, attempts=1,
        final_state="shadow",
    )
    trader = _trader(conn=conn, dry_run=True, om_outcome=om_outcome)
    res = await trader.open_trade(_decision())

    assert res == 42  # shadow row was inserted normally
    _stub_killswitch.is_real_execution_enabled.assert_not_called()


async def test_open_trade_killswitch_read_failure_refuses_trade(monkeypatch, _stub_killswitch):
    """If the strict-path read itself raises, fail SAFE — never assume ON."""
    _stub_killswitch.is_real_execution_enabled = AsyncMock(
        side_effect=ConnectionError("redis+db down")
    )

    conn = MagicMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    _patch_db(monkeypatch, conn)

    trader = _trader(conn=conn, dry_run=False)
    res = await trader.open_trade(_decision())

    assert res is None
    conn.fetchval.assert_not_called()
    trader._order_manager.place_for_position.assert_not_called()
