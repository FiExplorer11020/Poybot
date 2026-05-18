"""
WebSocket bridge: subscribes to Redis pub/sub and fans out to all connected browser clients.

F-04 / F-26: this used to share a single Redis client with API command
callers and re-iterate silently on disconnect — a Redis hiccup would
black out the dashboard until uvicorn was restarted. The ``Subscriber``
utility owns a dedicated client and reconnects with backoff.

A8 refactor (2026-05-18): the WS payload sent to the browser is now the
real typed delta (Pydantic-serialised) rather than a "go refetch"
trigger. Front consumers can dispatch on ``type`` directly instead of
polling ``/api/v1/live-summary`` every WS event. The legacy
``snapshot_updated`` broadcast is preserved in parallel so the in-flight
front-end (which still relies on it for refetch) keeps working until
A9 cuts over.

WS payload envelope::

    {
      "type":    "<event_type>",        # see CHANNEL_TO_WS_TYPE
      "channel": "<redis_channel>",     # raw Redis channel name
      "ts":      <float>,               # wall-clock seconds at fan-out
      "data":    {...}                  # event.model_dump(mode="json")
    }

Backwards-compat: ``{"type": "snapshot_updated", "ts": <float>}`` is
still emitted by the snapshot debounce handler.

Rate limiting: each typed channel has a 100 msg/s broadcast cap (token
bucket, refilled monotonically). Excess events are dropped and a
periodic warning summarises how many were shed. ``trades:observed``
peaks at ~50 msg/s in prod so this is a safety cap, not a routine
throttle. The legacy ``snapshot_updated`` channel retains its own
2-second debounce (see ``_on_snapshot_updated``); it is NOT subject to
the token bucket.
"""

import json
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, ValidationError

from src.config import settings
from src.control.redis_pubsub import Subscriber
from src.events.schemas import (
    CHANNEL_DECISIONS,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_RECONCILIATION,
    CHANNEL_SCHEMA,
    CHANNEL_SYSTEM_STATUS,
    CHANNEL_TRADES_OBSERVED,
)

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

# --------------------------------------------------------------------------- #
# Redis channel → WS event type — front consumers switch on this value        #
# --------------------------------------------------------------------------- #
#
# Single source of truth: any new typed channel added to CHANNEL_SCHEMA in
# src/events/schemas.py SHOULD also be registered here AND in
# WSBridge.start(). The startup-time _assert_channel_coverage() check
# raises loudly if the two diverge (anti-drift guard, A8).
CHANNEL_TO_WS_TYPE: dict[str, str] = {
    CHANNEL_TRADES_OBSERVED: "trade",
    CHANNEL_DECISIONS: "decision",
    CHANNEL_PAPER_CLOSED: "position_closed",
    CHANNEL_SYSTEM_STATUS: "system_status",
    CHANNEL_RECONCILIATION: "reconciliation",
}

# Rate-limit budget per typed channel (broadcast-wide, NOT per-client).
# ``trades:observed`` peaks at ~50 msg/s in prod; 100/s is a 2x headroom
# that still protects browsers under an upstream burst.
_RATE_LIMIT_MAX_PER_S: dict[str, int] = {
    CHANNEL_TRADES_OBSERVED: 100,
    CHANNEL_DECISIONS: 100,
    CHANNEL_PAPER_CLOSED: 100,
    CHANNEL_SYSTEM_STATUS: 100,
    CHANNEL_RECONCILIATION: 100,
}

# Window over which the token bucket refills (1 second).
_RATE_LIMIT_WINDOW_S = 1.0

# How often we log accumulated drop counts. We want to know about
# sustained drops without flooding the log on every dropped event.
_RATE_LIMIT_LOG_INTERVAL_S = 10.0


