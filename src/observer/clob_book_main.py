"""Entry point for the CLOB Book L3 observer daemon (Round 11 § 3.1).

Run via the ``polymarket-book-l3.service`` systemd unit:

    [Service]
    ExecStart=/opt/polymarket-bot/.venv/bin/python -m src.observer.clob_book_main

This is a **separate daemon** from the existing :mod:`src.observer.main`
(trade-level WS + REST polling) by design — Round 6 § 3.5 daemon-split
principle. L3 book events are high-volume (~5,000/s peak); colocating
them with the trade observer would have the GIL block trade ingestion
during bursts.
"""

from __future__ import annotations

import asyncio
import signal

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging
from src.observer.clob_book_observer import CLOBBookObserver
from src.observer.websocket_client import PolymarketWSClient


def _make_ws_factory(markets: set[str]):
    """Build the WS factory the observer hands its on_message callback to.

    The factory signature is ``(on_message) -> ws_client`` where
    ``ws_client`` exposes ``start()`` and ``stop()`` coroutines. We
    return the real :class:`PolymarketWSClient`; tests inject a stub.
    """

    def factory(on_message):
        return PolymarketWSClient(
            on_message=on_message,
            markets=markets,
        )

    return factory


async def _bootstrap_market_set() -> set[str]:
    """Pick the top-N markets to subscribe to. Hands off the same query
    the trade observer uses but is bounded by ``CLOB_BOOK_TOP_MARKETS``
    so the L3 firehose stays under its memory budget.
    """
    from src.database.connection import get_db

    n = max(1, int(settings.CLOB_BOOK_TOP_MARKETS))
    tokens: set[str] = set()
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT token_yes, token_no
                FROM markets
                WHERE active = TRUE
                  AND (NULLIF(token_yes, '') IS NOT NULL
                       OR NULLIF(token_no, '') IS NOT NULL)
                ORDER BY volume_24h DESC NULLS LAST
                LIMIT $1
                """,
                n,
            )
        for row in rows:
            if row.get("token_yes"):
                tokens.add(str(row["token_yes"]))
            if row.get("token_no"):
                tokens.add(str(row["token_no"]))
    except Exception as exc:
        logger.warning(f"clob_book bootstrap: market query failed: {exc}")
    logger.info(
        f"polymarket-book-l3 bootstrap: {len(tokens)} tokens "
        f"(target {n} markets × 2 tokens)"
    )
    return tokens


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting CLOB Book L3 observer (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    markets = await _bootstrap_market_set()
    observer = CLOBBookObserver(
        redis_client=redis_client,
        ws_factory=_make_ws_factory(markets),
        markets=markets,
    )

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down CLOB Book L3 observer")
        stop_event.set()

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
        loop.add_signal_handler(signal.SIGINT, handle_signal)
    except (NotImplementedError, RuntimeError):
        # Signal handlers can't be installed in some test envs; skip.
        pass

    try:
        await observer.start()
        await stop_event.wait()
    finally:
        await observer.stop()
        await close_pool()
        try:
            await redis_client.aclose()
        except Exception:
            pass
        logger.info("CLOB Book L3 observer stopped")


if __name__ == "__main__":
    asyncio.run(main())
