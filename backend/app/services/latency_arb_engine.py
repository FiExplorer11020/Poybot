from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import exp, sqrt

from app.ingestion.universe import MarketUniverse

_STRIKE_PATTERN = re.compile(r"[$€]\s*([\d,]+(?:\.\d+)?)")
_SQRT_2 = sqrt(2.0)


@dataclass
class ArbConfig:
    min_edge: float = 0.04
    min_time_to_expiry_h: float = 0.25
    max_time_to_expiry_h: float = 24.0
    max_poly_spread: float = 0.06
    max_spot_age_ms: float = 500.0
    vol_daily: float = 0.04
    spot_symbol: str = "BTCUSDT"
    confidence_edge_scale: float = 0.10
    confidence_spread_scale: float = 0.08


@dataclass
class ArbSignal:
    market_id: str
    market_title: str
    direction: str
    poly_mid: float
    fair_prob: float
    edge: float
    lag_ms: float
    confidence: float
    spot_symbol: str
    spot_mid: float
    strike: float
    expiry_ts: float


@dataclass
class TopOfBookData:
    best_bid: float
    best_ask: float
    updated_ts: float | None = None
    age_ms: float | None = None

    def normalized(self) -> tuple[float, float]:
        best_bid = _clip_probability(self.best_bid)
        best_ask = _clip_probability(self.best_ask)
        if best_ask < best_bid:
            best_ask = best_bid
        return best_bid, best_ask


@dataclass
class SpotPriceData:
    symbol: str
    mid: float
    updated_ts: float | None = None
    age_ms: float | None = None


@dataclass
class SpotPriceCache:
    quotes: dict[str, SpotPriceData] = field(default_factory=dict)

    def get(self, symbol: str) -> SpotPriceData | None:
        return self.quotes.get(symbol)

    def set(self, spot_data: SpotPriceData) -> None:
        self.quotes[spot_data.symbol] = spot_data


