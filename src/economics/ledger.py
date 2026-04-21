from dataclasses import dataclass
from decimal import Decimal

from src.economics.pnl import PnLResult, calculate_long_pnl


@dataclass
class OpenLot:
    market_id: str
    token_id: str
    entry_price: Decimal
    size_shares: Decimal


@dataclass(frozen=True)
class ClosedLot:
    market_id: str
    token_id: str
    entry_price: Decimal
    exit_price: Decimal
    size_shares: Decimal
    gross_pnl_usdc: Decimal
    net_pnl_usdc: Decimal


class PositionLedger:
    def __init__(self) -> None:
        self._lots: dict[tuple[str, str], list[OpenLot]] = {}

    def buy(
        self,
        market_id: str,
        token_id: str,
        price: Decimal,
        size_shares: Decimal,
    ) -> None:
        key = (market_id, token_id)
        self._lots.setdefault(key, []).append(
            OpenLot(
                market_id=market_id,
                token_id=token_id,
                entry_price=price,
                size_shares=size_shares,
            )
        )

    def sell(
        self,
        market_id: str,
        token_id: str,
        price: Decimal,
        size_shares: Decimal,
    ) -> list[ClosedLot]:
        key = (market_id, token_id)
        lots = self._lots.get(key, [])
        remaining = size_shares
        closed: list[ClosedLot] = []

        while remaining > 0 and lots:
            lot = lots[0]
            close_shares = min(remaining, lot.size_shares)
            pnl: PnLResult = calculate_long_pnl(
                entry_price=lot.entry_price,
                exit_price=price,
                size_shares=close_shares,
            )
            closed.append(
                ClosedLot(
                    market_id=market_id,
                    token_id=token_id,
                    entry_price=lot.entry_price,
                    exit_price=price,
                    size_shares=close_shares,
                    gross_pnl_usdc=pnl.gross_pnl_usdc,
                    net_pnl_usdc=pnl.net_pnl_usdc,
                )
            )
            lot.size_shares -= close_shares
            remaining -= close_shares
            if lot.size_shares == 0:
                lots.pop(0)

        if not lots and key in self._lots:
            del self._lots[key]
        return closed

    def resolve_market(self, market_id: str, resolution_price: Decimal) -> list[ClosedLot]:
        closed: list[ClosedLot] = []
        keys = [key for key in self._lots if key[0] == market_id]
        for _, token_id in keys:
            shares = self.open_shares(market_id, token_id)
            closed.extend(self.sell(market_id, token_id, resolution_price, shares))
        return closed

    def open_shares(self, market_id: str, token_id: str) -> Decimal:
        return sum(
            (lot.size_shares for lot in self._lots.get((market_id, token_id), [])),
            Decimal("0"),
        )
