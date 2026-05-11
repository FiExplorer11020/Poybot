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
import os
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber
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
# Phase 3 Task D: ingestion gap alert channel. The IngestHealthMonitor
# publishes one payload per (source, gap-event) with shape:
#   {"source": "falcon_leaderboard", "duration_s": 2400.0, "severity": "warning"}
# The notifier rate-limits to one alert per INGEST_ALERT_COOLDOWN_S
# (default 300) per (source), so a Falcon outage that spans hours
# doesn't paginate operators while still flagging recurrence.
CHANNEL_INGEST_GAP = "ingest:gap"

ALL_CHANNELS = (
    CHANNEL_PAPER_OPENED,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_LIVE_OPENED,
    CHANNEL_LIVE_CLOSED,
    CHANNEL_KILLSWITCH,
    CHANNEL_ENGINE_CRASH,
    CHANNEL_INGEST_GAP,
)

# Per-source cooldown for ingest-gap Telegram alerts (seconds). The
# global outbound throttle still applies AFTER this gate — this exists
# specifically to prevent spam during a long-running incident where
# the gap-detection loop fires every tick. Override with
# INGEST_ALERT_COOLDOWN_S env var (read at TelegramNotifier construction).
DEFAULT_INGEST_ALERT_COOLDOWN_S = 300


# Type alias for the send function injected by TelegramBot. It takes
# (chat_id, text) and is awaited. Kept as a callable so tests can
# replace it with an AsyncMock without a real Telegram client.
SendFn = Callable[[int, str], "asyncio.Future"]


class TelegramNotifier:
    """Subscribes to Redis pub/sub and pushes alerts to Telegram.

    F-04: prior to Phase 2 Task D this class used ``get_message`` against
    the shared command client, which made it the only existing subscriber
    that survived single message bursts cleanly — but it still lost its
    subscription on a real disconnect. It now delegates to ``Subscriber``
    for parity with every other subscriber site.

    The constructor still accepts ``redis_client`` for backwards-compat
    with bot wiring and tests. When provided, that client drives the
    pub/sub (test fixtures use a fakeredis instance shared with the
    publisher). When ``None``, a dedicated client is opened from
    ``settings.REDIS_URL``.
    """

    def __init__(
        self,
        *,
        redis_client,
        send_fn: SendFn,
        max_per_minute: Optional[int] = None,
        ingest_alert_cooldown_s: Optional[int] = None,
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
        self._stop_event = asyncio.Event()
        self._subscriber: Subscriber | None = None
        # Phase 3 Task D: per-source cooldown for ingest_gap alerts.
        # Falcon outages can persist for hours; we want exactly one
        # alert when the gap opens, NOT one per watchdog tick.
        env_cd = os.environ.get("INGEST_ALERT_COOLDOWN_S")
        if ingest_alert_cooldown_s is not None:
            cooldown = ingest_alert_cooldown_s
        elif env_cd:
            try:
                cooldown = int(env_cd)
            except ValueError:
                cooldown = DEFAULT_INGEST_ALERT_COOLDOWN_S
        else:
            cooldown = DEFAULT_INGEST_ALERT_COOLDOWN_S
        self._ingest_alert_cooldown_s = max(0, int(cooldown))
        # Monotonic timestamps: last allowed ingest_gap alert per source.
        self._ingest_last_alert_at: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._subscriber = Subscriber(
            settings.REDIS_URL, name="telegram.notifier"
        )
        for channel in ALL_CHANNELS:
            self._subscriber.register(channel, self._on_message)
        # Tests pass a fakeredis instance via ``redis_client``; production
        # passes a real ``redis.asyncio.Redis``. Either way, Subscriber
        # uses it directly instead of opening a fresh URL connection,
        # because pub/sub in fakeredis only works between handles
        # spawned from the same FakeRedis() instance.
        await self._subscriber.start(redis_client=self._redis)
        logger.info("TelegramNotifier started")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None
        logger.info("TelegramNotifier stopped")

    # ------------------------------------------------------------------ #
    # Subscriber callback                                                 #
    # ------------------------------------------------------------------ #

    async def _on_message(self, payload, channel: str) -> None:
        # Subscriber decodes JSON for us, but if it failed to parse it
        # would have skipped this handler entirely. Defensive: tolerate
        # non-dict payloads by coercing.
        if not isinstance(payload, dict):
            payload = {}
        await self._handle(channel, payload)

    # ------------------------------------------------------------------ #
    # Routing per-channel                                                 #
    # ------------------------------------------------------------------ #

    async def _handle(self, channel: str, payload: dict) -> None:
        # Phase 3 Task D: gate ingest_gap alerts on a per-source cooldown
        # BEFORE formatting. The global outbound throttle (max/min) is a
        # safety net; this is the primary defence against alert storms
        # during a multi-hour Falcon outage.
        if channel == CHANNEL_INGEST_GAP:
            source = str(payload.get("source", "?")) if isinstance(payload, dict) else "?"
            if not self._ingest_alert_allowed(source):
                logger.debug(
                    f"TelegramNotifier: ingest_gap for {source!r} suppressed "
                    f"(within {self._ingest_alert_cooldown_s}s cooldown)"
                )
                return
        text = self._format(channel, payload)
        if text is None:
            return
        await self._broadcast(text)

    def _ingest_alert_allowed(self, source: str) -> bool:
        """Per-source cooldown gate for the ingest_gap channel."""
        if self._ingest_alert_cooldown_s <= 0:
            return True
        now = time.monotonic()
        last = self._ingest_last_alert_at.get(source, 0.0)
        if last > 0 and (now - last) < self._ingest_alert_cooldown_s:
            return False
        self._ingest_last_alert_at[source] = now
        return True

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
        if channel == CHANNEL_INGEST_GAP:
            return formatters.format_ingest_gap(payload)
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
