from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotTrade, PortfolioSnapshot
from app.repositories.portfolio_repository import PortfolioRepository


class PortfolioService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = PortfolioRepository(session)

    async def record_simulated_trade(
        self,
        market_id: str,
        market_title: str,
        outcome: str,
        side: str,
        price: Decimal,
        size: Decimal,
        pnl_abs: Decimal,
    ) -> BotTrade:
        notional = price * size
        pnl_pct = Decimal("0") if notional == 0 else (pnl_abs / notional) * Decimal("100")
        trade = BotTrade(
            id=f"bt-{uuid4().hex[:16]}",
            market_id=market_id,
            market_title=market_title,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            notional=notional,
            pnl_abs=pnl_abs,
            pnl_pct=pnl_pct,
            status="open",
            executed_at=datetime.now(timezone.utc),
        )
        await self.repo.add_bot_trade(trade)
        await self.session.commit()
        return trade

    async def record_snapshot(self, total_equity: Decimal, capital_in_trade: Decimal, pnl_abs: Decimal) -> PortfolioSnapshot:
        base = total_equity - pnl_abs
        pnl_pct = Decimal("0") if base == 0 else (pnl_abs / base) * Decimal("100")
        snapshot = PortfolioSnapshot(
            total_equity=total_equity,
            capital_in_trade=capital_in_trade,
            pnl_abs=pnl_abs,
            pnl_pct=pnl_pct,
            observed_at=datetime.now(timezone.utc),
        )
        await self.repo.add_snapshot(snapshot)
        await self.session.commit()
        return snapshot
