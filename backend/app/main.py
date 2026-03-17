import asyncio
from contextlib import suppress

from fastapi import FastAPI, WebSocket
from fastapi.responses import ORJSONResponse
from sqlalchemy import text

from app.api.v1.live_routes import router as live_router
from app.api.v1.routes import router as v1_router
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.db.session import SessionLocal
from app.live.state import live_hub

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, version="0.2.0-mvp", default_response_class=ORJSONResponse)
app.include_router(v1_router, prefix=settings.api_prefix)
app.include_router(live_router, prefix=settings.api_prefix)


async def _tick_loop() -> None:
    while True:
        await live_hub.tick()
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event() -> None:
    app.state.ticker_task = asyncio.create_task(_tick_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task = app.state.ticker_task
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    await live_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        await live_hub.disconnect(websocket)
