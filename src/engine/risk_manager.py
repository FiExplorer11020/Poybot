"""
Risk Manager — pre-trade circuit breakers and portfolio exposure controls.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.economics.versioning import valid_paper_trade_filter

V1_PAPER_TRADE_SQL = valid_paper_trade_filter()


@dataclass
class PortfolioStats:
    capital: float
    peak_capital: float
    drawdown_pct: float
    open_positions: int
    win_rate: float
    total_trades: int
    consecutive_losses: int


class RiskManager:
    def __init__(self):
        self._consecutive_losses: int = 0
        self._peak_capital: float = settings.PAPER_CAPITAL_USDC

    def hydrate_from_state(self, *, peak_capital: float, consecutive_losses: int) -> None:
        """Restore counters from the persisted portfolio_state row.

        Called by PaperTrader.load_persisted_state() on boot so the drawdown
        circuit breaker stays accurate across restarts.
        """
        try:
            self._peak_capital = max(float(peak_capital), settings.PAPER_CAPITAL_USDC)
            self._consecutive_losses = max(0, int(consecutive_losses))
        except (TypeError, ValueError):
            pass

    async def check_can_trade(self, signal: dict, current_capital: float) -> bool:
        """
        Returns True if it is safe to trade. Checks:
        - Drawdown < 20%
        - Fewer than 5 consecutive losses
        - Fewer than 3 losses on the same market in the last 24h
        - Market exposure < MAX_MARKET_EXPOSURE_PCT
        - Total open positions < 10
        """
        drawdown = (
            (self._peak_capital - current_capital) / self._peak_capital
            if self._peak_capital > 0
            else 0.0
        )
        if drawdown >= 0.20:
            logger.warning(f"Circuit breaker: drawdown={drawdown:.1%} >= 20%")
            return False

        if self._consecutive_losses >= 5:
            logger.warning(f"Circuit breaker: {self._consecutive_losses} consecutive losses")
            return False

        market_id = signal.get("market_id", "")
        recent_losses = await self._count_recent_losses(market_id)
        if recent_losses >= 3:
            logger.warning(f"Circuit breaker: {recent_losses} losses on {market_id} in 24h")
            return False

        open_count = await self._count_open_positions()
        if open_count >= 10:
            logger.warning(f"Circuit breaker: {open_count} open positions >= 10")
            return False

        market_exposure = await self._market_exposure(market_id, current_capital)
        if market_exposure >= settings.MAX_MARKET_EXPOSURE_PCT:
            logger.warning(
                f"Circuit breaker: market exposure {market_exposure:.1%} >= "
                f"{settings.MAX_MARKET_EXPOSURE_PCT:.1%}"
            )
            return False

        return True

    def apply_size(self, kelly_size: float, signal: dict) -> float:
        """Enforce position size limits and return the allowed size in USDC."""
        if kelly_size < settings.MIN_POSITION_USDC:
            return 0.0

        max_size = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT

        if signal.get("action") == "fade":
            max_size *= settings.FADE_SIZE_RATIO

        # Warm circuit breaker: halve max if approaching consecutive-loss threshold
        if self._consecutive_losses >= 3:
            max_size *= 0.5

        size = min(kelly_size, max_size)
        return size if size >= settings.MIN_POSITION_USDC else 0.0

    def record_outcome(self, won: bool, capital: float) -> None:
        """Update consecutive loss counter and peak capital tracker."""
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        self._peak_capital = max(self._peak_capital, capital)

    async def get_portfolio_stats(self, current_capital: float) -> PortfolioStats:
        """Compute current portfolio statistics."""
        drawdown = (
            (self._peak_capital - current_capital) / self._peak_capital
            if self._peak_capital > 0
            else 0.0
        )
        open_count = await self._count_open_positions()
        win_rate, total_trades = await self._compute_win_rate()

        return PortfolioStats(
            capital=current_capital,
            peak_capital=self._peak_capital,
            drawdown_pct=round(drawdown, 4),
            open_positions=open_count,
            win_rate=round(win_rate, 4),
            total_trades=total_trades,
            consecutive_losses=self._consecutive_losses,
        )

    async def _count_recent_losses(self, market_id: str) -> int:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT COUNT(*) AS cnt FROM paper_trades
                    WHERE market_id=$1
                      AND closed_at >= $2
                      AND pnl_usdc < 0
                      AND {V1_PAPER_TRADE_SQL}
                    """,
                    market_id,
                    since,
                )
                return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    async def _count_open_positions(self) -> int:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM paper_trades
                    WHERE status='open'
                      AND {V1_PAPER_TRADE_SQL}
                    """,
                )
                return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    async def _market_exposure(self, market_id: str, current_capital: float) -> float:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT COALESCE(SUM(size_usdc), 0) AS total
                    FROM paper_trades
                    WHERE market_id=$1
                      AND status='open'
                      AND {V1_PAPER_TRADE_SQL}
                    """,
                    market_id,
                )
                total = float(row["total"]) if row else 0.0
                return total / current_capital if current_capital > 0 else 0.0
        except Exception:
            return 0.0

    async def _compute_win_rate(self) -> tuple[float, int]:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS wins
                    FROM paper_trades
                    WHERE status='closed'
                      AND {V1_PAPER_TRADE_SQL}
                    """
                )
                if row and row["total"]:
                    total = int(row["total"])
                    wins = int(row["wins"] or 0)
                    return wins / total, total
        except Exception:
            pass
        return 0.0, 0
