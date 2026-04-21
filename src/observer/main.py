"""Entry point for the Observer module (WebSocket + TradeObserver + PositionTracker)."""

import asyncio
import signal

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.observer.position_tracker import PositionTracker
from src.observer.trade_observer import TradeObserver
from src.registry.falcon_client import FalconClient


async def main() -> None:
    logger.info("Starting Observer")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    falcon = FalconClient(redis_client=redis_client)
    observer = TradeObserver(falcon_client=falcon, redis_client=redis_client)
    tracker = PositionTracker(redis_client=redis_client)

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down Observer")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    try:
        await asyncio.gather(
            observer.start(),
            tracker.start(),
            stop_event.wait(),
        )
    finally:
        await observer.stop()
        await tracker.stop()
        await close_pool()
        await redis_client.aclose()
        logger.info("Observer stopped")


if __name__ == "__main__":
    asyncio.run(main())
