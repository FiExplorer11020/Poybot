"""
Unit tests for src/engine/risk_manager.py
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.engine.risk_manager import RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(market_id: str = "market-1", action: str = "follow") -> dict:
    return {"market_id": market_id, "action": action}


def _make_db_cm_returning(cnt_value: int = 0, total_value: float = 0.0):
    """Build a simple async context manager whose fetchrow returns given cnt/total."""

    @asynccontextmanager
    async def _cm():
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": cnt_value, "total": total_value, "wins": 0})
        yield conn

    return _cm


# ---------------------------------------------------------------------------
# check_can_trade
# ---------------------------------------------------------------------------


class TestCheckCanTrade:
    @pytest.mark.asyncio
    async def test_check_can_trade_passes_clean_state(self):
        """All DB calls return 0 — clean slate should allow trading."""
        rm = RiskManager()
        signal = _make_signal()

        with patch(
            "src.engine.risk_manager.get_db", _make_db_cm_returning(cnt_value=0, total_value=0.0)
        ):
            result = await rm.check_can_trade(signal, current_capital=settings.PAPER_CAPITAL_USDC)

        assert result is True

    @pytest.mark.asyncio
    async def test_drawdown_circuit_breaker(self):
        """21% drawdown (peak=10000, capital=7900) must block trading."""
        rm = RiskManager()
        rm._peak_capital = 10_000.0

        with patch("src.engine.risk_manager.get_db", _make_db_cm_returning(0, 0.0)):
            result = await rm.check_can_trade(_make_signal(), current_capital=7_900.0)

        assert result is False

    @pytest.mark.asyncio
    async def test_exactly_20pct_drawdown_blocked(self):
        """Exactly 20% drawdown (boundary) must also be blocked (>= check)."""
        rm = RiskManager()
        rm._peak_capital = 10_000.0

        with patch("src.engine.risk_manager.get_db", _make_db_cm_returning(0, 0.0)):
            result = await rm.check_can_trade(_make_signal(), current_capital=8_000.0)

        assert result is False

    @pytest.mark.asyncio
    async def test_consecutive_losses_circuit_breaker(self):
        """5 consecutive losses must block trading regardless of drawdown."""
        rm = RiskManager()
        rm._consecutive_losses = 5

        with patch("src.engine.risk_manager.get_db", _make_db_cm_returning(0, 0.0)):
            result = await rm.check_can_trade(
                _make_signal(), current_capital=settings.PAPER_CAPITAL_USDC
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_market_losses_circuit_breaker(self):
        """3 losses on the same market in 24h must block trading."""
        rm = RiskManager()

        @asynccontextmanager
        async def _db_with_3_losses():
            conn = AsyncMock()
            # First call: _count_recent_losses → return 3
            conn.fetchrow = AsyncMock(return_value={"cnt": 3, "total": 0, "wins": 0})
            yield conn

        with patch("src.engine.risk_manager.get_db", _db_with_3_losses):
            result = await rm.check_can_trade(
                _make_signal(), current_capital=settings.PAPER_CAPITAL_USDC
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_open_positions_circuit_breaker(self):
        """10 open positions must block new trades."""
        rm = RiskManager()

        call_count = 0

        @asynccontextmanager
        async def _db_varying():
            nonlocal call_count
            call_count += 1
            conn = AsyncMock()
            if call_count == 1:
                # _count_recent_losses → 0
                conn.fetchrow = AsyncMock(return_value={"cnt": 0})
            elif call_count == 2:
                # _count_open_positions → 10
                conn.fetchrow = AsyncMock(return_value={"cnt": 10})
            else:
                conn.fetchrow = AsyncMock(return_value={"cnt": 0, "total": 0.0})
            yield conn

        with patch("src.engine.risk_manager.get_db", _db_varying):
            result = await rm.check_can_trade(
                _make_signal(), current_capital=settings.PAPER_CAPITAL_USDC
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_market_exposure_circuit_breaker(self):
        """Market exposure at or above MAX_MARKET_EXPOSURE_PCT must block trading."""
        rm = RiskManager()
        capital = 10_000.0
        # Market exposure = 2500 / 10000 = 25% = MAX_MARKET_EXPOSURE_PCT
        exposed_usdc = capital * settings.MAX_MARKET_EXPOSURE_PCT

        call_count = 0

        @asynccontextmanager
        async def _db_exposure():
            nonlocal call_count
            call_count += 1
            conn = AsyncMock()
            if call_count == 1:
                conn.fetchrow = AsyncMock(return_value={"cnt": 0})  # recent losses
            elif call_count == 2:
                conn.fetchrow = AsyncMock(return_value={"cnt": 0})  # open positions
            else:
                conn.fetchrow = AsyncMock(return_value={"total": exposed_usdc})  # exposure
            yield conn

        with patch("src.engine.risk_manager.get_db", _db_exposure):
            result = await rm.check_can_trade(_make_signal(), current_capital=capital)

        assert result is False


# ---------------------------------------------------------------------------
# apply_size
# ---------------------------------------------------------------------------


class TestApplySize:
    def test_apply_size_caps_at_max(self):
        """kelly_size=500 should be capped at 2% of 10000 = 200."""
        rm = RiskManager()
        size = rm.apply_size(500.0, _make_signal(action="follow"))
        expected_max = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT  # 200
        assert size == pytest.approx(expected_max)

    def test_apply_size_fade_at_half_max(self):
        """FADE strategy caps at 50% of follow max = 200 * 0.5 = 100."""
        rm = RiskManager()
        size = rm.apply_size(500.0, _make_signal(action="fade"))
        expected = (
            settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT * settings.FADE_SIZE_RATIO
        )  # 100
        assert size == pytest.approx(expected)

    def test_apply_size_below_min_returns_zero(self):
        """kelly_size below MIN_POSITION_USDC must return 0."""
        rm = RiskManager()
        result = rm.apply_size(30.0, _make_signal())
        assert result == 0.0

    def test_apply_size_exact_kelly_within_limits(self):
        """If kelly_size is within limits, it should be returned unchanged."""
        rm = RiskManager()
        size = rm.apply_size(150.0, _make_signal(action="follow"))
        assert size == pytest.approx(150.0)

    def test_warm_circuit_breaker_halves_size(self):
        """3 consecutive losses should halve the allowed max size."""
        rm = RiskManager()
        rm._consecutive_losses = 3
        # Without warm CB: max = 200. With CB: max = 100 for follow.
        size = rm.apply_size(500.0, _make_signal(action="follow"))
        expected_max = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT * 0.5  # 100
        assert size == pytest.approx(expected_max)

    def test_warm_circuit_breaker_on_fade(self):
        """3 consecutive losses on a FADE: max = 100 * 0.5 = 50, exactly at MIN."""
        rm = RiskManager()
        rm._consecutive_losses = 3
        size = rm.apply_size(500.0, _make_signal(action="fade"))
        # max = 200 * 0.5 (fade) * 0.5 (warm CB) = 50
        expected = (
            settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT * settings.FADE_SIZE_RATIO * 0.5
        )
        # 50.0 >= MIN_POSITION_USDC so it should not be zeroed
        assert size == pytest.approx(expected)


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


class TestRecordOutcome:
    def test_record_outcome_resets_consecutive_losses(self):
        """A win after 3 losses should reset consecutive loss counter to 0."""
        rm = RiskManager()
        rm._consecutive_losses = 3
        rm.record_outcome(won=True, capital=9_500.0)
        assert rm._consecutive_losses == 0

    def test_record_outcome_increments_on_loss(self):
        """A loss should increment consecutive loss counter."""
        rm = RiskManager()
        rm._consecutive_losses = 2
        rm.record_outcome(won=False, capital=9_800.0)
        assert rm._consecutive_losses == 3

    def test_record_outcome_updates_peak_capital(self):
        """Peak capital should update if new capital is higher."""
        rm = RiskManager()
        rm._peak_capital = 10_000.0
        rm.record_outcome(won=True, capital=10_500.0)
        assert rm._peak_capital == pytest.approx(10_500.0)

    def test_record_outcome_does_not_decrease_peak(self):
        """Peak capital must never go down."""
        rm = RiskManager()
        rm._peak_capital = 10_000.0
        rm.record_outcome(won=False, capital=9_500.0)
        assert rm._peak_capital == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# get_portfolio_stats
# ---------------------------------------------------------------------------


class TestGetPortfolioStats:
    @pytest.mark.asyncio
    async def test_get_portfolio_stats_structure(self):
        """get_portfolio_stats should return a PortfolioStats with correct fields."""
        rm = RiskManager()
        rm._peak_capital = 10_000.0
        rm._consecutive_losses = 1

        @asynccontextmanager
        async def _db_stats():
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"cnt": 2, "total": 10, "wins": 6})
            yield conn

        with patch("src.engine.risk_manager.get_db", _db_stats):
            stats = await rm.get_portfolio_stats(current_capital=9_800.0)

        assert stats.capital == pytest.approx(9_800.0)
        assert stats.peak_capital == pytest.approx(10_000.0)
        assert stats.drawdown_pct == pytest.approx(0.02)
        assert stats.consecutive_losses == 1
