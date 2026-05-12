"""Microstructure deriver daemon — Round 11 § 3.2.

Subscribes to the ``book:events:stream`` Redis Stream, runs the four
detectors via :class:`MicrostructureFeatureDeriver`, and flushes per-bucket
rollups to ``microstructure_features`` once per
``settings.MICROSTRUCTURE_ROLLUP_BUCKET_S``.

Runs under the ``polymarket-microstructure.service`` systemd unit
(400 MB envelope). The L3 firehose lives in a different daemon
(``polymarket-book-l3.service``) for blast-radius isolation per the
daemon-split principle of R6 § 3.5.
"""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging
from src.microstructure.derivers import (
    MicrostructureFeatureDeriver,
    next_bucket_boundary,
    truncate_to_bucket,
)
from src.microstructure.rollup import MicrostructureRollup
from src.observer.clob_book_observer import BookEvent, decode_ws_message


CONSUMER_GROUP = "microstructure_deriver"
CONSUMER_NAME = "deriver-1"


def _decode_stream_entry(fields: dict[str, Any]) -> BookEvent | None:
    """Reverse of :meth:`BookEvent.to_stream_payload`. The publisher
    writes ``{'data': json}``; we unpack it and rebuild a BookEvent via
    :func:`decode_ws_message` so the detectors get the canonical shape.
    """
    raw = fields.get("data") if isinstance(fields, dict) else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    # The stream payload uses 'event_time' (ISO) — decode_ws_message
    # accepts both ISO strings and numeric timestamps under the
    # 'event_time' / 'timestamp' keys.
    return decode_ws_message(payload)


class MicrostructureDaemon:
    """Composes the deriver pipeline:

      WS stream → :class:`MicrostructureFeatureDeriver` →
      per-bucket flush via :class:`MicrostructureRollup`.

    The daemon owns the bucket clock: it wakes at every bucket boundary,
    flushes the deriver, and writes the rollup. Between boundaries it
    blocks on Redis XREAD with a short timeout so it both makes progress
    and stays responsive to SIGTERM.
    """

    def __init__(
        self,
        redis_client,
        *,
        stream_name: str | None = None,
        bucket_s: int | None = None,
        rollup: MicrostructureRollup | None = None,
        deriver: MicrostructureFeatureDeriver | None = None,
    ) -> None:
        self._redis = redis_client
        self._stream_name = str(stream_name or settings.CLOB_BOOK_STREAM_NAME)
        self.bucket_s = int(
            bucket_s if bucket_s is not None else settings.MICROSTRUCTURE_ROLLUP_BUCKET_S
        )
        self.deriver = deriver or MicrostructureFeatureDeriver()
        self.rollup = rollup or MicrostructureRollup(bucket_s=self.bucket_s)

        self._running = False
        self._stop_event = asyncio.Event()
        self._last_id: str = "$"  # tail by default — production starts fresh
        self._current_bucket: datetime | None = None
        self.events_processed: int = 0
        self.buckets_flushed: int = 0

    async def _ensure_group(self) -> None:
        """Create the consumer group if it doesn't exist. We ignore the
        BUSYGROUP error that fires when the group already exists."""
        try:
            await self._redis.xgroup_create(
                self._stream_name,
                CONSUMER_GROUP,
                id="$",
                mkstream=True,
            )
        except Exception as exc:
            # BUSYGROUP is fine.
            msg = str(exc).lower()
            if "busygroup" not in msg and "already" not in msg:
                logger.debug(
                    f"MicrostructureDaemon: xgroup_create raised "
                    f"({exc!r}); proceeding (group may already exist)"
                )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        await self._ensure_group()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        # Flush any pending bucket so we don't lose the last partial
        # window on a clean shutdown.
        await self._flush_if_bucket_complete(
            datetime.now(tz=timezone.utc), force=True
        )

    async def _read_batch(self) -> list[BookEvent]:
        """One XREADGROUP cycle. Returns the decoded events (may be []
        if no messages were waiting)."""
        try:
            response = await self._redis.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {self._stream_name: ">"},
                count=500,
                block=int(self.bucket_s * 250),  # ms — ~quarter bucket
            )
        except Exception as exc:
            logger.debug(f"MicrostructureDaemon: xreadgroup failed: {exc}")
            return []
        out: list[BookEvent] = []
        if not response:
            return out
        for _stream_key, entries in response:
            for entry_id, fields in entries:
                event = _decode_stream_entry(fields)
                if event is not None:
                    out.append(event)
                try:
                    await self._redis.xack(
                        self._stream_name, CONSUMER_GROUP, entry_id
                    )
                except Exception:
                    pass
        return out

    async def _flush_if_bucket_complete(
        self, now: datetime, *, force: bool = False
    ) -> int:
        """If we've crossed a bucket boundary OR ``force`` is set, flush
        the deriver and write the rollup. Returns the row count written.
        """
        bucket = truncate_to_bucket(now, self.bucket_s)
        if self._current_bucket is None:
            self._current_bucket = bucket
            return 0
        if not force and bucket <= self._current_bucket:
            return 0
        bucket_to_write = self._current_bucket
        self._current_bucket = bucket
        snapshot = self.deriver.flush_bucket()
        n = await self.rollup.flush(bucket_to_write, snapshot)
        if n:
            self.buckets_flushed += 1
        return n

    async def run_once(self) -> int:
        """One iteration of the main loop. Returns events processed in
        this iteration. Public so tests can drive the daemon without
        spinning up the full loop."""
        events = await self._read_batch()
        for event in events:
            self.deriver.observe(event)
            self.events_processed += 1
        await self._flush_if_bucket_complete(datetime.now(tz=timezone.utc))
        return len(events)

    async def run_forever(self) -> None:
        await self.start()
        try:
            while self._running and not self._stop_event.is_set():
                await self.run_once()
        finally:
            await self.stop()


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting microstructure deriver (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(
        settings.REDIS_URL, decode_responses=True
    )

    daemon = MicrostructureDaemon(redis_client=redis_client)

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down microstructure deriver")
        stop_event.set()
        daemon._stop_event.set()  # noqa: SLF001

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
        loop.add_signal_handler(signal.SIGINT, handle_signal)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await daemon.run_forever()
    finally:
        await close_pool()
        try:
            await redis_client.aclose()
        except Exception:
            pass
        logger.info("Microstructure deriver stopped")


if __name__ == "__main__":
    asyncio.run(main())
