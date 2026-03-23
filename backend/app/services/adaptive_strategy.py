from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from math import sqrt


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01
    max_total_exposure_pct: float = 0.25
    kelly_fraction: float = 0.25
    max_drawdown_stop_pct: float = 0.10
    fee_bps: float = 8.0
    base_entry_threshold: float = 0.005
    spread_cap: float = 0.06
    allocation_mode: str = "automatic"
    manual_notional_amount: float = 100.0
    min_observations: int = 4
    min_signal_strength: float = 1.0
    max_concurrent_positions: int = 4
    max_positions_per_tick: int = 1
    cooldown_seconds: int = 10
    signal_staleness_seconds: int = 3
    max_holding_seconds: int = 180
    display_market_limit: int = 80


@dataclass
class PortfolioState:
    equity: float
    capital_in_trade: float
    total_pnl: float


class MarketRegime(StrEnum):
    LOW_VOL = "low_vol"
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"


class AdaptiveStrategyEngine:
    """Signal model tuned for short-lived Polymarket order book changes."""

    def __init__(self, cfg: RiskConfig | None = None, lookback: int = 20) -> None:
        self.cfg = cfg or RiskConfig()
        self.lookback = lookback
        self._series: dict[str, deque[float]] = {}

    @staticmethod
    def _regime_from_volatility(volatility: float) -> MarketRegime:
        if volatility > 0.03:
            return MarketRegime.CRISIS
        if volatility > 0.015:
            return MarketRegime.HIGH_VOL
        if volatility < 0.005:
            return MarketRegime.LOW_VOL
        return MarketRegime.NORMAL

    @staticmethod
    def _threshold_multiplier(regime: MarketRegime) -> float:
        return {
            MarketRegime.LOW_VOL: 0.7,
            MarketRegime.NORMAL: 1.0,
            MarketRegime.HIGH_VOL: 1.5,
            MarketRegime.CRISIS: 1.0,
        }[regime]

    @staticmethod
    def _regime_severity(regime: MarketRegime) -> int:
        return {
            MarketRegime.LOW_VOL: 0,
            MarketRegime.NORMAL: 1,
            MarketRegime.HIGH_VOL: 2,
            MarketRegime.CRISIS: 3,
        }[regime]

    def classify_regime(self, market_id: str) -> MarketRegime:
        series = self._series.get(market_id)
        if series is None:
            return MarketRegime.LOW_VOL
        volatility = self._rolling_volatility(series)
        return self._regime_from_volatility(volatility)

    def portfolio_regime(self, market_ids: list[str]) -> MarketRegime:
        if not market_ids:
            return MarketRegime.NORMAL
        return max(
            (self.classify_regime(market_id) for market_id in market_ids),
            key=self._regime_severity,
        )

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

        momentum = series[-1] - series[0] if len(series) >= 2 else 0.0
        vol = self._rolling_volatility(series)

        # Liquidity and cost alignment
        liquidity_score = max(0.0, 1 - min(1.0, spread / max(self.cfg.spread_cap, 0.0001)))
        trading_cost = spread + (self.cfg.fee_bps / 10_000)

        # Edge calculation: momentum adjusted for vol and costs
        raw_edge = max(0.0, abs(momentum) - trading_cost - (vol * 0.4))
        expected_edge = raw_edge * liquidity_score

        regime = self.classify_regime(market_id)
        threshold = max(self.cfg.base_entry_threshold, trading_cost * 1.1 + (vol * 0.3))
        threshold *= self._threshold_multiplier(regime)
        signal_strength = 0.0 if threshold <= 0 else expected_edge / threshold

        enough_history = len(series) >= self.cfg.min_observations
        detected = (
            enough_history
            and spread <= self.cfg.spread_cap
            and signal_strength >= self.cfg.min_signal_strength
            and regime != MarketRegime.CRISIS
        )

        direction = "HOLD"
        if detected:
            direction = "BUY_YES" if momentum >= 0 else "BUY_NO"

        return {
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "mid_price": round(mid, 4),
            "spread": round(spread, 4),
            "volatility": round(vol, 6),
            "liquidity_score": round(liquidity_score, 4),
            "expected_edge": round(expected_edge, 6),
            "entry_threshold": round(threshold, 6),
            "signal_strength": round(signal_strength, 4),
            "regime": regime.value,
            "detected": detected,
            "direction": direction,
            "price_delta": round(mid - prev, 6),
            "observations": len(series),
            "momentum": round(momentum, 6),
        }

    def size_position(self, portfolio: PortfolioState, expected_edge: float) -> tuple[float, float]:
        if self.cfg.allocation_mode == "manual":
            notional = float(self.cfg.manual_notional_amount)
            risk_pct = 0.0 if portfolio.equity <= 0 else (notional / portfolio.equity) * 100
            return round(notional, 2), round(risk_pct, 3)

        if portfolio.equity <= 0:
            return 0.0, 0.0

        risk_cap = portfolio.equity * self.cfg.risk_per_trade_pct
        exposure_cap = max(
            0.0,
            portfolio.equity * self.cfg.max_total_exposure_pct - portfolio.capital_in_trade,
        )
        if exposure_cap <= 0:
            return 0.0, 0.0

        edge_scale = min(1.0, max(0.0, expected_edge / max(self.cfg.base_entry_threshold, 0.0001)))
        kelly_scale = min(1.0, max(0.0, self.cfg.kelly_fraction * edge_scale))
        notional = min(risk_cap, exposure_cap) * kelly_scale
        risk_pct = (notional / portfolio.equity) * 100
        return round(notional, 2), round(risk_pct, 3)

    def estimate_round_trip_pnl(self, entry_price: float, exit_price: float, size: float) -> dict:
        gross = (exit_price - entry_price) * size
        fees = (entry_price + exit_price) * size * (self.cfg.fee_bps / 10_000)
        pnl_abs = gross - fees
        notional = max(entry_price * size, 0.0001)
        pnl_pct = (pnl_abs / notional) * 100
        return {
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
