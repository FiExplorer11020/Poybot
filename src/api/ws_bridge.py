"""
WebSocket bridge: subscribes to Redis pub/sub and fans out to all connected browser clients.

F-04 / F-26: this used to share a single Redis client with API command
callers and re-iterate silently on disconnect — a Redis hiccup would
black out the dashboard until uvicorn was restarted. The ``Subscriber``
utility owns a dedicated client and reconnects with backoff.
"""

import json
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber

# Agent A (src/api/snapshot_builder.py) will export this as
# SNAPSHOT_PUBSUB_CHANNEL. Until that lands we hold the literal string
# locally so this bridge can ship independently. The constant must stay
# in sync — see docs/autonomous_session_2026_05_17_strategy/04_*.md §2.1.
SNAPSHOT_PUBSUB_CHANNEL = "snapshot:live_summary:updated"

# Defensive debounce window: maintenance writes a fresh snapshot every
# ~30s, but a misconfigured loop (or a backfill burst) could trigger
# multiple publishes in quick succession. The dashboard already debounces
# its refetch, but throttling here keeps the WS fan-out cheap and avoids
# spamming every connected client. 2s matches the doc §6 risk note
# ("rate-limit broadcasts to 1/2s").
_SNAPSHOT_BROADCAST_MIN_INTERVAL_S = 2.0


class WSBridge:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._redis: Any = None
        self._running = False
        # Subscriber is constructed in start() so attach_redis() can run
        # first and pass the (test) client through.
        self._subscriber: Subscriber | None = None
        # Monotonic watermark for debouncing snapshot_updated broadcasts.
        # 0.0 means "never broadcast yet" — first event always passes.
        self._last_snapshot_broadcast_ts: float = 0.0

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
        self._subscriber.register(SNAPSHOT_PUBSUB_CHANNEL, self._on_snapshot_updated)
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

    async def _on_snapshot_updated(self, payload: Any, _channel: str) -> None:
        """Maintenance just wrote a fresh snapshot — notify all WS clients.

        The payload from the publisher (Agent A) is not propagated to
        clients: the dashboard fetches the snapshot via the standard
        ``GET /api/v1/live-summary`` endpoint, which already reads from
        Redis. The WS event is purely a "go refetch" trigger. We
        include ``ts`` (wall-clock seconds) so clients can ignore
        out-of-order events under reconnect.

        Debounce: if two updates land within
        ``_SNAPSHOT_BROADCAST_MIN_INTERVAL_S``, only the first is
        broadcast. Monotonic clock to be immune to wall-clock jumps.
        """
        if not self._running:
            return
        now_monotonic = time.monotonic()
        elapsed = now_monotonic - self._last_snapshot_broadcast_ts
        if (
            self._last_snapshot_broadcast_ts > 0.0
            and elapsed < _SNAPSHOT_BROADCAST_MIN_INTERVAL_S
        ):
            logger.debug(
                "WSBridge: debounced snapshot_updated "
                f"(elapsed={elapsed:.2f}s < {_SNAPSHOT_BROADCAST_MIN_INTERVAL_S:.1f}s)"
            )
            return
        self._last_snapshot_broadcast_ts = now_monotonic
        await self.broadcast({"type": "snapshot_updated", "ts": time.time()})
