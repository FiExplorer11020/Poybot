from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.gamma import GammaClient
from app.repositories.market_repository import MarketRepository


class MarketSyncService:
    def __init__(self, session: AsyncSession, gamma_client: GammaClient) -> None:
        self.repo = MarketRepository(session)
        self.gamma_client = gamma_client
        self.session = session

    async def sync_metadata(self, page_size: int = 100, pages: int = 3) -> dict:
        markets_seen = 0
        events_seen = 0
        for page in range(pages):
            offset = page * page_size
            events = await self.gamma_client.fetch_events(limit=page_size, offset=offset)
            markets = await self.gamma_client.fetch_markets(limit=page_size, offset=offset)

            for event in events:
                await self.repo.upsert_event(event)
                events_seen += 1
            for market in markets:
                event_id = str(market.get("eventId") or market.get("event_id") or "")
                if not event_id:
                    continue
                await self.repo.upsert_market(market, event_id=event_id)
                token_ids = [str(token) for token in market.get("clobTokenIds", [])]
                outcomes = market.get("outcomes") or []
                await self.repo.replace_tokens(str(market["id"]), token_ids=token_ids, outcomes=outcomes)
                markets_seen += 1
            await self.repo.insert_raw_metadata("gamma", {"events": events, "markets": markets})

        await self.repo.upsert_sync_status(
            "metadata_sync", "success", metadata={"events": events_seen, "markets": markets_seen}
        )
        await self.session.commit()
        return {"events_synced": events_seen, "markets_synced": markets_seen}
