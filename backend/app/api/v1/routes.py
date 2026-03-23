from datetime import datetime
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.market_analytics import MarketAnalyticsService
from app.api.deps import get_db_session
from app.core.settings import get_settings
from app.repositories.market_repository import MarketRepository
from app.schemas.market import (
    EventOut,
    MarketOut,
    MarketSummary,
    SyncStatusOut,
    TopOfBookOut,
    TradeOut,
)
from app.services.adaptive_strategy import RiskConfig
from app.services.backtest_engine import BacktestConfig, BacktestEngine
from app.services.backtest_exporter import (
    BacktestExportService,
    BacktestResultInvalidError,
    BacktestResultNotFoundError,
)

router = APIRouter()
settings = get_settings()


class RiskConfigIn(BaseModel):
    risk_per_trade_pct: float = 0.01
    max_total_exposure_pct: float = 0.25
    kelly_fraction: float = 0.25
    max_drawdown_stop_pct: float = 0.10
    fee_bps: float = 8.0
    base_entry_threshold: float = 0.005
    spread_cap: float = 0.06
    allocation_mode: str = "automatic"
    manual_notional_amount: float = 100.0
    min_observations: int = 4
    min_signal_strength: float = 1.0
    max_concurrent_positions: int = 4
    max_positions_per_tick: int = 1
    cooldown_seconds: int = 10
    signal_staleness_seconds: int = 3
    max_holding_seconds: int = 180
    display_market_limit: int = 80

    def to_domain(self) -> RiskConfig:
        return RiskConfig(**self.model_dump())


class BacktestConfigIn(BaseModel):
    start_date: datetime
    end_date: datetime
    initial_equity: float = 1000.0
    strategy: Literal["latency_arb", "spread_arb", "adaptive"] = "adaptive"
    risk_cfg: RiskConfigIn = Field(default_factory=RiskConfigIn)
    slippage_model: Literal["fixed", "spread_pct"] = "spread_pct"
    fee_bps: float = 8.0
    market_ids: list[str] | None = None

    def to_domain(self) -> BacktestConfig:
        return BacktestConfig(
            start_date=self.start_date,
            end_date=self.end_date,
            initial_equity=self.initial_equity,
            strategy=self.strategy,
            risk_cfg=self.risk_cfg.to_domain(),
            slippage_model=self.slippage_model,
            fee_bps=self.fee_bps,
            market_ids=self.market_ids,
        )


@router.get("/events")
async def list_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=settings.default_page_size, le=settings.max_page_size),
    active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = MarketRepository(session)
    items, total = await repo.list_events(page, page_size, active)
    return {
        "data": [
            EventOut.model_validate(event, from_attributes=True).model_dump() for event in items
        ],
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
    page: int = Query(default=1, ge=1),
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
async def market_book(
    market_id: str, session: AsyncSession = Depends(get_db_session)
) -> list[TopOfBookOut]:
    repo = MarketRepository(session)
    return [
        TopOfBookOut.model_validate(x, from_attributes=True)
        for x in await repo.list_top_of_book(market_id)
    ]


@router.get("/markets/{market_id}/trades", response_model=list[TradeOut])
async def market_trades(
    market_id: str, session: AsyncSession = Depends(get_db_session)
) -> list[TradeOut]:
    repo = MarketRepository(session)
    return [
        TradeOut.model_validate(x, from_attributes=True)
        for x in await repo.list_market_trades(market_id)
    ]


@router.get("/markets/{market_id}/price-history")
async def market_price_history(
    market_id: str, session: AsyncSession = Depends(get_db_session)
) -> dict:
    analytics = MarketAnalyticsService(session)
    return {"data": await analytics.price_history(market_id)}


@router.get("/markets/{market_id}/summary", response_model=MarketSummary)
async def market_summary(
    market_id: str, session: AsyncSession = Depends(get_db_session)
) -> MarketSummary:
    analytics = MarketAnalyticsService(session)
    return MarketSummary.model_validate(await analytics.market_summary(market_id))


@router.get("/tags")
async def list_tags(session: AsyncSession = Depends(get_db_session)) -> dict:
    repo = MarketRepository(session)
    markets, _ = await repo.list_markets(
        page=1, page_size=settings.max_page_size, status=None, tag=None
    )
    tags = sorted({tag for market in markets for tag in market.tags})
    return {"data": tags}


@router.get("/system/sync-status", response_model=list[SyncStatusOut])
async def sync_status(session: AsyncSession = Depends(get_db_session)) -> list[SyncStatusOut]:
    repo = MarketRepository(session)
    rows = await repo.list_sync_status()
    return [SyncStatusOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/backtest")
async def run_backtest(
    payload: BacktestConfigIn,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if payload.end_date <= payload.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    engine = BacktestEngine(session)
    result = await engine.run(payload.to_domain())
    response_payload = jsonable_encoder(result)
    response_payload["parameters"] = payload.model_dump(mode="json")

    exporter = BacktestExportService(settings.backtest_results_dir)
    backtest_id = exporter.save_backtest_result(response_payload)
    response_payload["backtest_id"] = backtest_id
    return response_payload


@router.get("/backtest/{backtest_id}/export")
async def export_backtest(backtest_id: str) -> StreamingResponse:
    exporter = BacktestExportService(settings.backtest_results_dir)

    try:
        filename, workbook_bytes = exporter.export_backtest(backtest_id)
    except BacktestResultNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BacktestResultInvalidError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return StreamingResponse(
        BytesIO(workbook_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
