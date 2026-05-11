"""Entry point for the mempool watcher daemon.

Run as ``python -m src.mempool.main`` (or via the
``polymarket-mempool.service`` systemd unit).

Round 7 / The Front Door — § 3.1-3.4 + § 7 (Rollout Phase 7.A).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from dataclasses import replace

from loguru import logger

from src.config import settings
from src.mempool.event_emitter import LeaderIntentPublisher
from src.mempool.node_client import MempoolSubscription, NonceTracker
from src.mempool.tx_decoder import CLOBTxDecoder
from src.mempool.wallet_index import WatchedWalletIndex

# Defensive metrics import — pattern matches the other R7 modules.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        mempool_tx_decoded_total,
        mempool_wallet_matches_total,
    )
except Exception:  # pragma: no cover — defensive fallback

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    mempool_tx_decoded_total = _NoOpLabel()  # type: ignore[assignment]
    mempool_wallet_matches_total = _NoOpLabel()  # type: ignore[assignment]


async def _build_rpc_client():
    """Construct the RPCClient. Mirrors :func:`src.onchain.main._build_rpc_client`.

    Returns the constructed client or raises a RuntimeError on
    failure (systemd handles restart).
    """
    try:
        from src.rpc.client import RPCClient
        from src.rpc.providers import RPCProvider  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "src.rpc client unavailable — cannot start mempool watcher"
        ) from exc
    try:
        from src.rpc.providers import ProviderPool  # type: ignore[attr-defined]

        providers = ProviderPool.from_settings().providers  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "ProviderPool.from_settings() failed ({!r}); attempting bare "
            "RPCClient construction — this will likely raise",
            exc,
        )
        providers = []
    return RPCClient(providers)


async def main() -> None:
    """Boot the mempool watcher daemon."""
    logger.info(
        "polymarket-mempool.service starting "
        "contract={}",
        settings.POLYMARKET_CLOB_CONTRACT_ADDRESS,
    )

    try:
        rpc_client = await _build_rpc_client()
    except Exception as exc:
        logger.error("polymarket-mempool: cannot start RPC client: {!r}", exc)
        sys.exit(1)

    wallet_index = WatchedWalletIndex()
    try:
        n = await wallet_index.refresh_from_universe()
        if n == 0:
            logger.warning(
                "polymarket-mempool: wallet_universe returned 0 rows — "
                "running with empty filter (no signal until crawler "
                "populates wallet_universe)"
            )
    except Exception as exc:
        logger.warning(
            "polymarket-mempool: initial wallet_index refresh failed: {!r}",
            exc,
        )

    decoder = CLOBTxDecoder()
    publisher = LeaderIntentPublisher(redis_url=settings.REDIS_URL)
    await publisher.start()

    nonce_tracker = NonceTracker()
    subscription = MempoolSubscription(rpc_client, wallet_index)

    stop_event = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        logger.info("polymarket-mempool: signal {} received, draining", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows / unusual loops — fall back to default handlers.
            pass

    # Background refresh of the wallet index.
    refresh_task = asyncio.create_task(
        wallet_index.run_refresh_loop(
            interval_s=settings.WATCHED_WALLET_INDEX_REFRESH_S
        ),
        name="mempool.wallet_index.refresh",
    )

    stream_task = asyncio.create_task(
        _run_stream_loop(subscription, decoder, publisher, nonce_tracker, wallet_index),
        name="mempool.stream",
    )

    try:
        await stop_event.wait()
    finally:
        # Cancel background tasks.
        for task in (refresh_task, stream_task):
            task.cancel()
        for task in (refresh_task, stream_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await subscription.close()
        except Exception:
            pass
        try:
            await publisher.stop()
        except Exception:
            pass
        try:
            await rpc_client.close()
        except Exception:
            pass
        logger.info("polymarket-mempool: shutdown complete")


async def _run_stream_loop(
    subscription: MempoolSubscription,
    decoder: CLOBTxDecoder,
    publisher: LeaderIntentPublisher,
    nonce_tracker: NonceTracker,
    wallet_index: WatchedWalletIndex,
) -> None:
    """Main stream loop. Extracted for test-readability."""
    try:
        async for tx in subscription.stream():
            # Defense in depth — subscription already checks but the
            # bloom may have been refreshed between yield and consume.
            if tx.from_wallet not in wallet_index:
                continue
            replaced = nonce_tracker.observe(tx)
            if replaced is not None:
                tx = replace(tx, replaces=replaced)
            intent = decoder.decode(tx)
            if intent is None:
                # Decoder already incremented not_clob / decode_failed.
                continue
            # Final liveness gate: the IntentRouter performs the same
            # check at consume time, but the publisher is the cheapest
            # place to filter — fewer obsolete intents on the stream.
            if not nonce_tracker.is_live_for(
                intent.wallet, intent.nonce, intent.tx_hash
            ):
                continue
            try:
                await publisher.publish(intent)
            except Exception:
                logger.exception("LeaderIntentPublisher publish failed")
            try:
                mempool_wallet_matches_total.inc()
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mempool: subscription stream loop ended unexpectedly")


if __name__ == "__main__":
    asyncio.run(main())
