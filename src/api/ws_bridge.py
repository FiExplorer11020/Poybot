"""
WebSocket bridge: subscribes to Redis pub/sub and fans out to all connected browser clients.
"""

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger


class WSBridge:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._redis: Any = None
        self._running = False

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def start(self) -> None:
        """Start consuming Redis channels in the background."""
        if self._redis is None:
            logger.warning("WSBridge: no Redis client attached, skipping")
            return
        self._running = True
        asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        self._running = False

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

    async def _consume_loop(self) -> None:
        channels = ["trades:observed", "decisions", "positions:paper_closed"]
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        logger.info(f"WSBridge subscribed to Redis channels: {channels}")
        try:
            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                channel = message["channel"]
                try:
                    data = json.loads(message["data"])
                except Exception:
                    continue

                if channel == "trades:observed":
                    await self.broadcast({"type": "trade", "data": data})
                elif channel == "decisions":
                    await self.broadcast({"type": "decision", "data": data})
                elif channel == "positions:paper_closed":
                    await self.broadcast({"type": "pnl_update", "data": data})
        except Exception as e:
            logger.error(f"WSBridge consume loop error: {e}")
        finally:
            await pubsub.unsubscribe(*channels)
