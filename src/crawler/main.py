"""Entry point for the Wallet Universe Crawler daemon.

Run as ``python -m src.crawler`` (the systemd unit
``polymarket-crawler.service`` invokes this). See
docs/ROUND_6_THE_SPINE.md § 3.4 and § 3.5.

Responsibilities:
  * Maintain the ``wallet_universe`` table — every wallet that has ever
    traded on Polymarket, with light-touch metadata.
  * Periodically (default once daily) review every wallet's depth_tier
    and promote/demote based on recent activity.

The one-time historical backfill (``WalletUniverse.backfill_from_chain``)
is invoked separately by an operator script and requires an
:class:`RPCClient`; the steady-state daemon only needs the DB pool.
"""

from __future__ import annotations

import asyncio
import signal

from loguru import logger

from src.config import settings
from src.crawler.depth_tiers import AdaptiveDepth
from src.crawler.universe import WalletUniverse
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging


async def main() -> None:
    """Daemon body. Wires DB pool → universe → adaptive-depth loop,
    installs signal handlers, then awaits cancellation."""
    level = configure_logging()
    logger.info(f"Starting Wallet Universe Crawler (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )

    # No RPC client — the daemon never backfills. Backfill is an
    # operator-triggered one-shot (see migration 020 post-migration note).
    universe = WalletUniverse(rpc_client=None)
    adaptive = AdaptiveDepth(universe=universe)

    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        logger.info("Wallet Universe Crawler: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover — Windows/event loop quirks
            pass

    loop_task = asyncio.create_task(adaptive.run_daemon_loop())
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        await asyncio.wait(
            {loop_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not loop_task.done():
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass
        if not stop_task.done():
            stop_task.cancel()
        await close_pool()
        logger.info("Wallet Universe Crawler stopped")


if __name__ == "__main__":
    asyncio.run(main())
