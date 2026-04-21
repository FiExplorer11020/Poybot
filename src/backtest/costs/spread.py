from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.backtest.models import BacktestBookSnapshot

USD_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class CandleRange:
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class SpreadEstimate:
    spread_price: Decimal
    cost_usdc: Decimal
    source: str


CONSTANT_SPREAD_BPS = {
    "crypto": Decimal("40"),
    "politics": Decimal("60"),
    "sports": Decimal("80"),
    "other": Decimal("100"),
}


def _q(value: Decimal) -> Decimal:
    return value.quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def estimate_spread_cost(
    *,
    price: Decimal,
    size_shares: Decimal,
    category: str,
    book: BacktestBookSnapshot | None = None,
    candle: CandleRange | None = None,
) -> SpreadEstimate:
    if book is not None:
        spread_price = max(Decimal("0"), book.best_ask - book.best_bid)
        return SpreadEstimate(_q(spread_price), _q(spread_price * size_shares), "orderbook")

    if candle is not None:
        spread_price = max(Decimal("0"), (candle.high - candle.low) * Decimal("0.3"))
        return SpreadEstimate(_q(spread_price), _q(spread_price * size_shares), "candle")

    key = category if category in CONSTANT_SPREAD_BPS else "other"
    spread_price = price * CONSTANT_SPREAD_BPS[key] / Decimal("10000")
    return SpreadEstimate(_q(spread_price), _q(spread_price * size_shares), f"constant:{key}")
