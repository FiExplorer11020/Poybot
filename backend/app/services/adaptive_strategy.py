from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01
    max_total_exposure_pct: float = 0.40
    kelly_fraction: float = 0.5
    max_drawdown_stop_pct: float = 0.10
    fee_bps: float = 8.0
    base_entry_threshold: float = 0.004
    spread_cap: float = 0.08


@dataclass
class PortfolioState:
    equity: float
    capital_in_trade: float
    total_pnl: float


class AdaptiveStrategyEngine:
    """Spec-aligned strategy/risk/execution engine for binary probability markets."""

    def __init__(self, cfg: RiskConfig | None = None, lookback: int = 30) -> None:
        self.cfg = cfg or RiskConfig()
        self.lookback = lookback
        self._series: dict[str, deque[float]] = {}

    def evaluate_market(self, market_id: str, best_bid: float, best_ask: float) -> dict:
        best_bid = self._clip_probability(best_bid)
        best_ask = self._clip_probability(best_ask)
        if best_ask < best_bid:
            best_ask = best_bid
        spread = max(0.0, best_ask - best_bid)
        mid = self._clip_probability((best_bid + best_ask) / 2)

        series = self._series.setdefault(market_id, deque(maxlen=self.lookback))
        prev = series[-1] if series else mid
        series.append(mid)

        vol = self._rolling_volatility(series)
        liquidity_score = max(0.0, 1 - min(1.0, spread / self.cfg.spread_cap))
        expected_edge = max(0.0, (0.03 - spread) * (1 + liquidity_score) - vol * 1.5)
        threshold = (
            self.cfg.base_entry_threshold
            + spread * 0.35
            + vol * 0.2
            - liquidity_score * 0.002
        )
        detected = expected_edge >= max(0.0005, threshold)
        direction = "BUY_YES" if mid <= 0.5 else "BUY_NO"

        return {
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "mid_price": round(mid, 4),
            "spread": round(spread, 4),
            "volatility": round(vol, 6),
            "liquidity_score": round(liquidity_score, 4),
            "expected_edge": round(expected_edge, 6),
            "entry_threshold": round(threshold, 6),
            "detected": detected,
            "direction": direction,
            "price_delta": round(mid - prev, 6),
        }

    def size_position(self, portfolio: PortfolioState, expected_edge: float) -> tuple[float, float]:
        risk_cap = portfolio.equity * self.cfg.risk_per_trade_pct
        exposure_cap = max(0.0, portfolio.equity * self.cfg.max_total_exposure_pct - portfolio.capital_in_trade)
        notional = min(risk_cap, exposure_cap)
        kelly_score = min(1.0, max(0.1, expected_edge / 0.02))
        notional *= min(1.0, max(0.1, self.cfg.kelly_fraction * kelly_score))
        risk_pct = 0.0 if portfolio.equity <= 0 else (notional / portfolio.equity) * 100
        return round(notional, 2), round(risk_pct, 3)

    def estimate_trade_outcome(self, notional: float, spread: float, volatility: float, expected_edge: float) -> dict:
        slippage = max(0.0005, spread * 0.35 + volatility * 0.25)
        fees = notional * (self.cfg.fee_bps / 10_000)
        gross = notional * expected_edge
        pnl_abs = gross - fees - notional * slippage
        pnl_pct = 0.0 if notional == 0 else (pnl_abs / notional) * 100
        return {
            "slippage": round(slippage, 6),
            "fees": round(fees, 4),
            "pnl_abs": round(pnl_abs, 4),
            "pnl_pct": round(pnl_pct, 4),
        }

    @staticmethod
    def _clip_probability(value: float) -> float:
        return max(0.01, min(0.99, float(value)))

    @staticmethod
    def _rolling_volatility(series: deque[float]) -> float:
        if len(series) < 3:
            return 0.0
        diffs = [series[i] - series[i - 1] for i in range(1, len(series))]
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / max(1, len(diffs) - 1)
        return sqrt(var)
