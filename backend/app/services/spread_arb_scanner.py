from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ONE = Decimal("1.0")
MIN_NET_PROFIT = Decimal("0.002")
BPS_DENOMINATOR = Decimal("10000")
PERCENT_MULTIPLIER = Decimal("100")
TWO = Decimal("2")


def _to_decimal(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class TopOfBookData:
    market_id: str
    ask: float | Decimal
    liquidity: float | Decimal

    @property
    def ask_decimal(self) -> Decimal:
        return _to_decimal(self.ask)

    @property
    def liquidity_decimal(self) -> Decimal:
        return _to_decimal(self.liquidity)


@dataclass(frozen=True, slots=True)
class MarketPair:
    market_id: str
    yes_book: TopOfBookData
    no_book: TopOfBookData


@dataclass(frozen=True, slots=True)
class SpreadArbOpportunity:
    market_id: str
    yes_ask: float
    no_ask: float
    combined_cost: float
    gross_profit: float
    net_profit: float
    net_profit_pct: float
    max_size_usdc: float


class SpreadArbScanner:
    def scan(
        self,
        yes_book: TopOfBookData,
        no_book: TopOfBookData,
        fee_bps: float = 8.0,
    ) -> SpreadArbOpportunity | None:
        if yes_book.market_id != no_book.market_id:
            raise ValueError("YES and NO books must belong to the same market")

        yes_ask = yes_book.ask_decimal
        no_ask = no_book.ask_decimal
        yes_liquidity = yes_book.liquidity_decimal
        no_liquidity = no_book.liquidity_decimal

        if yes_ask <= 0 or no_ask <= 0:
            return None

        combined_cost = yes_ask + no_ask
        if combined_cost >= ONE:
            return None

        gross_profit = ONE - combined_cost
        fees = combined_cost * (_to_decimal(fee_bps) / BPS_DENOMINATOR) * TWO
        net_profit = gross_profit - fees
        if net_profit <= MIN_NET_PROFIT:
            return None

        max_size_usdc = min(yes_liquidity, no_liquidity)
        if max_size_usdc <= 0:
            return None

        net_profit_pct = (net_profit / combined_cost) * PERCENT_MULTIPLIER

        return SpreadArbOpportunity(
            market_id=yes_book.market_id,
            yes_ask=float(yes_ask),
            no_ask=float(no_ask),
            combined_cost=float(combined_cost),
            gross_profit=float(gross_profit),
            net_profit=float(net_profit),
            net_profit_pct=float(net_profit_pct),
            max_size_usdc=float(max_size_usdc),
        )

    def scan_universe(
        self,
        universe: list[MarketPair],
        fee_bps: float = 8.0,
    ) -> list[SpreadArbOpportunity]:
        opportunities = [
            opportunity
            for pair in universe
            if (opportunity := self.scan(pair.yes_book, pair.no_book, fee_bps=fee_bps)) is not None
        ]
        return sorted(
            opportunities,
            key=lambda opportunity: opportunity.net_profit_pct,
            reverse=True,
        )


def scan_universe(
    universe: list[MarketPair],
    fee_bps: float = 8.0,
) -> list[SpreadArbOpportunity]:
    return SpreadArbScanner().scan_universe(universe, fee_bps=fee_bps)
