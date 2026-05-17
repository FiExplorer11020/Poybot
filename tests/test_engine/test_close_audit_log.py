"""Pillar 5 — close_audit_log hook in ``PaperTrader.close_trade``.

Every successful close must insert one row into ``close_audit_log``
capturing the oracle source, snapshot evidence, leader state and
decision payload. Failures in the audit insert must NOT roll back the
close itself — the audit is observational, the close is the contract.

These tests run the real ``close_trade`` path with a patched
``get_db`` that records the SQL it sees, so we can assert both:

  * The UPDATE paper_trades happened (the close itself).
  * The INSERT close_audit_log happened with the right values.
  * If the INSERT fails, the close still completes.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.control.price_oracle import PriceQuote
from src.engine.paper_trader import OpenPaperTrade, PaperTrader


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


def _make_open_trade(*, leader_context: dict | None = None) -> OpenPaperTrade:
    return OpenPaperTrade(
        id=42,
        market_id="market-X",
        token_id="tok-A",
        direction="yes",
        strategy="follow",
        entry_price=0.40,
        size_usdc=200.0,
        size_shares=500.0,
        leader_wallet="0xLeader",
        confidence=0.8,
        fee_rate_pct=0.0,
        opened_at=datetime.now(tz=timezone.utc),
        leader_context=leader_context or {"trade_context": {"category": "crypto"}},
    )


def _recording_db(
    *,
    leader_state_row: dict | None = None,
    insert_fails: bool = False,
):
    """Build a patched ``get_db`` that records every (sql, args) pair
    the trader executes. Returns (patcher, calls list).

    ``leader_state_row`` is what the leader_state SELECT returns
    (positions_reconstructed lookup in ``_snapshot_leader_state``).
    ``insert_fails=True`` causes the close_audit_log INSERT to raise.
    """
    calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def _db():
        conn = AsyncMock()

        async def _execute(sql, *args):
            calls.append((sql, args))
            if insert_fails and "INSERT INTO close_audit_log" in sql:
                raise RuntimeError("simulated INSERT failure")
            return None

        async def _fetchrow(sql, *args):
            calls.append((sql, args))
            if "FROM positions_reconstructed" in sql:
                return leader_state_row
            return None

        @asynccontextmanager
        async def _tx():
            yield None

        conn.execute = _execute
        conn.fetchrow = _fetchrow
        conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
        yield conn

    return _db, calls


def _make_trader_with_oracle_quote(quote: PriceQuote) -> PaperTrader:
    trader = PaperTrader(redis_client=_make_redis())
    trader._price_oracle.get_close_price = AsyncMock(return_value=quote)
    trader._get_fee_rate = AsyncMock(return_value=0.0)
    return trader


# --------------------------------------------------------------------------- #
# 1. Audit row inserted on close                                              #
# --------------------------------------------------------------------------- #


class TestCloseAuditLogInsert:
    @pytest.mark.asyncio
    async def test_close_audit_log_inserted_on_close(self):
        """A vanilla close must produce exactly one INSERT close_audit_log."""
        quote = PriceQuote(
            price=0.50,
            source="book",
            observed_ts=time.time(),
            spread_pct=0.04,
            raw_book={"best_bid": 0.49, "best_ask": 0.51, "mid": 0.50, "spread_pct": 0.04, "observed_ts": time.time()},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db()

        with patch("src.engine.paper_trader.get_db", db):
            ok = await trader.close_trade(
                trade.id, 0.50, "take_profit", price_quote=quote
            )

        assert ok is True
        audit_inserts = [
            c for c in calls if "INSERT INTO close_audit_log" in c[0]
        ]
        assert len(audit_inserts) == 1, (
            f"Expected exactly one close_audit_log INSERT; got {len(audit_inserts)}"
        )
        sql, args = audit_inserts[0]
        # Args order: paper_trade_id, closed_at, close_reason, oracle_source,
        # exit_price, computed_pnl_usdc, book_snapshot, gamma_snapshot,
        # resolution_snapshot, leader_state, decision_payload
        assert args[0] == trade.id
        assert args[2] == "take_profit"
        assert args[3] == "book"
        assert args[4] == pytest.approx(0.50)


# --------------------------------------------------------------------------- #
# 2. Per-source snapshot routing                                              #
# --------------------------------------------------------------------------- #


class TestSnapshotRouting:
    @pytest.mark.asyncio
    async def test_close_audit_oracle_source_book(self):
        """source='book' → book_snapshot populated, gamma/resolution NULL."""
        quote = PriceQuote(
            price=0.50,
            source="book",
            observed_ts=time.time(),
            spread_pct=0.04,
            raw_book={"best_bid": 0.49, "best_ask": 0.51},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db()
        with patch("src.engine.paper_trader.get_db", db):
            await trader.close_trade(trade.id, 0.50, "take_profit", price_quote=quote)

        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        # args[6] = book_snapshot, args[7] = gamma_snapshot, args[8] = resolution_snapshot
        assert args[6] is not None, "book_snapshot must be present for source=book"
        # JSON string check: contains the bid value we set.
        assert "0.49" in args[6]
        assert args[7] is None, "gamma_snapshot must be NULL for source=book"
        assert args[8] is None, "resolution_snapshot must be NULL for source=book"

    @pytest.mark.asyncio
    async def test_close_audit_oracle_source_gamma(self):
        quote = PriceQuote(
            price=0.62,
            source="gamma",
            observed_ts=time.time(),
            raw_gamma={
                "last_trade_price": 0.62,
                "last_trade_age_s": 30.0,
                "condition_id": "0xabc",
            },
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db()
        with patch("src.engine.paper_trader.get_db", db):
            await trader.close_trade(trade.id, 0.62, "take_profit", price_quote=quote)

        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        assert args[3] == "gamma"
        assert args[6] is None
        assert args[7] is not None
        assert "0xabc" in args[7]
        assert args[8] is None

    @pytest.mark.asyncio
    async def test_close_audit_oracle_source_resolved(self):
        quote = PriceQuote(
            price=1.0,
            source="resolved",
            observed_ts=time.time(),
            raw_resolution={
                "resolved_outcome": "yes",
                "winning_token": "tok-A",
                "held_token": "tok-A",
                "direction": "yes",
            },
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db()
        with patch("src.engine.paper_trader.get_db", db):
            await trader.close_trade(trade.id, 1.0, "market_resolved", price_quote=quote)

        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        assert args[3] == "resolved"
        assert args[6] is None
        assert args[7] is None
        assert args[8] is not None
        assert "yes" in args[8]


# --------------------------------------------------------------------------- #
# 3. Leader state best-effort                                                 #
# --------------------------------------------------------------------------- #


class TestLeaderStateSnapshot:
    @pytest.mark.asyncio
    async def test_close_audit_no_leader_state_does_not_crash(self):
        """positions_reconstructed empty → leader_state NULL, close OK."""
        quote = PriceQuote(
            price=0.50, source="book", observed_ts=time.time(),
            spread_pct=0.04, raw_book={"best_bid": 0.5, "best_ask": 0.5},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db(leader_state_row=None)
        with patch("src.engine.paper_trader.get_db", db):
            ok = await trader.close_trade(
                trade.id, 0.50, "take_profit", price_quote=quote
            )
        assert ok is True
        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        # leader_state at args[9] must be None when positions_reconstructed
        # has no row for this (wallet, market).
        assert args[9] is None

    @pytest.mark.asyncio
    async def test_close_audit_leader_state_populated_when_available(self):
        """When positions_reconstructed has a row, leader_state JSON
        carries wallet/last_trade_price/side."""
        quote = PriceQuote(
            price=0.50, source="book", observed_ts=time.time(),
            spread_pct=0.04, raw_book={"best_bid": 0.5, "best_ask": 0.5},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        row = {
            "wallet_address": "0xLeader",
            "open_time": datetime.now(tz=timezone.utc),
            "close_time": None,
            "entry_price": 0.41,
            "exit_price": None,
            "direction": "yes",
        }
        db, calls = _recording_db(leader_state_row=row)
        with patch("src.engine.paper_trader.get_db", db):
            await trader.close_trade(
                trade.id, 0.50, "take_profit", price_quote=quote
            )
        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        assert args[9] is not None
        parsed = json.loads(args[9])
        assert parsed["wallet"] == "0xLeader"
        assert parsed["last_trade_price"] == pytest.approx(0.41)
        assert parsed["side"] == "yes"
        assert parsed["still_open"] is True


# --------------------------------------------------------------------------- #
# 4. Failure isolation                                                        #
# --------------------------------------------------------------------------- #


class TestAuditFailureIsolation:
    @pytest.mark.asyncio
    async def test_close_audit_failure_does_not_block_close(self):
        """If INSERT close_audit_log raises, the close itself still
        succeeds — the UPDATE paper_trades was committed first."""
        quote = PriceQuote(
            price=0.50, source="book", observed_ts=time.time(),
            spread_pct=0.04, raw_book={"best_bid": 0.5, "best_ask": 0.5},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db(insert_fails=True)
        with patch("src.engine.paper_trader.get_db", db):
            ok = await trader.close_trade(
                trade.id, 0.50, "take_profit", price_quote=quote
            )

        # Close must have succeeded.
        assert ok is True
        # The trade is gone from open_trades — the UPDATE committed.
        assert trade.id not in {t.id for t in trader._open_trades}
        # The UPDATE paper_trades was issued.
        updates = [c for c in calls if "UPDATE paper_trades" in c[0]]
        assert len(updates) == 1
        # And an attempted INSERT was issued (even though it raised).
        attempts = [c for c in calls if "INSERT INTO close_audit_log" in c[0]]
        assert len(attempts) == 1

    @pytest.mark.asyncio
    async def test_close_without_quote_records_fallback_source(self):
        """When close_trade is called without price_quote (legacy
        callers / manual closes), the audit row records
        oracle_source='fallback' and all snapshots are NULL."""
        quote = PriceQuote(
            price=0.50, source="book", observed_ts=time.time(),
            spread_pct=0.04, raw_book={"best_bid": 0.5, "best_ask": 0.5},
        )
        trader = _make_trader_with_oracle_quote(quote)
        trade = _make_open_trade()
        trader._open_trades = [trade]
        db, calls = _recording_db()
        with patch("src.engine.paper_trader.get_db", db):
            ok = await trader.close_trade(trade.id, 0.50, "take_profit")
        assert ok is True
        audit = [c for c in calls if "INSERT INTO close_audit_log" in c[0]][0]
        _sql, args = audit
        assert args[3] == "fallback"
        assert args[6] is None
        assert args[7] is None
        assert args[8] is None
