from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.security import require_api_token
from app.live.state import live_hub

router = APIRouter()


class BotControlIn(BaseModel):
    command: str


class SimulateTradeIn(BaseModel):
    market_title: str


@router.get("/live-summary")
async def live_summary() -> dict:
    return {"data": live_hub.snapshot()}


@router.get("/strategy/spec")
async def strategy_spec() -> dict:
    return {
        "data": {
            "pricing_domain": [0.0, 1.0],
            "binary_constraint": "YES + NO ~= 1",
            "entry_rule": "expected_edge >= dynamic_threshold",
            "risk_caps": {
                "risk_per_trade_pct": live_hub.strategy.cfg.risk_per_trade_pct,
                "max_total_exposure_pct": live_hub.strategy.cfg.max_total_exposure_pct,
                "max_drawdown_stop_pct": live_hub.strategy.cfg.max_drawdown_stop_pct,
                "kelly_fraction": live_hub.strategy.cfg.kelly_fraction,
                "fee_bps": live_hub.strategy.cfg.fee_bps,
            },
        }
    }


@router.post("/bot/control", dependencies=[Depends(require_api_token)])
async def bot_control(payload: BotControlIn) -> dict:
    try:
        return {"data": await live_hub.set_command(payload.command)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/markets/{market_id}/execute", dependencies=[Depends(require_api_token)])
async def simulate_exec(market_id: str, payload: SimulateTradeIn) -> dict:
    try:
        return {"data": await live_hub.execute_trade(market_id, payload.market_title)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trades/bot-history")
async def bot_trade_history(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    return {"data": live_hub.snapshot()["recent_trades"][:limit]}


@router.get("/portfolio/pnl-by-timeframe")
async def pnl_by_timeframe(timeframe: str = Query(default="7d", pattern="^(24h|7d|30d|90d)$")) -> dict:
    return {"data": live_hub.pnl_series(timeframe), "timeframe": timeframe}


class ConfigUpdateIn(BaseModel):
    risk_per_trade_pct: float | None = None
    max_total_exposure_pct: float | None = None
    kelly_fraction: float | None = None
    max_drawdown_stop_pct: float | None = None
    base_entry_threshold: float | None = None
    spread_cap: float | None = None
    allocation_mode: str | None = None
    manual_notional_amount: float | None = None
    max_concurrent_positions: int | None = None
    max_positions_per_tick: int | None = None
    min_observations: int | None = None
    min_signal_strength: float | None = None
    cooldown_seconds: int | None = None
    signal_staleness_seconds: int | None = None
    max_holding_seconds: int | None = None
    display_market_limit: int | None = None


@router.get("/bot/config")
async def bot_config() -> dict:
    return {"data": live_hub.snapshot()["risk_config"]}


@router.patch("/bot/config", dependencies=[Depends(require_api_token)])
async def patch_bot_config(payload: ConfigUpdateIn) -> dict:
    return {"data": await live_hub.update_config(payload.model_dump(exclude_unset=True))}


@router.post("/bot/config", dependencies=[Depends(require_api_token)])
async def post_bot_config(payload: ConfigUpdateIn) -> dict:
    return {"data": await live_hub.update_config(payload.model_dump(exclude_unset=True))}


@router.post("/strategy/config", dependencies=[Depends(require_api_token)])
async def update_config(payload: ConfigUpdateIn) -> dict:
    return {"data": await live_hub.update_config(payload.model_dump(exclude_unset=True))}


@router.post("/trades/{trade_id}/close", dependencies=[Depends(require_api_token)])
async def close_trade(trade_id: str) -> dict:
    try:
        return {"data": await live_hub.close_position(trade_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
