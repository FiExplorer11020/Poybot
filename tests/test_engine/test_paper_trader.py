"""
Unit tests for src/engine/paper_trader.py
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.engine.paper_trader import REDIS_PAPER_CLOSED_CHANNEL, OpenPaperTrade, PaperTrader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_decision(
    *,
    action: str = "follow",
    market_id: str = "market-1",
    token_id: str = "token-1",
    size_usdc: float = 200.0,
    confidence: float = 0.8,
    leader_wallet: str = "0xLeader",
) -> dict:
    # `market_category=sports` lives in the default whitelist
    # ("sports,crypto,macro") so the strategy-upgrade 2026-05-17
    # `category_not_whitelisted` gate does not reject these synthetic
    # decisions. Production upstream (confidence_engine.evaluate) always
    # sets a market_category before publishing to the decisions channel.
    return {
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "size_usdc": size_usdc,
        "confidence": confidence,
        "leader_wallet": leader_wallet,
        "signal_audit": {"accepted": True},
        "trade_context": {"market_category": "sports"},
    }


def _make_db_cm(fetchrow_return=None, execute_return=None):
    """Return a context-manager mock that yields an asyncpg-like connection.

    `conn.transaction()` returns a no-op async context manager so production
    code wrapping multi-statement chains in `async with conn.transaction():`
    works under unit tests.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value=execute_return)

    @asynccontextmanager
    async def _tx():
        yield None

    # `transaction()` is sync (returns the Transaction object); only the
    # returned object is an async CM. Hence MagicMock, not AsyncMock.
    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())

    @asynccontextmanager
    async def _cm():
        yield conn

    return _cm, conn


def _attach_transaction(conn) -> None:
    """Attach a no-op `transaction()` async-CM to a mock asyncpg connection.

    Production code now wraps multi-statement write chains in
    `async with conn.transaction():` — without this attachment the bare
    AsyncMock's `transaction()` returns a coroutine, not an async CM, and
    every test that exercises a write path explodes with TypeError.
    """

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())


def _make_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.pubsub = MagicMock()
    return redis


