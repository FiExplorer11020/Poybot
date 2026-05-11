"""Publish :class:`LeaderIntent` events to ``mempool:leader_intent``.

Round 7 / The Front Door — § 3.4.

Wraps a :class:`src.control.redis_streams.StreamProducer` for the new
``mempool:leader_intent`` stream. Every successfully decoded intent
becomes one stream entry; the entry payload matches the contract
documented in ROUND_7_MEMPOOL_AND_PREFILL.md § 3.4.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.4 + § 3.7 for the
full spec.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from src.control.redis_streams import StreamProducer

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.tx_decoder import LeaderIntent


# Defensive metrics import.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        mempool_tx_decoded_total,
    )
except Exception:  # pragma: no cover — defensive fallback

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    mempool_tx_decoded_total = _NoOpLabel()  # type: ignore[assignment]


# Canonical stream name. Hardcoded here (not in settings) for the same
# reason the existing ``trades:stream`` is — these names are part of
# the public Redis-stream contract and changing them is a breaking
# event that warrants a code change.
MEMPOOL_LEADER_INTENT_STREAM: str = "mempool:leader_intent"


def _to_epoch_ms(dt: datetime) -> int:
    """Serialise a ``datetime`` as integer epoch milliseconds.

    Handles both naive and timezone-aware ``datetime`` objects. Naive
    ``datetime``s are treated as UTC (matches the convention used by
    ``MempoolTx.received_at``, which is built with ``datetime.now(UTC)``).
    """
    if dt.tzinfo is None:
        # Treat naive as UTC, mirroring how MempoolTx builds it.
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _intent_to_payload(intent: "LeaderIntent") -> dict:
    """Build the wire payload for an intent.

    The :class:`StreamProducer` injects ``trace_id`` and
    ``published_at_ms`` on its own; we add ``trace_id`` here too
    (set to the intent's ``intent_id``) so the producer doesn't
    overwrite it — one UUID, one decision lifecycle.

    ``Decimal`` fields are stringified explicitly so the test
    assertions can pin the exact representation. The producer's
    ``json.dumps(..., default=str)`` would do the same fallback but
    we don't want to rely on its fallback for a publish-shape
    contract.
    """
    payload: dict = dataclasses.asdict(intent)
    # Coerce Decimals → strings for JSON portability.
    for key in ("size_usdc", "price"):
        val = payload.get(key)
        if isinstance(val, Decimal):
            payload[key] = str(val)
    # Coerce datetime → epoch ms with the documented field name.
    received_at = payload.pop("intent_received_at", None)
    if isinstance(received_at, datetime):
        payload["intent_received_at_ms"] = _to_epoch_ms(received_at)
    elif received_at is not None:
        # Already-serialised case (test fixtures sometimes pass an int).
        try:
            payload["intent_received_at_ms"] = int(received_at)
        except (TypeError, ValueError):
            payload["intent_received_at_ms"] = 0
    # Set trace_id = intent_id so the publisher and downstream
    # consumers share one correlation handle.
    intent_id = payload.get("intent_id")
    if intent_id and "trace_id" not in payload:
        payload["trace_id"] = intent_id
    return payload


class LeaderIntentPublisher:
    """Async wrapper around :class:`StreamProducer` for leader intents.

    Owns one :class:`StreamProducer` instance for the lifetime of the
    daemon.
    """

    def __init__(
        self,
        redis_url: str,
        stream_name: str = MEMPOOL_LEADER_INTENT_STREAM,
    ) -> None:
        self._stream_name = stream_name
        self._producer = StreamProducer(
            redis_url,
            stream_name,
            name=f"mempool.{stream_name}",
        )

    async def start(self, *, redis_client=None) -> None:
        """Open the underlying producer's Redis connection. Idempotent.

        ``redis_client`` is a test-only escape hatch — pass a
        ``fakeredis.aioredis.FakeRedis`` instance to bypass the URL.
        """
        await self._producer.start(redis_client=redis_client)

    async def stop(self) -> None:
        """Close the producer cleanly. Idempotent."""
        await self._producer.stop()

    async def publish(self, intent: "LeaderIntent") -> str:
        """Publish ``intent`` to the stream. Returns the entry id.

        On success, ``polybot_mempool_tx_decoded_total{result="decoded"}``
        increments. (The decoder also bumps this on a successful decode;
        publishing is the second checkpoint and tells operators "this
        intent actually made it to the stream" — a meaningful health
        signal independent of the decode path.)
        """
        payload = _intent_to_payload(intent)
        try:
            entry_id = await self._producer.publish(
                payload, trace_id=payload.get("trace_id")
            )
        except Exception:
            # Surface the failure to the caller (the subscription loop
            # decides whether to back off) but log first for ops.
            logger.exception(
                "LeaderIntentPublisher: publish failed for intent_id={}",
                getattr(intent, "intent_id", None),
            )
            raise
        try:
            mempool_tx_decoded_total.labels(result="decoded").inc()
        except Exception:
            pass
        return entry_id

    @property
    def stream_name(self) -> str:
        return self._stream_name

    @property
    def producer(self) -> StreamProducer:
        """Read-only access to the underlying producer (tests only)."""
        return self._producer
