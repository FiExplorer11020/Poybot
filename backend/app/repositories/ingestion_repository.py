from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RawWebsocketMessage, TopOfBook, Trade


class IngestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_raw_ws_message(self, channel: str, market_id: str | None, payload: dict) -> None:
        self.session.add(RawWebsocketMessage(channel=channel, market_id=market_id, payload=payload))

    async def upsert_trade(self, payload: dict, market_id: str) -> None:
        trade_id = str(payload["id"])
        existing = await self.session.get(Trade, trade_id)
        if existing:
            return
        self.session.add(
            Trade(
                id=trade_id,
                market_id=market_id,
                token_id=str(payload.get("token_id") or payload.get("asset_id") or ""),
                side=payload.get("side", "unknown"),
                price=Decimal(str(payload.get("price", 0))),
                size=Decimal(str(payload.get("size", 0))),
                traded_at=datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
                if payload.get("timestamp")
                else datetime.utcnow(),
            )
        )

    async def insert_top_of_book(self, market_id: str, token_id: str, best_bid: Decimal | None, best_ask: Decimal | None) -> None:
        spread = (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None
        mid = ((best_ask + best_bid) / 2) if (best_ask is not None and best_bid is not None) else None
        self.session.add(
            TopOfBook(
                market_id=market_id,
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                mid_price=mid,
                observed_at=datetime.utcnow(),
            )
        )

    async def latest_mid_price(self, market_id: str) -> Decimal | None:
        row = await self.session.scalar(
            select(TopOfBook.mid_price)
            .where(TopOfBook.market_id == market_id)
            .order_by(TopOfBook.observed_at.desc())
            .limit(1)
        )
        return row