class LatencyArbEngine:
    """Estimate a short Polymarket repricing lag against a fast spot feed."""

    def __init__(
        self,
        cfg: ArbConfig | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg or ArbConfig()
        self._now_fn = now_fn or time.time

    def evaluate(
        self,
        market: MarketUniverse,
        poly_book: TopOfBookData,
        spot_cache: SpotPriceCache,
    ) -> ArbSignal | None:
        strike = self.extract_strike(market.market_title)
        if strike is None:
            return None

        expiry_ts = self._resolve_expiry_ts(market)
        if expiry_ts is None:
            return None

        now_ts = self._now_fn()
        time_to_expiry_h = (expiry_ts - now_ts) / 3600
        if time_to_expiry_h <= 0:
            return None

        spot_data = spot_cache.get(self.cfg.spot_symbol)
        if spot_data is None:
            return None

        best_bid, best_ask = poly_book.normalized()
        poly_mid = _clip_probability((best_bid + best_ask) / 2)
        poly_spread = max(0.0, best_ask - best_bid)
        spot_mid = float(spot_data.mid)
        spot_age_ms = self._age_ms(spot_data, now_ts)
        poly_age_ms = self._age_ms(poly_book, now_ts)
        lag_ms = max(0.0, poly_age_ms - spot_age_ms)

        fair_prob = self._fair_probability(
            spot_mid=spot_mid,
            strike=strike,
            time_to_expiry_h=time_to_expiry_h,
        )
        if fair_prob > poly_mid:
            edge = fair_prob - poly_mid
            direction = "BUY_YES"
        else:
            edge = poly_mid - fair_prob
            direction = "BUY_NO"

        if edge < self.cfg.min_edge:
            return None
        if time_to_expiry_h < self.cfg.min_time_to_expiry_h:
            return None
        if time_to_expiry_h > self.cfg.max_time_to_expiry_h:
            return None
        if poly_spread > self.cfg.max_poly_spread:
            return None
        if spot_age_ms > self.cfg.max_spot_age_ms:
            return None

        confidence = self._confidence(edge=edge, poly_spread=poly_spread, spot_age_ms=spot_age_ms)
        return ArbSignal(
            market_id=market.market_id,
            market_title=market.market_title,
            direction=direction,
            poly_mid=round(poly_mid, 4),
            fair_prob=round(fair_prob, 6),
            edge=round(edge, 6),
            lag_ms=round(lag_ms, 3),
            confidence=round(confidence, 6),
            spot_symbol=self.cfg.spot_symbol,
            spot_mid=round(spot_mid, 4),
            strike=round(strike, 4),
            expiry_ts=expiry_ts,
        )

    @staticmethod
    def extract_strike(title: str) -> float | None:
        match = _STRIKE_PATTERN.search(title)
        if match is None:
            return None

        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def norm_cdf(z: float) -> float:
        return 0.5 * (1.0 + LatencyArbEngine._erf(z / _SQRT_2))

    @staticmethod
    def _erf(x: float) -> float:
        sign = 1.0 if x >= 0 else -1.0
        x = abs(x)

        p = 0.3275911
        a1 = 0.254829592
        a2 = -0.284496736
        a3 = 1.421413741
        a4 = -1.453152027
        a5 = 1.061405429

        t = 1.0 / (1.0 + p * x)
        poly = (((((a5 * t) + a4) * t) + a3) * t + a2) * t + a1
        y = 1.0 - (poly * t * exp(-(x * x)))
        return sign * y

    def _fair_probability(self, spot_mid: float, strike: float, time_to_expiry_h: float) -> float:
        if strike <= 0:
            return 0.5

        sigma = self.cfg.vol_daily * sqrt(max(time_to_expiry_h, 0.0) / 24.0)
        if sigma <= 0:
            if spot_mid > strike:
                return 1.0
            if spot_mid < strike:
                return 0.0
            return 0.5

        distance_pct = (spot_mid - strike) / strike
        z = distance_pct / sigma
        return max(0.0, min(1.0, self.norm_cdf(z)))

    def _confidence(self, edge: float, poly_spread: float, spot_age_ms: float) -> float:
        edge_factor = min(1.0, edge / max(self.cfg.confidence_edge_scale, 1e-9))
        spread_factor = max(0.0, 1.0 - (poly_spread / max(self.cfg.confidence_spread_scale, 1e-9)))
        freshness_factor = max(0.0, 1.0 - (spot_age_ms / max(self.cfg.max_spot_age_ms, 1e-9)))
        return max(0.0, min(1.0, edge_factor * spread_factor * freshness_factor))

    @staticmethod
    def _age_ms(data: TopOfBookData | SpotPriceData, now_ts: float) -> float:
        if data.age_ms is not None:
            return max(0.0, float(data.age_ms))
        if data.updated_ts is None:
            return float("inf")
        return max(0.0, (now_ts - float(data.updated_ts)) * 1000)

    @staticmethod
    def _resolve_expiry_ts(market: MarketUniverse) -> float | None:
        direct_ts = getattr(market, "expiry_ts", None)
        if direct_ts is not None:
            try:
                return float(direct_ts)
            except (TypeError, ValueError):
                return None

        for attr_name in ("end_ts", "expires_at_ts", "end_timestamp"):
            value = getattr(market, attr_name, None)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        for attr_name in ("end_date", "endDate", "end_date_iso", "endDateIso", "expiry"):
            value = getattr(market, attr_name, None)
            if value is None:
                continue
            parsed = LatencyArbEngine._parse_timestamp(value)
            if parsed is not None:
                return parsed

        return None

    @staticmethod
    def _parse_timestamp(raw_value: object) -> float | None:
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        if not isinstance(raw_value, str):
            return None

        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()


def _clip_probability(value: float) -> float:
    return max(0.01, min(0.99, float(value)))
