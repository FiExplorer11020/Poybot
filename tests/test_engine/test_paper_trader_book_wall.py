"""
Tests for the 2026-05-17 round 3 quick-win book-wall guard in
``paper_trader.open_trade``.

Post-mortem of the 11 trades that each lost -97% on the May 16-17 cycle
showed the bid-ask spread was >= 0.50 in ALL of them — the order book
had collapsed to a binary pre-resolution wall (bid=0.01, ask=0.99) with
no meaningful price to enter at. The guard fires BEFORE any other
downstream check so a broken book never costs us a posterior update or
a category-whitelist lookup.

Each test below pins one cell of the spread x decision matrix to keep
regressions cheap to diagnose.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.engine.paper_trader import PaperTrader


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.publish = AsyncMock()
    r.hincrby = AsyncMock()
    r.expire = AsyncMock()
    r.pubsub = MagicMock()
    return r


def _book_payload(*, best_bid: float, best_ask: float, age_s: float = 5.0) -> str:
    """Serialize a `book:last:*` payload mirroring the production schema.

    `age_s` defaults to 5 s — well within `MAX_BOOK_AGE_PAPER_S` so the
    freshness gate inside `_get_book_quote` lets the quote through to
    the wall guard under test.
    """
    captured_at = datetime.now(tz=timezone.utc) - timedelta(seconds=age_s)
    return json.dumps(
        {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "captured_at": captured_at.isoformat(),
        }
    )


def _make_decision(
    *,
    market_id: str = "market-book-wall",
    token_id: str = "tok-wall",
    size_usdc: float = 200.0,
) -> dict:
    return {
        "action": "follow",
        "market_id": market_id,
        "token_id": token_id,
        "size_usdc": size_usdc,
        "confidence": 0.8,
        "leader_wallet": "0xLeaderWall",
        "signal_audit": {"accepted": True},
        "trade_context": {"market_category": "sports"},
    }


def _wire_redis_with_book(*, best_bid: float, best_ask: float) -> AsyncMock:
    """Make a Redis mock whose ``get('book:last:<market>:<token>')`` returns
    a fresh payload with the configured (bid, ask). All other ``get``
    keys (read by other paths in `_get_current_price`) return None so
    the trader falls back to the `mid_fallback` argument."""
    redis = _make_redis()
    payload = _book_payload(best_bid=best_bid, best_ask=best_ask)

    async def fake_get(key):
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8")
        if key and key.startswith("book:last:"):
            return payload
        return None

    redis.get = AsyncMock(side_effect=fake_get)
    return redis


def _make_trader(redis: AsyncMock) -> PaperTrader:
    return PaperTrader(redis_client=redis)


def _make_db_cm():
    """A get_db context manager that returns a no-op connection.

    Useful for the PASS cases — `open_trade` will reach the DB only
    AFTER all early gates clear, and we want to short-circuit BEFORE
    any DB write would matter. The fetchrow callback returns a far-
    future end_date so the time-to-resolution gate also passes.
    """
    far_future_end = datetime.now(tz=timezone.utc) + timedelta(days=14)

    @asynccontextmanager
    async def _cm():
        conn = AsyncMock()

        async def fetchrow(sql, *args):
            if "FROM paper_trades" in sql and "status = 'open'" in sql:
                return None
            if "FROM paper_trades" in sql and "opened_at >=" in sql:
                return None
            if "FROM markets m" in sql and "last_trade_time" in sql:
                return {"end_date": None, "last_trade_time": None}
            if "SELECT end_date FROM markets" in sql:
                return {"end_date": far_future_end}
            if "FROM trades_observed" in sql:
                return {"price": 0.50}
            if "SELECT fee_rate_pct FROM markets" in sql:
                return None
            if "INSERT INTO paper_trades" in sql:
                return {"id": 999}
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.execute = AsyncMock()

        @asynccontextmanager
        async def _tx():
            yield None

        conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
        yield conn

    return _cm


# --------------------------------------------------------------------------- #
# REJECT cases — the book has collapsed past the spread ceiling.              #
# --------------------------------------------------------------------------- #


class TestBookWallRejectsWideSpread:
    """The 0.01/0.99 wall (spread = 0.98) is the canonical failure mode."""

    @pytest.mark.asyncio
    async def test_rejects_full_binary_wall_001_099(self):
        """bid=0.01 ask=0.99 → spread=0.98 >= 0.50 → reject with
        reason ``book_wall_spread``.
        """
        redis = _wire_redis_with_book(best_bid=0.01, best_ask=0.99)
        trader = _make_trader(redis)

        # No DB patch needed — the guard must fire before the first
        # DB roundtrip. If it doesn't, the test will hit AsyncMock
        # defaults from `get_db` and we'd see surprise behaviour.
        result = await trader.open_trade(_make_decision())

        assert result is None, (
            "open_trade returned a trade ID despite the 0.01/0.99 wall — "
            "the book_wall guard did not fire."
        )
        # Refusal counter must be tagged with the new reason.
        redis.hincrby.assert_any_call(
            "paper:rejections:1h", "book_wall_spread", 1
        )
        redis.hincrby.assert_any_call(
            "paper:rejections:24h", "book_wall_spread", 1
        )

    @pytest.mark.asyncio
    async def test_rejects_spread_just_above_ceiling(self):
        """bid=0.20 ask=0.71 → spread=0.51 >= 0.50 → reject.

        This boundary test pins the >= behaviour: the default threshold
        is 0.50 and 0.51 should land on the REJECT side. Without this
        check a regression toward strict `>` would silently widen
        acceptance into the failure cohort.
        """
        redis = _wire_redis_with_book(best_bid=0.20, best_ask=0.71)
        trader = _make_trader(redis)

        result = await trader.open_trade(_make_decision())

        assert result is None
        redis.hincrby.assert_any_call(
            "paper:rejections:1h", "book_wall_spread", 1
        )

    @pytest.mark.asyncio
    async def test_rejects_exact_boundary_spread_050(self):
        """bid=0.25 ask=0.75 → spread=0.50 >= 0.50 → reject.

        Exact boundary case. The guard uses `>=` so 0.50 is REJECTED,
        not allowed.
        """
        redis = _wire_redis_with_book(best_bid=0.25, best_ask=0.75)
        trader = _make_trader(redis)

        result = await trader.open_trade(_make_decision())

        assert result is None


# --------------------------------------------------------------------------- #
# PASS cases — tight spreads on a healthy book.                               #
# --------------------------------------------------------------------------- #


class TestBookWallAcceptsTightSpread:
    """Spreads below the ceiling must NOT fire the guard. Tests use the
    full `open_trade` path (DB roundtrips mocked) but only assert that
    the guard did NOT short-circuit — full INSERT validation lives in
    test_paper_trader.py.
    """

    @pytest.mark.asyncio
    async def test_passes_tight_spread_010(self):
        """bid=0.45 ask=0.55 → spread=0.10 < 0.50 → guard does NOT
        fire and `open_trade` proceeds to downstream gates.
        """
        redis = _wire_redis_with_book(best_bid=0.45, best_ask=0.55)
        trader = _make_trader(redis)

        with patch("src.engine.paper_trader.get_db", _make_db_cm()):
            result = await trader.open_trade(_make_decision())

        # The guard must not stamp `book_wall_spread`.
        for call in redis.hincrby.await_args_list:
            args = call.args or call[0]
            if len(args) >= 2:
                assert args[1] != "book_wall_spread", (
                    f"book_wall_spread guard fired on bid=0.45 ask=0.55 "
                    f"(spread=0.10) — guard should be silent. Calls: "
                    f"{[c.args for c in redis.hincrby.await_args_list]}"
                )
        # Proof the guard let the request through to the rest of the
        # pipeline: a trade id must come back from the mocked INSERT
        # (or the call ran far enough to attempt other gates).
        assert result == 999

    @pytest.mark.asyncio
    async def test_passes_spread_just_below_ceiling(self):
        """bid=0.20 ask=0.69 → spread=0.49 < 0.50 → guard does NOT
        fire. Symmetric boundary test with ``test_rejects_spread_just_
        above_ceiling`` — together they pin the ceiling at exactly 0.50.
        """
        redis = _wire_redis_with_book(best_bid=0.20, best_ask=0.69)
        trader = _make_trader(redis)

        with patch("src.engine.paper_trader.get_db", _make_db_cm()):
            result = await trader.open_trade(_make_decision())

        for call in redis.hincrby.await_args_list:
            args = call.args or call[0]
            if len(args) >= 2:
                assert args[1] != "book_wall_spread", (
                    f"book_wall_spread guard fired on spread=0.49 — "
                    "must be silent under the 0.50 ceiling."
                )
        assert result == 999


# --------------------------------------------------------------------------- #
# Runtime knob — the operator can tighten / loosen via RuntimeConfig.         #
# --------------------------------------------------------------------------- #


class TestBookWallRuntimeOverride:
    @pytest.mark.asyncio
    async def test_runtime_override_can_tighten_threshold(self):
        """With ``book_wall_max_spread=0.10`` set in runtime_config,
        a spread of 0.20 (bid=0.40 ask=0.60) — normally fine — must
        now REJECT. Pins the runtime-tunability contract: incident
        response can pull the threshold without redeploying.
        """
        redis = _wire_redis_with_book(best_bid=0.40, best_ask=0.60)
        trader = _make_trader(redis)

        # Patch `_read_runtime_setting` directly so we don't depend on
        # the runtime_config singleton wiring in unit tests.
        original = trader._read_runtime_setting

        async def patched_runtime(key, fallback):
            if key == "book_wall_max_spread":
                return 0.10
            return await original(key, fallback)

        trader._read_runtime_setting = patched_runtime

        result = await trader.open_trade(_make_decision())

        assert result is None, (
            "With book_wall_max_spread tightened to 0.10, the bid=0.40 "
            "ask=0.60 quote (spread=0.20) must be rejected."
        )
        redis.hincrby.assert_any_call(
            "paper:rejections:1h", "book_wall_spread", 1
        )
