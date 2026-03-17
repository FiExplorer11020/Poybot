from fastapi import APIRouter
from pydantic import BaseModel

from app.live.state import live_hub

router = APIRouter()


class BotControlIn(BaseModel):
    command: str


@router.get("/live-summary")
async def live_summary() -> dict:
    return {"data": live_hub.snapshot()}


@router.post("/bot/control")
async def bot_control(payload: BotControlIn) -> dict:
    return {"data": await live_hub.set_command(payload.command)}


@router.post("/markets/{market_id}/simulate-exec")
async def simulate_exec(market_id: str) -> dict:
    return {"data": await live_hub.simulate_execution(market_id)}
