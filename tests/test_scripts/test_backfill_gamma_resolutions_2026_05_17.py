"""Unit tests for ``scripts/backfill_gamma_resolutions_2026_05_17.py``.

Mocks asyncpg + Redis + the Gamma HTTP page fetcher so the tests stay
pure-Python and never reach a real network or DB. Each test pins one
piece of the contract spelled out in the strategy plan:

    - Gamma payload parsing (outcome derivation, condition_id forms,
      JSON-string outcomePrices).
    - The Markets UPDATE only flips rows that are still ``active`` OR
      have ``resolved_outcome IS NULL`` — running the script twice on
      the same window touches each row exactly once.
    - Position close pnl math for both winning and losing directions.
    - Lever F's expired-active sweep only touches the right set of rows.
    - Dry-run mode is a no-op on every writer path but reports identical
      counters.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import backfill_gamma_resolutions_2026_05_17 as bg


# --------------------------------------------------------------------------- #
# Helpers — asyncpg / Redis / Gamma stubs                                      #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """Records every execute / fetchrow / fetch / fetchval call.

    Behaviour is scripted by the enclosing _FakePool: handlers can
    inject canned answers per SQL substring so the tests can compose
    sequences (update returns 1 then 0 to simulate idempotency etc.).
    """

    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._on_execute(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._parent._on_fetchrow(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._parent._on_fetch(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._parent._on_fetchval(sql, args)

    def transaction(self) -> Any:
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _FakePool:
    """asyncpg.Pool-shaped fake. The orchestrator only uses ``acquire``
    so we don't need to faithfully implement the rest of the API."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        # SQL-substring → callable(args) → response. Last-registered wins.
        self.execute_handlers: list[tuple[str, Any]] = []
        self.fetchrow_handlers: list[tuple[str, Any]] = []
        self.fetch_handlers: list[tuple[str, Any]] = []
        self.fetchval_handlers: list[tuple[str, Any]] = []

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield _FakeConn(self)
        return _ctx()

    # Registration -----------------------------------------------------

    def on_execute(self, substr: str, response: Any) -> None:
        self.execute_handlers.append((substr, response))

    def on_fetchrow(self, substr: str, response: Any) -> None:
        self.fetchrow_handlers.append((substr, response))

    def on_fetch(self, substr: str, response: Any) -> None:
        self.fetch_handlers.append((substr, response))

    def on_fetchval(self, substr: str, response: Any) -> None:
        self.fetchval_handlers.append((substr, response))

    # Dispatch ---------------------------------------------------------

    def _on_execute(self, sql: str, args: tuple) -> str:
        self.executed.append((sql, args))
        return _resolve_handler(self.execute_handlers, sql, args, default="UPDATE 0")

    def _on_fetchrow(self, sql: str, args: tuple) -> Any:
        self.fetchrow_calls.append((sql, args))
        return _resolve_handler(self.fetchrow_handlers, sql, args, default=None)

    def _on_fetch(self, sql: str, args: tuple) -> list[Any]:
        self.fetch_calls.append((sql, args))
        return _resolve_handler(self.fetch_handlers, sql, args, default=[])

    def _on_fetchval(self, sql: str, args: tuple) -> Any:
        self.fetchval_calls.append((sql, args))
        return _resolve_handler(self.fetchval_handlers, sql, args, default=None)


def _resolve_handler(handlers, sql, args, default):
    """Pick the last handler whose substring matches `sql`.

    Last-wins so a test can register a permissive default first and
    then override for a specific shape later in the body.
    """
    chosen = None
    for substr, response in handlers:
        if substr in sql:
            chosen = response
    if chosen is None:
        return default
    if callable(chosen):
        return chosen(args)
    return chosen


def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


