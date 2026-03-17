from arq import cron
from arq.connections import RedisSettings

from app.clients.clob import ClobClient
from app.clients.gamma import GammaClient
from app.core.settings import get_settings
from app.db.session import SessionLocal
from app.repositories.market_repository import MarketRepository
from app.services.market_sync_service import MarketSyncService
from app.services.trade_backfill_service import TradeBackfillService

settings = get_settings()


async def sync_metadata_job(ctx: dict) -> dict:
    async with SessionLocal() as session:
        gamma = GammaClient(settings.polymarket_gamma_base_url)
        try:
            service = MarketSyncService(session, gamma)
            return await service.sync_metadata()
        finally:
            await gamma.close()


async def refresh_recent_trades_job(ctx: dict) -> dict:
    async with SessionLocal() as session:
        repo = MarketRepository(session)
        markets, _ = await repo.list_markets(page=1, page_size=20, status="active", tag=None)
        clob = ClobClient(settings.polymarket_clob_rest_base_url)
        try:
            backfill = TradeBackfillService(session, clob)
            ingested = 0
            for market in markets:
                ingested += await backfill.backfill_market(market.id, pages=1, page_size=50)
            return {"ingested": ingested}
        finally:
            await clob.close()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [sync_metadata_job, refresh_recent_trades_job]
    cron_jobs = [
        cron(sync_metadata_job, hour={0, 6, 12, 18}),
        cron(refresh_recent_trades_job, minute={0, 15, 30, 45}),
    ]
