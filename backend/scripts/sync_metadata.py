import asyncio

from app.clients.gamma import GammaClient
from app.core.settings import get_settings
from app.db.session import SessionLocal
from app.services.market_sync_service import MarketSyncService


async def main() -> None:
    settings = get_settings()
    gamma = GammaClient(settings.polymarket_gamma_base_url)
    async with SessionLocal() as session:
        service = MarketSyncService(session, gamma)
        result = await service.sync_metadata()
        print(result)
    await gamma.close()


if __name__ == "__main__":
    asyncio.run(main())
