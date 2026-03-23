from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.price_state_cache import CachedTopOfBook, PriceStateCache


@pytest.mark.asyncio
async def test_price_state_cache_returns_latest_book_by_market_and_exact_token() -> None:
    cache = PriceStateCache()
    earlier = datetime.now(timezone.utc) - timedelta(seconds=2)
    later = datetime.now(timezone.utc) - timedelta(seconds=1)

    yes_book = CachedTopOfBook(
        market_id="m1",
        token_id="yes-token",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.47"),
        mid_price=Decimal("0.46"),
        spread=Decimal("0.02"),
        observed_at=earlier,
    )
    no_book = CachedTopOfBook(
        market_id="m1",
        token_id="no-token",
        best_bid=Decimal("0.53"),
        best_ask=Decimal("0.55"),
        mid_price=Decimal("0.54"),
        spread=Decimal("0.02"),
        observed_at=later,
    )

    await cache.set(yes_book)
    await cache.set(no_book)

    assert await cache.get("m1", token_id="yes-token") == yes_book
    assert await cache.get("m1") == no_book
