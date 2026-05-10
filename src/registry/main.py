"""Entry point for the Leader Registry module."""

import asyncio
import signal

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting Leader Registry (log_level={level})")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    falcon = FalconClient(redis_client=redis_client)
    registry = LeaderRegistry(falcon_client=falcon)

    loop = asyncio.get_event_loop()

    def handle_signal(*_):
        logger.info("Shutting down Leader Registry")
        asyncio.create_task(registry.stop())
        asyncio.create_task(falcon.close())

    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    try:
        await registry.run()
    finally:
        await close_pool()
        await redis_client.aclose()
        logger.info("Leader Registry stopped")


if __name__ == "__main__":
    asyncio.run(main())
