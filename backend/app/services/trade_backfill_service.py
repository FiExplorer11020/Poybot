from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clob import ClobClient
from app.repositories.ingestion_repository import IngestionRepository
from app.repositories.market_repository import MarketRepository


class TradeBackfillService:
    def __init__(self, session: AsyncSession, clob_client: ClobClient) -> None:
        self.session = session
        self.repo = IngestionRepository(session)
        self.market_repo = MarketRepository(session)
        self.clob = clob_client

    async def backfill_market(self, market_id: str, pages: int = 5, page_size: int = 100) -> int:
        ingested = 0
        for page in range(pages):
            trades = await self.clob.fetch_trades(market=market_id, limit=page_size, offset=page * page_size)
            if not trades:
                break
            for trade in trades:
                await self.repo.upsert_trade(trade, market_id=market_id)
                ingested += 1
        await self.market_repo.upsert_sync_status(
            "trade_backfill", "success", metadata={"market_id": market_id, "ingested": ingested}
        )
        await self.session.commit()
        return ingested
