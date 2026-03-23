import asyncio
import logging
import random
from decimal import Decimal

import orjson
import websockets
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.repositories.ingestion_repository import IngestionRepository
from app.services.price_state_cache import CachedTopOfBook, PriceStateCache

logger = logging.getLogger(__name__)


class PolymarketWsIngestor:
    def __init__(
        self,
        ws_url: str,
        session_factory: async_sessionmaker,
        token_ids: list[str],
        price_cache: PriceStateCache | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.session_factory = session_factory
        self.token_ids = token_ids
        self.price_cache = price_cache or PriceStateCache()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=15, ping_timeout=10) as ws:
                    attempt = 0
                    await ws.send(
                        orjson.dumps(
                            {
                                "type": "subscribe",
                                "channel": "market",
                                "assets_ids": self.token_ids,
                            }
                        )
                    )
                    await self._consume(ws)
            except Exception as exc:
                logger.warning("ws reconnecting due to error: %s", exc)
                backoff = min(30.0, 1.0 * (2**attempt))
                jitter = random.uniform(0, min(1.0, backoff * 0.2))
                await asyncio.sleep(backoff + jitter)
                attempt = min(attempt + 1, 6)

    async def _consume(self, ws: websockets.WebSocketClientProtocol) -> None:
        buffer: list[dict] = []
        async for message in ws:
            buffer.append(orjson.loads(message))
            if len(buffer) >= 25:
                await self._flush(buffer)
                buffer.clear()
        if buffer:
            await self._flush(buffer)

    async def _flush(self, payloads: list[dict]) -> None:
        books_to_cache: list[CachedTopOfBook] = []
        async with self.session_factory() as session:
            repo = IngestionRepository(session)
            for payload in payloads:
                market_id = payload.get("market")
                await repo.add_raw_ws_message(payload.get("channel", "market"), market_id, payload)

                if payload.get("event_type") == "book":
                    bid = (
                        Decimal(str(payload.get("best_bid", 0)))
                        if payload.get("best_bid") is not None
                        else None
                    )
                    ask = (
                        Decimal(str(payload.get("best_ask", 0)))
                        if payload.get("best_ask") is not None
                        else None
                    )
                    top_of_book = await repo.insert_top_of_book(
                        market_id=str(payload.get("market", "")),
                        token_id=str(payload.get("asset_id", "")),
                        best_bid=bid,
                        best_ask=ask,
                    )
                    books_to_cache.append(CachedTopOfBook.from_top_of_book(top_of_book))
            await session.commit()

        for book in books_to_cache:
            await self.price_cache.set(book)