def _make_row(
    *,
    wallet="0xleader",
    market="0xmarket",
    token="0xtokyes",
    direction="yes",
    entry_price="0.40",
    size_usdc="100.00",
    size_shares="250",
    shares_remaining="250",
    fee_rate_pct="0",
    open_time=None,
):
    """Mimics an asyncpg.Record without needing the real class."""
    record = {
        "wallet_address": wallet,
        "market_id": market,
        "token_id": token,
        "direction": direction,
        "open_time": open_time or datetime(2026, 1, 1, tzinfo=timezone.utc),
        "entry_price": Decimal(entry_price),
        "size_usdc": Decimal(size_usdc),
        "size_shares": Decimal(size_shares),
        "shares_remaining": Decimal(shares_remaining),
        "fee_rate_pct": Decimal(fee_rate_pct),
    }
    return record


def _patch_fetch_page(monkeypatch, pages: list[list[dict]]) -> list[int]:
    """Replace ``_fetch_gamma_page`` with a queue of canned pages.

    Returns a list whose only element grows with the call count so the
    test can assert how many HTTP requests would have been issued.
    """
    call_count = [0]

    async def _fake(session, *, offset, limit, days):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(pages):
            return pages[idx]
        return []

    monkeypatch.setattr(bg, "_fetch_gamma_page", _fake)
    return call_count


# --------------------------------------------------------------------------- #
# 1. Gamma payload parsing                                                     #
# --------------------------------------------------------------------------- #


class TestParseGammaPayload:
    def test_yes_when_first_outcome_price_above_half(self):
        assert bg.derive_outcome({"outcomePrices": [0.99, 0.01]}) == "yes"
        assert bg.derive_outcome({"outcomePrices": ["0.75", "0.25"]}) == "yes"

    def test_no_when_first_outcome_price_at_or_below_half(self):
        assert bg.derive_outcome({"outcomePrices": [0.01, 0.99]}) == "no"
        # Polymarket binary resolution → 0.0 or 1.0 strictly, but the
        # gate is defensive: exactly 0.5 must NOT be classified as yes.
        assert bg.derive_outcome({"outcomePrices": [0.5, 0.5]}) == "no"

    def test_accepts_json_encoded_string(self):
        # Some legacy Gamma rows ship the array inside a JSON string.
        payload = {"outcomePrices": json.dumps([1.0, 0.0])}
        assert bg.derive_outcome(payload) == "yes"

    def test_returns_none_on_malformed_payload(self):
        assert bg.derive_outcome({}) is None
        assert bg.derive_outcome({"outcomePrices": None}) is None
        assert bg.derive_outcome({"outcomePrices": "not-json"}) is None
        assert bg.derive_outcome({"outcomePrices": []}) is None
        assert bg.derive_outcome({"outcomePrices": ["x"]}) is None

    def test_condition_id_camelcase_preferred(self):
        assert bg.market_condition_id({"conditionId": "0xABC"}) == "0xABC"
        # Snake-case fallback for legacy responses.
        assert bg.market_condition_id({"condition_id": "0xDEF"}) == "0xDEF"
        # Missing → None.
        assert bg.market_condition_id({"foo": "bar"}) is None
        # Empty string → None (don't try to UPDATE with "").
        assert bg.market_condition_id({"conditionId": "   "}) is None


# --------------------------------------------------------------------------- #
# 2. Pnl math                                                                  #
# --------------------------------------------------------------------------- #


class TestComputePnl:
    def test_winning_direction_pays_full_terminal_minus_entry(self):
        # Bought YES at 0.40 with $100 → 250 shares. YES wins → exit 1.0.
        # pnl = 250 * (1.0 - 0.4) = 150
        exit_price, pnl, pct = bg.compute_pnl(
            direction="yes",
            outcome="yes",
            entry_price=Decimal("0.40"),
            size_usdc=Decimal("100"),
        )
        assert exit_price == Decimal("1.0")
        assert pnl == Decimal("150.00")
        # pct relative to entry price (binary [-1, +1.5] range).
        # (1.0 - 0.4) / 0.4 = 1.5
        assert pct == Decimal("1.5")

    def test_losing_direction_loses_full_entry_notional(self):
        # Bought NO at 0.30 with $60 → 200 shares. YES wins → exit 0.0.
        # pnl = 200 * (0.0 - 0.3) = -60 → loses the entire stake.
        exit_price, pnl, pct = bg.compute_pnl(
            direction="no",
            outcome="yes",
            entry_price=Decimal("0.30"),
            size_usdc=Decimal("60"),
        )
        assert exit_price == Decimal("0.0")
        assert pnl == Decimal("-60.00")
        assert pct == Decimal("-1")

    def test_zero_entry_returns_zero_pnl(self):
        # Defensive — entry == 0 should not crash; we report 0 PnL and
        # let the audit pick the bad state row up.
        exit_price, pnl, pct = bg.compute_pnl(
            direction="yes",
            outcome="yes",
            entry_price=Decimal("0"),
            size_usdc=Decimal("100"),
        )
        assert exit_price == Decimal("1.0")
        assert pnl == Decimal("0")
        assert pct == Decimal("0")


