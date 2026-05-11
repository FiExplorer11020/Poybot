"""Erigon mempool subscription + per-wallet nonce-chain tracking.

Round 7 / The Front Door — § 3.1.

This module is the FIRST hop in the pre-confirmation pipeline. It owns
the long-lived ``eth_subscribe('newPendingTransactions', {fromAddress:
[...]})`` connection against the local Erigon node (which supports the
filtered-subscription extension that public providers don't expose) and
yields decoded :class:`MempoolTx` objects to the downstream decoder.

Two collaborating primitives:

* :class:`MempoolSubscription` wraps :meth:`src.rpc.client.RPCClient.eth_subscribe`
  with a Polymarket-specific protocol: the subscription filter is
  populated from a :class:`src.mempool.wallet_index.WatchedWalletIndex`
  so we never decode tx from wallets we don't care about. On
  disconnect, the underlying ``RPCClient.eth_subscribe`` bounded-backoff
  reconnects; this class re-asserts the filter on every reconnect.

* :class:`NonceTracker` is the replacement-chain detector. The leader
  can re-broadcast a tx with a higher gas price (same wallet, same
  nonce) before the original is mined — only the LAST tx in the
  per-(wallet, nonce) chain actually executes. If we fire against an
  obsolete tx we trade against stale intent. NonceTracker keeps a
  per-wallet ``{nonce -> latest_tx_hash}`` map and returns the
  replaced-hash on every observe so the publisher can emit a
  ``replaces`` field downstream.

Wave-2 will own the *implementations*. Wave-1 ships the type contracts
and the docstring detail required to fill them in.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.1 for the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, AsyncIterator, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.wallet_index import WatchedWalletIndex
    from src.rpc.client import RPCClient


_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.1"


@dataclass(slots=True)
class MempoolTx:
    """One decoded raw mempool transaction.

    This is the PRE-Polymarket-specific shape: we have a wallet, a
    target contract, a gas envelope, and the calldata bytes — but we
    have NOT yet decoded the calldata against the CLOB ABI. That's the
    job of :class:`src.mempool.tx_decoder.CLOBTxDecoder`.

    Attributes
    ----------
    tx_hash
        The 0x-prefixed lowercase transaction hash. Stable identifier
        used for nonce-chain bookkeeping and downstream correlation.
    from_wallet
        EOA that signed the tx, 0x-prefixed lowercase. Matched against
        the :class:`WatchedWalletIndex` membership test in the
        subscription hot path.
    to_contract
        The target contract address (the Polymarket CTF Exchange when
        we care; anything else is filtered out by the decoder).
    gas_price
        Effective gas price in wei. NOTE: for EIP-1559 tx this is the
        max-fee-per-gas; we don't model maxPriorityFee separately
        because for the nonce-chain check we only need monotonic
        ordering.
    gas_limit
        Tx-declared gas limit. Useful for sanity checks (a wildly
        oversized gas_limit hints at a non-CLOB call).
    nonce
        Transaction nonce from the wallet. Key for replacement-chain
        detection — see :class:`NonceTracker`.
    calldata
        Raw input bytes (the function selector + ABI-encoded args).
        The decoder slices this against the CLOB function ABI.
    received_at
        Wall-clock time the subscription handed us the tx. Used as
        ``intent_received_at`` downstream and for end-to-end latency
        metrics (``polybot_intent_router_latency_seconds``).
    replaces
        OPTIONAL: tx_hash of the tx this one replaces in the
        (wallet, nonce) chain. Populated by NonceTracker AFTER the raw
        :class:`MempoolTx` is constructed; the subscription itself
        produces ``None`` here.
    """

    tx_hash: str
    from_wallet: str
    to_contract: str
    gas_price: int
    gas_limit: int
    nonce: int
    calldata: bytes
    received_at: datetime
    replaces: Optional[str] = None


class MempoolSubscription:
    """Erigon filtered-subscription wrapper.

    Subscribes to::

        eth_subscribe(
            'newPendingTransactions',
            {fromAddress: [watched leader wallet addresses]},
        )

    Erigon supports the filtered-subscription extension that the
    public RPC providers (Alchemy, QuickNode) do not. We deliberately
    target the LOCAL Erigon endpoint here (priority-0 provider in the
    pool, see ``src.rpc.providers``). If only paid providers are
    available the subscription will degrade to "full firehose"
    (~1000 tx/sec public mempool) and the in-process
    :class:`src.mempool.wallet_index.WatchedWalletIndex` becomes the
    only filter — bloom check is ~50ns so it's still tractable.

    With the watched-address filter active we expect ~10-100 tx/sec at
    steady state across the ~2000 watched wallets.

    Reconnect contract
    ------------------
    The underlying :meth:`src.rpc.client.RPCClient.eth_subscribe` runs
    its own bounded-backoff reconnect (1s → 30s cap). On every
    reconnect we re-issue the subscription with the *current* watched
    wallet list (the index may have grown). The reconnect itself is
    transparent to the consumer of :meth:`stream`.

    Lifecycle
    ---------
    The subscription is started lazily on the first ``async for``
    iteration. :meth:`close` cancels the in-flight subscription and
    is safe to call multiple times.
    """

    def __init__(
        self,
        rpc_client: "RPCClient",
        wallet_index: "WatchedWalletIndex",
    ) -> None:
        """Bind to an RPCClient + the bloom-filter wallet membership index.

        Parameters
        ----------
        rpc_client
            Configured :class:`src.rpc.client.RPCClient`. The
            subscription will pick the first provider with a ``ws_url``
            (the local Erigon by priority); fall-through to paid
            providers happens at the RPCClient layer.
        wallet_index
            :class:`src.mempool.wallet_index.WatchedWalletIndex`
            instance — the subscription reads
            ``wallet_index.snapshot_addresses()`` to build the
            ``fromAddress`` filter each time it opens / reopens the
            socket.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def stream(self) -> AsyncIterator[MempoolTx]:
        """Yield decoded :class:`MempoolTx` objects forever.

        Wave-2 implementation outline:

        1. Compose the eth_subscribe payload using
           ``wallet_index.snapshot_addresses()``.
        2. Open the subscription via ``rpc_client.eth_subscribe(...)``.
        3. For every raw JSON envelope yielded by the RPC client:

           a. Decode the tx fields into :class:`MempoolTx`. The raw
              fields Erigon sends are ``hash``, ``from``, ``to``,
              ``gasPrice`` (hex), ``gas`` (hex), ``nonce`` (hex),
              ``input`` (hex).
           b. Skip if ``from_wallet not in wallet_index`` (defense in
              depth — Erigon's filter is authoritative but we
              double-check).
           c. Increment ``polybot_mempool_tx_received_total{source}``.
           d. Increment ``polybot_mempool_wallet_matches_total`` if
              the address is in the index.
           e. ``yield`` the :class:`MempoolTx`.

        4. On the underlying WS reconnect (handled by RPCClient), the
           ``async for`` simply pauses, no per-message action needed.

        Note
        ----
        This method does NOT decode the calldata against the CLOB ABI
        — that's a downstream concern handled by
        :class:`src.mempool.tx_decoder.CLOBTxDecoder`. Keeping the two
        stages separate lets us measure ``tx_received_total`` vs
        ``tx_decoded_total{result}`` and diagnose decode-failure
        spikes that hint at contract upgrades.
        """
        raise NotImplementedError(_WAVE_2_REF)
        # The yield-in-unreachable trick keeps mypy and the runtime
        # both happy: Python recognises this as an async generator
        # because of the unreachable ``yield`` below.
        if False:  # pragma: no cover — type-only
            yield  # type: ignore[misc]

    async def close(self) -> None:
        """Cancel the in-flight subscription and release resources.

        Idempotent. Does NOT close the underlying ``RPCClient`` (that's
        owned by the daemon entrypoint and may be shared with other
        subscribers in the future).
        """
        raise NotImplementedError(_WAVE_2_REF)


