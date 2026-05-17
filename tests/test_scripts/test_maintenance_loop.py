"""Unit tests for ``scripts/maintenance_loop.py``.

Pins the three contract changes shipped on 2026-05-17:

  - ``sweep_expired_active_markets`` issues the right UPDATE and only
    touches expired+active rows.
  - ``reconcile_profiles`` refreshes ``last_updated`` even when the
    trade count is unchanged (the guard clause stranded 702 stale
    profiles).
  - ``refresh_gamma_markets`` respects Gamma's ``closed`` flag: when
    closed=TRUE it writes active=FALSE; when closed is falsy it
    preserves the existing active value (no longer forces TRUE).

All tests use lightweight asyncpg/aiohttp stubs so they stay pure
Python and never touch a real DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import maintenance_loop as ml


# --------------------------------------------------------------------------- #
# Helpers — asyncpg pool stubs                                                 #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """Records every execute call and replays a scripted answer.

    Handlers are looked up by SQL substring, last-registered wins.
    """

    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._on_execute(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._parent._on_fetch(sql, args)


class _FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetched: list[tuple[str, tuple]] = []
        self.execute_handlers: list[tuple[str, Any]] = []
        self.fetch_handlers: list[tuple[str, Any]] = []

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield _FakeConn(self)
        return _ctx()

    def on_execute(self, substr: str, response: Any) -> None:
        self.execute_handlers.append((substr, response))

    def on_fetch(self, substr: str, response: Any) -> None:
        self.fetch_handlers.append((substr, response))

    def _on_execute(self, sql: str, args: tuple) -> str:
        self.executed.append((sql, args))
        return _resolve(self.execute_handlers, sql, args, default="UPDATE 0")

    def _on_fetch(self, sql: str, args: tuple) -> list[Any]:
        self.fetched.append((sql, args))
        return _resolve(self.fetch_handlers, sql, args, default=[])


def _resolve(handlers, sql, args, default):
    chosen = None
    for substr, response in handlers:
        if substr in sql:
            chosen = response
    if chosen is None:
        return default
    if callable(chosen):
        return chosen(args)
    return chosen


# --------------------------------------------------------------------------- #
# 1. sweep_expired_active_markets                                              #
# --------------------------------------------------------------------------- #


class TestSweepExpiredActiveMarkets:
    async def test_flips_active_false_for_expired_rows(self):
        """The sweep issues exactly the right UPDATE on the right
        filter set: ``end_date < NOW() - 1 day AND active = TRUE``.
        Returns the row count parsed from the asyncpg status string.
        """
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 4518")

        n = await ml.sweep_expired_active_markets(pool)

        assert n == 4518
        # Exactly one UPDATE statement.
        assert len(pool.executed) == 1
        sql, _ = pool.executed[0]
        assert "UPDATE markets" in sql
        assert "SET active = FALSE" in sql
        assert "end_date < NOW() - INTERVAL '1 day'" in sql
        assert "active = TRUE" in sql

    async def test_returns_zero_when_no_rows_match(self):
        """When the UPDATE affects zero rows, the parsed count is 0 —
        no exception, no negative number, no crash on the loop."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 0")

        n = await ml.sweep_expired_active_markets(pool)

        assert n == 0

    async def test_returns_zero_on_malformed_status_string(self):
        """Defensive: if asyncpg returns something that doesn't split
        into an int (it won't, but the loop must survive it), we
        return 0 rather than propagating ValueError."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "")

        n = await ml.sweep_expired_active_markets(pool)

        assert n == 0


# --------------------------------------------------------------------------- #
# 2. reconcile_profiles — last_updated refresh                                 #
# --------------------------------------------------------------------------- #


class TestReconcileProfiles:
    async def test_refreshes_last_updated_when_count_unchanged(self):
        """Regression: the previous version filtered out leaders whose
        trade count was unchanged, leaving ``last_updated`` stale for
        702 profiles. We assert the UPDATE no longer carries the
        ``sub.cnt > COALESCE(lp.trades_observed, 0)`` clause."""
        pool = _FakePool()
        pool.on_execute("UPDATE leader_profiles", "UPDATE 702")

        n = await ml.reconcile_profiles(pool)

        assert n == 702
        assert len(pool.executed) == 1
        sql, _ = pool.executed[0]
        # The set-clause is the SOURCE OF TRUTH refresh.
        assert "trades_observed = sub.cnt" in sql
        assert "last_updated = NOW()" in sql
        # And the dead filter clause is GONE.
        assert "sub.cnt > COALESCE" not in sql

    async def test_returns_zero_when_no_profiles(self):
        pool = _FakePool()
        pool.on_execute("UPDATE leader_profiles", "UPDATE 0")

        n = await ml.reconcile_profiles(pool)

        assert n == 0


# --------------------------------------------------------------------------- #
# 3. refresh_gamma_markets — closed flag overrides active                      #
# --------------------------------------------------------------------------- #


class _FakeAiohttpResponse:
    """Mimics aiohttp's async-context-manager response shape."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeAiohttpSession:
    """Returns canned pages keyed by call order — first call → page 0
    contents, second call → empty (terminates the loop)."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = pages
        self._call = 0

    def get(self, url, *, params=None, timeout=None):
        idx = self._call
        self._call += 1
        page = self._pages[idx] if idx < len(self._pages) else []
        return _FakeAiohttpResponse(200, page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestRefreshGammaMarketsRespectsClosedFlag:
    async def test_closed_true_writes_active_false(self, monkeypatch):
        """When Gamma's payload carries ``closed: true`` the UPDATE
        must pass ``active_preserve=False`` so the CASE expression
        flips ``active=FALSE``. Tests the boolean parameter delivery
        directly — the actual SQL evaluation is exercised by the
        real DB in integration tests."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")

        # Patch the session factory so refresh_gamma_markets sees our
        # canned page on the first call and stops on the second.
        page = [
            {
                "conditionId": "0xCLOSED",
                "endDate": "2026-05-10T00:00:00Z",
                "volume24hr": 1234.5,
                "clobTokenIds": ["tok-yes", "tok-no"],
                "closed": True,
            }
        ]
        fake_session = _FakeAiohttpSession([page, []])

        @asynccontextmanager
        async def _stub_factory(*args, **kwargs):
            yield fake_session

        # aiohttp.ClientSession used as `async with aiohttp.ClientSession(...)`
        monkeypatch.setattr(ml.aiohttp, "ClientSession", _stub_factory)

        updated, inserted = await ml.refresh_gamma_markets(pool, max_pages=2)

        assert (updated, inserted) == (1, 0)
        # Find the UPDATE call and assert the active_preserve flag (last
        # positional arg) is False.
        update_calls = [
            (sql, args) for sql, args in pool.executed
            if "UPDATE markets" in sql
        ]
        assert len(update_calls) == 1
        sql, args = update_calls[0]
        # Schema of args is (cid, end_date, vol, token_yes, token_no, active_preserve).
        assert args[0] == "0xCLOSED"
        assert args[-1] is False, (
            "closed=True must produce active_preserve=False so the "
            "CASE expression evaluates to active=FALSE"
        )
        # SQL contract: no longer forcing TRUE.
        assert "active = TRUE" not in sql
        assert "CASE WHEN" in sql

    async def test_closed_false_preserves_existing_active(self, monkeypatch):
        """When ``closed`` is falsy (or missing) we must NOT force
        active=TRUE — the CASE expression keeps ``markets.active``
        as-is so a market the sweep just deactivated stays deactivated."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")

        page = [
            {
                "conditionId": "0xOPEN",
                "endDate": "2027-01-01T00:00:00Z",
                "volume24hr": 5000,
                "clobTokenIds": ["tok-yes", "tok-no"],
                "closed": False,
            }
        ]
        fake_session = _FakeAiohttpSession([page, []])

        @asynccontextmanager
        async def _stub_factory(*args, **kwargs):
            yield fake_session

        monkeypatch.setattr(ml.aiohttp, "ClientSession", _stub_factory)

        await ml.refresh_gamma_markets(pool, max_pages=2)

        update_calls = [
            (sql, args) for sql, args in pool.executed
            if "UPDATE markets" in sql
        ]
        assert len(update_calls) == 1
        _, args = update_calls[0]
        assert args[-1] is True, (
            "closed=False must produce active_preserve=True so the "
            "CASE expression preserves markets.active"
        )

    async def test_missing_closed_treated_as_open(self, monkeypatch):
        """If Gamma omits ``closed`` we must default to ``preserve``
        (active_preserve=True), not assume the market is closed.
        That keeps backward compatibility with legacy Gamma payloads
        that never shipped the flag."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")

        page = [
            {
                "conditionId": "0xNOFIELD",
                "endDate": "2027-01-01T00:00:00Z",
                "volume24hr": 5000,
                "clobTokenIds": ["tok-yes", "tok-no"],
                # No 'closed' key at all.
            }
        ]
        fake_session = _FakeAiohttpSession([page, []])

        @asynccontextmanager
        async def _stub_factory(*args, **kwargs):
            yield fake_session

        monkeypatch.setattr(ml.aiohttp, "ClientSession", _stub_factory)

        await ml.refresh_gamma_markets(pool, max_pages=2)

        update_calls = [
            (sql, args) for sql, args in pool.executed
            if "UPDATE markets" in sql
        ]
        assert len(update_calls) == 1
        _, args = update_calls[0]
        assert args[-1] is True
