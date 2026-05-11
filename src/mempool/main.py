"""Entry point for the mempool watcher daemon.

Run as ``python -m src.mempool.main`` (or via the
``polymarket-mempool.service`` systemd unit). The process owns:

  * one :class:`src.rpc.client.RPCClient` (priority-0 = local Erigon)
  * one :class:`src.mempool.wallet_index.WatchedWalletIndex`
  * one :class:`src.mempool.tx_decoder.CLOBTxDecoder`
  * one :class:`src.mempool.event_emitter.LeaderIntentPublisher`
  * one :class:`src.mempool.node_client.NonceTracker`
  * one :class:`src.mempool.node_client.MempoolSubscription`

Mirrors the structural shape of :mod:`src.onchain.main`: build the
collaborators, wire signal handlers, run the subscription loop, drain
on SIGTERM.

Round 7 / The Front Door — § 3.1-3.4 + § 7 (Rollout Phase 7.A).
"""

from __future__ import annotations

_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 7 (Phase 7.A)"


async def main() -> None:
    """Boot the mempool watcher daemon.

    Wave-2 implementation outline
    -----------------------------
    1. Log "polymarket-mempool.service starting" with the contract
       address from settings.
    2. Build an :class:`RPCClient` via the same defensive helper used
       in :mod:`src.onchain.main` (``_build_rpc_client``). Exit non-zero
       on failure — systemd will back off.
    3. Build :class:`WatchedWalletIndex`. ``await
       index.refresh_from_universe()``. If 0 rows come back, log a
       WARNING but don't exit — the bot may be on a fresh box where
       the crawler hasn't populated wallet_universe yet; we'd rather
       run with an empty filter (no false positives, just no signal)
       than refuse to boot.
    4. Build :class:`CLOBTxDecoder` against the canonical ABI from
       :mod:`src.onchain.clob_abi`.
    5. Build :class:`LeaderIntentPublisher`; ``await publisher.start()``.
    6. Build :class:`NonceTracker`.
    7. Build :class:`MempoolSubscription(rpc, index)`.

    8. Spawn the index refresh background task::

           asyncio.create_task(index.run_refresh_loop(
               interval_s=settings.WATCHED_WALLET_INDEX_REFRESH_S
           ))

    9. Install SIGTERM / SIGINT handlers (asyncio.Event toggle), as
       in :mod:`src.onchain.main`.

    10. Main stream loop::

            async for tx in subscription.stream():
                replaced = nonce_tracker.observe(tx)
                if replaced is not None:
                    tx = replace(tx, replaces=replaced)
                intent = decoder.decode(tx)
                if intent is None:
                    continue
                try:
                    await publisher.publish(intent)
                except Exception:
                    logger.exception("LeaderIntentPublisher publish failed")

    11. On stop_event:
        * cancel the index-refresh task
        * await subscription.close()
        * await publisher.stop()
        * await rpc_client.close()
        * log "polymarket-mempool: shutdown complete"

    Open question (Wave-2)
    ----------------------
    Subscribe to ``chain:trades:stream`` (R6 listener output) in this
    process so :meth:`NonceTracker.mark_confirmed` gets called when a
    watched-wallet trade actually mines? The alternative is letting
    the tracker grow unboundedly (bounded by the 100k-entry envelope
    above), with a periodic prune of entries older than ~10 minutes
    as a safety valve. The "subscribe to chain trades" path is the
    correct long-term answer; the periodic prune is a cheap day-1
    fallback.
    """
    raise NotImplementedError(_WAVE_2_REF)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
