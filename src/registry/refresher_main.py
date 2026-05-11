"""Entry point for the event-driven Falcon refresher daemon.

Round 6 / The Spine § 3.5 splits ingestion across separate processes.
The ``polymarket-falcon-refresher.service`` systemd unit launches this
module — it subscribes to the ``trades:observed`` channel and triggers
incremental ``LeaderRegistry.refresh_wallet`` calls when a new wallet
is observed or a high-notional trade hits an existing leader.

This is a thin wrapper around the existing components:

* :class:`src.registry.falcon_client.FalconClient` — the smart Falcon
  HTTP client (rate-limit + coalescing + 48 h cache).
* :class:`src.registry.leader_registry.LeaderRegistry` — owns the
  ``refresh_wallet`` coroutine. We only need its event-driven path;
  the timer-driven ``run()`` loop runs in ``polymarket-engine`` so we
  do NOT start it here (calling :meth:`LeaderRegistry.run` from this
  daemon would compete with the engine's own scheduler).
* :class:`src.registry.event_bridge.LeaderEventBridge` — the Redis
  pub/sub adaptor that listens on ``trades:observed`` and fires
  ``refresh_wallet`` for qualifying trades.

Lifecycle is the standard pattern shared with ``src.crawler.main`` and
``src.onchain.main``: initialise the DB pool, start the bridge,
``await`` a SIGTERM/SIGINT signal, then graceful-stop.
"""
from __future__ import annotations

import asyncio
import signal

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging
from src.registry.event_bridge import LeaderEventBridge
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry


async def main() -> None:
    """Daemon body. Wires DB pool → Falcon → registry → bridge."""
    level = configure_logging()
    logger.info(f"Starting Falcon Refresher daemon (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    falcon = FalconClient(redis_client=redis_client)
    registry = LeaderRegistry(falcon_client=falcon)
    bridge = LeaderEventBridge(registry=registry, redis_url=settings.REDIS_URL)

    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        logger.info("Falcon Refresher: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover — Windows/event loop quirks
            pass

    await bridge.start()
    logger.info(
        "Falcon Refresher: bridge online, awaiting trades:observed events"
    )
    try:
        await stop_event.wait()
    finally:
        try:
            await bridge.stop()
        except Exception:  # pragma: no cover — defensive shutdown
            logger.exception("Falcon Refresher: bridge.stop() raised")
        try:
            await falcon.close()
        except Exception:  # pragma: no cover
            logger.exception("Falcon Refresher: falcon.close() raised")
        try:
            await redis_client.aclose()
        except Exception:  # pragma: no cover
            logger.exception("Falcon Refresher: redis_client.aclose() raised")
        await close_pool()
        logger.info("Falcon Refresher: stopped")


if __name__ == "__main__":
    asyncio.run(main())
