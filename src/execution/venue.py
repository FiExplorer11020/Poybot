from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from src.economics.models import OrderSide, StrategyTrack


@dataclass(frozen=True)
class DryRunOrder:
    strategy_track: StrategyTrack
    market_id: str
    token_id: str
    side: OrderSide
    size_shares: Decimal
    limit_price: Decimal
    client_order_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DryRunReceipt:
    accepted: bool
    venue: str
    dry_run: bool
    would_submit: bool
    order: DryRunOrder
    reason: str


class ExecutionVenue(Protocol):
    async def submit_order(self, order: DryRunOrder) -> DryRunReceipt: ...


class ClobVenueDryRun:
    venue_name = "clob_dry_run"

    async def submit_order(self, order: DryRunOrder) -> DryRunReceipt:
        signal_audit = order.metadata.get("signal_audit")
        if not isinstance(signal_audit, dict) or signal_audit.get("accepted") is not True:
            return DryRunReceipt(
                accepted=False,
                venue=self.venue_name,
                dry_run=True,
                would_submit=False,
                order=order,
                reason="missing_accepted_signal_audit",
            )

        return DryRunReceipt(
            accepted=True,
            venue=self.venue_name,
            dry_run=True,
            would_submit=False,
            order=order,
            reason="dry_run_only",
        )


class PaperVenue(ClobVenueDryRun):
    venue_name = "paper"
