from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.models import TopOfBook


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CachedTopOfBook:
    market_id: str
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid_price: Decimal | None
    spread: Decimal | None
    observed_at: datetime

    @classmethod
    def from_top_of_book(cls, book: TopOfBook) -> "CachedTopOfBook":
        return cls(
            market_id=book.market_id,
            token_id=book.token_id,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            mid_price=book.mid_price,
            spread=book.spread,
            observed_at=_as_utc(book.observed_at),
        )


class PriceStateCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._books_by_market: dict[str, dict[str, CachedTopOfBook]] = {}

    async def set(self, book: CachedTopOfBook) -> None:
        async with self._lock:
            market_books = self._books_by_market.setdefault(book.market_id, {})
            market_books[book.token_id] = book

    async def get(self, market_id: str, token_id: str | None = None) -> CachedTopOfBook | None:
        async with self._lock:
            market_books = self._books_by_market.get(market_id)
            if not market_books:
                return None
            if token_id is not None:
                return market_books.get(token_id)
            return max(market_books.values(), key=lambda item: item.observed_at, default=None)

    async def clear(self) -> None:
        async with self._lock:
            self._books_by_market.clear()
