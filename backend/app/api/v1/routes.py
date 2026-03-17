from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.market_analytics import MarketAnalyticsService
from app.api.deps import get_db_session
from app.core.settings import get_settings
from app.repositories.market_repository import MarketRepository
from app.schemas.market import EventOut, MarketOut, MarketSummary, SyncStatusOut, TopOfBookOut, TradeOut

router = APIRouter()
settings = get_settings()


@router.get("/events")
async def list_events(
    page: int = 1,
    page_size: int = Query(default=settings.default_page_size, le=settings.max_page_size),
    active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = MarketRepository(session)
    items, total = await repo.list_events(page, page_size, active)
    return {
        "data": [EventOut.model_validate(event, from_attributes=True).model_dump() for event in items],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: str, session: AsyncSession = Depends(get_db_session)) -> EventOut:
    repo = MarketRepository(session)
    event = await repo.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return EventOut.model_validate(event, from_attributes=True)


@router.get("/markets")
async def list_markets(
    page: int = 1,
    page_size: int = Query(default=settings.default_page_size, le=settings.max_page_size),
    status: str | None = None,
    tag: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = MarketRepository(session)
    items, total = await repo.list_markets(page, page_size, status, tag)
    return {
        "data": [MarketOut.model_validate(m, from_attributes=True).model_dump() for m in items],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.get("/markets/{market_id}", response_model=MarketOut)
async def get_market(market_id: str, session: AsyncSession = Depends(get_db_session)) -> MarketOut:
    repo = MarketRepository(session)
    market = await repo.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="market not found")
    return MarketOut.model_validate(market, from_attributes=True)


@router.get("/markets/{market_id}/book", response_model=list[TopOfBookOut])
async def market_book(market_id: str, session: AsyncSession = Depends(get_db_session)) -> list[TopOfBookOut]:
    repo = MarketRepository(session)
    return [TopOfBookOut.model_validate(x, from_attributes=True) for x in await repo.list_top_of_book(market_id)]


@router.get("/markets/{market_id}/trades", response_model=list[TradeOut])
async def market_trades(market_id: str, session: AsyncSession = Depends(get_db_session)) -> list[TradeOut]:
    repo = MarketRepository(session)
    return [TradeOut.model_validate(x, from_attributes=True) for x in await repo.list_market_trades(market_id)]


@router.get("/markets/{market_id}/price-history")
async def market_price_history(market_id: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    analytics = MarketAnalyticsService(session)
    return {"data": await analytics.price_history(market_id)}


@router.get("/markets/{market_id}/summary", response_model=MarketSummary)
async def market_summary(market_id: str, session: AsyncSession = Depends(get_db_session)) -> MarketSummary:
    analytics = MarketAnalyticsService(session)
    return MarketSummary.model_validate(await analytics.market_summary(market_id))


@router.get("/tags")
async def list_tags(session: AsyncSession = Depends(get_db_session)) -> dict:
    repo = MarketRepository(session)
    markets, _ = await repo.list_markets(page=1, page_size=1000, status=None, tag=None)
    tags = sorted({tag for market in markets for tag in market.tags})
    return {"data": tags}


@router.get("/system/sync-status", response_model=list[SyncStatusOut])
async def sync_status(session: AsyncSession = Depends(get_db_session)) -> list[SyncStatusOut]:
    repo = MarketRepository(session)
    rows = await repo.list_sync_status()
    return [SyncStatusOut.model_validate(row, from_attributes=True) for row in rows]
