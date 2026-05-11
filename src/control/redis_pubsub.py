"""
Centralized, reconnect-safe Redis pub/sub subscriber.

Solves audit F-04 (shared client between commands and pub/sub) and the
silent-resubscribe failure on disconnect.

The historical pattern in this codebase was::

    self._pubsub = self._redis.pubsub()
    await self._pubsub.subscribe("trades:observed")
    async for msg in self._pubsub.listen():
        try:
            ...
        except Exception:
            continue

That has two correctness bugs:

1. ``self._redis`` is the SAME ``redis.asyncio.Redis`` instance used by
   command callers (``publish``, ``hincrby``, ``set``). A long-running
   ``pubsub.listen()`` pins one pool connection forever; with 6+
   subscribers in the engine container, the pool runs hot.
2. On a Redis disconnect, ``pubsub.listen()`` raises a ``ConnectionError``
   from inside the ``async for``. The ``try/except`` only wraps message
   handling, not the iterator itself; the iterator dies, the ``finally``
   tries to ``unsubscribe`` on a dead socket, and the loop exits. The
   watchdog restarts the coroutine but the SUBSCRIBE registration is
   gone with the connection — any message published in the gap window
   is lost silently.

The :class:`Subscriber` here fixes both:

* It opens its OWN ``redis.asyncio.Redis`` instance — disjoint from the
  project-wide command client. Pub/sub no longer fights with commands
  for pool slots.
* The run loop is wrapped in ``while self._running:`` with exponential
  backoff (1s, 2s, 4s, 8s, 16s, capped at 30s). On any reconnect we
  RE-ISSUE ``SUBSCRIBE`` for every registered channel and resume
  ``listen()``. Handlers that raise increment a counter but do not kill
  the loop.

Public contract: if you registered a handler before ``start()``, you
will receive every message published while the subscriber is alive,
modulo the disconnect gap. Phase 3 closes the gap with Redis Streams +
a server-side cursor; for Phase 2 we accept "messages published during
the reconnect window are lost" and surface it via the
``polybot_redis_subscriber_reconnects_total`` counter.

Usage::

    sub = Subscriber(settings.REDIS_URL, name="profiler.behavior")
    sub.register("trades:observed", self._on_trade)
    sub.register("positions:closed", self._on_position_closed)
    await sub.start()
    ...
    await sub.stop()
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import redis.asyncio as redis_async
from loguru import logger

# Phase 3 Task D: every pubsub message proves the in-process control
# plane is alive — the IngestHealthMonitor's `redis_pubsub` source key
# is heartbeated from `_consume_once` on every received message. Import
# defensively so the legacy module still loads if monitoring is absent.
try:
    from src.monitoring.ingest_health import (  # type: ignore[attr-defined]
        SOURCE_REDIS_PUBSUB,
        get_health_monitor,
    )

    def _heartbeat_pubsub() -> None:
        try:
            get_health_monitor().heartbeat(SOURCE_REDIS_PUBSUB)
        except Exception:
            pass
except Exception:  # pragma: no cover
    def _heartbeat_pubsub() -> None:
        return None

# Handler signature: receives the decoded payload (dict for JSON) and the
# channel name. Async only — sync handlers are explicitly out of scope
# because every existing call site is already async.
Handler = Callable[[Any, str], Awaitable[None]]

# Backoff schedule on reconnect, in seconds. Caps at 30s — Redis restarts
# in production typically finish in <10s, so the long tail covers a
# Redis-side incident without spamming logs.
_BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

# Polling timeout on get_message(). Short enough to react to `stop()`
# quickly, long enough to avoid burning CPU on a quiet channel.
_GET_MESSAGE_TIMEOUT_S = 1.0


class Subscriber:
    """Owns a dedicated ``redis.asyncio.Redis`` + a pub/sub task.

    Reconnect strategy:
      * On ``ConnectionError`` / ``redis.RedisError`` / ``TimeoutError``
        inside the listen loop, increment the reconnect counter, sleep
        with backoff, then re-create the pubsub object and re-issue
        ``SUBSCRIBE`` for every registered channel.
      * Handler exceptions are caught per-message and increment the
        handler-error counter; they DO NOT trigger reconnect.
    """

    def __init__(self, redis_url: str, *, name: str) -> None:
        if not name:
            raise ValueError("Subscriber requires a non-empty name (used for metrics)")
        self._url = redis_url
        self._name = name
        self._handlers: dict[str, Handler] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        # Owned client — disjoint from the command-issuing client. We
        # build it lazily in start() so unit tests can swap in fakeredis
        # via the optional `redis_client` param.
        self._redis: Any | None = None
        self._owns_redis = True
        # Health/metrics state. Stay numeric here; the
        # src/monitoring/metrics.py counters are incremented from the
        # run loop via helper hooks (see _bump_*).
        self._is_connected = False
        self._total_messages = 0
        self._total_reconnects = 0
        self._handler_errors = 0
        self._last_message_ts: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Registration                                                        #
    # ------------------------------------------------------------------ #

    def register(self, channel: str, handler: Handler) -> None:
        """Bind ``handler`` to ``channel``. Must be called BEFORE ``start()``.

        Why before-start: ``start()`` issues SUBSCRIBE in one shot for the
        whole channel set; adding a channel post-start would require
        re-acquiring the subscriber lock and re-issuing SUBSCRIBE, which
        is intentionally out of scope for Phase 2. Tests cover the
        before-start path only.
        """
        if self._running:
            raise RuntimeError(
                f"Subscriber({self._name}): register() must be called before start()"
            )
        if not channel:
            raise ValueError("channel must be a non-empty string")
        if channel in self._handlers:
            raise ValueError(
                f"Subscriber({self._name}): handler already registered for {channel!r}"
            )
        self._handlers[channel] = handler

    def handler(self, channel: str) -> Callable[[Handler], Handler]:
        """Decorator form of ``register``::

            @sub.handler("trades:observed")
            async def on_trade(payload, channel): ...
        """

        def _decorator(fn: Handler) -> Handler:
            self.register(channel, fn)
            return fn

        return _decorator

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self, *, redis_client: Any | None = None) -> None:
        """Open the dedicated Redis client and kick off the run loop.

        ``redis_client`` is an escape hatch for tests that need to drive
        a shared fakeredis instance — production code does NOT pass it.
        When provided, ``stop()`` will NOT close the client (caller owns
        its lifetime).
        """
        if self._running:
            logger.debug(f"Subscriber({self._name}): start() called twice — ignoring")
            return
        if not self._handlers:
            raise RuntimeError(
                f"Subscriber({self._name}): no handlers registered — refusing to start"
            )
        if redis_client is not None:
            self._redis = redis_client
            self._owns_redis = False
        else:
            # Dedicated client: pub/sub no longer fights commands for the
            # pool. ``decode_responses=True`` matches the project-wide
            # convention so JSON parsing works on the str payload.
            self._redis = redis_async.from_url(self._url, decode_responses=True)
            self._owns_redis = True
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"sub:{self._name}")
        self._bump_active_gauge(+1)
        logger.info(
            f"Subscriber({self._name}): started with channels={sorted(self._handlers)}"
        )

    async def restart(self) -> None:
        """Tear down + restart the subscriber. Used by Phase 3 Task D
        ingest-health recovery when ``redis_pubsub`` heartbeats stop.

        Idempotent on a stopped subscriber: if not running, behaves like
        ``start()``. The owned Redis client is rebuilt so a half-open
        socket doesn't survive the restart.
        """
        if self._running:
            await self.stop()
        # Re-arm the stop event so start() can run cleanly.
        try:
            self._stop_event = asyncio.Event()
        except Exception:
            pass
        await self.start()

    async def stop(self) -> None:
        """Cancel the run loop and close the owned Redis client."""
        if not self._running:
            return
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # CancelledError is expected; any other exception during
                # teardown was already logged inside the loop.
                pass
        self._bump_active_gauge(-1)
        self._is_connected = False
        if self._owns_redis and self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                # Close errors during shutdown are not actionable.
                pass
        self._redis = None
        logger.info(f"Subscriber({self._name}): stopped")

    # ------------------------------------------------------------------ #
    # Health                                                              #
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def total_messages(self) -> int:
        return self._total_messages

    @property
    def total_reconnects(self) -> int:
        return self._total_reconnects

    @property
    def handler_errors(self) -> int:
        return self._handler_errors

    @property
    def name(self) -> str:
        return self._name

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def last_message_ts(self, channel: str) -> float | None:
        return self._last_message_ts.get(channel)

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _run_loop(self) -> None:
        """Outer reconnect loop. Each iteration owns one pubsub object."""
        attempt = 0
        while self._running:
            try:
                await self._consume_once()
                # Clean exit from _consume_once means we hit `not self._running`.
                # Don't reconnect — drop through to the while-check.
                attempt = 0
            except asyncio.CancelledError:
                # Cooperative shutdown — re-raise so the task is properly
                # cancelled.
                raise
            except (
                redis_async.ConnectionError,
                redis_async.TimeoutError,
                ConnectionError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                reason = _classify_reconnect_reason(exc)
                self._is_connected = False
                self._total_reconnects += 1
                self._bump_reconnect_counter(reason)
                backoff = _BACKOFF_SCHEDULE_S[
                    min(attempt, len(_BACKOFF_SCHEDULE_S) - 1)
                ]
                attempt += 1
                logger.warning(
                    f"Subscriber({self._name}): reconnect "
                    f"#{self._total_reconnects} reason={reason} "
                    f"channels={sorted(self._handlers)} "
                    f"backoff={backoff:.1f}s err={exc!r}"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
            except Exception:
                # An unknown exception class. Treat as a reconnect-worthy
                # error so we don't silently die, but log as ERROR so it
                # gets attention. This is the path that would have masked
                # silent message loss in the pre-fix code.
                self._is_connected = False
                self._total_reconnects += 1
                self._bump_reconnect_counter("other")
                backoff = _BACKOFF_SCHEDULE_S[
                    min(attempt, len(_BACKOFF_SCHEDULE_S) - 1)
                ]
                attempt += 1
                logger.exception(
                    f"Subscriber({self._name}): unexpected error in run loop, "
                    f"will reconnect in {backoff:.1f}s"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    async def _consume_once(self) -> None:
        """Open ONE pubsub session, SUBSCRIBE, then loop on get_message.

        Returns cleanly when ``self._running`` flips to False. Raises on
        any I/O error — the outer loop classifies + backs off.
        """
        assert self._redis is not None
        pubsub = self._redis.pubsub()
        channels = sorted(self._handlers)
        try:
            await pubsub.subscribe(*channels)
            self._is_connected = True
            logger.debug(
                f"Subscriber({self._name}): SUBSCRIBE ok on {channels}"
            )
            while self._running:
                # We use get_message(timeout=...) instead of `async for
                # listen()` because the iterator form swallows the
                # `self._running` flag — it only checks after the next
                # message arrives. With a quiet channel + a kill signal,
                # that wedges shutdown for minutes. The timeout-based
                # poll wakes us up regularly to check.
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_GET_MESSAGE_TIMEOUT_S,
                )
                if msg is None:
                    continue
                if msg.get("type") != "message":
                    continue
                channel = _decode(msg.get("channel"))
                raw = msg.get("data")
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                self._total_messages += 1
                self._last_message_ts[channel] = time.time()
                self._bump_message_counter(channel)
                # Phase 3 Task D: ingest-health heartbeat. ANY pubsub
                # message from ANY subscriber counts — the Subscriber
                # class is shared infrastructure so a single live
                # subscriber refreshes the global `redis_pubsub`
                # source. O(1), best-effort.
                _heartbeat_pubsub()
                await self._dispatch(channel, raw)
        finally:
            # Best-effort unsubscribe + close. If the connection is
            # already broken we don't care — the outer loop will rebuild
            # the pubsub object next iteration.
            try:
                await asyncio.wait_for(
                    pubsub.unsubscribe(*channels),
                    timeout=2.0,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(pubsub.aclose(), timeout=2.0)
            except Exception:
                pass

    async def _dispatch(self, channel: str, raw: Any) -> None:
        """Decode JSON (if applicable) and call the handler.

        Handlers raising do NOT kill the loop — we log + bump the error
        counter and keep iterating. The audit's F-04 fix demands this:
        one bad message must not silence a subscriber.
        """
        handler = self._handlers.get(channel)
        if handler is None:
            # Subscribed to a channel we don't have a handler for — should
            # be impossible because register() must be called before
            # start(), but defensive: log and drop.
            logger.warning(
                f"Subscriber({self._name}): message on unregistered channel "
                f"{channel!r} dropped"
            )
            return
        payload: Any
        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped and stripped[0] in "{[\"":
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._handler_errors += 1
                    self._bump_handler_error(channel)
                    logger.warning(
                        f"Subscriber({self._name}): bad JSON on {channel}: {exc}"
                    )
                    return
            else:
                payload = raw
        else:
            payload = raw
        try:
            await handler(payload, channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._handler_errors += 1
            self._bump_handler_error(channel)
            logger.exception(
                f"Subscriber({self._name}): handler error on {channel}: {exc}"
            )

    # ------------------------------------------------------------------ #
    # Metrics hooks — kept as small methods so tests can monkey-patch.    #
    # ------------------------------------------------------------------ #

    def _bump_active_gauge(self, delta: int) -> None:
        try:
            from src.monitoring.metrics import redis_subscribers_active

            if delta > 0:
                for _ in range(delta):
                    redis_subscribers_active.inc()
            elif delta < 0:
                for _ in range(-delta):
                    redis_subscribers_active.dec()
        except Exception:
            pass

    def _bump_reconnect_counter(self, reason: str) -> None:
        try:
            from src.monitoring.metrics import redis_subscriber_reconnects_total

            redis_subscriber_reconnects_total.labels(
                subscriber=self._name, reason=reason
            ).inc()
        except Exception:
            pass

    def _bump_message_counter(self, channel: str) -> None:
        try:
            from src.monitoring.metrics import redis_subscriber_messages_total

            redis_subscriber_messages_total.labels(
                subscriber=self._name, channel=channel
            ).inc()
        except Exception:
            pass

    def _bump_handler_error(self, channel: str) -> None:
        try:
            from src.monitoring.metrics import redis_subscriber_handler_errors_total

            redis_subscriber_handler_errors_total.labels(
                subscriber=self._name, channel=channel
            ).inc()
        except Exception:
            pass


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _classify_reconnect_reason(exc: BaseException) -> str:
    """Map an exception to a low-cardinality label for the reconnect counter."""
    if isinstance(exc, (asyncio.TimeoutError, redis_async.TimeoutError)):
        return "timeout"
    if isinstance(exc, (redis_async.ConnectionError, ConnectionError, OSError)):
        return "conn_error"
    return "other"