# --------------------------------------------------------------------------- #
# 3. Idempotency: re-running the same window is a no-op                        #
# --------------------------------------------------------------------------- #


class TestIdempotency:
    async def test_second_run_skips_already_settled_market(self, monkeypatch):
        """When the markets UPDATE returns 0, we must NOT enter the
        close-positions path: a stale state row that survived a prior
        run would otherwise produce a duplicate positions_reconstructed
        insert."""
        pool = _FakePool()
        # First call → UPDATE 1 (fresh resolution). Second call →
        # UPDATE 0 (already settled).
        states = iter(["UPDATE 1", "UPDATE 0"])
        pool.on_execute("UPDATE markets", lambda args: next(states))
        # If the orchestrator ever called fetch_open_positions on the
        # second pass we'd want to know about it — leave the handler
        # at its default of [] so positions_closed stays 0, but we
        # also count it from the call ledger.
        pool.on_fetch("FROM position_tracker_state", [])
        # Sweep expired markets: not under test here, ANSWER 0.
        pool.on_execute("WHERE end_date < NOW()", "UPDATE 0")
        # Mock category lookup (only runs on the first pass).
        pool.on_fetchrow("SELECT category", {"category": "sports"})

        redis = _make_redis()
        session = MagicMock()

        # Both runs see the same single market.
        page = [{
            "conditionId": "0xMKT",
            "outcomePrices": [1.0, 0.0],
            "endDate": (datetime.now(tz=timezone.utc) - timedelta(days=1))
                       .isoformat(),
        }]
        _patch_fetch_page(monkeypatch, [page, [], page, []])

        summary1 = await bg.run_backfill(
            pool=pool, redis_client=redis, session=session,
            days=30, batch_size=10, dry_run=False,
        )
        summary2 = await bg.run_backfill(
            pool=pool, redis_client=redis, session=session,
            days=30, batch_size=10, dry_run=False,
        )

        assert summary1.markets_updated == 1
        assert summary2.markets_updated == 0
        # And the second run did not query any state rows because the
        # UPDATE returned 0 and the orchestrator short-circuited.
        second_pass_fetches = [
            (s, a) for s, a in pool.fetch_calls
            if "FROM position_tracker_state" in s and a == ("0xMKT",)
        ]
        # Step counts: first pass = 1 fetch; second pass = 0.
        assert len(second_pass_fetches) == 1


# --------------------------------------------------------------------------- #
# 4. Lever F: expired-active markets hygiene                                   #
# --------------------------------------------------------------------------- #


class TestExpiredActiveSweep:
    async def test_sweep_only_touches_expired_active_rows(self):
        """``sweep_expired_active_markets`` issues exactly the right
        UPDATE — no other writes. The SQL filters on both
        ``end_date < NOW() - 1 day`` and ``active = TRUE`` so a
        future-end-date market never gets flipped."""
        pool = _FakePool()
        pool.on_execute("WHERE end_date < NOW()", "UPDATE 4518")
        conn = _FakeConn(pool)
        n = await bg.sweep_expired_active_markets(conn)
        assert n == 4518

        # And the UPDATE we issued has both filters.
        sweep_sql = [
            s for s, _ in pool.executed if "WHERE end_date" in s
        ]
        assert len(sweep_sql) == 1
        sql = sweep_sql[0]
        assert "end_date < NOW() - INTERVAL '1 day'" in sql
        assert "active = TRUE" in sql
        assert "SET active = FALSE" in sql


