"""Regression tests for the ``market_resolved`` → ``close_market_positions``
wiring added 2026-05-17 (diagnosis §A.1).

Before this wire-up, ``close_method='resolution'`` was never written to
``positions_reconstructed`` because no caller ever invoked
``close_market_positions``. The fix has two halves:

1. ``PositionTracker._on_market_resolved_message`` subscribes to a Redis
   pub/sub channel and dispatches to ``close_market_positions`` with
   the per-direction outcome ("yes" / "no").
2. ``close_market_positions`` accepts an ``outcome`` kwarg and resolves
   to ``1.0`` for the winning token and ``0.0`` for the losing token
   per-position, by looking up the market's ``token_yes`` / ``token_no``.

Both halves are exercised here in isolation; the actual observer-to-
tracker plumbing (Redis subscribe loop) is integration-tested via the
existing market_events parser tests and the dispatcher in
``src/observer/main.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.position_tracker import (
    REDIS_MARKET_RESOLVED_CHANNEL,
    OpenPosition,
    PositionTracker,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


_WALLET_A = "0xWalletA"
_WALLET_B = "0xWalletB"
_MARKET = "0xMarket1"
_TOKEN_YES = "0xTokYes"
_TOKEN_NO = "0xTokNo"


def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


def _make_tracker() -> PositionTracker:
    """Tracker with fee lookup stubbed to 0 so PnL math is trivial."""
    tracker = PositionTracker(redis_client=_make_redis())

    async def _zero_fee(_market_id: str) -> Decimal:
        return Decimal("0")

    tracker._get_fee_rate = _zero_fee  # type: ignore[method-assign]
    return tracker


def _add_open(
    tracker: PositionTracker,
    *,
    wallet: str,
    token_id: str,
    direction: str,
    entry_price: str = "0.40",
    shares: str = "100",
) -> OpenPosition:
    pos = OpenPosition(
        wallet_address=wallet,
        market_id=_MARKET,
        token_id=token_id,
        direction=direction,
        open_time=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        entry_price=Decimal(entry_price),
        size_usdc=Decimal(entry_price) * Decimal(shares),
        size_shares=Decimal(shares),
        shares_remaining=Decimal(shares),
        fee_rate_pct=Decimal("0"),
    )
    key = (wallet, _MARKET, token_id)
    tracker._open_positions.setdefault(key, []).append(pos)
    return pos


def _patch_get_db(conn):
    """Patch the get_db() async-CM in position_tracker to yield `conn`."""

    @asynccontextmanager
    async def _cm():
        yield conn

    return patch("src.observer.position_tracker.get_db", _cm)


def _make_conn():
    """Mock asyncpg conn with a no-op transaction() async-CM."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    return conn


# --------------------------------------------------------------------------- #
# Direct close_market_positions(outcome=…) behavior                            #
# --------------------------------------------------------------------------- #


class TestCloseMarketPositionsByOutcome:
    """``close_market_positions(market_id, outcome="yes"|"no")`` must close
    every open position on the market at the correct per-direction
    terminal value (1.0 if holding the winning token, 0.0 otherwise).
    """

    @pytest.mark.asyncio
    async def test_yes_outcome_closes_yes_at_one_no_at_zero(self):
        tracker = _make_tracker()
        # Pre-populate the (token_yes, token_no) cache so the tracker
        # doesn't need a DB hit to figure out winning side.
        tracker._market_tokens[_MARKET] = (_TOKEN_YES, _TOKEN_NO)

        _add_open(tracker, wallet=_WALLET_A, token_id=_TOKEN_YES, direction="yes")
        _add_open(tracker, wallet=_WALLET_B, token_id=_TOKEN_NO, direction="no")

        captured: list[tuple[str, tuple]] = []
        conn = _make_conn()

        async def capture(sql, *args):
            captured.append((sql, args))

        conn.execute = AsyncMock(side_effect=capture)

        with _patch_get_db(conn):
            closed = await tracker.close_market_positions(_MARKET, outcome="yes")

        assert closed == 2

        # Two positions_reconstructed INSERTs, both with close_method='resolution'.
        inserts = [
            (s, a) for s, a in captured if "INSERT INTO positions_reconstructed" in s
        ]
        assert len(inserts) == 2
        # exit_price is positional arg index 7 (0-based) on the INSERT call.
        # See _close_position in position_tracker.py.
        exit_prices_by_wallet = {a[0]: a[7] for _, a in inserts}
        assert exit_prices_by_wallet[_WALLET_A] == Decimal("1.0")
        assert exit_prices_by_wallet[_WALLET_B] == Decimal("0.0")

        # All open positions for this market are gone.
        assert not [k for k in tracker._open_positions if k[1] == _MARKET]

    @pytest.mark.asyncio
    async def test_no_outcome_closes_no_at_one_yes_at_zero(self):
        """Symmetric to the yes-outcome test."""
        tracker = _make_tracker()
        tracker._market_tokens[_MARKET] = (_TOKEN_YES, _TOKEN_NO)
        _add_open(tracker, wallet=_WALLET_A, token_id=_TOKEN_YES, direction="yes")
        _add_open(tracker, wallet=_WALLET_B, token_id=_TOKEN_NO, direction="no")

        captured: list[tuple[str, tuple]] = []
        conn = _make_conn()

        async def capture(sql, *args):
            captured.append((sql, args))

        conn.execute = AsyncMock(side_effect=capture)

        with _patch_get_db(conn):
            closed = await tracker.close_market_positions(_MARKET, outcome="no")

        assert closed == 2
        inserts = [
            (s, a) for s, a in captured if "INSERT INTO positions_reconstructed" in s
        ]
        exit_prices_by_wallet = {a[0]: a[7] for _, a in inserts}
        # NO wins → NO holder gets 1.0, YES holder gets 0.0.
        assert exit_prices_by_wallet[_WALLET_A] == Decimal("0.0")
        assert exit_prices_by_wallet[_WALLET_B] == Decimal("1.0")

    @pytest.mark.asyncio
    async def test_idempotent_on_empty_market(self):
        """A resolution publish for a market with no open positions must
        be a silent no-op — the maintenance-loop sweep republishes
        envelopes for every Gamma-closed market, including ones the WS
        already processed, so duplicates are normal."""
        tracker = _make_tracker()
        # No positions added.
        conn = _make_conn()
        with _patch_get_db(conn):
            closed = await tracker.close_market_positions(_MARKET, outcome="yes")
        assert closed == 0
        # No INSERTs at all.
        conn.execute.assert_not_called()


