"""Publish :class:`LeaderIntent` events to ``mempool:leader_intent``.

Round 7 / The Front Door — § 3.4.

Wraps a :class:`src.control.redis_streams.StreamProducer` for the new
``mempool:leader_intent`` stream. Every successfully decoded intent
becomes one stream entry; the entry payload matches the contract
documented in ROUND_7_MEMPOOL_AND_PREFILL.md § 3.4::

    {
      "intent_id": "<uuid>",
      "wallet": "0x...",
      "market_id": "...",
      "token_id": "...",
      "side": "buy" | "sell",
      "size_usdc": "1234.56",
      "price": "0.6234",
      "order_type": "FOK" | "GTC" | "GTD",
      "intent_received_at_ms": 1234567890123,
      "tx_hash": "0x...",
      "nonce": 42,
      "replaces": null | "0x...",
      "expected_block": 12345678,
      "trace_id": "<uuid>",          # injected by StreamProducer
      "published_at_ms": 1234567890456,  # injected by StreamProducer
    }

The :class:`StreamProducer` injects ``trace_id`` and ``published_at_ms``
automatically — see :meth:`src.control.redis_streams.StreamProducer.publish`.
We populate the rest from the decoded :class:`LeaderIntent`.

Consumer groups
---------------
At launch (R7 Phase 7.A) the stream has TWO consumer groups:

* ``prefill_router``  — :class:`src.execution.prefill.intent_router.IntentRouter`
  (fires real or paper orders, gated by shadow mode + killswitch).
* ``paper_shadow``    — same logic but always paper-only, runs in
  parallel during the 30-day shadow soak so PnL comparisons are clean.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.4 + § 3.7 for the
full spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.tx_decoder import LeaderIntent


_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.4"


# Canonical stream name. Hardcoded here (not in settings) for the same
# reason the existing ``trades:stream`` is — these names are part of
# the public Redis-stream contract and changing them is a breaking
# event that warrants a code change.
MEMPOOL_LEADER_INTENT_STREAM: str = "mempool:leader_intent"


class LeaderIntentPublisher:
    """Async wrapper around :class:`StreamProducer` for leader intents.

    Owns one :class:`StreamProducer` instance for the lifetime of the
    daemon. The single-stream specialisation lets us:

    * Encode the :class:`LeaderIntent` → dict mapping in one place.
    * Maintain an in-process counter of total publishes for the
      ``polybot_mempool_wallet_matches_total`` metric.
    * Surface failure handling consistent with the rest of the bot:
      log the exception, count it, but DO NOT crash the subscription
      loop — losing a single mempool intent is not catastrophic.

    Wave-2 implementation outline
    -----------------------------
    1. :meth:`__init__` constructs a :class:`StreamProducer` with
       ``stream=MEMPOOL_LEADER_INTENT_STREAM``. Default maxlen is the
       producer's :data:`_DEFAULT_MAXLEN` (1_000_000) — that's
       ~11 days at 1 publish/sec which exceeds R7's design rate.

    2. :meth:`start` -> ``await self._producer.start()``.

    3. :meth:`publish` builds the payload dict from the intent and
       calls ``self._producer.publish(payload)``. The producer
       returns the assigned stream entry id; we return it for
       end-to-end tracing.

    4. :meth:`stop` -> ``await self._producer.stop()``.

    5. ``trace_id``: pass ``intent.intent_id`` as the explicit
       ``trace_id`` kwarg to :meth:`StreamProducer.publish` so the
       UUID flows through both the decision log and the trades
       stream consistently — one ID, one decision lifecycle.
    """

    def __init__(
        self,
        redis_url: str,
        stream_name: str = MEMPOOL_LEADER_INTENT_STREAM,
    ) -> None:
        """Bind to a Redis URL and (optionally) override the stream name.

        Parameters
        ----------
        redis_url
            Standard Redis connection URL — passed through to
            :class:`StreamProducer`.
        stream_name
            Override only for tests. Production callers should use
            the default :data:`MEMPOOL_LEADER_INTENT_STREAM`.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def start(self) -> None:
        """Open the underlying producer's Redis connection.

        Idempotent. Must be called before :meth:`publish`.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def stop(self) -> None:
        """Close the producer cleanly. Idempotent."""
        raise NotImplementedError(_WAVE_2_REF)

    async def publish(self, intent: "LeaderIntent") -> str:
        """Publish ``intent`` to the stream. Returns the entry id.

        Payload mapping per R7 § 3.4. ``Decimal`` fields are encoded
        as strings (the producer's ``json.dumps(..., default=str)``
        handles this automatically); the consumer parses them back
        with ``Decimal(payload["size_usdc"])`` to preserve precision.

        On failure (Redis unreachable after producer-side retry) the
        underlying :meth:`StreamProducer.publish` raises; Wave-2's
        contract is to log + count the failure (a new
        ``polybot_mempool_publish_failures_total`` counter at the
        SAME label cardinality as the other producer-failure
        counters) and re-raise so the subscription loop can decide
        whether to back off. Losing a single intent is recoverable;
        crashing the whole subscription is not.
        """
        raise NotImplementedError(_WAVE_2_REF)