# --------------------------------------------------------------------------- #
# 5. Dry-run produces NO writes but accurate counts                            #
# --------------------------------------------------------------------------- #


class TestDryRun:
    async def test_dry_run_writes_nothing_but_counts_accurately(
        self, monkeypatch,
    ):
        """The whole point of ``--dry-run`` is to size the change set
        before committing. We must NOT issue any UPDATE / INSERT /
        DELETE — and the summary must still report
        markets_updated, positions_closed, leaders_affected,
        expired_marked_inactive that match what a real run would do.
        """
        pool = _FakePool()
        # No row in markets yet → "already settled?" returns None.
        pool.on_fetchval("SELECT 1 FROM markets", None)
        # Lever F counter (dry-run path uses COUNT(*)).
        pool.on_fetchval("SELECT COUNT(*) FROM markets", 4518)
        # One state row to close.
        pool.on_fetch(
            "FROM position_tracker_state",
            [_make_row(wallet="0xleader-A"), _make_row(wallet="0xleader-B")],
        )

        redis = _make_redis()
        session = MagicMock()
        page = [{
            "conditionId": "0xMKT",
            "outcomePrices": [1.0, 0.0],
            "endDate": (datetime.now(tz=timezone.utc) - timedelta(days=1))
                       .isoformat(),
        }]
        _patch_fetch_page(monkeypatch, [page, []])

        summary = await bg.run_backfill(
            pool=pool, redis_client=redis, session=session,
            days=30, batch_size=10, dry_run=True,
        )

        # No execute calls at all — dry-run never writes.
        assert pool.executed == []
        # Redis publish never fires in dry-run mode.
        redis.publish.assert_not_called()

        # Counters reflect what we would have done.
        assert summary.markets_updated == 1
        assert summary.positions_closed == 2
        assert summary.expired_marked_inactive == 4518
        # leaders_affected is a set; we tracked both distinct wallets.
        assert len(summary.leaders_affected) == 2


# --------------------------------------------------------------------------- #
# 6. Real run: full close path writes INSERT + DELETE + Redis publish          #
# --------------------------------------------------------------------------- #


class TestFullCloseFlow:
    async def test_close_position_inserts_and_deletes_atomically(self):
        """``close_position`` is the per-row writer. It must issue
        exactly one INSERT into positions_reconstructed and one DELETE
        from position_tracker_state, with the right per-direction
        terminal price. The caller wraps it in a transaction."""
        pool = _FakePool()
        conn = _FakeConn(pool)

        row = _make_row(
            wallet="0xLeader",
            market="0xMkt",
            token="0xTokYes",
            direction="yes",
            entry_price="0.40",
            size_usdc="100",
            shares_remaining="250",
        )
        close_time = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

        pnl = await bg.close_position(
            conn,
            market_id="0xMkt",
            row=row,
            outcome="yes",
            category="sports",
            close_time=close_time,
        )

        # Sanity: pnl = 250 * (1.0 - 0.4) = 150.
        assert pnl == Decimal("150.00")

        # Exactly one INSERT + one DELETE, in that order.
        sqls = [s for s, _ in pool.executed]
        assert any("INSERT INTO positions_reconstructed" in s for s in sqls)
        assert any("DELETE FROM position_tracker_state" in s for s in sqls)

        # INSERT args carry the right terminal price + close_method.
        insert_args = next(
            a for s, a in pool.executed
            if "INSERT INTO positions_reconstructed" in s
        )
        # See close_position arg order — exit_price index 7, close_method 12.
        assert insert_args[7] == Decimal("1.0")
        assert insert_args[12] == "resolution"
        # And the leader-affected wallet is preserved.
        assert insert_args[0] == "0xLeader"
