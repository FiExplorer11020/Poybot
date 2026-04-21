from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

USD_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class SlippageEstimate:
    cost_usdc: Decimal
    impact_bps: Decimal
    source: str


def _q(value: Decimal) -> Decimal:
    return value.quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def estimate_slippage_usdc(
    *,
    size_usdc: Decimal,
    volume_24h_usdc: Decimal,
    volatility_24h: Decimal,
    impact_k: Decimal = Decimal("0.5"),
    fallback_bps: Decimal = Decimal("50"),
) -> SlippageEstimate:
    if size_usdc < 0:
        raise ValueError("size_usdc must be non-negative")
    if volume_24h_usdc > 0 and volatility_24h >= 0:
        ratio = size_usdc / volume_24h_usdc
        impact_fraction = impact_k * volatility_24h * Decimal(str(float(ratio) ** 0.5))
        return SlippageEstimate(
            cost_usdc=_q(size_usdc * impact_fraction),
            impact_bps=_q(impact_fraction * Decimal("10000")),
            source="sqrt_impact",
        )
    fallback_fraction = fallback_bps / Decimal("10000")
    return SlippageEstimate(
        cost_usdc=_q(size_usdc * fallback_fraction),
        impact_bps=_q(fallback_bps),
        source="constant",
    )
