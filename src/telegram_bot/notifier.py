"""
Redis → Telegram notifier (S3.9 + S3.11 expansion).

Subscribes to a wide set of operational channels and pushes formatted
alerts to every authorized chat_id. Stays out of the hot path: every
send is best-effort, throttled, deduplicated, and isolated from the
producer.

Channel coverage by tier (filtered against settings.TELEGRAM_VERBOSITY):

  CRITICAL (always sent, even in "quiet"):
    engine:crash
    control:killswitch_changed
    paper:audit:suspicious_close
    engine:risk:breaker_tripped
    engine:portfolio:drawdown_threshold

  ALERT (sent in "normal" and above):
    positions:live_opened, positions:live_closed
    ingest:gap
    profiler:drift:detected
    profiler:phase:upgraded
    engine:watchdog:restarted
    engine:position:market_resolved
    engine:backfill:lag_alert
    paper:audit:divergence

  INFO (sent in "verbose" and above):
    positions:paper_opened, positions:paper_closed
    graph:follower:confirmed
    registry:leader:added, registry:leader:excluded
    runtime_config:changed

Throttle: sliding window capped at TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE
(default 60). CRITICAL-tier messages bypass the throttle so we never
miss a crash because of paper-trade chatter.

Dedup: rolling 60s window of message-hash → suppress identical text. Kills
the "publisher reconnects and re-emits the last 3 events" path that
otherwise produces visible spam on flaky links.

Backoff: exponential on Telegram API failures (rate-limit-by-Telegram,
network, 5xx). Doubles up to TELEGRAM_BACKOFF_MAX_S, resets on success.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber
from src.telegram_bot import formatters
from src.telegram_bot.auth import authorized_chat_ids


# --------------------------------------------------------------------------- #
# Channel constants                                                            #
# --------------------------------------------------------------------------- #
# We re-declare here so the notifier doesn't have to import every producer
# module — the strings are the contract, not the import path.

# --- Original (S3.9) channels ---
CHANNEL_PAPER_OPENED = "positions:paper_opened"
CHANNEL_PAPER_CLOSED = "positions:paper_closed"
CHANNEL_LIVE_OPENED = "positions:live_opened"
CHANNEL_LIVE_CLOSED = "positions:live_closed"
CHANNEL_KILLSWITCH = "control:killswitch_changed"
CHANNEL_ENGINE_CRASH = "engine:crash"
CHANNEL_INGEST_GAP = "ingest:gap"

# --- S3.11 expansion: full operator visibility ---
# Audit flag emitted by PaperTrader when a close's PnL ratio exceeds
# MAX_TRADE_RETURN_RATIO (likely stale-cache exit). Always alert.
CHANNEL_SUSPICIOUS_CLOSE = "paper:audit:suspicious_close"
# RiskManager refused a trade because a circuit breaker tripped
# (drawdown / consecutive_losses / recent_market_losses / open_count /
# market_exposure). Always alert — silent breakers are a footgun.
CHANNEL_RISK_BREAKER = "engine:risk:breaker_tripped"
# Portfolio drawdown crossed a threshold (3 / 5 / 10%). One alert per
# threshold per session; resets when capital climbs back above peak.
CHANNEL_DRAWDOWN_THRESHOLD = "engine:portfolio:drawdown_threshold"
# error_model CUSUM detected drift and downgraded the phase.
CHANNEL_DRIFT_DETECTED = "profiler:drift:detected"
# error_model upgraded a leader's phase (1→2 or 2→3) — major learning event.
CHANNEL_PHASE_UPGRADED = "profiler:phase:upgraded"
# Watchdog restarted a coroutine (heartbeat freeze or task crash).
# Separate from engine:crash because the engine ISN'T dying; one component is.
CHANNEL_WATCHDOG_RESTART = "engine:watchdog:restarted"
# graph_engine confirmed a new (leader → follower) edge: follow_probability
# crossed the threshold + same_direction_rate met the minimum.
CHANNEL_FOLLOWER_CONFIRMED = "graph:follower:confirmed"
# leader_registry inserted a new wallet to the watchlist.
CHANNEL_LEADER_ADDED = "registry:leader:added"
# leader_registry excluded a wallet (bot detection / falcon_no_data).
CHANNEL_LEADER_EXCLUDED = "registry:leader:excluded"
# runtime_config edited via dashboard or /set command — surface the diff
# so the operator sees who tweaked what.
CHANNEL_RUNTIME_CONFIG_CHANGED = "runtime_config:changed"
# A market we hold a position in resolved. Includes outcome + our PnL.
CHANNEL_MARKET_RESOLVED_POSITION = "engine:position:market_resolved"
# maintenance_loop.backfill_resolved_outcomes lag exceeds threshold:
# Gamma is rate-limiting us or the endpoint is degraded and the
# (active=FALSE AND resolved_outcome IS NULL) count is still climbing.
CHANNEL_BACKFILL_LAG_ALERT = "engine:backfill:lag_alert"
# Pillar 2 (audit 2026-05-17) — nightly Gamma reconciliation emitted a
# new ``paper_close_divergences`` row. Carries flag distribution +
# top-3-worst so the operator can spot the +39,784 USDC class of
# phantom PnL without scrolling the dashboard.
CHANNEL_PAPER_AUDIT_DIVERGENCE = "paper:audit:divergence"


ALL_CHANNELS: tuple[str, ...] = (
    CHANNEL_PAPER_OPENED,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_LIVE_OPENED,
    CHANNEL_LIVE_CLOSED,
    CHANNEL_KILLSWITCH,
    CHANNEL_ENGINE_CRASH,
    CHANNEL_INGEST_GAP,
    CHANNEL_SUSPICIOUS_CLOSE,
    CHANNEL_RISK_BREAKER,
    CHANNEL_DRAWDOWN_THRESHOLD,
    CHANNEL_DRIFT_DETECTED,
    CHANNEL_PHASE_UPGRADED,
    CHANNEL_WATCHDOG_RESTART,
    CHANNEL_FOLLOWER_CONFIRMED,
    CHANNEL_LEADER_ADDED,
    CHANNEL_LEADER_EXCLUDED,
    CHANNEL_RUNTIME_CONFIG_CHANGED,
    CHANNEL_MARKET_RESOLVED_POSITION,
    CHANNEL_BACKFILL_LAG_ALERT,
    CHANNEL_PAPER_AUDIT_DIVERGENCE,
)


# Verbosity tiers. The map is the single source of truth — formatters,
# /verbosity command, and tests all read from here. Lower number = more critical.
TIER_CRITICAL = 0
TIER_ALERT = 1
TIER_INFO = 2

CHANNEL_TIER: dict[str, int] = {
    CHANNEL_ENGINE_CRASH: TIER_CRITICAL,
    CHANNEL_KILLSWITCH: TIER_CRITICAL,
    CHANNEL_SUSPICIOUS_CLOSE: TIER_CRITICAL,
    CHANNEL_RISK_BREAKER: TIER_CRITICAL,
    CHANNEL_DRAWDOWN_THRESHOLD: TIER_CRITICAL,

    CHANNEL_LIVE_OPENED: TIER_ALERT,
    CHANNEL_LIVE_CLOSED: TIER_ALERT,
    CHANNEL_INGEST_GAP: TIER_ALERT,
    CHANNEL_DRIFT_DETECTED: TIER_ALERT,
    CHANNEL_PHASE_UPGRADED: TIER_ALERT,
    CHANNEL_WATCHDOG_RESTART: TIER_ALERT,
    CHANNEL_MARKET_RESOLVED_POSITION: TIER_ALERT,
    CHANNEL_BACKFILL_LAG_ALERT: TIER_ALERT,
    CHANNEL_PAPER_AUDIT_DIVERGENCE: TIER_ALERT,

    CHANNEL_PAPER_OPENED: TIER_INFO,
    CHANNEL_PAPER_CLOSED: TIER_INFO,
    CHANNEL_FOLLOWER_CONFIRMED: TIER_INFO,
    CHANNEL_LEADER_ADDED: TIER_INFO,
    CHANNEL_LEADER_EXCLUDED: TIER_INFO,
    CHANNEL_RUNTIME_CONFIG_CHANGED: TIER_INFO,
}

# S3.12 — per-event counters for the daily digest. The notifier
# transparently bumps a 1h + 24h Redis counter whenever it sees a
# message on one of these channels, so the digest builder reads them
# without each producer needing to also INCR.
COUNTED_CHANNELS: dict[str, str] = {}
# (populated after VERBOSITY_MAX_TIER below so we can reference the
# channel constants without forward-declaration noise; see __init__-time
# population at end of this section.)

# Channels for which we ONLY bump the counter and do NOT send an
# instant Telegram message. The operator sees the count in the daily
# digest instead. Useful for high-frequency "model maturity" events
# (e.g. graph:follower:confirmed during cold-start fanout) that would
# otherwise flood the chat.
SILENT_COUNT_CHANNELS: set[str] = set()


VERBOSITY_MAX_TIER: dict[str, int] = {
    "quiet": TIER_CRITICAL,
    "normal": TIER_ALERT,
    "verbose": TIER_INFO,
    "debug": 99,  # send everything, ignore tier filtering
}


# Populate the S3.12 counter map now that the channel constants are in
# scope. The values are the per-counter "name" used in the Redis key
# prefix `telegram:counter:<name>:<window>`. They MUST match what
# src/telegram_bot/digest.py reads.
COUNTED_CHANNELS.update({
    CHANNEL_RISK_BREAKER: "breaker_hits",
    CHANNEL_DRIFT_DETECTED: "drift_events",
    CHANNEL_PHASE_UPGRADED: "phase_transitions",
    CHANNEL_LEADER_ADDED: "new_leaders",
    CHANNEL_FOLLOWER_CONFIRMED: "follower_confirmed",
})
SILENT_COUNT_CHANNELS.update({CHANNEL_FOLLOWER_CONFIRMED})


# Per-source cooldown for ingest-gap alerts (seconds). Override with
# INGEST_ALERT_COOLDOWN_S env var. Stays as-is from S3.9.
DEFAULT_INGEST_ALERT_COOLDOWN_S = 300


SendFn = Callable[[int, str], "asyncio.Future"]


# --------------------------------------------------------------------------- #
# TelegramNotifier                                                             #
# --------------------------------------------------------------------------- #


class TelegramNotifier:
    """Subscribes to Redis pub/sub and pushes alerts to Telegram.

    F-04: ``Subscriber`` (from src.control.redis_pubsub) drives the pub/sub
    instead of raw ``get_message`` so reconnects don't drop our subscription.

    S3.11 expansion: tier-based filtering against TELEGRAM_VERBOSITY,
    sliding-window dedup on the message hash, and exponential backoff on
    Telegram API errors (separately from the outbound throttle).
    """

    def __init__(
        self,
        *,
        redis_client,
        send_fn: SendFn,
        max_per_minute: Optional[int] = None,
        ingest_alert_cooldown_s: Optional[int] = None,
        dedup_window_s: Optional[int] = None,
        verbosity: Optional[str] = None,
    ) -> None:
        self._redis = redis_client
        self._send = send_fn
        self._max_per_minute = (
            max_per_minute
            if max_per_minute is not None
            else settings.TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE
        )
        self._sent_timestamps: deque[float] = deque(maxlen=max(1, self._max_per_minute))
        self._running = False
        self._stop_event = asyncio.Event()
        self._subscriber: Subscriber | None = None

        # Ingest-gap per-source cooldown (S3.9, kept unchanged).
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
        self._ingest_last_alert_at: dict[str, float] = {}

        # Dedup window (S3.11). dedup_window_s=0 disables. We store
        # (hash, expires_at) pairs in a deque and pop expired entries on
        # each check. A dict-set hybrid would be O(1) lookup but the
        # expected window is tiny (60s × ~60 msgs = 60 entries), so a
        # linear scan is fine and avoids a separate expiry timer.
        self._dedup_window_s = (
            dedup_window_s
            if dedup_window_s is not None
            else settings.TELEGRAM_DEDUP_WINDOW_S
        )
        self._recent_hashes: deque[tuple[str, float]] = deque()

        # Verbosity tier filter.
        verb = (verbosity or settings.TELEGRAM_VERBOSITY or "verbose").strip().lower()
        self._max_tier = VERBOSITY_MAX_TIER.get(verb, TIER_INFO)
        self._verbosity = verb

        # Backoff state on Telegram API failures. Resets to 0 after a
        # successful send.
        self._backoff_until: float = 0.0
        self._backoff_current_s: float = 0.0

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
        await self._subscriber.start(redis_client=self._redis)
        logger.info(
            f"TelegramNotifier started (verbosity={self._verbosity}, "
            f"max/min={self._max_per_minute}, dedup_window={self._dedup_window_s}s, "
            f"channels={len(ALL_CHANNELS)})"
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None
        logger.info("TelegramNotifier stopped")

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def set_verbosity(self, verbosity: str) -> str:
        """Hot-swap the verbosity tier (used by /verbosity command)."""
        verb = (verbosity or "verbose").strip().lower()
        if verb not in VERBOSITY_MAX_TIER:
            raise ValueError(
                f"unknown verbosity {verb!r}; choose one of {list(VERBOSITY_MAX_TIER)}"
            )
        self._verbosity = verb
        self._max_tier = VERBOSITY_MAX_TIER[verb]
        logger.info(f"TelegramNotifier verbosity now {verb} (max_tier={self._max_tier})")
        return verb

    def current_verbosity(self) -> str:
        return self._verbosity

    async def push(self, text: str, *, tier: int = TIER_ALERT) -> bool:
        """Send an ad-hoc message bypassing channel routing.

        Used by the digest scheduler and the /alert subsystem when we
        need to push something that doesn't come from Redis pub/sub.
        Returns True if the message was actually broadcast.
        """
        if tier > self._max_tier:
            return False
        return await self._broadcast(text, tier=tier)

    # ------------------------------------------------------------------ #
    # Subscriber callback                                                 #
    # ------------------------------------------------------------------ #

    async def _on_message(self, payload, channel: str) -> None:
        if not isinstance(payload, dict):
            payload = {}
        await self._handle(channel, payload)

    # ------------------------------------------------------------------ #
    # Routing per-channel                                                 #
    # ------------------------------------------------------------------ #

    async def _handle(self, channel: str, payload: dict) -> None:
        tier = CHANNEL_TIER.get(channel, TIER_INFO)

        # S3.12: bump 1h+24h counters BEFORE any filtering so the daily
        # digest reflects every event even if the operator lowered the
        # verbosity or dedup would have suppressed the message.
        await self._bump_counter(channel)

        # SILENT counters: count and stop. The operator only sees these
        # in the daily digest, not as instant alerts. Added specifically
        # to keep graph:follower:confirmed off the chat during cold-start
        # fanout (dozens of edges crossing thresholds simultaneously).
        if channel in SILENT_COUNT_CHANNELS:
            return

        if tier > self._max_tier:
            return

        # Per-source cooldown for ingest_gap (kept from S3.9).
        if channel == CHANNEL_INGEST_GAP:
            source = str(payload.get("source", "?"))
            if not self._ingest_alert_allowed(source):
                logger.debug(
                    f"TelegramNotifier: ingest_gap for {source!r} suppressed "
                    f"(within {self._ingest_alert_cooldown_s}s cooldown)"
                )
                return

        text = self._format(channel, payload)
        if text is None:
            return
        await self._broadcast(text, tier=tier)

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
        if channel == CHANNEL_SUSPICIOUS_CLOSE:
            return formatters.format_suspicious_close(payload)
        if channel == CHANNEL_RISK_BREAKER:
            return formatters.format_risk_breaker(payload)
        if channel == CHANNEL_DRAWDOWN_THRESHOLD:
            return formatters.format_drawdown_threshold(payload)
        if channel == CHANNEL_DRIFT_DETECTED:
            return formatters.format_drift_detected(payload)
        if channel == CHANNEL_PHASE_UPGRADED:
            return formatters.format_phase_upgraded(payload)
        if channel == CHANNEL_WATCHDOG_RESTART:
            return formatters.format_watchdog_restart(payload)
        if channel == CHANNEL_FOLLOWER_CONFIRMED:
            return formatters.format_follower_confirmed(payload)
        if channel == CHANNEL_LEADER_ADDED:
            return formatters.format_leader_added(payload)
        if channel == CHANNEL_LEADER_EXCLUDED:
            return formatters.format_leader_excluded(payload)
        if channel == CHANNEL_RUNTIME_CONFIG_CHANGED:
            return formatters.format_runtime_config_changed(payload)
        if channel == CHANNEL_MARKET_RESOLVED_POSITION:
            return formatters.format_market_resolved_position(payload)
        if channel == CHANNEL_BACKFILL_LAG_ALERT:
            return formatters.format_backfill_lag_alert(payload)
        if channel == CHANNEL_PAPER_AUDIT_DIVERGENCE:
            return formatters.format_paper_audit_divergence(payload)
        logger.warning(f"TelegramNotifier: unknown channel {channel!r}")
        return None

    # ------------------------------------------------------------------ #
    # Broadcast (dedup + throttle + backoff)                              #
    # ------------------------------------------------------------------ #

    async def _broadcast(self, text: str, *, tier: int = TIER_INFO) -> bool:
        chat_ids = authorized_chat_ids()
        if not chat_ids:
            logger.debug("TelegramNotifier: no chat_ids configured, skipping")
            return False

        # Dedup: hash of the text, sliding window. CRITICAL bypasses dedup
        # — an operator who sees "killswitch ON" twice in a row is much
        # less harmed than one who misses it because we suppressed it.
        if tier > TIER_CRITICAL and self._dedup_window_s > 0:
            if self._is_duplicate(text):
                logger.debug("TelegramNotifier: dropping duplicate within dedup window")
                return False

        # Throttle: sliding window. CRITICAL always sends.
        if tier > TIER_CRITICAL and not self._allow_send():
            logger.warning(
                "TelegramNotifier: rate limit hit "
                f"(>{self._max_per_minute}/min), dropping {text[:80]!r}"
            )
            return False

        # Backoff: if we're in a backoff window from a prior Telegram
        # failure, skip everything except CRITICAL. CRITICAL still
        # attempts because the backoff might be stale; if Telegram
        # really is down, the attempt logs and we move on.
        now = time.monotonic()
        if tier > TIER_CRITICAL and now < self._backoff_until:
            logger.debug(
                f"TelegramNotifier: in backoff for {self._backoff_until - now:.1f}s, "
                "dropping non-critical"
            )
            return False

        sent_any = False
        for chat_id in chat_ids:
            try:
                await self._send(chat_id, text)
                sent_any = True
                # Success — reset backoff.
                self._backoff_current_s = 0.0
                self._backoff_until = 0.0
            except Exception as e:
                logger.warning(
                    f"TelegramNotifier: send to {chat_id} failed: {e}"
                )
                self._bump_backoff()
        return sent_any

    async def _bump_counter(self, channel: str) -> None:
        """Best-effort INCR for both the 1h and 24h windows.

        TTL is set with NX so the first INCR creates the window and
        subsequent INCRs do not extend it — a tumbling window close to
        the digest's notion of "events in the last N hours". Without NX
        we'd reset the TTL on every event and a 25h-old event would
        still show in the 24h counter.
        """
        name = COUNTED_CHANNELS.get(channel)
        if name is None or self._redis is None:
            return
        for window, ttl in (("1h", 3600), ("24h", 86400)):
            key = f"telegram:counter:{name}:{window}"
            try:
                await self._redis.incr(key)
                # nx=True → only set TTL if the key has none. Requires
                # Redis 7.0+ (prod is 7.2-alpine per CLAUDE.md § 8).
                await self._redis.expire(key, ttl, nx=True)
            except Exception as e:
                logger.debug(f"counter bump {channel} failed: {e}")

    def _allow_send(self) -> bool:
        """Sliding-window rate limit. Returns True if we may send now."""
        if self._max_per_minute <= 0:
            return False
        now = time.monotonic()
        cutoff = now - 60.0
        while self._sent_timestamps and self._sent_timestamps[0] < cutoff:
            self._sent_timestamps.popleft()
        if len(self._sent_timestamps) >= self._max_per_minute:
            return False
        self._sent_timestamps.append(now)
        return True

    def _is_duplicate(self, text: str) -> bool:
        """True if `text` was sent within the last dedup_window_s."""
        if self._dedup_window_s <= 0:
            return False
        now = time.monotonic()
        # Evict expired hashes from the left.
        while self._recent_hashes and self._recent_hashes[0][1] < now:
            self._recent_hashes.popleft()
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        for existing_h, _ in self._recent_hashes:
            if existing_h == h:
                return True
        self._recent_hashes.append((h, now + self._dedup_window_s))
        return False

    def _bump_backoff(self) -> None:
        """Failed send → enter exponential backoff window."""
        initial = float(settings.TELEGRAM_BACKOFF_INITIAL_S)
        cap = float(settings.TELEGRAM_BACKOFF_MAX_S)
        if self._backoff_current_s <= 0.0:
            self._backoff_current_s = initial
        else:
            self._backoff_current_s = min(cap, self._backoff_current_s * 2.0)
        self._backoff_until = time.monotonic() + self._backoff_current_s
        logger.info(
            f"TelegramNotifier: backoff {self._backoff_current_s:.1f}s "
            f"(until +{self._backoff_current_s:.1f}s)"
        )