def _make_trader(redis=None, confidence_engine=None):
    r = redis or _make_redis()
    trader = PaperTrader(redis_client=r, confidence_engine=confidence_engine)
    return trader


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenTrade:
    @pytest.mark.asyncio
    async def test_open_trade_deducts_capital(self):
        """Opening a 200 USDC trade reduces capital from 10 000 to 9 800."""
        trader = _make_trader()
        decision = _make_decision(size_usdc=200.0)
        # Resolution far enough in the future to pass MIN_HOURS_TO_RESOLUTION_FOLLOW.
        far_future_end = datetime.now(tz=timezone.utc) + timedelta(days=7)

        @asynccontextmanager
        async def _multi_cm():
            mock_conn = AsyncMock()

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
                    return {"price": 0.55}
                if "SELECT fee_rate_pct FROM markets" in sql:
                    return None
                if "INSERT INTO paper_trades" in sql:
                    return {"id": 42}
                raise AssertionError(f"Unexpected SQL in test_open_trade_deducts_capital: {sql}")

            mock_conn.fetchrow = AsyncMock(side_effect=fetchrow)
            mock_conn.execute = AsyncMock()
            _attach_transaction(mock_conn)
            yield mock_conn

        with patch("src.engine.paper_trader.get_db", _multi_cm):
            trade_id = await trader.open_trade(decision)

        assert trade_id == 42
        assert trader.capital == pytest.approx(settings.PAPER_CAPITAL_USDC - 200.0)

    @pytest.mark.asyncio
    async def test_open_trade_below_min_ignored(self):
        """size_usdc below MIN_POSITION_USDC (50) must return None without inserting."""
        trader = _make_trader()
        decision = _make_decision(size_usdc=10.0)

        with patch("src.engine.paper_trader.get_db") as mock_get_db:
            result = await trader.open_trade(decision)

        assert result is None
        mock_get_db.assert_not_called()
        assert trader.capital == settings.PAPER_CAPITAL_USDC

    @pytest.mark.asyncio
    async def test_open_trade_exceeds_capital_ignored(self):
        """size_usdc greater than current capital must return None."""
        trader = _make_trader()
        # Manually set capital to something very small
        trader._capital = 100.0
        decision = _make_decision(size_usdc=500.0)

        with patch("src.engine.paper_trader.get_db") as mock_get_db:
            result = await trader.open_trade(decision)

        assert result is None
        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_trade_ignores_stale_decision_context(self):
        trader = _make_trader()
        decision = _make_decision()
        decision["trade_context"] = {"live_candidate": False, "trade_age_s": 3600}

        with patch("src.engine.paper_trader.get_db") as mock_get_db:
            result = await trader.open_trade(decision)

        assert result is None
        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_trade_requires_accepted_signal_audit(self):
        trader = _make_trader()
        decision = _make_decision()
        decision.pop("signal_audit")

        with patch("src.engine.paper_trader.get_db") as mock_get_db:
            result = await trader.open_trade(decision)

        assert result is None
        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_trade_records_rejection_counter_for_missing_signal_audit(self):
        redis = _make_redis()
        redis.hincrby = AsyncMock()
        redis.expire = AsyncMock()
        trader = _make_trader(redis=redis)
        decision = _make_decision()
        decision.pop("signal_audit")

        result = await trader.open_trade(decision)

        assert result is None
        # ``_record_open_trade_refusal`` now bumps BOTH ``:1h`` and ``:24h``
        # buckets (2026-05-17 diagnosis §A.7). Verify each separately rather
        # than asserting awaited-once.
        redis.hincrby.assert_any_call(
            "paper:rejections:1h",
            "missing_accepted_signal_audit",
            1,
        )
        redis.hincrby.assert_any_call(
            "paper:rejections:24h",
            "missing_accepted_signal_audit",
            1,
        )
        redis.expire.assert_any_call("paper:rejections:1h", 3600)
        redis.expire.assert_any_call("paper:rejections:24h", 86400)

    @pytest.mark.asyncio
    async def test_open_trade_skips_when_matching_open_trade_already_exists(self):
        trader = _make_trader()
        trader._open_trades.append(
            OpenPaperTrade(
                id=1,
                market_id="market-1",
                token_id="token-1",
                direction="yes",
                strategy="follow",
                entry_price=0.5,
                size_usdc=200.0,
                leader_wallet="0xLeader",
                confidence=0.8,
            )
        )

        with patch("src.engine.paper_trader.get_db") as mock_get_db:
            result = await trader.open_trade(_make_decision())

        assert result is None
        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_trade_skips_recent_reentry(self):
        trader = _make_trader()

        call_count = 0

        @asynccontextmanager
        async def _multi_cm():
            nonlocal call_count
            call_count += 1
            conn = AsyncMock()
            if call_count == 1:
                conn.fetchrow = AsyncMock(return_value=None)
            else:
                conn.fetchrow = AsyncMock(return_value={"opened_at": datetime.now(tz=timezone.utc)})
            conn.execute = AsyncMock()
            _attach_transaction(conn)
            yield conn

        with patch("src.engine.paper_trader.get_db", _multi_cm):
            result = await trader.open_trade(_make_decision())

        assert result is None

    @pytest.mark.asyncio
    async def test_open_trade_skips_resolved_market(self):
        trader = _make_trader()

        call_count = 0

        @asynccontextmanager
        async def _multi_cm():
            nonlocal call_count
            call_count += 1
            conn = AsyncMock()
            if call_count == 1:
                conn.fetchrow = AsyncMock(return_value=None)
            elif call_count == 2:
                conn.fetchrow = AsyncMock(return_value=None)
            else:
                conn.fetchrow = AsyncMock(
                    return_value={
                        "end_date": datetime(2026, 4, 1, tzinfo=timezone.utc),
                        "last_trade_time": None,
                    }
                )
            conn.execute = AsyncMock()
            _attach_transaction(conn)
            yield conn

        with patch("src.engine.paper_trader.get_db", _multi_cm):
            result = await trader.open_trade(_make_decision())

        assert result is None


