from decimal import ROUND_HALF_UP, Decimal

from src.economics.models import LiquidityRole

USD_QUANT = Decimal("0.00000")


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def calculate_polymarket_fee(
    *,
    shares: Decimal | int | float | str,
    price: Decimal | int | float | str,
    fee_rate: Decimal | int | float | str,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    fees_enabled: bool = True,
) -> Decimal:
    shares_d = _to_decimal(shares)
    price_d = _to_decimal(price)
    fee_rate_d = _to_decimal(fee_rate)
    role = LiquidityRole(liquidity_role)

    if shares_d < 0:
        raise ValueError("shares must be non-negative")
    if price_d < 0 or price_d > 1:
        raise ValueError("binary price must be between 0 and 1")
    if fee_rate_d < 0:
        raise ValueError("fee_rate must be non-negative")
    if not fees_enabled or role == LiquidityRole.MAKER:
        return Decimal("0").quantize(USD_QUANT)

    fee = shares_d * fee_rate_d * price_d * (Decimal("1") - price_d)
    return fee.quantize(USD_QUANT, rounding=ROUND_HALF_UP)
