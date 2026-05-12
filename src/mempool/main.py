"""Entry point for the mempool watcher daemon.

Run as ``python -m src.mempool.main`` (or via the
``polymarket-mempool.service`` systemd unit).

Round 7 / The Front Door — § 3.1-3.4 + § 7 (Rollout Phase 7.A).

Sprint 3.5 — § 4 Décision #5 (re-aim to Polymarket WS via the
observer's ``trades:observed`` pub/sub channel). The daemon branches
on ``settings.MEMPOOL_SUBSCRIPTION_MODE``:

* ``erigon`` (legacy)  — build the RPCClient and the
  :class:`src.mempool.node_client.MempoolSubscription` against the
  local Erigon node. Decoder + nonce-tracker + publisher pipeline as
  shipped in Wave-1 R7.
* ``polymarket_ws_proxy`` (default) — open a dedicated Redis client
  and feed the :class:`src.mempool.node_client.LeaderTradeSubscription`
  with the wallet index. The CLOB ABI decoder is BYPASSED in this
  mode (synthetic tx have empty calldata); the daemon builds the
  :class:`LeaderIntent` directly from the original ``trades:observed``
  payload stashed on :attr:`MempoolTx.source_payload`.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import AsyncIterator, Optional

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.mempool.event_emitter import LeaderIntentPublisher
from src.mempool.node_client import (
    LeaderTradeSubscription,
    MempoolSubscription,
    MempoolTx,
    NonceTracker,
)
from src.mempool.tx_decoder import CLOBTxDecoder, LeaderIntent
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
    mode = settings.MEMPOOL_SUBSCRIPTION_MODE
    logger.info(
        "polymarket-mempool.service starting "
        "mode={} contract={}",
        mode,
        settings.POLYMARKET_CLOB_CONTRACT_ADDRESS,
    )

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

    # Build the subscription according to the configured mode.
    rpc_client = None
    proxy_redis = None
    if mode == "erigon":
        try:
            rpc_client = await _build_rpc_client()
        except Exception as exc:
            logger.error(
                "polymarket-mempool: cannot start RPC client: {!r}", exc
            )
            sys.exit(1)
        subscription = MempoolSubscription(rpc_client, wallet_index)
    elif mode == "polymarket_ws_proxy":
        # Dedicated Redis client for the pub/sub session — disjoint
        # from the publisher's client so a pubsub disconnect doesn't
        # take down the publish path.
        proxy_redis = redis_async.from_url(
            settings.REDIS_URL, decode_responses=True
        )
        subscription = LeaderTradeSubscription(
            proxy_redis,
            wallet_index,
            clob_contract=settings.POLYMARKET_CLOB_CONTRACT_ADDRESS,
        )
    else:
        # Validator above guards against this, but defensive anyway.
        logger.error(
            "polymarket-mempool: unknown MEMPOOL_SUBSCRIPTION_MODE={!r}", mode
        )
        sys.exit(1)

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
        _run_stream_loop(
            subscription, decoder, publisher, nonce_tracker, wallet_index, mode
        ),
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
        if rpc_client is not None:
            try:
                await rpc_client.close()
            except Exception:
                pass
        if proxy_redis is not None:
            try:
                await proxy_redis.aclose()
            except Exception:
                pass
        logger.info("polymarket-mempool: shutdown complete")


def _to_decimal(value: object) -> Decimal:
    """Coerce a string / number / None into Decimal, defaulting to 0."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _payload_to_leader_intent(tx: MempoolTx) -> Optional[LeaderIntent]:
    """Build a :class:`LeaderIntent` from a proxy-path synthetic
    :class:`MempoolTx`.

    Returns ``None`` if the source payload is missing or malformed —
    the daemon then skips publish for this tx. The decoder is NOT
    consulted; the observer's trade payload is the source of truth
    for side / size / price / market / token.
    """
    payload = tx.source_payload
    if not isinstance(payload, dict):
        return None
    market_id = payload.get("market_id")
    token_id = payload.get("token_id")
    side_raw = payload.get("side")
    wallet = payload.get("wallet_address")
    if not (
        isinstance(market_id, str)
        and isinstance(token_id, str)
        and isinstance(side_raw, str)
        and isinstance(wallet, str)
    ):
        return None
    side = side_raw.lower()
    if side not in ("buy", "sell"):
        return None
    return LeaderIntent(
        intent_id=str(uuid.uuid4()),
        wallet=wallet.lower() if isinstance(wallet, str) else wallet,
        market_id=market_id,
        token_id=token_id,
        side=side,  # type: ignore[arg-type]
        size_usdc=_to_decimal(payload.get("size_usdc")),
        price=_to_decimal(payload.get("price")),
        order_type="GTC",
        intent_received_at=tx.received_at,
        expected_block=0,  # router resolves at consume time
        tx_hash=tx.tx_hash,
        nonce=tx.nonce,
        replaces=None,
    )


async def _run_stream_loop(
    subscription,
    decoder: CLOBTxDecoder,
    publisher: LeaderIntentPublisher,
    nonce_tracker: NonceTracker,
    wallet_index: WatchedWalletIndex,
    mode: str,
) -> None:
    """Main stream loop. Extracted for test-readability.

    Behaviour depends on ``mode``:

    * ``erigon``                — decoder + nonce-tracker live-gate
                                  pipeline as shipped in R7 Wave-1.
    * ``polymarket_ws_proxy``   — skip the ABI decoder (calldata is
                                  empty); build the LeaderIntent
                                  directly from the observer's
                                  source payload.
    """
    is_proxy = mode == "polymarket_ws_proxy"
    try:
        async for tx in subscription.stream():
            # Defense in depth — subscription already checks but the
            # bloom may have been refreshed between yield and consume.
            if tx.from_wallet not in wallet_index:
                continue
            if is_proxy:
                # Proxy path: bypass decoder + nonce-tracker. Synthetic
                # tx have zero nonces so the replacement-chain logic
                # would degenerate to "always same nonce, always
                # replace". The observer's dedup already guarantees we
                # don't see the same trade twice.
                intent = _payload_to_leader_intent(tx)
                if intent is None:
                    continue
                try:
                    mempool_tx_decoded_total.labels(result="decoded").inc()
                except Exception:
                    pass
            else:
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
