"""
Redis → Telegram notifier (S3.9).

Subscribes to a fixed set of operational channels and pushes a
formatted alert to every authorized chat_id. Stays out of the hot
path: every send is best-effort, throttled, and isolated from the
producer.

Channels (consumer side):
  positions:paper_opened   — PaperTrader on successful open
  positions:paper_closed   — PaperTrader on close (any reason)
  positions:live_opened    — LiveTrader on filled open (NOT shadow)
  positions:live_closed    — LiveTrader on close
  control:killswitch_changed — KillswitchService on every mutation
  engine:crash             — wired in main() exception handler

Throttle: leaky-bucket capped at TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE.
Telegram bots are limited to ~30 msg/sec, but we cap far below to be
polite during incident storms (a flurry of stop-losses must not get
us rate-limited just when we need the alerts most).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger

from src.config import settings
from src.telegram_bot import formatters
from src.telegram_bot.auth import authorized_chat_ids


# Channel constants — duplicated here so the notifier doesn't have to
# import every producer module.
CHANNEL_PAPER_OPENED = "positions:paper_opened"
CHANNEL_PAPER_CLOSED = "positions:paper_closed"
CHANNEL_LIVE_OPENED = "positions:live_opened"
CHANNEL_LIVE_CLOSED = "positions:live_closed"
CHANNEL_KILLSWITCH = "control:killswitch_changed"
CHANNEL_ENGINE_CRASH = "engine:crash"

ALL_CHANNELS = (
    CHANNEL_PAPER_OPENED,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_LIVE_OPENED,
    CHANNEL_LIVE_CLOSED,
    CHANNEL_KILLSWITCH,
    CHANNEL_ENGINE_CRASH,
)


# Type alias for the send function injected by TelegramBot. It takes
# (chat_id, text) and is awaited. Kept as a callable so tests can
# replace it with an AsyncMock without a real Telegram client.
SendFn = Callable[[int, str], "asyncio.Future"]


class TelegramNotifier:
    """Subscribes to Redis pub/sub and pushes alerts to Telegram."""

    def __init__(
        self,
        *,
        redis_client,
        send_fn: SendFn,
        max_per_minute: Optional[int] = None,
    ) -> None:
        self._redis = redis_client
        self._send = send_fn
        self._max_per_minute = (
            max_per_minute
            if max_per_minute is not None
            else settings.TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE
        )
        self._sent_timestamps: deque[float] = deque(maxlen=self._max_per_minute or 1)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("TelegramNotifier started")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("TelegramNotifier stopped")

    # ------------------------------------------------------------------ #
    # Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(*ALL_CHANNELS)
        except Exception as e:
            logger.error(f"TelegramNotifier: failed to subscribe: {e}")
            return
        try:
            while self._running:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg is None:
                    continue
                if msg.get("type") != "message":
                    continue
                channel = msg.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                raw = msg.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception as e:
                    logger.warning(
                        f"TelegramNotifier: bad JSON on {channel}: {e}"
                    )
                    continue
                await self._handle(channel, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TelegramNotifier: subscribe loop crashed")
        finally:
            try:
                await pubsub.unsubscribe(*ALL_CHANNELS)
                await pubsub.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Routing per-channel                                                 #
    # ------------------------------------------------------------------ #

    async def _handle(self, channel: str, payload: dict) -> None:
        text = self._format(channel, payload)
        if text is None:
            return
        await self._broadcast(text)

    @staticmethod
    def _format(channel: str, payload: dict) -> Optional[str]:
        if channel == CHANNEL_PAPER_OPENED:
            return formatters.format_position_opened(venue="paper", payload=payload)
        if channel == CHANNEL_PAPER_CLOSED:
            return formatters.format_position_closed(venue="paper", payload=payload)
        if channel == CHANNEL_LIVE_OPENED:
            return formatters.format_position_opened(venue="live", payload=payload)
        if channel == CHANNEL_LIVE_CLOSED:
            return formatters.format_position_closed(venue="live", payload=payload)
        if channel == CHANNEL_KILLSWITCH:
            return formatters.format_killswitch_changed(payload)
        if channel == CHANNEL_ENGINE_CRASH:
            return formatters.format_engine_crash(payload)
        logger.warning(f"TelegramNotifier: unknown channel {channel!r}")
        return None

    # ------------------------------------------------------------------ #
    # Broadcast                                                           #
    # ------------------------------------------------------------------ #

    async def _broadcast(self, text: str) -> None:
        chat_ids = authorized_chat_ids()
        if not chat_ids:
            logger.debug("TelegramNotifier: no chat_ids configured, skipping")
            return
        if not self._allow_send():
            logger.warning(
                "TelegramNotifier: rate limit hit "
                f"(>{self._max_per_minute}/min), dropping notification"
            )
            return
        # Send to every chat. If one fails we keep going — one bad
        # chat_id must not starve the others.
        for chat_id in chat_ids:
            try:
                await self._send(chat_id, text)
            except Exception as e:
                logger.warning(
                    f"TelegramNotifier: send to {chat_id} failed: {e}"
                )

    def _allow_send(self) -> bool:
        """Sliding-window rate limit. Returns True if we may send now."""
        if self._max_per_minute <= 0:
            return False
        now = time.monotonic()
        cutoff = now - 60.0
        # Drop expired timestamps from the left.
        while self._sent_timestamps and self._sent_timestamps[0] < cutoff:
            self._sent_timestamps.popleft()
        if len(self._sent_timestamps) >= self._max_per_minute:
            return False
        self._sent_timestamps.append(now)
        return True
