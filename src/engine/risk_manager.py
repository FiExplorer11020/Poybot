"""
Risk Manager — pre-trade circuit breakers and portfolio exposure controls.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.config import settings
from src.control.killswitch import get_killswitch
from src.control.runtime_config import get_runtime_config
from src.database.connection import get_db
from src.economics.versioning import valid_paper_trade_filter

V1_PAPER_TRADE_SQL = valid_paper_trade_filter()

# Telegram surfacing channels (S3.11). Kept as module constants so the
# notifier subscriber and producer agree on the contract without
# importing each other.
CHANNEL_RISK_BREAKER = "engine:risk:breaker_tripped"
CHANNEL_DRAWDOWN_THRESHOLD = "engine:portfolio:drawdown_threshold"

# Drawdown tiers we surface to the operator. One alert per crossing of
# a NEW (higher) tier; reset to 0 when capital recovers above half the
# last-published threshold so we don't spam during sustained drawdown.
DRAWDOWN_THRESHOLDS = (0.03, 0.05, 0.10, 0.15, 0.20)


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
    def __init__(self, redis_client=None):
        self._consecutive_losses: int = 0
        self._peak_capital: float = settings.PAPER_CAPITAL_USDC
        # S3.11: optional redis_client so circuit-breaker trips surface
        # to Telegram. None = silent (kept that way for the legacy test
        # constructor that passes no args).
        self._redis = redis_client
        # Highest drawdown threshold already published this session. Resets
        # when drawdown halves to avoid retrigger flapping.
        self._drawdown_published_threshold: float = 0.0

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
        Returns True if it is safe to trade. Thresholds are read from the
        mutable RuntimeConfig (Redis-backed) so the dashboard's Risk &
        Config cockpit can flip them at runtime without a redeploy.

        Checks (in order):
        - Global killswitch (execution_enabled flag in system_control)
        - Drawdown < runtime.max_drawdown_stop_pct (default 20%)
        - consecutive_losses < runtime.max_consecutive_losses (default 5)
        - same-market 24h losses < runtime.max_recent_losses_per_market (default 3)
        - open positions < runtime.max_concurrent_positions (default 10)
        - market exposure < runtime.max_total_exposure_pct
        """
        # Global killswitch — must be first. If False (or infra failure), refuse.
        try:
            if not await get_killswitch().is_execution_enabled():
                logger.warning("Circuit breaker: killswitch is OFF, refusing trade")
                return False
        except Exception as e:
            # Fail safe: if we can't read the killswitch, assume it's OFF.
            logger.error(f"Circuit breaker: killswitch read failed ({e}), refusing trade")
            return False

        cfg = await get_runtime_config().effective()
        max_dd = float(cfg.get("max_drawdown_stop_pct", 0.20))
        max_cons_losses = int(cfg.get("max_consecutive_losses", 5))
        max_recent_market_losses = int(cfg.get("max_recent_losses_per_market", 3))
        max_open = int(cfg.get("max_concurrent_positions", 10))
        max_market_exposure = float(cfg.get("max_total_exposure_pct", settings.MAX_MARKET_EXPOSURE_PCT))

        drawdown = (
            (self._peak_capital - current_capital) / self._peak_capital
            if self._peak_capital > 0
            else 0.0
        )
        # Surface drawdown threshold crossings BEFORE evaluating the hard
        # breaker, so the operator gets warnings at 3/5/10% instead of
        # only seeing a refused trade at 20%.
        await self._publish_drawdown_crossing(drawdown, current_capital)

        if drawdown >= max_dd:
            logger.warning(f"Circuit breaker: drawdown={drawdown:.1%} >= {max_dd:.1%}")
            await self._publish_breaker("drawdown", drawdown, max_dd)
            return False

        if self._consecutive_losses >= max_cons_losses:
            logger.warning(
                f"Circuit breaker: {self._consecutive_losses} consecutive losses "
                f">= {max_cons_losses}"
            )
            await self._publish_breaker(
                "consecutive_losses", self._consecutive_losses, max_cons_losses
            )
            return False

        market_id = signal.get("market_id", "")
        recent_losses = await self._count_recent_losses(market_id)
        if recent_losses >= max_recent_market_losses:
            logger.warning(
                f"Circuit breaker: {recent_losses} losses on {market_id} in 24h "
                f">= {max_recent_market_losses}"
            )
            await self._publish_breaker(
                "recent_market_losses",
                recent_losses,
                max_recent_market_losses,
                market_id=market_id,
            )
            return False

        open_count = await self._count_open_positions()
        if open_count >= max_open:
            logger.warning(f"Circuit breaker: {open_count} open positions >= {max_open}")
            await self._publish_breaker("open_count", open_count, max_open)
            return False

        market_exposure = await self._market_exposure(market_id, current_capital)
        if market_exposure >= max_market_exposure:
            logger.warning(
                f"Circuit breaker: market exposure {market_exposure:.1%} >= "
                f"{max_market_exposure:.1%}"
            )
            await self._publish_breaker(
                "market_exposure",
                market_exposure,
                max_market_exposure,
                market_id=market_id,
            )
            return False

        return True

    # ------------------------------------------------------------------ #
    # S3.11 publishers — best-effort, never block the hot path           #
    # ------------------------------------------------------------------ #

    async def _publish_breaker(
        self,
        breaker: str,
        value,
        threshold,
        *,
        market_id: str | None = None,
    ) -> None:
        """Surface a circuit-breaker trip to Telegram. Silent on no-redis."""
        if self._redis is None:
            return
        try:
            payload = {
                "breaker": breaker,
                "value": float(value),
                "threshold": float(threshold),
                "market_id": market_id,
            }
            await self._redis.publish(CHANNEL_RISK_BREAKER, json.dumps(payload))
        except Exception as e:
            logger.debug(f"risk_manager publish breaker failed: {e}")

    async def _publish_drawdown_crossing(
        self, drawdown: float, current_capital: float
    ) -> None:
        """Fire one alert per NEW (higher) drawdown threshold crossed.

        Reset condition: drawdown falls below half the last-published
        threshold (so a 5% alert isn't refired during oscillation in
        the 4-5% band; it stays armed until we drop back to <2.5%).
        """
        if self._redis is None or drawdown <= 0:
            # Recovery → re-arm at zero.
            if drawdown <= 0 and self._drawdown_published_threshold > 0:
                self._drawdown_published_threshold = 0.0
            return

        if (
            self._drawdown_published_threshold > 0
            and drawdown < self._drawdown_published_threshold * 0.5
        ):
            self._drawdown_published_threshold = 0.0

        for thr in DRAWDOWN_THRESHOLDS:
            if drawdown >= thr and self._drawdown_published_threshold < thr:
                try:
                    payload = {
                        "drawdown_pct": float(drawdown),
                        "threshold": float(thr),
                        "peak_capital": float(self._peak_capital),
                        "current_capital": float(current_capital),
                    }
                    await self._redis.publish(
                        CHANNEL_DRAWDOWN_THRESHOLD, json.dumps(payload)
                    )
                    self._drawdown_published_threshold = thr
                except Exception as e:
                    logger.debug(f"risk_manager publish drawdown failed: {e}")
                break

    async def apply_size_async(self, kelly_size: float, signal: dict) -> float:
        """Enforce position size limits and return the allowed size in USDC.

        Reads ``risk_per_trade_pct`` and ``fade_size_ratio`` from the
        runtime config so the dashboard cockpit can scale exposure
        without a redeploy. The synchronous ``apply_size`` shim below
        falls back to the env-driven defaults for callers that haven't
        been switched to the async path yet.
        """
        if kelly_size < settings.MIN_POSITION_USDC:
            return 0.0

        cfg = await get_runtime_config().effective()
        risk_pct = float(cfg.get("risk_per_trade_pct", settings.MAX_POSITION_PCT))
        fade_ratio = float(cfg.get("fade_size_ratio", settings.FADE_SIZE_RATIO))
        max_cons = int(cfg.get("max_consecutive_losses", 5))

        max_size = settings.PAPER_CAPITAL_USDC * risk_pct

        if signal.get("action") == "fade":
            max_size *= fade_ratio

        # Warm circuit breaker: halve max once we cross 60% of the consecutive-loss
        # ceiling. Smoothly anticipates the hard breaker rather than only reacting
        # at the last possible moment.
        warm_breaker = max(1, int(max_cons * 0.6))
        if self._consecutive_losses >= warm_breaker:
            max_size *= 0.5

        size = min(kelly_size, max_size)
        return size if size >= settings.MIN_POSITION_USDC else 0.0

    def apply_size(self, kelly_size: float, signal: dict) -> float:
        """Synchronous fallback that uses env defaults only — kept for
        compatibility with callers that don't yet use the async API."""
        if kelly_size < settings.MIN_POSITION_USDC:
            return 0.0

        max_size = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT

        if signal.get("action") == "fade":
            max_size *= settings.FADE_SIZE_RATIO

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
