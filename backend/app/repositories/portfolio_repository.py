from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotTrade, PortfolioSnapshot


class PortfolioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_bot_trade(self, trade: BotTrade) -> None:
        self.session.add(trade)

    async def list_bot_trades(self, limit: int = 50) -> list[BotTrade]:
        stmt = select(BotTrade).order_by(BotTrade.executed_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def add_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self.session.add(snapshot)

    async def latest_snapshot(self) -> PortfolioSnapshot | None:
        stmt = select(PortfolioSnapshot).order_by(PortfolioSnapshot.observed_at.desc()).limit(1)
        return await self.session.scalar(stmt)

    async def pnl_by_timeframe(self, timeframe: str) -> list[PortfolioSnapshot]:
        windows = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "90d": timedelta(days=90),
        }
        window = windows.get(timeframe, timedelta(days=7))
        from_dt = datetime.now(timezone.utc) - window
        stmt: Select = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.observed_at >= from_dt)
            .order_by(PortfolioSnapshot.observed_at.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def aggregate_trade_usage(self) -> Decimal:
        stmt = select(func.coalesce(func.sum(BotTrade.notional), 0)).where(BotTrade.status == "open")
        value = await self.session.scalar(stmt)
        return Decimal(str(value or 0))