class _TokenBucket:
    """Simple monotonic-clock token bucket. Not async-safe — but the
    bridge dispatches messages serially inside the Subscriber run loop
    so there's no concurrent access.

    The bucket is rebuilt on every check: tokens consumed in the current
    1s window count against ``capacity``; once the window rolls over,
    consumption resets to zero. Cheaper than a refill-rate scheme and
    sufficient for "max N msg/s" semantics.
    """

    __slots__ = ("capacity", "window_s", "_window_start", "_consumed")

    def __init__(self, capacity: int, window_s: float = _RATE_LIMIT_WINDOW_S) -> None:
        self.capacity = capacity
        self.window_s = window_s
        self._window_start = 0.0
        self._consumed = 0

    def try_consume(self, now: float) -> bool:
        # Roll the window if we've moved past it.
        if now - self._window_start >= self.window_s:
            self._window_start = now
            self._consumed = 0
        if self._consumed < self.capacity:
            self._consumed += 1
            return True
        return False


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
        # Per-channel rate limiters (typed channels only). Constructed
        # lazily so tests that exercise a single handler don't pay for
        # the full table.
        self._buckets: dict[str, _TokenBucket] = {
            ch: _TokenBucket(capacity)
            for ch, capacity in _RATE_LIMIT_MAX_PER_S.items()
        }
        # Drop counters: incremented on rate-limit miss, periodically
        # flushed to the log. Resets after each flush.
        self._drop_counts: dict[str, int] = {ch: 0 for ch in _RATE_LIMIT_MAX_PER_S}
        self._last_drop_log_ts: float = 0.0

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def start(self) -> None:
        """Start consuming Redis channels in the background."""
        if self._redis is None:
            logger.warning("WSBridge: no Redis client attached, skipping")
            return
        self._running = True
        self._subscriber = Subscriber(settings.REDIS_URL, name="api.ws_bridge")
        # Typed channels: one generic handler dispatches via CHANNEL_SCHEMA.
        # Each handler is a tiny closure over the channel name so the
        # Subscriber wiring still passes (channel, payload) → handler(channel).
        self._subscriber.register(CHANNEL_TRADES_OBSERVED, self._on_typed_event)
        self._subscriber.register(CHANNEL_DECISIONS, self._on_typed_event)
        self._subscriber.register(CHANNEL_PAPER_CLOSED, self._on_typed_event)
        self._subscriber.register(CHANNEL_SYSTEM_STATUS, self._on_typed_event)
        self._subscriber.register(CHANNEL_RECONCILIATION, self._on_typed_event)
        # Legacy snapshot trigger — kept until A9 cuts the front off it.
        self._subscriber.register(SNAPSHOT_PUBSUB_CHANNEL, self._on_snapshot_updated)
        self._assert_channel_coverage()
        # In dev the API and engine share a Redis instance, so passing
        # `self._redis` keeps the pub/sub graph compatible with the
        # existing test wiring (and with fakeredis-based integration
        # tests). Subscriber will NOT close it on stop().
        await self._subscriber.start(redis_client=self._redis)
        logger.info(
            "WSBridge subscribed to Redis channels via Subscriber: "
            f"{list(self._subscriber.channels)}"
        )

    @staticmethod
    def _assert_channel_coverage() -> None:
        """Anti-drift: every channel in CHANNEL_SCHEMA must have a WS-type
        mapping (and vice versa), otherwise a producer can publish a
        valid Pydantic event that the bridge silently turns into
        ``type: "unknown"``. We fail loudly at startup instead.
        """
        schema_keys = set(CHANNEL_SCHEMA.keys())
        ws_keys = set(CHANNEL_TO_WS_TYPE.keys())
        if schema_keys != ws_keys:
            missing_in_ws = schema_keys - ws_keys
            missing_in_schema = ws_keys - schema_keys
            raise RuntimeError(
                "WSBridge channel coverage drift: "
                f"in CHANNEL_SCHEMA but missing WS mapping={missing_in_ws}; "
                f"in CHANNEL_TO_WS_TYPE but missing schema={missing_in_schema}"
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
        """Fan ``payload`` out as JSON text to every connected client.

        Slow clients (write buffer full / hung socket) are detected via
        send_text raising. We drop them from the connection set rather
        than block the whole bridge — losing a frame for ONE client is
        always better than stalling the broadcast pipeline.
        """
        if not self._connections:
            return
        text = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception:
                # Slow / disconnected client. Drop it; the next reconnect
                # will re-add via handle().
                dead.add(ws)
        self._connections -= dead

    # ------------------------------------------------------------------ #
    # Typed delta path                                                    #
    # ------------------------------------------------------------------ #

    async def _on_typed_event(self, data: Any, channel: str) -> None:
        """Generic handler for all typed channels (trades / decisions /
        positions / system / reconciliation).

        Pipeline:
          1. Look up the Pydantic schema for ``channel`` in CHANNEL_SCHEMA.
          2. Validate the payload — drop + warn on schema mismatch.
          3. Apply rate-limit (token bucket per channel).
          4. Build the WS envelope and broadcast.

        On any unrecoverable error (unknown channel, serialisation crash)
        we fall back to the legacy ``snapshot_updated`` trigger so the
        front-end still refetches.
        """
        if not self._running:
            return

        ws_type = CHANNEL_TO_WS_TYPE.get(channel)
        model_cls = CHANNEL_SCHEMA.get(channel)
        if ws_type is None or model_cls is None:
            # Defensive: a producer published on a channel we subscribed
            # to but don't have a schema for. Should be impossible given
            # _assert_channel_coverage runs at start(), but log + skip.
            logger.warning(
                f"WSBridge: typed handler called on unmapped channel={channel}, skipping"
            )
            return

        # ----- 1) Validate ------------------------------------------------ #
        validated_dict = self._validate_to_dict(model_cls, data, channel)
        if validated_dict is None:
            return  # already logged in _validate_to_dict

        # ----- 2) Rate-limit --------------------------------------------- #
        now_monotonic = time.monotonic()
        bucket = self._buckets.get(channel)
        if bucket is not None and not bucket.try_consume(now_monotonic):
            self._drop_counts[channel] = self._drop_counts.get(channel, 0) + 1
            self._maybe_log_drops(now_monotonic)
            return
        self._maybe_log_drops(now_monotonic)

        # ----- 3) Build envelope + broadcast ----------------------------- #
        try:
            envelope = {
                "type": ws_type,
                "channel": channel,
                "ts": time.time(),
                "data": validated_dict,
            }
            await self.broadcast(envelope)
        except Exception as exc:
            # Serialisation/broadcast crash is highly unusual — keep the
            # dashboard usable by falling back to the legacy refetch trigger.
            logger.exception(
                f"WSBridge: typed broadcast failed on {channel}: {exc}; "
                "falling back to legacy snapshot_updated"
            )
            try:
                await self.broadcast({"type": "snapshot_updated", "ts": time.time()})
            except Exception:
                # If even the fallback raises, swallow — the watchdog
                # will resurface this via the Subscriber error counter.
                pass

    @staticmethod
    def _validate_to_dict(
        model_cls: type[BaseModel], raw: Any, channel: str
    ) -> dict | None:
        """Typed dispatch helper. Subscriber already decodes JSON so we
        get a ``dict`` (or ``str`` if it wasn't JSON). On schema mismatch
        we log + return None so a single drifting publisher cannot black
        out the whole dashboard.
        """
        try:
            if isinstance(raw, str):
                event = model_cls.model_validate_json(raw)
            else:
                event = model_cls.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                f"WSBridge: dropped malformed event on {channel}: {exc}"
            )
            return None
        except Exception as exc:
            # Defensive against Pydantic raising something other than
            # ValidationError on weird inputs (e.g. None).
            logger.warning(
                f"WSBridge: dropped unparseable event on {channel}: {exc!r}"
            )
            return None
        # Re-serialise via the model so what we forward to browsers
        # matches the typed contract exactly. ``mode='json'`` produces
        # JSON-safe primitives (datetime → ISO string) so json.dumps
        # downstream stays cheap.
        return event.model_dump(mode="json")

    def _maybe_log_drops(self, now_monotonic: float) -> None:
        """Flush accumulated drop counts at most every
        ``_RATE_LIMIT_LOG_INTERVAL_S`` seconds. Keeps the log calm under
        a sustained burst while still surfacing the volume.
        """
        if now_monotonic - self._last_drop_log_ts < _RATE_LIMIT_LOG_INTERVAL_S:
            return
        nonzero = {ch: n for ch, n in self._drop_counts.items() if n > 0}
        if nonzero:
            logger.warning(
                "WSBridge: rate-limit dropped events in last "
                f"{_RATE_LIMIT_LOG_INTERVAL_S:.0f}s: {nonzero}"
            )
            for ch in nonzero:
                self._drop_counts[ch] = 0
        self._last_drop_log_ts = now_monotonic

    # ------------------------------------------------------------------ #
    # Legacy snapshot trigger — kept until A9 retires it                  #
    # ------------------------------------------------------------------ #

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

        A8 note: this channel keeps being emitted in parallel to the new
        typed deltas. A9 will retire it once the front-end stops
        listening for ``snapshot_updated``.
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
