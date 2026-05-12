"""CLOB Book L3 firehose subscriber — Round 11 (The Microscope) § 3.1.

Subscribes to Polymarket's WebSocket at maximum book granularity for the
top-N markets and captures every order-life event:

  * ``order_placed``       — new order resting on the book
  * ``order_modified``     — size/price update on an existing order
  * ``order_cancelled``    — order pulled before any fill
  * ``order_partial_fill`` — order partially executed; remainder rests
  * ``order_filled``       — order fully executed (the trade)

This is the **highest-volume** producer in the bot: ~5,000 events/sec at
peak across the top-100 markets, ~13 GB/day. The design is therefore
load-bearing:

  * Bounded :class:`asyncio.Queue` (size ``CLOB_BOOK_QUEUE_MAXSIZE``,
    default 50,000) buffers events from the WS reader to the DB writer.
  * A dedicated ``_db_writer_loop`` drains the queue in batches; under
    overload the **oldest** event is dropped (spec contract) and the
    metric ``polybot_book_events_dropped_total{reason='queue_full'}``
    is incremented.
  * Each event is also published to ``settings.CLOB_BOOK_STREAM_NAME``
    (default ``book:events:stream``) for the downstream microstructure
    deriver in :mod:`src.microstructure.derivers`.

**Wallet attribution caveat** (spec § 3.1): Polymarket's WS does NOT
include the wallet on placement / modification / cancellation events —
it's only present on fills. The observer preserves the NULL on
non-fill events; downstream readers that need wallet attribution join
with ``trades_observed`` on (tx_hash, log_index) via the R6 on-chain
reconciliation path.

The pure-Python message decoder lives in
:mod:`src.observer.clob_book_decoder` so this file stays under the
500-line module ceiling.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable

from loguru import logger

from src.config import settings
from src.database.connection import get_db

# Decoder lives in its own module for blast-radius isolation and to keep
# this file under the 500-line ceiling. Re-export the public surface so
# existing imports from ``src.observer.clob_book_observer`` keep working.
from src.observer.clob_book_decoder import (  # noqa: F401
    EVENT_CANCELLED,
    EVENT_FILLED,
    EVENT_MODIFIED,
    EVENT_PARTIAL_FILL,
    EVENT_PLACED,
    BookEvent,
    decode_ws_message,
    decode_ws_messages,
    is_known_non_event_message,
)

# --------------------------------------------------------------------------- #
# Metrics — defensive import (mirrors the R6/R7/R8 pattern).                  #
# --------------------------------------------------------------------------- #
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        book_events_dropped_total,
        book_events_received_total,
        book_queue_depth,
        book_ws_latency_seconds,
    )
except Exception:  # pragma: no cover — defensive fallback

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    book_events_received_total = _NoOpLabel()  # type: ignore[assignment]
    book_events_dropped_total = _NoOpLabel()  # type: ignore[assignment]
    book_ws_latency_seconds = _NoOpLabel()  # type: ignore[assignment]
    book_queue_depth = _NoOpLabel()  # type: ignore[assignment]


class CLOBBookObserver:
    """L3 firehose subscriber — see module docstring.

    The observer is split into three coroutines launched by :meth:`start`:

      1. ``_ws_reader_loop`` — pulls messages off the WS, decodes them,
         puts them on the bounded queue. Drops OLDEST on overflow.
      2. ``_db_writer_loop`` — drains the queue in batches and writes to
         ``clob_book_events``.
      3. ``_stream_publisher_loop`` — drains the same queue (independent
         consumer) and publishes to the Redis Stream.

    In practice the queue feeds the writer; the stream publisher reads
    from a second tee'd queue so a slow Redis doesn't backpressure the
    DB write (each consumer has its own bound). This matches the R3
    trade_observer architecture: producer → bounded queue → consumer.
    """

    def __init__(
        self,
        redis_client,  # redis.asyncio.Redis | fakeredis.aioredis.FakeRedis
        *,
        ws_factory: Callable[[Callable[[dict], Any]], Any] | None = None,
        markets: set[str] | None = None,
        queue_maxsize: int | None = None,
        db_batch_size: int | None = None,
        db_batch_interval_s: float | None = None,
        stream_name: str | None = None,
        stream_maxlen: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._ws_factory = ws_factory
        self._markets: set[str] = set(markets or set())

        self._queue_maxsize = int(
            queue_maxsize if queue_maxsize is not None else settings.CLOB_BOOK_QUEUE_MAXSIZE
        )
        self._db_batch_size = int(
            db_batch_size if db_batch_size is not None else settings.CLOB_BOOK_DB_BATCH_SIZE
        )
        self._db_batch_interval_s = float(
            db_batch_interval_s
            if db_batch_interval_s is not None
            else settings.CLOB_BOOK_DB_BATCH_INTERVAL_S
        )
        self._stream_name = str(stream_name or settings.CLOB_BOOK_STREAM_NAME)
        self._stream_maxlen = int(
            stream_maxlen if stream_maxlen is not None else settings.CLOB_BOOK_STREAM_MAXLEN
        )

        self._db_queue: deque[BookEvent] = deque(maxlen=self._queue_maxsize)
        self._stream_queue: deque[BookEvent] = deque(maxlen=self._queue_maxsize)
        self._queue_event = asyncio.Event()

        # Sprint 3: in-memory last-known resting size per
        # ``(token_id, price, side)``. The WS Market channel ships
        # absolute level sizes on ``price_change`` events, not deltas —
        # the decoder needs this cache to synthesise signed deltas.
        # Bounded implicitly by the active-markets × price-levels space
        # (~50 markets × ~200 levels × 2 sides ≈ 20k entries at worst).
        from decimal import Decimal as _Decimal

        self._level_state: dict[tuple[str, str, str], _Decimal] = {}

        self._running = False
        self._stop_event = asyncio.Event()
        self._ws_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None

        # Counters exposed for tests / introspection.
        self.events_received: int = 0
        self.events_dropped_queue_full: int = 0
        self.events_dropped_invalid: int = 0

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def update_markets(self, markets: set[str]) -> None:
        self._markets = set(markets)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        if self._ws_factory is not None:
            self._ws_task = asyncio.create_task(self._ws_reader_loop())
        # Sprint 3: `_db_writer_loop` only runs when CLOB_BOOK_PERSIST_RAW
        # is True. Default is False (rollup-only mode) — the Redis stream
        # path stays active so the microstructure deriver still gets
        # every event, but no row hits `clob_book_events`. See R11 spec
        # § 2.3 (13 GB/day raw vs ~100 MB/day rollup).
        if bool(getattr(settings, "CLOB_BOOK_PERSIST_RAW", False)):
            self._writer_task = asyncio.create_task(self._db_writer_loop())
        self._stream_task = asyncio.create_task(self._stream_publisher_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._queue_event.set()
        for task in (self._ws_task, self._writer_task, self._stream_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._ws_task = None
        self._writer_task = None
        self._stream_task = None

    async def handle_message(self, msg: dict[str, Any]) -> BookEvent | None:
        """Decode + enqueue a single WS message. Returns the FIRST
        :class:`BookEvent` produced (for backward compat with the
        single-event tests) or ``None`` when the message produced no
        events (book snapshot / control plane / malformed / queue-full).

        A single WS frame can fan out into N BookEvents
        (``price_change`` packs multiple price-level updates per
        message). All resulting events are enqueued; the return value
        is the first one for callers that need any handle to the
        emitted event. Internal tests inspect the queue directly when
        they care about the full batch.

        Public for the WS callback wiring AND for unit tests that drive
        the observer without a real WebSocket.
        """
        now_s = time.time()
        events = decode_ws_messages(msg, now_s=now_s, level_state=self._level_state)
        if not events:
            # Distinguish "valid but no delta to emit" (snapshot / ticker
            # / control plane) from "junk we should count under invalid".
            # The former is normal traffic on the firehose; counting it
            # as invalid would blow up the dropped-total metric and mask
            # real malformed payloads.
            if not is_known_non_event_message(msg):
                self.events_dropped_invalid += 1
                book_events_dropped_total.labels(reason="invalid").inc()
            return None

        first_event: BookEvent | None = None
        for event in events:
            self.events_received += 1
            try:
                book_events_received_total.labels(event_type=event.event_type).inc()
                book_ws_latency_seconds.observe(max(0.0, now_s - event.received_at))
            except Exception:  # pragma: no cover — defensive
                pass
            self._enqueue(event)
            if first_event is None:
                first_event = event
        return first_event

    # ------------------------------------------------------------------ #
    # Internal: queueing with oldest-drop semantics                       #
    # ------------------------------------------------------------------ #

    def _enqueue(self, event: BookEvent) -> None:
        """Push onto BOTH the DB queue and the stream queue. The
        :class:`collections.deque` with bounded ``maxlen`` gives us
        constant-time oldest-drop semantics for free — the spec contract.

        The drop counter is **event-level** not sink-level: one increment
        per incoming event that caused at least one sink to evict its
        oldest entry. The spec § 6 acceptance gate
        ``polybot_book_events_dropped_total{reason="queue_full"} = 0``
        is defined per ingest event, not per sink overflow.

        Sprint 3 (R11 rollup-only): when ``CLOB_BOOK_PERSIST_RAW`` is
        False (default) we don't bother feeding ``_db_queue`` — no
        writer task is running and an unbounded growth there would be
        a slow leak. The stream queue still gets the event so the
        microstructure deriver runs unchanged.
        """
        persist_raw = bool(getattr(settings, "CLOB_BOOK_PERSIST_RAW", False))
        any_dropped = False
        queues = (
            (self._db_queue, self._stream_queue) if persist_raw else (self._stream_queue,)
        )
        for q in queues:
            if self._push_with_oldest_drop(q, event):
                any_dropped = True
        if any_dropped:
            self.events_dropped_queue_full += 1
            book_events_dropped_total.labels(reason="queue_full").inc()
        try:
            # Report stream-queue depth in rollup-only mode so the gauge
            # still reflects backpressure on the active sink.
            book_queue_depth.set(
                len(self._db_queue if persist_raw else self._stream_queue)
            )
        except Exception:  # pragma: no cover
            pass
        self._queue_event.set()

    @staticmethod
    def _push_with_oldest_drop(
        queue: deque[BookEvent], event: BookEvent
    ) -> bool:
        """Append ``event`` to ``queue``; if the queue was already at
        capacity, the deque's ``maxlen`` semantics evict the OLDEST entry
        (popleft) automatically — we just observe whether eviction
        happened so we can count the drop.

        Returns True iff an eviction occurred.
        """
        evicted = len(queue) >= (queue.maxlen or 0)
        queue.append(event)
        return evicted

    # ------------------------------------------------------------------ #
    # Internal: producer + consumers                                      #
    # ------------------------------------------------------------------ #

    async def _ws_reader_loop(self) -> None:
        """Bind the WS factory's on_message callback to handle_message.

        The factory is expected to return an object with ``start()`` /
        ``stop()`` coroutines (matches :class:`PolymarketWSClient`'s
        contract). Tests pass a stub factory; production wires
        :class:`src.observer.websocket_client.PolymarketWSClient` here.
        """
        if self._ws_factory is None:
            return
        ws = self._ws_factory(self.handle_message)
        try:
            await ws.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"CLOBBookObserver WS reader crashed: {exc}")
        finally:
            try:
                await ws.stop()
            except Exception:
                pass

    async def _db_writer_loop(self) -> None:
        """Drain ``_db_queue`` in batches and write to clob_book_events.

        The writer wakes on ``_queue_event`` OR every
        ``_db_batch_interval_s``, flushes whatever is queued (up to
        ``_db_batch_size``), and goes back to sleep.
        """
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._queue_event.wait(), timeout=self._db_batch_interval_s
                )
            except asyncio.TimeoutError:
                pass
            self._queue_event.clear()
            batch = self._drain(self._db_queue, self._db_batch_size)
            if not batch:
                continue
            try:
                await self._flush_db_batch(batch)
            except Exception as exc:
                logger.warning(
                    f"CLOBBookObserver DB writer flush failed (n={len(batch)}): {exc}"
                )

    async def _stream_publisher_loop(self) -> None:
        """Drain ``_stream_queue`` and publish each event to the Redis
        Stream so the microstructure deriver can consume them in
        real-time.

        Uses ``XADD … MAXLEN ~ <N>`` for approximate trim — keeps the
        stream bounded without paying the exact-trim cost per write.
        """
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._queue_event.wait(), timeout=self._db_batch_interval_s
                )
            except asyncio.TimeoutError:
                pass
            batch = self._drain(self._stream_queue, self._db_batch_size)
            if not batch:
                continue
            if self._redis is None:
                continue
            for event in batch:
                try:
                    payload = event.to_stream_payload()
                    await self._redis.xadd(
                        self._stream_name,
                        {"data": json.dumps(payload, default=str)},
                        maxlen=self._stream_maxlen,
                        approximate=True,
                    )
                except Exception as exc:
                    logger.debug(
                        f"CLOBBookObserver: xadd to {self._stream_name} failed: {exc}"
                    )

    @staticmethod
    def _drain(queue: deque[BookEvent], max_items: int) -> list[BookEvent]:
        n = min(len(queue), max(1, int(max_items)))
        if n <= 0:
            return []
        batch: list[BookEvent] = []
        for _ in range(n):
            try:
                batch.append(queue.popleft())
            except IndexError:
                break
        return batch

    async def _flush_db_batch(self, batch: list[BookEvent]) -> None:
        """Single-roundtrip INSERT of the entire batch into
        clob_book_events. The unique index pattern of trades_observed
        doesn't apply here — every order event is naturally distinct
        by (event_id, event_time). We let the BIGSERIAL generate event_id.
        """
        rows = [
            (
                e.event_time,
                e.market_id,
                e.token_id,
                e.event_type,
                e.side,
                e.price,
                e.size_delta,
                e.order_hash,
                e.wallet_address,
                e.source,
                json.dumps(e.raw_payload) if e.raw_payload is not None else None,
            )
            for e in batch
        ]
        async with get_db() as conn:
            await conn.executemany(
                """
                INSERT INTO clob_book_events
                    (event_time, market_id, token_id, event_type, side,
                     price, size_delta, order_hash, wallet_address,
                     source, raw_payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                """,
                rows,
            )

    # ------------------------------------------------------------------ #
    # Test affordances                                                    #
    # ------------------------------------------------------------------ #

    def queue_depth(self) -> int:
        return len(self._db_queue)

    def stream_queue_depth(self) -> int:
        return len(self._stream_queue)

    async def _drain_stream_for_test(self) -> list[BookEvent]:
        """Pull everything currently in the stream queue without
        publishing. Used by unit tests that don't want to spin up the
        full publisher loop."""
        return self._drain(self._stream_queue, self._queue_maxsize)
