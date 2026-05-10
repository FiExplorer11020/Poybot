"""
Polymarket CLOB WebSocket client — market channel subscription.
Handles auto-reconnect, ping/pong keepalive, dynamic market subscription updates.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config import settings


class PolymarketWSClient:
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    SUBSCRIBE_CHUNK_SIZE = 100

    def __init__(
        self,
        on_message: Callable[[dict], Awaitable[None]],
        markets: set[str] | None = None,
    ):
        self._on_message = on_message
        self._markets: set[str] = markets or set()
        self._ws = None
        self._running = False
        self._stop_event = asyncio.Event()

        # Metrics
        self.reconnect_count: int = 0
        self.messages_received: int = 0
        self.last_message_at: float | None = None

    async def start(self) -> None:
        """Start the WebSocket connection loop."""
        self._running = True
        self._stop_event.clear()
        await self._connect_loop()

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        self._stop_event.set()
        if self._ws:
            await self._ws.close()

    def update_markets(self, markets: set[str]) -> None:
        """Update the set of subscribed market token IDs."""
        self._markets = markets

    async def _connect_loop(self) -> None:
        backoff = 1
        max_backoff = 60
        while self._running and not self._stop_event.is_set():
            try:
                await self._connect_and_run()
                backoff = 1  # Reset on clean disconnect
            except (ConnectionClosed, WebSocketException, OSError) as e:
                if not self._running:
                    break
                self.reconnect_count += 1
                logger.warning(
                    f"WebSocket disconnected: {e}. "
                    f"Reconnecting in {backoff}s (attempt #{self.reconnect_count})"
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    break  # stop_event was set
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"Unexpected WebSocket error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _connect_and_run(self) -> None:
        logger.info(f"Connecting to {self.WS_URL}")
        async with websockets.connect(
            self.WS_URL,
            ping_interval=settings.WEBSOCKET_PING_INTERVAL_S,
            ping_timeout=settings.WEBSOCKET_PONG_TIMEOUT_S,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connected")
            if self._markets:
                await self._subscribe(ws)
            try:
                async for raw in ws:
                    self.messages_received += 1
                    self.last_message_at = time.time()
                    # Loud INFO log on the first message and one every 100
                    # so we can prove in `docker compose logs observer` that
                    # the WS is actually receiving (not just connected). The
                    # health dashboard's "0 msgs/min" can mislead: this log
                    # is the source of truth.
                    if self.messages_received == 1 or self.messages_received % 100 == 0:
                        logger.info(
                            f"WS messages_received={self.messages_received} "
                            f"(first 80 chars: {str(raw)[:80]})"
                        )
                    try:
                        data = json.loads(raw)
                        if isinstance(data, list):
                            for item in data:
                                await self._on_message(item)
                        else:
                            await self._on_message(data)
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON message: {raw[:100]}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
            finally:
                self._ws = None

    async def _ping_loop(self, ws) -> None:
        """Explicit ping loop used by tests and as a fallback keepalive helper."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(max(0, int(settings.WEBSOCKET_PING_INTERVAL_S)))
                pong_waiter = await ws.ping()
                await asyncio.wait_for(
                    pong_waiter,
                    timeout=max(0, int(settings.WEBSOCKET_PONG_TIMEOUT_S)),
                )
            except asyncio.TimeoutError:
                await ws.close()
                break
            except (ConnectionClosed, WebSocketException, OSError):
                break
            if not self._running:
                break

    async def _subscribe(self, ws) -> None:
        market_ids = sorted(self._markets)
        for start in range(0, len(market_ids), self.SUBSCRIBE_CHUNK_SIZE):
            chunk = market_ids[start : start + self.SUBSCRIBE_CHUNK_SIZE]
            msg = {
                "assets_ids": chunk,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(msg))
            if start + self.SUBSCRIBE_CHUNK_SIZE < len(market_ids):
                await asyncio.sleep(0.05)
        logger.info(f"Subscribed to {len(self._markets)} markets")
