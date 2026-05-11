"""Subscribes to Polymarket CLOB contract events on Polygon.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.3.

Events we decode (Polymarket CTF Exchange ABI):
  - OrderFilled(maker, taker, makerAssetId, takerAssetId, ...)
  - OrderCancelled(orderHash)
  - OrdersMatched(takerOrderHash, takerOrderMaker, ...)
  - FeeRateUpdated, TradingStatusUpdated, ...

For each event:
  1. Decode against the ABI (via :class:`src.onchain.event_decoder.EventDecoder`).
  2. Resolve wallet from event topic (it's right there — no REST cross-ref).
  3. Look up market_id / token_id from event data.
  4. Publish to Redis Stream ``chain:trades:stream``.
  5. UPSERT into ``trades_observed`` (the existing table) with
     source='onchain' for cross-source dedup (migration 021).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.control.redis_streams import StreamProducer
    from src.rpc.client import RPCClient


class CLOBChainListener:
    """Subscribes to Polymarket CLOB contract events on Polygon.

    Lifecycle:
      * ``start()`` — boot from chain_sync_state cursor (or chain head
        if first run), spawn the subscription loop.
      * ``stop()`` — drain in-flight events, persist final cursor, close
        the RPC subscription cleanly.

    The class is the long-lived inhabitant of the
    ``polymarket-onchain.service`` systemd unit (see ``infra/systemd/``).
    """

    def __init__(
        self,
        rpc_client: "RPCClient",
        redis_stream_producer: "StreamProducer",
    ) -> None:
        """
        Args:
            rpc_client: Pre-configured :class:`src.rpc.client.RPCClient`
                with the provider pool loaded. The listener uses
                eth_subscribe for live events and eth_getLogs for
                catch-up on boot.
            redis_stream_producer: A :class:`StreamProducer` bound to
                ``chain:trades:stream``. Wave 2 fetches this via
                ``src.control.redis_streams.get_trades_stream_publisher``
                or constructs a dedicated one for the chain channel.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.3
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def start(self) -> None:
        """Boot the listener.

        Steps (Wave 2):
          1. Read ``chain_sync_state.last_processed_block`` (migration 022).
             If empty, fall back to ``chain_head - CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS``.
          2. Catch-up via eth_getLogs from cursor to current head; UPSERT
             rows + advance cursor in batched transactions.
          3. Spawn the live subscription loop (``_run_subscription_loop``)
             as a long-lived task.
          4. Emit ``polybot_chain_blocks_processed_total`` and
             ``polybot_chain_blocks_behind`` from the catch-up.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancel the subscription task, drain any in-flight events
        through the publisher, persist the final cursor, close the
        RPC client.

        Idempotent: safe to call from a SIGTERM handler.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def _run_subscription_loop(self) -> None:
        """The hot loop: receive raw logs from RPCClient.eth_subscribe,
        decode, publish, UPSERT.

        Pseudocode::

            async for raw in rpc_client.eth_subscribe(self._filter):
                t0 = time.monotonic()
                event = self._decoder.decode_log(raw)
                if event is None:
                    continue
                await self._publish_event(event)
                if event["block_number"] - self._last_committed_block >= BATCH_COMMIT_BLOCKS:
                    await self._update_sync_state(event["block_number"])
                metrics.chain_ingestion_latency_seconds.observe(
                    time.time() - event["block_timestamp"]
                )

        Reconnect / replay invariant: ``rpc_client.eth_subscribe`` is
        responsible for transparently reconnecting on transient
        provider drops; on a longer outage the loop exits, ``start()``
        is re-entered via the supervisor, and the catch-up phase
        re-processes from the cursor (UNIQUE INDEX makes replay safe).
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def _decode_event(self, raw_log: dict) -> dict | None:
        """Thin wrapper around :meth:`EventDecoder.decode_log` that adds:
          * Per-event-type counter increment
            (``polybot_chain_events_decoded_total{event_type}``).
          * Failed-decode counter on exception
            (``polybot_chain_events_failed_decode_total{event_type, reason}``).
          * Optional block_timestamp resolution via eth_getBlockByNumber
            when the raw log doesn't carry it (some providers omit it).

        Args:
            raw_log: Raw JSON-RPC log dict from eth_subscribe.

        Returns:
            Canonical event dict (see event_decoder module docstring),
            or None when the event isn't relevant / decode failed.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def _publish_event(self, decoded: dict) -> None:
        """Publish a decoded event downstream.

        Two destinations:
          1. ``chain:trades:stream`` — Redis Stream, for fanout to
             profile/graph/engine consumers. Uses StreamProducer.publish()
             which adds the trace_id and published_at_ms (see
             src/control/redis_streams.py).
          2. ``trades_observed`` table — Postgres UPSERT with
             ``source='onchain'``. Conflict path uses the migration-021
             partial UNIQUE INDEX on (tx_hash, log_index).

        For non-trade events (FeeRateUpdated, TradingStatusUpdated),
        only the Redis publish happens — those don't go in
        trades_observed.

        Args:
            decoded: Canonical event dict from ``_decode_event``.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def _update_sync_state(self, block_number: int) -> None:
        """Persist the cursor.

        Writes a single-row UPSERT to ``chain_sync_state`` (migration 022).
        Called from ``_run_subscription_loop`` every BATCH_COMMIT_BLOCKS
        blocks OR every BATCH_COMMIT_INTERVAL_S seconds, whichever first.

        Wave 2: this method MUST be called inside the same DB
        transaction as the trades_observed UPSERTs whose blocks are
        being committed. The cursor lying ahead of the trades it covers
        violates the resume contract.

        Args:
            block_number: The highest block whose events have been
                durably committed to trades_observed AND published to
                chain:trades:stream.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def chain_blocks_behind(self) -> int:
        """Returns ``chain_head - last_processed_block``.

        Driven by the ``polybot_chain_blocks_behind`` gauge — useful
        for the dashboard's "is the chain listener keeping up?" panel.
        Wave 2 caches the chain_head reading for ~2 s to avoid hammering
        eth_blockNumber on every scrape.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    async def __aenter__(self) -> "CLOBChainListener":
        """Async context-manager sugar — convenience for tests."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()