# --------------------------------------------------------------------------- #
# Subscriber handler dispatches to close_market_positions                      #
# --------------------------------------------------------------------------- #


class TestMarketResolvedHandlerDispatch:
    """The Subscriber handler must convert a ``{market_id, outcome}``
    envelope into a ``close_market_positions(...)`` call with the
    correct kwargs. We mock ``close_market_positions`` directly to
    keep the assertion scope tight.
    """

    @pytest.mark.asyncio
    async def test_handler_dispatches_to_close_market_positions(self):
        tracker = _make_tracker()
        tracker._running = True
        tracker.close_market_positions = AsyncMock(return_value=2)

        payload = {"market_id": _MARKET, "outcome": "yes"}
        await tracker._on_market_resolved_message(
            payload, REDIS_MARKET_RESOLVED_CHANNEL
        )

        tracker.close_market_positions.assert_awaited_once_with(
            _MARKET, outcome="yes"
        )

    @pytest.mark.asyncio
    async def test_handler_drops_malformed_payload(self):
        """Missing market_id OR outcome → handler must NOT call
        close_market_positions (defensive; producer contract violation
        should not stall the subscriber)."""
        tracker = _make_tracker()
        tracker._running = True
        tracker.close_market_positions = AsyncMock(return_value=0)

        # Missing outcome.
        await tracker._on_market_resolved_message(
            {"market_id": _MARKET}, REDIS_MARKET_RESOLVED_CHANNEL
        )
        # Missing market_id.
        await tracker._on_market_resolved_message(
            {"outcome": "yes"}, REDIS_MARKET_RESOLVED_CHANNEL
        )
        # Not-a-dict.
        await tracker._on_market_resolved_message(
            "garbage", REDIS_MARKET_RESOLVED_CHANNEL  # type: ignore[arg-type]
        )

        tracker.close_market_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_does_not_run_when_stopped(self):
        """The handler must respect ``_running`` so a SIGTERM-mid-publish
        doesn't write through after shutdown started."""
        tracker = _make_tracker()
        tracker._running = False
        tracker.close_market_positions = AsyncMock(return_value=1)

        await tracker._on_market_resolved_message(
            {"market_id": _MARKET, "outcome": "yes"},
            REDIS_MARKET_RESOLVED_CHANNEL,
        )

        tracker.close_market_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_swallows_close_errors(self):
        """If close_market_positions raises (e.g. transient DB blip),
        the handler must NOT propagate — the subscriber loop must
        keep running for other markets."""
        tracker = _make_tracker()
        tracker._running = True
        tracker.close_market_positions = AsyncMock(
            side_effect=RuntimeError("DB blip")
        )

        # Must not raise.
        await tracker._on_market_resolved_message(
            {"market_id": _MARKET, "outcome": "yes"},
            REDIS_MARKET_RESOLVED_CHANNEL,
        )

        tracker.close_market_positions.assert_awaited_once()
