"""
Phase 2 Task C — persistence tests for PositionTracker.

These tests verify the six contracts spelled out in the audit deliverable:

1. OPEN persists a row into `position_tracker_state` (via UPSERT).
2. CLOSE deletes the row inside the SAME transaction as the
   `positions_reconstructed` INSERT (atomicity).
3. `warm_start` rehydrates `_open_positions` from the DB.
4. Eviction triggers when slot count exceeds MAX_OPEN_POSITIONS_TRACKED
   and drops the oldest open by open_time.
5. Partial OPEN updates the row via the ON CONFLICT branch.
6. Crash mid-close (the positions_reconstructed INSERT raises) rolls
   back BOTH the INSERT and the state-row DELETE.

The mocks model asyncpg's `conn.transaction()` faithfully:

    * `conn.transaction()` is *sync* (returns a Transaction object).
    * The returned object is an *async* CM.

The shared fixture builds a connection whose `conn.execute` records every
SQL statement run, and whose `transaction()` returns a context manager
that COMMITS on clean exit and DISCARDS recorded SQL on exception.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.position_tracker import (
    OpenPosition,
    PositionTracker,
)


# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

_WALLET = "0xwallet1"
_MARKET = "0xmarket1"
_TOKEN_YES = "0xtoken_yes"
_TOKEN_NO = "0xtoken_no"

_T0 = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)


def _make_redis():
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


def _make_tracker(fee_rate=None):
    redis = _make_redis()
    tracker = PositionTracker(redis_client=redis)
    rate = Decimal(str(fee_rate)) if fee_rate is not None else Decimal("0")

    async def _stub_fee(market_id: str) -> Decimal:
        return rate

    tracker._get_fee_rate = _stub_fee
    return tracker, redis


def _make_conn(*, fail_on_sql_substr: str | None = None):
    """asyncpg-shaped mock.

    `conn.execute` and `conn.fetchrow` are AsyncMocks. The transaction CM
    rolls back recorded ops on exception (a coarse simulation of asyncpg
    behaviour — enough for the persistence assertions).
    """
    conn = AsyncMock()
    statements: list[tuple[str, tuple]] = []
    # snapshot of statements at tx-enter so a rollback can restore them
    tx_snapshot: dict = {}

    async def _execute(sql, *args):
        if fail_on_sql_substr and fail_on_sql_substr in sql:
            raise RuntimeError(f"injected failure on SQL: {fail_on_sql_substr}")
        statements.append((sql, args))

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _tx():
        # Snapshot length so we can truncate on rollback.
        tx_snapshot["len"] = len(statements)
        try:
            yield None
        except Exception:
            # asyncpg's transaction rolls back — discard everything appended
            # inside the tx so assertions can verify atomicity.
            del statements[tx_snapshot["len"]:]
            raise

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    # Expose recorded statements so tests can inspect.
    conn._statements = statements  # type: ignore[attr-defined]
    return conn


def _mock_get_db(conn):
    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch("src.observer.position_tracker.get_db", fake_get_db)


def _buy(token_id=_TOKEN_YES, time=None, price="0.60", size_usdc="600",
         size_shares="1000"):
    return {
        "wallet_address": _WALLET,
        "market_id": _MARKET,
        "token_id": token_id,
        "side": "BUY",
        "price": price,
        "size_usdc": size_usdc,
        "size_shares": size_shares,
        "time": (time or _T0).isoformat(),
    }


def _sell(token_id=_TOKEN_YES, time=None, price="0.70", size_usdc="600",
          size_shares="1000"):
    return {
        "wallet_address": _WALLET,
        "market_id": _MARKET,
        "token_id": token_id,
        "side": "SELL",
        "price": price,
        "size_usdc": size_usdc,
        "size_shares": size_shares,
        "time": (time or _T1).isoformat(),
    }


def _sql_count_substr(conn, substr: str) -> int:
    return sum(1 for sql, _ in conn._statements if substr in sql)


# ---------------------------------------------------------------------------
# 1. OPEN persists a row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_persists_state_row():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy())

    # In-memory state is populated …
    key = (_WALLET, _MARKET, _TOKEN_YES)
    assert key in tracker._open_positions

    # … and the state-table row was UPSERTed.
    upserts = _sql_count_substr(conn, "INSERT INTO position_tracker_state")
    assert upserts == 1, conn._statements

    # The state row carries the correct primary key & direction.
    sql, args = next(
        (s, a) for s, a in conn._statements
        if "INSERT INTO position_tracker_state" in s
    )
    assert args[0] == _WALLET
    assert args[1] == _MARKET
    assert args[2] == _TOKEN_YES
    assert args[3] in ("yes", "no")


# ---------------------------------------------------------------------------
# 2. CLOSE deletes the row in the same tx as positions_reconstructed insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_deletes_state_row_same_tx():
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy())
        # Reset the recorded statements so we only see the close-tx writes.
        conn._statements.clear()
        await tracker.on_trade(_sell())

    # The close-tx fired exactly one INSERT into positions_reconstructed
    # and one DELETE from position_tracker_state, IN THAT ORDER, inside
    # the same transaction (no other UPSERT in between).
    sql_seq = [s for s, _ in conn._statements]
    insert_idx = next(
        i for i, s in enumerate(sql_seq) if "INSERT INTO positions_reconstructed" in s
    )
    delete_idx = next(
        i for i, s in enumerate(sql_seq) if "DELETE FROM position_tracker_state" in s
    )
    assert delete_idx == insert_idx + 1, sql_seq

    # And the key is gone from memory.
    assert (_WALLET, _MARKET, _TOKEN_YES) not in tracker._open_positions


# ---------------------------------------------------------------------------
# 3. warm_start rehydrates _open_positions from the DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_start_rehydrates_open_positions():
    tracker, _ = _make_tracker()

    rows = [
        {
            "wallet_address": _WALLET,
            "market_id": _MARKET,
            "token_id": _TOKEN_YES,
            "direction": "yes",
            "open_time": _T0,
            "entry_price": Decimal("0.60"),
            "size_usdc": Decimal("600"),
            "size_shares": Decimal("1000"),
            "shares_remaining": Decimal("1000"),
            "fee_rate_pct": Decimal("0"),
        },
        {
            "wallet_address": _WALLET,
            "market_id": _MARKET,
            "token_id": _TOKEN_NO,
            "direction": "no",
            "open_time": _T0 + timedelta(minutes=5),
            "entry_price": Decimal("0.40"),
            "size_usdc": Decimal("400"),
            "size_shares": Decimal("1000"),
            "shares_remaining": Decimal("500"),  # partial close survived
            "fee_rate_pct": Decimal("0"),
        },
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    with _mock_get_db(conn):
        loaded = await tracker.warm_start()

    assert loaded == 2
    assert (_WALLET, _MARKET, _TOKEN_YES) in tracker._open_positions
    assert (_WALLET, _MARKET, _TOKEN_NO) in tracker._open_positions
    no_slot = tracker._open_positions[(_WALLET, _MARKET, _TOKEN_NO)][0]
    assert no_slot.shares_remaining == Decimal("500")
    assert no_slot.direction == "no"


# ---------------------------------------------------------------------------
# 4. Eviction drops the oldest open when over the limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eviction_drops_oldest_when_over_limit(monkeypatch):
    monkeypatch.setattr(
        "src.observer.position_tracker.settings.MAX_OPEN_POSITIONS_TRACKED",
        2,
        raising=False,
    )
    tracker, _ = _make_tracker()
    conn = _make_conn()

    # Pre-load three slots with strictly increasing open_time.
    base = _T0
    for i in range(3):
        key = (_WALLET, _MARKET, f"tok{i}")
        tracker._open_positions[key] = [
            OpenPosition(
                wallet_address=_WALLET,
                market_id=_MARKET,
                token_id=f"tok{i}",
                direction="yes",
                open_time=base + timedelta(minutes=i),
                entry_price=Decimal("0.50"),
                size_usdc=Decimal("100"),
                size_shares=Decimal("200"),
                shares_remaining=Decimal("200"),
            )
        ]

    with _mock_get_db(conn):
        await tracker._enforce_capacity()

    # Down to 2 keys (cap), and the OLDEST (tok0 @ base) is gone.
    remaining_keys = {k[2] for k in tracker._open_positions}
    assert "tok0" not in remaining_keys
    assert remaining_keys == {"tok1", "tok2"}

    # Eviction triggered at least one DELETE on the state table.
    deletes = _sql_count_substr(conn, "DELETE FROM position_tracker_state")
    assert deletes >= 1, conn._statements


# ---------------------------------------------------------------------------
# 5. Partial OPEN updates the row (ON CONFLICT path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_open_upserts_existing_row():
    """Two BUYs on the SAME (wallet, market, token) collapse onto one
    state-row whose size fields aggregate over both slots. The second
    persist call hits the ON CONFLICT DO UPDATE branch."""
    tracker, _ = _make_tracker()
    conn = _make_conn()

    with _mock_get_db(conn):
        await tracker.on_trade(_buy(price="0.60", size_usdc="600", size_shares="1000"))
        await tracker.on_trade(
            _buy(
                price="0.65",
                size_usdc="650",
                size_shares="1000",
                time=_T0 + timedelta(minutes=1),
            )
        )

    # Two UPSERT statements observed (one per OPEN). Both target the SAME
    # primary key — the second is the ON CONFLICT branch from the table's
    # perspective.
    upserts = [
        (sql, args) for sql, args in conn._statements
        if "INSERT INTO position_tracker_state" in sql
    ]
    assert len(upserts) == 2
    # The second UPSERT carries the aggregated size_shares == 2000.
    _, second_args = upserts[1]
    # args layout: 0 wallet, 1 market, 2 token, 3 direction, 4 open_time,
    #              5 entry_price, 6 size_usdc, 7 size_shares,
    #              8 shares_remaining, 9 fee_rate_pct, 10 state_json
    assert Decimal(str(second_args[7])) == Decimal("2000")
    assert Decimal(str(second_args[8])) == Decimal("2000")

    # The UPSERT SQL has ON CONFLICT DO UPDATE → the table-level merge is
    # what realises the partial-OPEN semantics.
    assert "ON CONFLICT" in upserts[0][0]


# ---------------------------------------------------------------------------
# 6. Crash mid-close rolls back BOTH writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_mid_close_rolls_back_both_writes():
    """If positions_reconstructed INSERT raises mid-tx, the same-tx
    DELETE on position_tracker_state must also roll back. Verified
    indirectly: no positions_reconstructed INSERT AND no
    position_tracker_state DELETE appear in the recorded statements
    after the close attempt — the simulated tx rollback discarded them."""
    tracker, _ = _make_tracker()
    conn = _make_conn(fail_on_sql_substr="INSERT INTO positions_reconstructed")

    with _mock_get_db(conn):
        await tracker.on_trade(_buy())
        # Wipe the OPEN-side recordings; we're interested in the CLOSE tx.
        conn._statements.clear()
        await tracker.on_trade(_sell())

    # The close path swallowed the error (logger.error + return) — verify
    # neither write committed.
    inserts_after_close = _sql_count_substr(
        conn, "INSERT INTO positions_reconstructed"
    )
    deletes_after_close = _sql_count_substr(
        conn, "DELETE FROM position_tracker_state"
    )
    # Note: the failing INSERT was attempted (so its execute call DID
    # raise), but the tx rollback drops it from `_statements` together
    # with everything that would have followed inside the tx.
    assert inserts_after_close == 0
    assert deletes_after_close == 0


# ---------------------------------------------------------------------------
# Bonus: warm_start metric increments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_start_increments_counter():
    """A loaded row bumps polybot_position_tracker_warm_start_loaded_total."""
    from src.monitoring.metrics import position_tracker_warm_start_loaded_total

    tracker, _ = _make_tracker()
    rows = [
        {
            "wallet_address": _WALLET,
            "market_id": _MARKET,
            "token_id": _TOKEN_YES,
            "direction": "yes",
            "open_time": _T0,
            "entry_price": Decimal("0.60"),
            "size_usdc": Decimal("600"),
            "size_shares": Decimal("1000"),
            "shares_remaining": Decimal("1000"),
            "fee_rate_pct": Decimal("0"),
        },
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    before = position_tracker_warm_start_loaded_total._value.get()  # type: ignore[attr-defined]
    with _mock_get_db(conn):
        await tracker.warm_start()
    after = position_tracker_warm_start_loaded_total._value.get()  # type: ignore[attr-defined]
    assert after - before == 1
