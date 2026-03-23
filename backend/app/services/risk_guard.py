from __future__ import annotations

import logging
from typing import Any

from app.core.settings import get_settings
from app.services.adaptive_strategy import PortfolioState, RiskConfig
from app.services.trade_executor import TradeExecutor

log = logging.getLogger(__name__)


class DrawdownHaltException(Exception):
    def __init__(self, current_drawdown_pct: float, threshold_pct: float) -> None:
        self.current_drawdown_pct = current_drawdown_pct
        self.threshold_pct = threshold_pct
        super().__init__(
            "drawdown halt triggered: "
            f"{current_drawdown_pct:.4f} >= {threshold_pct:.4f}"
        )


class APIFailureHaltException(Exception):
    def __init__(self, consecutive_failures: int) -> None:
        self.consecutive_failures = consecutive_failures
        super().__init__(
            "api failure halt triggered: "
            f"{consecutive_failures} consecutive failures"
        )


class RiskGuard:
    def __init__(self, cfg: RiskConfig, executor: TradeExecutor) -> None:
        self.cfg = cfg
        self.executor = executor
        self.consecutive_failures = 0

    async def check_drawdown(self, portfolio: PortfolioState) -> None:
        threshold_pct = max(0.0, float(self.cfg.max_drawdown_stop_pct))
        starting_equity = float(portfolio.equity) - float(portfolio.total_pnl)
        if starting_equity <= 0:
            starting_equity = float(portfolio.equity)
        if starting_equity <= 0:
            return

        current_drawdown_pct = 0.0
        if portfolio.total_pnl < 0:
            current_drawdown_pct = abs(float(portfolio.total_pnl)) / starting_equity

        if current_drawdown_pct > 0 and current_drawdown_pct >= threshold_pct:
            raise DrawdownHaltException(
                current_drawdown_pct=current_drawdown_pct,
                threshold_pct=threshold_pct,
            )

    async def check_api_health(self) -> None:
        if self.consecutive_failures >= 3:
            raise APIFailureHaltException(consecutive_failures=self.consecutive_failures)

    async def cancel_all_open_orders(self) -> dict:
        result = {"cancelled": 0, "errors": []}
        try:
            payload = await self.executor.clob.cancel_all_orders(
                headers=self._auth_headers(),
            )
            result["cancelled"] = self._cancelled_count(payload)
            result["errors"] = self._cancel_errors(payload)
        except Exception as exc:
            log.exception("Failed to cancel open orders during trading halt: %s", exc)
            result["errors"].append(str(exc))
        return result

    def record_api_success(self) -> None:
        self.consecutive_failures = 0

    def record_api_failure(self, error: Exception) -> None:
        self.consecutive_failures += 1
        log.warning(
            "Trade API failure recorded (%s consecutive): %s",
            self.consecutive_failures,
            error,
        )

    def _auth_headers(self) -> dict[str, str]:
        settings = getattr(self.executor, "settings", None) or get_settings()
        return {
            "POLY_API_KEY": getattr(settings, "polymarket_api_key", None) or "",
            "POLY_API_SECRET": getattr(settings, "polymarket_api_secret", None) or "",
            "POLY_PASSPHRASE": getattr(settings, "polymarket_api_passphrase", None) or "",
        }

    @staticmethod
    def _cancelled_count(payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0

        for key in ("cancelled", "canceled", "cancelled_count", "canceled_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                return len(value)
        return 0

    @staticmethod
    def _cancel_errors(payload: Any) -> list:
        if not isinstance(payload, dict):
            return []

        raw_errors = payload.get("errors")
        if raw_errors is None:
            raw_errors = payload.get("not_canceled") or payload.get("not_cancelled") or []

        if isinstance(raw_errors, list):
            return raw_errors
        if raw_errors in (None, ""):
            return []
        return [raw_errors]