class TestCloseTrade:
    def _add_open_trade(
        self,
        trader: PaperTrader,
        trade_id: int = 1,
        entry_price: float = 0.50,
        size_usdc: float = 200.0,
        strategy: str = "follow",
    ):
        trade = OpenPaperTrade(
            id=trade_id,
            market_id="market-1",
            token_id="token-1",
            direction="yes",
            strategy=strategy,
            entry_price=entry_price,
            size_usdc=size_usdc,
            size_shares=size_usdc / entry_price,
            leader_wallet="0xLeader",
            confidence=0.8,
            fee_rate_pct=0.0,
        )
        trader._open_trades.append(trade)
        trader._capital -= size_usdc
        return trade

    @pytest.mark.asyncio
    async def test_close_trade_profitable(self):
        """Closing at a higher price than entry should yield positive pnl and increase capital."""
        trader = _make_trader()
        initial_capital = trader._capital
        self._add_open_trade(trader, trade_id=1, entry_price=0.50, size_usdc=200.0)

        cm, conn = _make_db_cm()

        with patch("src.engine.paper_trader.get_db", cm):
            success = await trader.close_trade(1, exit_price=0.60, close_reason="take_profit")

        assert success is True
        # size_usdc=200 at entry=0.50 means 400 shares.
        # exit=0.60 means gross pnl=(0.60-0.50)*400=40.
        assert trader.capital == pytest.approx(initial_capital + 40.0)
        assert len(trader._open_trades) == 0

    @pytest.mark.asyncio
    async def test_close_trade_stop_loss(self):
        """Closing at an 8% loss should produce negative pnl."""
        trader = _make_trader()
        self._add_open_trade(trader, trade_id=2, entry_price=0.50, size_usdc=200.0)

        cm, conn = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            success = await trader.close_trade(2, exit_price=0.46, close_reason="stop_loss")

        assert success is True
        # size_usdc=200 at entry=0.50 means 400 shares.
        # exit=0.46 means gross pnl=(0.46-0.50)*400=-16.
        assert trader.capital == pytest.approx(settings.PAPER_CAPITAL_USDC - 16.0)

    @pytest.mark.asyncio
    async def test_close_trade_updates_thompson(self):
        """When a trade closes profitably, update_thompson must be called with won=True."""
        mock_engine = MagicMock()
        trader = _make_trader(confidence_engine=mock_engine)
        self._add_open_trade(
            trader, trade_id=3, entry_price=0.50, size_usdc=200.0, strategy="follow"
        )

        cm, _ = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            await trader.close_trade(3, exit_price=0.60, close_reason="take_profit")

        mock_engine.update_thompson.assert_called_once_with(
            wallet="0xLeader",
            action="follow",
            won=True,
        )

    @pytest.mark.asyncio
    async def test_close_trade_updates_thompson_loss(self):
        """When a trade closes at a loss, update_thompson must be called with won=False."""
        mock_engine = MagicMock()
        trader = _make_trader(confidence_engine=mock_engine)
        self._add_open_trade(
            trader, trade_id=4, entry_price=0.50, size_usdc=200.0, strategy="follow"
        )

        cm, _ = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            await trader.close_trade(4, exit_price=0.40, close_reason="stop_loss")

        mock_engine.update_thompson.assert_called_once_with(
            wallet="0xLeader",
            action="follow",
            won=False,
        )

    @pytest.mark.asyncio
    async def test_close_trade_publishes_to_redis(self):
        """Closing a trade must publish an event to REDIS_PAPER_CLOSED_CHANNEL."""
        mock_redis = _make_redis()
        trader = _make_trader(redis=mock_redis)
        self._add_open_trade(trader, trade_id=5, entry_price=0.50, size_usdc=200.0)

        cm, _ = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            await trader.close_trade(5, exit_price=0.55, close_reason="leader_exit")

        mock_redis.publish.assert_called_once()
        channel_arg = mock_redis.publish.call_args[0][0]
        assert channel_arg == REDIS_PAPER_CLOSED_CHANNEL

    @pytest.mark.asyncio
    async def test_close_trade_unknown_id_returns_false(self):
        """Closing a trade that is not in open_trades must return False."""
        trader = _make_trader()
        cm, _ = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            result = await trader.close_trade(999, exit_price=0.60, close_reason="manual")
        assert result is False

    @pytest.mark.asyncio
    async def test_close_trade_prefers_record_outcome_when_available(self):
        class StubEngine:
            def __init__(self):
                self.update_thompson = MagicMock()
                self.record_outcome = AsyncMock(
                    return_value={"reason_codes": ["high_deviation"], "penalty": 0.4}
                )

        engine = StubEngine()
        trader = _make_trader(confidence_engine=engine)
        trade = OpenPaperTrade(
            id=6,
            market_id="market-1",
            token_id="token-1",
            direction="yes",
            strategy="follow",
            entry_price=0.50,
            size_usdc=200.0,
            size_shares=400.0,
            leader_wallet="0xLeader",
            confidence=0.8,
            fee_rate_pct=0.0,
            leader_context={"trade_context": {"category": "crypto"}},
        )
        trader._open_trades.append(trade)
        trader._capital -= trade.size_usdc

        cm, _ = _make_db_cm()
        with patch("src.engine.paper_trader.get_db", cm):
            await trader.close_trade(6, exit_price=0.40, close_reason="stop_loss")

        engine.record_outcome.assert_awaited_once()
        engine.update_thompson.assert_not_called()
