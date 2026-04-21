"""Entry point for the Intelligence Engine (Confidence + PaperTrader + RiskManager)."""

import asyncio
import signal

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.engine.confidence_engine import ConfidenceEngine
from src.engine.paper_trader import PaperTrader
from src.engine.risk_manager import RiskManager
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel


async def main() -> None:
    logger.info("Starting Intelligence Engine")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    error_model = ErrorModel()
    profiler = BehaviorProfiler(redis_client=redis_client, error_model=error_model)
    confidence = ConfidenceEngine(
        redis_client=redis_client,
        behavior_profiler=profiler,
        error_model=error_model,
    )
    risk_manager = RiskManager()
    paper_trader = PaperTrader(
        redis_client=redis_client,
        confidence_engine=confidence,
        risk_manager=risk_manager,  # FIX 4
    )

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down Intelligence Engine")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    try:
        await asyncio.gather(
            profiler.start(),
            confidence.start(),
            paper_trader.start(),
            stop_event.wait(),
        )
    finally:
        await profiler.stop()
        await confidence.stop()
        await paper_trader.stop()
        await close_pool()
        await redis_client.aclose()
        logger.info("Intelligence Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
