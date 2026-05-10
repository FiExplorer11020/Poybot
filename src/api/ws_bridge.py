"""
WebSocket bridge: subscribes to Redis pub/sub and fans out to all connected browser clients.

F-04 / F-26: this used to share a single Redis client with API command
callers and re-iterate silently on disconnect — a Redis hiccup would
black out the dashboard until uvicorn was restarted. The ``Subscriber``
utility owns a dedicated client and reconnects with backoff.
"""

import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber


class WSBridge:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._redis: Any = None
        self._running = False
        # Subscriber is constructed in start() so attach_redis() can run
        # first and pass the (test) client through.
        self._subscriber: Subscriber | None = None

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def start(self) -> None:
        """Start consuming Redis channels in the background."""
        if self._redis is None:
            logger.warning("WSBridge: no Redis client attached, skipping")
            return
        self._running = True
        self._subscriber = Subscriber(settings.REDIS_URL, name="api.ws_bridge")
        self._subscriber.register("trades:observed", self._on_trade)
        self._subscriber.register("decisions", self._on_decision)
        self._subscriber.register("positions:paper_closed", self._on_pnl_update)
        # In dev the API and engine share a Redis instance, so passing
        # `self._redis` keeps the pub/sub graph compatible with the
        # existing test wiring (and with fakeredis-based integration
        # tests). Subscriber will NOT close it on stop().
        await self._subscriber.start(redis_client=self._redis)
        logger.info(
            "WSBridge subscribed to Redis channels via Subscriber: "
            f"{list(self._subscriber.channels)}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None

    async def handle(self, ws: WebSocket) -> None:
        """Add a browser WebSocket, keep it alive until disconnect."""
        await ws.accept()
        self._connections.add(ws)
        logger.debug(f"WS client connected ({len(self._connections)} total)")
        try:
            while True:
                # Keep-alive: echo any ping from client
                data = await ws.receive_text()
                if data == "ping":
                    await ws.send_text("pong")
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            self._connections.discard(ws)
            logger.debug(f"WS client disconnected ({len(self._connections)} remaining)")

    @property
    def has_connections(self) -> bool:
        return bool(self._connections)

    async def broadcast(self, payload: dict) -> None:
        if not self._connections:
            return
        text = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    async def _on_trade(self, data: Any, _channel: str) -> None:
        if not self._running:
            return
        await self.broadcast({"type": "trade", "data": data})

    async def _on_decision(self, data: Any, _channel: str) -> None:
        if not self._running:
            return
        await self.broadcast({"type": "decision", "data": data})

    async def _on_pnl_update(self, data: Any, _channel: str) -> None:
        if not self._running:
            return
        await self.broadcast({"type": "pnl_update", "data": data})
