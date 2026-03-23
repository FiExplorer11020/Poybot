from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ingestion.ws_ingestor import PolymarketWsIngestor
from app.models import Event, Market, MarketStatus, TopOfBook
from app.services.price_state_cache import PriceStateCache


@pytest.mark.anyio
async def test_flush_persists_top_of_book_and_updates_price_cache(session_factory) -> None:
    async with session_factory() as session:
        session.add(Event(id="e1", title="Event", active=True, resolved=False))
        session.add(
            Market(
                id="m1",
                event_id="e1",
                question="Question",
                outcomes=["Yes", "No"],
                tags=["test"],
                status=MarketStatus.active,
                active=True,
                resolved=False,
            )
        )
        await session.commit()

    cache = PriceStateCache()
    ingestor = PolymarketWsIngestor(
        ws_url="wss://example.invalid",
        session_factory=session_factory,
        token_ids=["tok-yes"],
        price_cache=cache,
    )

    await ingestor._flush(
        [
            {
                "channel": "market",
                "market": "m1",
                "asset_id": "tok-yes",
                "event_type": "book",
                "best_bid": "0.45",
                "best_ask": "0.47",
            }
        ]
    )

    async with session_factory() as session:
        book = await session.scalar(select(TopOfBook).where(TopOfBook.market_id == "m1"))

    assert book is not None
    assert book.token_id == "tok-yes"
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.47")

    cached = await cache.get("m1", token_id="tok-yes")
    assert cached is not None
    assert cached.best_bid == Decimal("0.45")
    assert cached.best_ask == Decimal("0.47")
