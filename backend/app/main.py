import asyncio
import os
from contextlib import suppress

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import ORJSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.security import rate_limit_key_from_websocket, rate_limiter, require_ws_token
from app.api.v1.live_routes import router as live_router
from app.api.v1.routes import router as v1_router
from app.api.v1.wallet_routes import router as wallet_router
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.db.session import SessionLocal
from app.live.state import live_hub

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, version="0.2.0-mvp", default_response_class=ORJSONResponse)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix=settings.api_prefix)
app.include_router(live_router, prefix=settings.api_prefix)
app.include_router(wallet_router, prefix=settings.api_prefix)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if settings.enable_rate_limit:
        key = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(key):
            return ORJSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
    return await call_next(request)


async def _tick_loop() -> None:
    while True:
        await live_hub.tick()
        await asyncio.sleep(0.25)


@app.on_event("startup")
async def startup_event() -> None:
    await live_hub.startup()
    if os.getenv("PYTEST_CURRENT_TEST"):
        app.state.ticker_task = None
        return
    app.state.ticker_task = asyncio.create_task(_tick_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task = getattr(app.state, "ticker_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await live_hub.shutdown()


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
    if settings.enable_rate_limit and not rate_limiter.allow(rate_limit_key_from_websocket(websocket)):
        await websocket.close(code=1013)
        return

    try:
        require_ws_token(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await live_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        await live_hub.disconnect(websocket)
