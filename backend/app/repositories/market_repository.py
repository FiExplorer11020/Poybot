from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Event, Market, RawMetadataSnapshot, SyncJobStatus, Token, TopOfBook, Trade
from app.utils.polymarket import parse_json_list_field


class MarketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_event(self, payload: dict) -> Event:
        event = await self.session.get(Event, str(payload["id"]))
        if not event:
            event = Event(id=str(payload["id"]), title=payload.get("title", "Untitled"))
            self.session.add(event)
        event.slug = payload.get("slug")
        event.title = payload.get("title", event.title)
        event.active = payload.get("active", True)
        event.resolved = payload.get("resolved", False)
        return event

    async def upsert_market(self, payload: dict, event_id: str) -> Market:
        market = await self.session.get(Market, str(payload["id"]))
        if not market:
            market = Market(id=str(payload["id"]), event_id=event_id, question=payload.get("question", ""))
            self.session.add(market)
        market.condition_id = payload.get("conditionId")
        market.slug = payload.get("slug")
        market.question = payload.get("question", market.question)
        market.outcomes = [str(outcome) for outcome in parse_json_list_field(payload.get("outcomes"))]
        market.tags = [str(tag) for tag in parse_json_list_field(payload.get("tags"))]
        market.active = payload.get("active", True)
        market.resolved = payload.get("resolved", False)
        market.status = "resolved" if market.resolved else ("active" if market.active else "closed")
        return market

    async def replace_tokens(self, market_id: str, token_ids: list[str], outcomes: list[str]) -> None:
        market = await self.session.get(Market, market_id, options=[selectinload(Market.tokens)])
        existing = {token.id: token for token in (market.tokens if market else [])}
        for idx, token_id in enumerate(token_ids):
            if token_id in existing:
                existing[token_id].outcome = outcomes[idx] if idx < len(outcomes) else "UNKNOWN"
                continue
            self.session.add(
                Token(
                    id=token_id,
                    market_id=market_id,
                    outcome=outcomes[idx] if idx < len(outcomes) else "UNKNOWN",
                )
            )

    async def insert_raw_metadata(self, source: str, payload: dict) -> None:
        self.session.add(RawMetadataSnapshot(source=source, payload=payload))

    async def list_events(self, page: int, page_size: int, active: bool | None) -> tuple[list[Event], int]:
        stmt: Select = select(Event).order_by(Event.created_at.desc())
        count_stmt = select(func.count()).select_from(Event)
        if active is not None:
            stmt = stmt.where(Event.active == active)
            count_stmt = count_stmt.where(Event.active == active)
        events = (await self.session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
        total = int((await self.session.scalar(count_stmt)) or 0)
        return list(events), total

    async def get_event(self, event_id: str) -> Event | None:
        return await self.session.get(Event, event_id)

    async def list_markets(self, page: int, page_size: int, status: str | None, tag: str | None) -> tuple[list[Market], int]:
        stmt: Select = select(Market).options(selectinload(Market.tokens)).order_by(Market.updated_at.desc())
        count_stmt = select(func.count()).select_from(Market)
        if status:
            stmt = stmt.where(Market.status == status)
            count_stmt = count_stmt.where(Market.status == status)
        if tag:
            stmt = stmt.where(Market.tags.contains([tag]))
            count_stmt = count_stmt.where(Market.tags.contains([tag]))
        markets = (await self.session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
        total = int((await self.session.scalar(count_stmt)) or 0)
        return list(markets), total

    async def get_market(self, market_id: str) -> Market | None:
        return await self.session.get(Market, market_id, options=[selectinload(Market.tokens)])

    async def list_market_trades(self, market_id: str, limit: int = 100) -> list[Trade]:
        stmt = select(Trade).where(Trade.market_id == market_id).order_by(Trade.traded_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def list_top_of_book(self, market_id: str, limit: int = 100) -> list[TopOfBook]:
        stmt = select(TopOfBook).where(TopOfBook.market_id == market_id).order_by(TopOfBook.observed_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def upsert_sync_status(self, job_name: str, status: str, error: str | None = None, metadata: dict | None = None) -> None:
        row = await self.session.scalar(select(SyncJobStatus).where(SyncJobStatus.job_name == job_name))
        if not row:
            row = SyncJobStatus(job_name=job_name, status=status)
            self.session.add(row)
        row.status = status
        row.last_error = error
        row.meta_info = metadata
        if status == "success":
            row.last_success_at = datetime.utcnow()

    async def list_sync_status(self) -> list[SyncJobStatus]:
        return list((await self.session.scalars(select(SyncJobStatus).order_by(SyncJobStatus.job_name))).all())