class NonceTracker:
    """Per-wallet nonce-chain tracker for tx replacements.

    Polygon EOAs broadcast tx with strictly-increasing nonces; the
    network accepts a second tx with the SAME nonce only if it offers
    a higher gas price (typically +10%). The original is then evicted
    from validators' mempools and will never mine. We see both in our
    own mempool subscription, in arrival order, and need to mark the
    earlier one as "obsolete" so the IntentRouter doesn't fire against
    stale intent.

    State
    -----
    ``_chains: dict[wallet, dict[nonce, tx_hash]]``

    Per-wallet, per-nonce we hold the LATEST seen tx_hash. When a new
    tx arrives we return the displaced hash (or ``None`` if this is
    the first sighting at this nonce).

    Eviction
    --------
    On :meth:`mark_confirmed(wallet, nonce)` we drop all entries
    ``<= nonce`` for the wallet — those are settled and can never be
    replaced. Without confirmation feedback the per-wallet dict would
    grow unboundedly across a long session; the daemon's nonce-
    confirmation feed (from the on-chain listener, R6's
    ``chain:trades:stream``) provides the eviction signal.

    Memory bound
    ------------
    Worst case: ~2000 wallets × ~50 in-flight nonces ≈ 100k entries.
    At ~150 bytes per dict entry (wallet, nonce, tx_hash + Python
    overhead) that's ~15 MB. Comfortable inside the daemon's 300 MB
    budget.

    Concurrency
    -----------
    All public methods are SYNCHRONOUS. The caller (subscription loop)
    is single-async-task; if Wave-3 introduces a worker pool we'll
    revisit with an asyncio.Lock.
    """

    def __init__(self) -> None:
        """Empty tracker. State lives only in process memory."""
        raise NotImplementedError(_WAVE_2_REF)

    def observe(self, tx: MempoolTx) -> Optional[str]:
        """Record ``tx`` in the chain for ``(tx.from_wallet, tx.nonce)``.

        Returns the tx_hash of the tx this one REPLACES, or ``None``
        if this is a fresh entry (first sighting of the nonce, OR a
        re-sighting of the same tx_hash that's already the head of
        the chain).

        Hook for ``polybot_mempool_replacement_chain_length`` histogram:
        Wave-2 should keep a parallel counter of total observations
        per (wallet, nonce) so the chain length can be reported when
        the nonce eventually confirms (via :meth:`mark_confirmed`).
        """
        raise NotImplementedError(_WAVE_2_REF)

    def mark_confirmed(self, wallet: str, nonce: int) -> None:
        """Drop all entries ``<= nonce`` for ``wallet``.

        Called by the daemon when the on-chain listener reports a
        wallet-attributed trade — that trade's tx has been mined, so
        every same-or-lower nonce for that wallet is settled. The
        chain-length histogram is observed here (the length of the
        purged dict is the answer for the confirmed nonce).
        """
        raise NotImplementedError(_WAVE_2_REF)

    def is_live_for(self, wallet: str, nonce: int, tx_hash: str) -> bool:
        """Return True iff ``tx_hash`` is the CURRENT head of the
        (wallet, nonce) chain.

        Used by the IntentRouter as a final defence: if a replacement
        landed in our mempool AFTER the LeaderIntentPublisher emitted
        but BEFORE the router consumed the stream entry, we still want
        to refuse to fire. The router calls this with the intent's
        recorded (wallet, nonce, tx_hash) just before issuing the
        pre-signed order.
        """
        raise NotImplementedError(_WAVE_2_REF)
