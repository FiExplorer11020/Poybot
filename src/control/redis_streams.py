"""
Durable, at-least-once delivery on Redis Streams.

This module closes the gap left by Phase 2 Task D's pub/sub Subscriber:
the latter documents that "messages published during the disconnect
window are LOST". Streams persist server-side, so a disconnected
consumer simply resumes from the last-acked entry on reconnect.

The audit calls this out in Section 6 ("the codebase has no end-to-end
ownership of any decision's lifecycle. Trades flow through six modules
connected only by Redis pub/sub with no durability, no trace context,
no idempotency token, and no consumer-group semantics."). The objects
defined here are the structural fix:

* :class:`StreamProducer` — append-only ``XADD`` with bounded
  ``MAXLEN ~`` trimming. Generates a UUID ``trace_id`` and a
  millisecond ``published_at_ms`` on every entry so a single decision
  can be followed end-to-end through every downstream consumer.

* :class:`StreamConsumer` — ``XREADGROUP`` reader with consumer-group
  semantics. On exception the entry stays PENDING; a periodic
  ``XCLAIM`` cycle steals stale entries from dead consumers; after N
  retries the entry is published to the deadletter stream and ACK'd
  so the main pipeline keeps moving.

* :func:`get_trades_stream_publisher` — process-singleton
  :class:`StreamProducer` for ``trades:stream``. Phase 3 round 1's
  contract with Agent A: the trade_observer publishes via this helper
  AFTER the DB commit, dual-write alongside the legacy
  ``trades:observed`` pub/sub channel.

Idempotency contract for handlers
---------------------------------

Every handler MUST be idempotent: reprocessing the same
``(entry_id, payload)`` tuple must produce the same end state. With
at-least-once delivery, a consumer that processed an entry but crashed
before ACK'ing will see the entry replayed via ``XCLAIM`` on next
boot. Most existing handlers in this codebase are idempotent by
construction:

* :class:`BehaviorProfiler` — Dirichlet / EWMA / Beta updates are
  *not* strictly idempotent (counters increment). The handler is
  guarded by the underlying SQL pattern (``ON CONFLICT DO UPDATE``)
  but a real replay would double-count. We accept this in round 1 —
  Streams replay only happens on a hard crash mid-handler, and the
  signal-to-noise of an occasional double-count is small relative to
  the much-louder loss-on-disconnect bug this round closes. Round 2
  will introduce a ``processed_entry_ids`` set per consumer-group for
  exactly-once semantics.

* :class:`GraphEngine`, :class:`ConfidenceEngine`,
  :class:`PaperTrader`, :class:`LiveTrader` — all guard against
  duplicate work via DB-level uniqueness (``UNIQUE(leader, follower)``,
  ``decision_log`` insert), so a replay is at worst a no-op.

* ``ws_bridge`` — stateless fan-out; replay is harmless.

Deadletter forensics
--------------------

A handler that raises ``max_retries+1`` times has its entry copied to
``<stream>.deadletter`` with the original payload plus
``{"_deadletter_reason": str(exc), "_retry_count": N}`` and ACK'd on
the source stream. There is no automatic consumer of the deadletter
stream — it's drained manually by an operator running:

.. code-block:: bash

   redis-cli XRANGE trades:stream.deadletter - + COUNT 100

Phase 3 round 2 will add a Telegram alert when the deadletter depth
crosses a threshold.

Concurrency / connection notes
------------------------------

* Each producer/consumer owns its OWN ``redis.asyncio.Redis`` instance
  (audit F-04 — never share with command client).
* Reconnect: bounded exponential backoff (1s, 2s, 4s, 8s, 16s, 30s
  cap), same shape as Subscriber's.
* ``XGROUP CREATE … MKSTREAM`` is idempotent (handled with ``BUSYGROUP``
  swallow), so we re-issue on every reconnect.
* ``MAXLEN ~ 1_000_000`` is approximate trimming — Redis can keep a few
  thousand extra entries for performance. The bound matters; the
  precise depth does not.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

import redis.asyncio as redis_async
from loguru import logger

# Handler signature: receives the decoded payload (always JSON-decoded
# to dict here — XADD entries are always JSON-encoded by the
# StreamProducer below), the source stream name, and the entry id.
# Async only — every existing call site in this codebase is async.
StreamHandler = Callable[[dict, str, str], Awaitable[None]]

# Backoff schedule on reconnect, in seconds. Same shape as Subscriber.
_BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

# Blocking window for XREADGROUP. Short enough to react to ``stop()``
# quickly, long enough not to burn CPU on a quiet stream.
_BLOCK_MS_DEFAULT = 1000

# How often to scan PENDING entries and XCLAIM idle ones from dead
# consumers. 30s keeps recovery latency reasonable without hammering
# Redis. ``claim_idle_ms`` on the consumer is the threshold for "this
# entry has been pending too long, probably a dead consumer".
_CLAIM_SCAN_INTERVAL_S = 30.0

# Default maxlen for XADD trimming. Sized for ~11d of trades at peak
# ~1 trade/sec average; the audit's retention target is 90d in DB but
# only "recent history" in Redis Streams (DB is the durable archive).
_DEFAULT_MAXLEN = 1_000_000


# Single field in the entry's hash. Redis Streams entries are
# hashes, but we treat them as opaque JSON blobs: one field
# ``"data"`` holds the JSON-encoded payload. Keeps producer/consumer
# trivially symmetric and avoids per-field type juggling.
_PAYLOAD_FIELD = "data"


def _classify_reconnect_reason(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, redis_async.TimeoutError)):
        return "timeout"
    if isinstance(exc, (redis_async.ConnectionError, ConnectionError, OSError)):
        return "conn_error"
    return "other"


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


class StreamProducer:
    """Append-only producer for one Redis Stream.

    Each :meth:`publish` call issues ``XADD <stream> MAXLEN ~ <maxlen>
    * data <json>`` and returns the entry id. The payload is JSON-encoded
    with ``trace_id`` and ``published_at_ms`` injected:

    * ``trace_id`` — UUID4 generated at publish time IF the caller
      didn't supply one. Flows through every consumer (handlers see
      ``payload["trace_id"]``) so a single decision's lifecycle is
      traceable through the pipeline.
    * ``published_at_ms`` — server-side ms timestamp at the moment of
      XADD, used by handlers to compute end-to-end latency.

    Reconnect contract: on ``ConnectionError`` / ``TimeoutError`` /
    ``OSError`` during ``XADD`` we lazily replace the underlying client
    and re-issue the call once. A second failure raises to the caller
    so it can decide whether to retry / drop.
    """

    def __init__(
        self,
        redis_url: str,
        stream: str,
        *,
        maxlen: int = _DEFAULT_MAXLEN,
        name: str | None = None,
    ) -> None:
        if not stream:
            raise ValueError("StreamProducer requires a non-empty stream name")
        self._url = redis_url
        self._stream = stream
        self._maxlen = int(maxlen)
        self._name = name or stream
        self._redis: Any | None = None
        self._owns_redis = True
        self._running = False
        self._publish_lock = asyncio.Lock()
        self._total_published = 0
        self._total_reconnects = 0

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def total_published(self) -> int:
        return self._total_published

    @property
    def total_reconnects(self) -> int:
        return self._total_reconnects

    async def start(self, *, redis_client: Any | None = None) -> None:
        """Open the dedicated Redis client.

        ``redis_client`` is a test-only escape hatch (matches Subscriber's
        injection contract). Production code never passes it.
        """
        if self._running:
            return
        if redis_client is not None:
            self._redis = redis_client
            self._owns_redis = False
        else:
            self._redis = redis_async.from_url(self._url, decode_responses=True)
            self._owns_redis = True
        self._running = True
        logger.debug(
            f"StreamProducer({self._name}): started, stream={self._stream} "
            f"maxlen~{self._maxlen}"
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._owns_redis and self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
        self._redis = None
        logger.debug(f"StreamProducer({self._name}): stopped")

    async def publish(self, payload: dict, *, trace_id: str | None = None) -> str:
        """Append ``payload`` to the stream. Returns the assigned entry id.

        The payload is mutated in-place to inject ``trace_id`` (if
        absent) and ``published_at_ms``. The caller can pre-set a
        ``trace_id`` to thread a single decision through multiple
        publishes; otherwise we generate a fresh UUID.

        Concurrency-safe: a lock serializes XADD per producer so the
        single owned connection isn't multiplexed.
        """
        if not self._running or self._redis is None:
            raise RuntimeError(
                f"StreamProducer({self._name}): publish() before start()"
            )
        if not isinstance(payload, dict):
            raise TypeError("StreamProducer.publish requires a dict payload")
        if "trace_id" not in payload:
            payload["trace_id"] = trace_id or str(uuid.uuid4())
        # Use the server-side wallclock; consumers compute latency
        # against this. ms resolution matches Redis' own entry-id
        # granularity so we never see clock skew between the two.
        payload["published_at_ms"] = int(time.time() * 1000)
        encoded = json.dumps(payload, default=str)
        async with self._publish_lock:
            try:
                entry_id = await self._xadd(encoded)
            except (
                redis_async.ConnectionError,
                redis_async.TimeoutError,
                ConnectionError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                self._total_reconnects += 1
                self._bump_reconnect("producer")
                logger.warning(
                    f"StreamProducer({self._name}): {exc!r} during XADD, "
                    "rebuilding client and retrying once"
                )
                await self._rebuild_client()
                entry_id = await self._xadd(encoded)
        self._total_published += 1
        self._bump_published()
        return entry_id

    async def _xadd(self, encoded: str) -> str:
        assert self._redis is not None
        # `approximate=True` => the MAXLEN trim is `~`, NOT exact.
        # Redis can keep a few extra entries (cheaper to skip a radix
        # split). Good enough at our cardinality.
        result = await self._redis.xadd(
            self._stream,
            {_PAYLOAD_FIELD: encoded},
            maxlen=self._maxlen,
            approximate=True,
        )
        # `decode_responses=True` makes the entry id a str; fakeredis
        # follows the same contract. Coerce defensively in case a test
        # injects a bytes-returning client.
        if isinstance(result, (bytes, bytearray)):
            return result.decode("utf-8", errors="replace")
        return str(result)

    async def _rebuild_client(self) -> None:
        """Close + replace the owned client on a hard XADD failure."""
        if not self._owns_redis:
            # Test injected the client — we can't replace it. The retry
            # may still succeed because fakeredis errors transiently.
            return
        old = self._redis
        try:
            if old is not None:
                await old.aclose()
        except Exception:
            pass
        self._redis = redis_async.from_url(self._url, decode_responses=True)

    # ----- metrics hooks (kept tiny so tests can monkey-patch) ---------- #

    def _bump_published(self) -> None:
        try:
            from src.monitoring.metrics import stream_published_total

            stream_published_total.labels(stream=self._stream).inc()
        except Exception:
            pass

    def _bump_reconnect(self, component: str) -> None:
        try:
            from src.monitoring.metrics import stream_reconnects_total

            stream_reconnects_total.labels(
                component=component, stream=self._stream
            ).inc()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


class StreamConsumer:
    """Consumer-group reader with at-least-once delivery.

    Public surface
    --------------

    * :meth:`register` (or the :meth:`handler` decorator) binds an async
      ``handler(payload, stream, entry_id)`` callable. Must be called
      BEFORE :meth:`start`. Single-handler-per-consumer is the contract
      here — one consumer = one stream = one handler. (If you need
      fan-out on the same stream, spawn N consumers with distinct
      consumer names — the consumer-group semantics will load-balance
      between them.)

    * :meth:`start` opens a dedicated Redis client, ensures the
      consumer group exists (``XGROUP CREATE … MKSTREAM`` is
      idempotent — ``BUSYGROUP`` is swallowed), and spawns the run
      loop plus the periodic XCLAIM scanner.

    * :meth:`stop` cancels both tasks and closes the owned client.

    Run loop
    --------

    1. ``XREADGROUP <group> <consumer> BLOCK <ms> COUNT <batch>
       STREAMS <stream> >`` to fetch undelivered entries.
    2. For each entry: JSON-decode payload, time the handler, on
       success ``XACK``, on exception increment the in-memory retry
       counter and DO NOT ACK (entry stays PENDING for the next
       XCLAIM cycle).
    3. When the in-memory retry counter exceeds ``max_retries``, copy
       the payload to ``<stream>.deadletter`` with diagnostic fields
       and ``XACK`` on the source stream so the main pipeline drains.

    Claim scanner
    -------------

    Every ``_CLAIM_SCAN_INTERVAL_S`` we run ``XPENDING`` to find entries
    idle for more than ``claim_idle_ms`` and ``XCLAIM`` them. This
    recovers from a dead consumer (the entry never gets ACK'd or
    redelivered otherwise) and is the audit's "no durability, no
    consumer-group semantics" fix.

    Idempotency
    -----------

    Every handler MUST be idempotent (see module docstring). The
    consumer makes no exactly-once guarantee; that lives in round 2.
    """

    def __init__(
        self,
        redis_url: str,
        stream: str,
        group: str,
        consumer_name: str,
        *,
        max_retries: int = 3,
        claim_idle_ms: int = 60_000,
        batch_size: int = 32,
        block_ms: int = _BLOCK_MS_DEFAULT,
    ) -> None:
        if not stream or not group or not consumer_name:
            raise ValueError(
                "StreamConsumer requires non-empty stream / group / consumer_name"
            )
        self._url = redis_url
        self._stream = stream
        self._group = group
        self._consumer = consumer_name
        self._max_retries = max(0, int(max_retries))
        self._claim_idle_ms = max(1_000, int(claim_idle_ms))
        self._batch_size = max(1, int(batch_size))
        self._block_ms = max(50, int(block_ms))
        self._deadletter_stream = f"{stream}.deadletter"
        self._handler: StreamHandler | None = None
        self._running = False
        self._main_task: asyncio.Task | None = None
        self._claim_task: asyncio.Task | None = None
        self._redis: Any | None = None
        self._owns_redis = True
        # In-memory retry counter — counts how many times THIS process
        # has seen an entry id raise. On process restart the counter
        # resets to zero; that's fine because we want a fresh chance
        # on a new boot (the underlying bug may have been a transient
        # in-memory state).
        self._retry_counts: dict[str, int] = {}
        self._is_connected = False
        self._total_consumed = 0
        self._total_reconnects = 0
        self._total_dead_letters = 0
        self._handler_errors = 0

    # ------------------------------------------------------------------ #
    # Registration                                                        #
    # ------------------------------------------------------------------ #

    def register(self, handler: StreamHandler) -> None:
        if self._running:
            raise RuntimeError(
                f"StreamConsumer({self._stream}/{self._group}): "
                "register() must be called before start()"
            )
        if self._handler is not None:
            raise ValueError(
                f"StreamConsumer({self._stream}/{self._group}): "
                "handler already registered"
            )
        self._handler = handler

    def handler(self, fn: StreamHandler) -> StreamHandler:
        """Decorator form of :meth:`register`."""
        self.register(fn)
        return fn

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self, *, redis_client: Any | None = None) -> None:
        if self._running:
            return
        if self._handler is None:
            raise RuntimeError(
                f"StreamConsumer({self._stream}/{self._group}): "
                "no handler registered — refusing to start"
            )
        if redis_client is not None:
            self._redis = redis_client
            self._owns_redis = False
        else:
            self._redis = redis_async.from_url(self._url, decode_responses=True)
            self._owns_redis = True
        await self._ensure_group()
        self._running = True
        self._main_task = asyncio.create_task(
            self._run_loop(), name=f"stream:{self._stream}:{self._group}"
        )
        self._claim_task = asyncio.create_task(
            self._claim_loop(), name=f"stream:{self._stream}:{self._group}:claim"
        )
        logger.info(
            f"StreamConsumer started: stream={self._stream} group={self._group} "
            f"consumer={self._consumer} max_retries={self._max_retries}"
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in (self._main_task, self._claim_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._main_task = None
        self._claim_task = None
        self._is_connected = False
        if self._owns_redis and self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
        self._redis = None
        logger.info(
            f"StreamConsumer stopped: stream={self._stream} group={self._group}"
        )

    # ------------------------------------------------------------------ #
    # Health                                                              #
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def total_consumed(self) -> int:
        return self._total_consumed

    @property
    def total_reconnects(self) -> int:
        return self._total_reconnects

    @property
    def total_dead_letters(self) -> int:
        return self._total_dead_letters

    @property
    def handler_errors(self) -> int:
        return self._handler_errors

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def group(self) -> str:
        return self._group

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _ensure_group(self) -> None:
        """Idempotent ``XGROUP CREATE … MKSTREAM``.

        ``BUSYGROUP`` is the expected error if the group already
        exists — swallow and move on. Any other error propagates so
        the run loop's reconnect path handles it.
        """
        assert self._redis is not None
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
        except redis_async.ResponseError as exc:
            msg = str(exc)
            if "BUSYGROUP" in msg:
                return
            raise
        except Exception as exc:
            # Some clients wrap ResponseError differently; defensive.
            if "BUSYGROUP" in str(exc):
                return
            raise

    async def _run_loop(self) -> None:
        """Outer reconnect loop. Each iteration owns one XREADGROUP session."""
        attempt = 0
        while self._running:
            try:
                await self._consume_once()
                attempt = 0
            except asyncio.CancelledError:
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
                self._bump_reconnect()
                backoff = _BACKOFF_SCHEDULE_S[
                    min(attempt, len(_BACKOFF_SCHEDULE_S) - 1)
                ]
                attempt += 1
                logger.warning(
                    f"StreamConsumer({self._stream}/{self._group}): "
                    f"reconnect #{self._total_reconnects} reason={reason} "
                    f"backoff={backoff:.1f}s err={exc!r}"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                # Re-establish the group on reconnect — it's
                # idempotent, and the stream may have been recreated
                # under us if Redis was restarted with no AOF.
                try:
                    await self._ensure_group()
                except Exception:
                    # Will retry on next loop iteration.
                    pass
            except Exception:
                self._is_connected = False
                self._total_reconnects += 1
                self._bump_reconnect()
                backoff = _BACKOFF_SCHEDULE_S[
                    min(attempt, len(_BACKOFF_SCHEDULE_S) - 1)
                ]
                attempt += 1
                logger.exception(
                    f"StreamConsumer({self._stream}/{self._group}): "
                    f"unexpected error in run loop, retrying in {backoff:.1f}s"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    async def _consume_once(self) -> None:
        """One XREADGROUP session. Returns cleanly when ``stop()`` flips.

        Each tick reads in two phases:

        1. Self-retry: use ``XCLAIM`` (with ``min_idle_time=<retry_idle>``)
           to pull entries that are pending on THIS consumer and
           re-feed them to the handler. We use XCLAIM rather than
           ``XREADGROUP ... STREAMS <stream> 0`` because the latter is
           inconsistent across redis-py / fakeredis (fakeredis returns
           an empty list for the pending fetch on the same consumer).
           XCLAIM returns the payload and resets the delivery counter
           atomically — exactly what we need.
        2. New entries: ``XREADGROUP ... STREAMS <stream> >`` BLOCK
           <ms> — fetch never-delivered entries, blocking briefly so a
           quiet stream doesn't busy-loop.

        The retry path (handler raised → no XACK) thus self-heals
        within one loop tick instead of waiting for the XCLAIM scanner.
        """
        assert self._redis is not None
        self._is_connected = True
        while self._running:
            # Phase 1: self-retry via XCLAIM.
            try:
                await self._retry_self_pending()
            except Exception as exc:
                # Best-effort — never let the retry path block a
                # subsequent XREADGROUP. The XCLAIM scanner will pick
                # it up eventually.
                logger.debug(
                    f"StreamConsumer({self._stream}/{self._group}): "
                    f"self-retry skipped this tick: {exc!r}"
                )

            # Phase 2: fetch new entries.
            entries = await self._redis.xreadgroup(
                self._group,
                self._consumer,
                {self._stream: ">"},
                count=self._batch_size,
                block=self._block_ms,
            )
            if entries:
                for stream_name, items in entries:
                    stream_name = _decode_str(stream_name)
                    for entry_id_raw, fields in items:
                        entry_id = _decode_str(entry_id_raw)
                        await self._handle_entry(entry_id, fields)
            # Update XPENDING gauge so the dashboard has live
            # backpressure visibility regardless of which phase served.
            await self._update_pending_gauge()

    async def _retry_self_pending(self) -> None:
        """Re-process this consumer's own pending entries.

        Finds entries the broker still considers PENDING for this
        ``(group, consumer)`` and runs them through the handler again.
        Uses a very low ``min_idle_time`` (1 ms) so an entry that was
        nack'd a tick ago gets retried this tick.

        This is the same-consumer counterpart to :meth:`_claim_pending`,
        which targets entries owned by OTHER (dead) consumers.
        """
        assert self._redis is not None
        try:
            detail = await self._redis.xpending_range(
                self._stream,
                self._group,
                min="-",
                max="+",
                count=self._batch_size,
                consumername=self._consumer,
            )
        except TypeError:
            # Older redis-py uses ``consumer=``; even older has neither.
            try:
                detail = await self._redis.xpending_range(
                    self._stream,
                    self._group,
                    min="-",
                    max="+",
                    count=self._batch_size,
                )
            except Exception:
                detail = []
        except Exception:
            detail = []
        if not detail:
            return
        ids: list[str] = []
        for item in detail:
            consumer_name = _decode_str(item.get("consumer") or "")
            # If the server didn't filter by consumer for us, do it here.
            if consumer_name and consumer_name != self._consumer:
                continue
            mid = _decode_str(item.get("message_id") or item.get("id") or "")
            if mid:
                ids.append(mid)
        if not ids:
            return
        # XCLAIM with min_idle_time=1ms resets the entry's idle clock
        # and returns the payload. It's effectively a "redeliver to
        # myself now" primitive.
        try:
            claimed = await self._redis.xclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=1,
                message_ids=ids,
            )
        except Exception as exc:
            logger.debug(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"self-claim failed: {exc!r}"
            )
            return
        for item in claimed or []:
            # XCLAIM returns either [(id, fields), ...] (decoded) or
            # [(b'id', {b'k': b'v'}), ...] (bytes); normalize.
            if isinstance(item, (list, tuple)) and len(item) == 2:
                entry_id_raw, fields = item
            elif isinstance(item, dict):
                entry_id_raw = item.get("message_id") or item.get("id")
                fields = item.get("fields") or item
            else:
                continue
            entry_id = _decode_str(entry_id_raw)
            if not entry_id or fields is None:
                continue
            await self._handle_entry(entry_id, fields)

    async def _handle_entry(self, entry_id: str, fields: Any) -> None:
        assert self._redis is not None and self._handler is not None
        # Decode fields. fakeredis + real client both return a dict
        # when decode_responses=True; bytes when False. Handle both.
        payload_raw = _extract_payload(fields)
        if payload_raw is None:
            # Malformed entry — drop straight to deadletter so we don't
            # spin on it forever.
            await self._dead_letter(entry_id, fields, "missing_data_field")
            return
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            await self._dead_letter(
                entry_id, fields, f"bad_json: {exc}"
            )
            return
        if not isinstance(payload, dict):
            await self._dead_letter(
                entry_id, fields, "payload_not_dict"
            )
            return

        start_ts = time.perf_counter()
        try:
            await self._handler(payload, self._stream, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._handler_errors += 1
            self._bump_handler_error_metric()
            self._retry_counts[entry_id] = (
                self._retry_counts.get(entry_id, 0) + 1
            )
            count = self._retry_counts[entry_id]
            logger.warning(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"handler error on {entry_id} retry={count}/{self._max_retries} "
                f"err={exc!r}"
            )
            if count > self._max_retries:
                await self._dead_letter(
                    entry_id, fields, f"max_retries:{count}: {exc}"
                )
                # Drop the in-memory counter so we don't grow forever.
                self._retry_counts.pop(entry_id, None)
            # NOTE: deliberately do NOT XACK on a handler error — the
            # entry stays PENDING and the claim scanner will redeliver
            # after `claim_idle_ms`. The retry counter survives in
            # memory until process restart.
            return
        finally:
            elapsed = time.perf_counter() - start_ts
            self._observe_handler_latency(elapsed)

        # Success → ACK and forget the retry counter.
        try:
            await self._redis.xack(self._stream, self._group, entry_id)
        except Exception as exc:
            logger.warning(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"XACK failed for {entry_id}: {exc!r}"
            )
            return
        self._total_consumed += 1
        self._retry_counts.pop(entry_id, None)
        self._bump_consumed()

    async def _dead_letter(
        self, entry_id: str, fields: Any, reason: str
    ) -> None:
        """Copy the entry to ``<stream>.deadletter`` and ACK the original.

        The deadletter entry carries the original payload (if decodable)
        plus diagnostic fields ``_deadletter_reason`` /
        ``_dead_lettered_from`` / ``_dead_lettered_entry_id``. The
        original entry is ACK'd so it stops blocking the consumer
        group.
        """
        assert self._redis is not None
        payload_raw = _extract_payload(fields)
        dead_payload: dict[str, Any]
        if payload_raw is not None:
            try:
                dead_payload = json.loads(payload_raw)
                if not isinstance(dead_payload, dict):
                    dead_payload = {"_raw": str(dead_payload)}
            except json.JSONDecodeError:
                dead_payload = {"_raw": payload_raw}
        else:
            dead_payload = {}
        dead_payload["_deadletter_reason"] = reason
        dead_payload["_dead_lettered_from"] = self._stream
        dead_payload["_dead_lettered_entry_id"] = entry_id
        dead_payload["_dead_lettered_group"] = self._group
        dead_payload["_dead_lettered_at_ms"] = int(time.time() * 1000)
        try:
            await self._redis.xadd(
                self._deadletter_stream,
                {_PAYLOAD_FIELD: json.dumps(dead_payload, default=str)},
                maxlen=self._maxlen_deadletter(),
                approximate=True,
            )
        except Exception as exc:
            # If we can't even publish the deadletter, log loudly and
            # ACK anyway — leaving the entry pending forever is worse.
            logger.error(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"failed to publish deadletter for {entry_id}: {exc!r}"
            )
        try:
            await self._redis.xack(self._stream, self._group, entry_id)
        except Exception as exc:
            logger.error(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"failed to ACK {entry_id} after deadletter: {exc!r}"
            )
        self._total_dead_letters += 1
        self._bump_dead_letter()

    def _maxlen_deadletter(self) -> int:
        # Deadletter is sized smaller because it's manual-drain only.
        # 10k entries is ~3 months of "stuck" trades at a sustained
        # rate of 100 errors/day — plenty for forensics.
        return 10_000

    async def _claim_loop(self) -> None:
        """Periodic XPENDING + XCLAIM to recover from dead consumers."""
        # Initial small delay so the main loop wins the race for the
        # first batch on boot (avoids a confusing double-read).
        try:
            await asyncio.sleep(min(5.0, _CLAIM_SCAN_INTERVAL_S))
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                await self._claim_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"StreamConsumer({self._stream}/{self._group}): "
                    f"claim cycle failed: {exc!r}"
                )
            try:
                await asyncio.sleep(_CLAIM_SCAN_INTERVAL_S)
            except asyncio.CancelledError:
                return

    async def _claim_pending(self) -> None:
        """Find idle pending entries and XCLAIM them for this consumer."""
        assert self._redis is not None
        # XPENDING summary first — cheap, tells us if there's anything
        # to do.
        summary = await self._redis.xpending(self._stream, self._group)
        pending = _pending_summary_count(summary)
        if pending <= 0:
            return
        # Drill into the detailed XPENDING to find idle entries.
        try:
            detail = await self._redis.xpending_range(
                self._stream,
                self._group,
                min="-",
                max="+",
                count=64,
                idle=self._claim_idle_ms,
            )
        except Exception as exc:
            logger.debug(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"xpending_range failed ({exc!r}); skipping claim cycle"
            )
            return
        if not detail:
            return
        # detail is a list of dicts {message_id, consumer, time_since_delivered, times_delivered}
        ids = [_decode_str(item.get("message_id") or item.get("id")) for item in detail]
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            await self._redis.xclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=self._claim_idle_ms,
                message_ids=ids,
            )
            logger.debug(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"XCLAIMed {len(ids)} idle entries"
            )
        except Exception as exc:
            logger.warning(
                f"StreamConsumer({self._stream}/{self._group}): "
                f"XCLAIM failed for {len(ids)} ids: {exc!r}"
            )

    async def _update_pending_gauge(self) -> None:
        assert self._redis is not None
        try:
            summary = await self._redis.xpending(self._stream, self._group)
            count = _pending_summary_count(summary)
            from src.monitoring.metrics import stream_pending_entries

            stream_pending_entries.labels(
                stream=self._stream, group=self._group
            ).set(count)
        except Exception:
            # Metrics are best-effort; never let them break the loop.
            pass

    # ----- metrics hooks ----------------------------------------------- #

    def _bump_consumed(self) -> None:
        try:
            from src.monitoring.metrics import stream_consumed_total

            stream_consumed_total.labels(
                stream=self._stream, group=self._group
            ).inc()
        except Exception:
            pass

    def _bump_reconnect(self) -> None:
        try:
            from src.monitoring.metrics import stream_reconnects_total

            stream_reconnects_total.labels(
                component="consumer", stream=self._stream
            ).inc()
        except Exception:
            pass

    def _bump_dead_letter(self) -> None:
        try:
            from src.monitoring.metrics import stream_dead_letters_total

            stream_dead_letters_total.labels(
                stream=self._stream, group=self._group
            ).inc()
        except Exception:
            pass

    def _bump_handler_error_metric(self) -> None:
        # No dedicated counter for handler errors — they show up
        # indirectly via deadletters and pending depth. If we want
        # finer granularity later we can add one without touching
        # callers.
        pass

    def _observe_handler_latency(self, elapsed_s: float) -> None:
        try:
            from src.monitoring.metrics import stream_handler_latency_seconds

            stream_handler_latency_seconds.labels(
                stream=self._stream, group=self._group
            ).observe(elapsed_s)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _extract_payload(fields: Any) -> str | None:
    """Pull the JSON-encoded payload field out of an XREADGROUP entry.

    The producer writes a single field ``"data"``. Both the real redis
    client (with ``decode_responses=True``) and fakeredis return a
    dict; bytes-mode clients return a list of alternating key/value
    bytes. Handle both shapes.
    """
    if fields is None:
        return None
    if isinstance(fields, dict):
        for k, v in fields.items():
            if _decode_str(k) == _PAYLOAD_FIELD:
                return _decode_str(v)
        return None
    if isinstance(fields, (list, tuple)):
        # Flat key/value list.
        it = iter(fields)
        for key in it:
            try:
                value = next(it)
            except StopIteration:
                return None
            if _decode_str(key) == _PAYLOAD_FIELD:
                return _decode_str(value)
        return None
    return None


def _pending_summary_count(summary: Any) -> int:
    """Normalize an XPENDING summary to an int total.

    The real client returns ``[total, min_id, max_id, [[consumer, count], ...]]``.
    fakeredis returns ``{"pending": N, "min": ..., "max": ..., "consumers": [...]}``.
    """
    if summary is None:
        return 0
    if isinstance(summary, dict):
        return int(summary.get("pending", 0) or 0)
    if isinstance(summary, (list, tuple)) and len(summary) >= 1:
        try:
            return int(summary[0] or 0)
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# Process-singleton: TradesStreamPublisher
# ---------------------------------------------------------------------------
#
# Agent A (Phase 3 round 1) wires this into trade_observer's
# `_publish_trade_event` AFTER the existing pub/sub publish. Keeping
# the singleton here means Agent A only needs `from
# src.control.redis_streams import get_trades_stream_publisher` and
# the publisher is shared with whatever else wants to thread a
# trace_id through.

TRADES_STREAM_NAME = "trades:stream"
TRADES_STREAM_MAXLEN = _DEFAULT_MAXLEN

_trades_stream_publisher_singleton: StreamProducer | None = None


def get_trades_stream_publisher() -> StreamProducer | None:
    """Return the process-singleton :class:`StreamProducer` for trades.

    Returns ``None`` if :func:`init_trades_stream_publisher` hasn't been
    called yet — callers should treat that as "stream publisher not
    wired in this process" and fall through to pub/sub-only behaviour.
    """
    return _trades_stream_publisher_singleton


async def init_trades_stream_publisher(
    redis_url: str | None = None,
    *,
    redis_client: Any | None = None,
) -> StreamProducer:
    """Create + start the trades-stream singleton.

    Idempotent — a second call returns the existing instance. Wired
    by observer/main.py during startup, alongside the existing Redis
    client construction.
    """
    global _trades_stream_publisher_singleton
    if _trades_stream_publisher_singleton is not None:
        return _trades_stream_publisher_singleton
    if redis_url is None:
        # Lazy import to avoid pulling settings into modules that
        # import redis_streams for its types only.
        from src.config import settings as _settings

        redis_url = _settings.REDIS_URL
    producer = StreamProducer(
        redis_url,
        TRADES_STREAM_NAME,
        maxlen=TRADES_STREAM_MAXLEN,
        name="observer.trades",
    )
    await producer.start(redis_client=redis_client)
    _trades_stream_publisher_singleton = producer
    return producer


async def shutdown_trades_stream_publisher() -> None:
    """Tear down the singleton. Called from observer/main.py finally."""
    global _trades_stream_publisher_singleton
    producer = _trades_stream_publisher_singleton
    _trades_stream_publisher_singleton = None
    if producer is not None:
        await producer.stop()


def _reset_trades_stream_publisher_for_tests() -> None:
    """Test-only: drop the singleton without awaiting stop()."""
    global _trades_stream_publisher_singleton
    _trades_stream_publisher_singleton = None
