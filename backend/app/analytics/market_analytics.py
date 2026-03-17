from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TopOfBook, Trade


class MarketAnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def market_summary(self, market_id: str) -> dict:
        latest_mid = await self.session.scalar(
            select(TopOfBook.mid_price)
            .where(TopOfBook.market_id == market_id)
            .order_by(TopOfBook.observed_at.desc())
            .limit(1)
        )
        avg_spread = await self.session.scalar(
            select(func.avg(TopOfBook.spread)).where(TopOfBook.market_id == market_id)
        )
        volume_24h = await self.session.scalar(
            select(func.coalesce(func.sum(Trade.size), 0)).where(Trade.market_id == market_id)
        )
        implied_probability = float(latest_mid) if latest_mid is not None else None
        consistency_flag = implied_probability is None or (0 <= implied_probability <= 1)
        return {
            "market_id": market_id,
            "latest_mid_price": latest_mid,
            "avg_spread": avg_spread,
            "volume_24h": Decimal(str(volume_24h)),
            "implied_probability": implied_probability,
            "consistency_flag": consistency_flag,
        }

    async def price_history(self, market_id: str, limit: int = 200) -> list[dict]:
        rows = (
            await self.session.execute(
                select(TopOfBook.observed_at, TopOfBook.mid_price)
                .where(TopOfBook.market_id == market_id)
                .order_by(TopOfBook.observed_at.desc())
                .limit(limit)
            )
        ).all()
        return [{"timestamp": row[0], "mid_price": row[1]} for row in rows]
