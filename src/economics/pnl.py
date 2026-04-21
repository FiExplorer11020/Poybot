from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

USD_QUANT = Decimal("0.000001")


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class PnLResult:
    gross_pnl_usdc: Decimal
    net_pnl_usdc: Decimal
    notional_usdc: Decimal
    pnl_pct: Decimal


def shares_from_notional(
    notional_usdc: Decimal | int | float | str,
    entry_price: Decimal | int | float | str,
) -> Decimal:
    notional = _to_decimal(notional_usdc)
    price = _to_decimal(entry_price)
    if price <= 0:
        raise ValueError("entry_price must be positive")
    if price > 1:
        raise ValueError("binary entry_price must be <= 1")
    if notional < 0:
        raise ValueError("notional_usdc must be non-negative")
    return (notional / price).quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def calculate_long_pnl(
    *,
    entry_price: Decimal | int | float | str,
    exit_price: Decimal | int | float | str,
    size_shares: Decimal | int | float | str,
    entry_fee_usdc: Decimal | int | float | str = Decimal("0"),
    exit_fee_usdc: Decimal | int | float | str = Decimal("0"),
    spread_cost_usdc: Decimal | int | float | str = Decimal("0"),
    slippage_usdc: Decimal | int | float | str = Decimal("0"),
) -> PnLResult:
    entry = _to_decimal(entry_price)
    exit_ = _to_decimal(exit_price)
    shares = _to_decimal(size_shares)
    entry_fee = _to_decimal(entry_fee_usdc)
    exit_fee = _to_decimal(exit_fee_usdc)
    spread = _to_decimal(spread_cost_usdc)
    slippage = _to_decimal(slippage_usdc)

    if entry <= 0 or entry > 1:
        raise ValueError("entry_price must be in (0, 1]")
    if exit_ < 0 or exit_ > 1:
        raise ValueError("exit_price must be in [0, 1]")
    if shares < 0:
        raise ValueError("size_shares must be non-negative")

    notional = (entry * shares).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    gross = ((exit_ - entry) * shares).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    net = (gross - entry_fee - exit_fee - spread - slippage).quantize(
        USD_QUANT,
        rounding=ROUND_HALF_UP,
    )
    pnl_pct = Decimal("0")
    if notional > 0:
        pnl_pct = (net / notional).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    return PnLResult(
        gross_pnl_usdc=gross,
        net_pnl_usdc=net,
        notional_usdc=notional,
        pnl_pct=pnl_pct,
    )
