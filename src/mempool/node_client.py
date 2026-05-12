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
  per-wallet ``{nonce -> [tx_hashes]}`` chain (so we can report
  replacement-chain length on confirm) and returns the replaced-hash on
  every observe so the publisher can emit a ``replaces`` field
  downstream.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.1 for the spec.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.wallet_index import WatchedWalletIndex
    from src.rpc.client import RPCClient


# Defensive metrics import — keep tests happy in checkouts without
# prometheus_client by falling back to no-op shims.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        mempool_replacement_chain_length,
        mempool_tx_received_total,
        mempool_wallet_matches_total,
    )
except Exception:  # pragma: no cover — defensive fallback

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

    mempool_replacement_chain_length = _NoOpLabel()  # type: ignore[assignment]
    mempool_tx_received_total = _NoOpLabel()  # type: ignore[assignment]
    mempool_wallet_matches_total = _NoOpLabel()  # type: ignore[assignment]


# How long an unconfirmed nonce-chain can sit in memory before we
# evict it. Tx that's been waiting > 30 s has almost certainly been
# dropped from validator mempools (Polygon block time is ~2 s). The
# eviction is a memory-bound; the mark_confirmed path is the
# authoritative signal.
_CHAIN_MAX_AGE_S: float = 30.0


@dataclass(slots=True)
class MempoolTx:
    """One decoded raw mempool transaction.

    This is the PRE-Polymarket-specific shape: we have a wallet, a
    target contract, a gas envelope, and the calldata bytes — but we
    have NOT yet decoded the calldata against the CLOB ABI. That's the
    job of :class:`src.mempool.tx_decoder.CLOBTxDecoder`.

    See module docstring for the field contract; the Wave-1 architect
    docstring captures the per-field rationale.

    Sprint 3.5 proxy-path note
    --------------------------
    When the daemon runs in ``polymarket_ws_proxy`` mode
    (:class:`LeaderTradeSubscription`), the tx is SYNTHETIC: there is
    no on-chain transaction behind it, ``calldata`` is empty and
    gas/nonce fields are zero. The original trade payload from the
    observer's ``trades:observed`` channel is stashed on
    ``source_payload`` so the daemon can short-circuit the
    :class:`src.mempool.tx_decoder.CLOBTxDecoder` step (which requires
    real CLOB calldata) and build the :class:`LeaderIntent` directly.
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
    # Proxy-mode payload (None for the legacy Erigon path). Holds the
    # JSON-decoded ``trades:observed`` event so downstream code can
    # build a :class:`LeaderIntent` without going through the ABI
    # decoder. Backwards-compatible default — existing call sites
    # (Erigon path, tx_decoder, tests) don't have to change.
    source_payload: Optional[dict] = None


def _hex_to_int(value: object) -> int:
    """Coerce a JSON-RPC int field (``"0x..."`` hex or plain int).

    Returns 0 for missing / un-parseable inputs; the caller decides
    whether 0 is a meaningful signal (a 0-nonce tx is real; a 0
    gas_price isn't).
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        try:
            if s.lower().startswith("0x"):
                return int(s, 16)
            return int(s)
        except ValueError:
            return 0
    return 0


def _hex_to_bytes(value: object) -> bytes:
    """Coerce a JSON-RPC ``0x``-prefixed hex string to bytes."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return b""
        if s.lower().startswith("0x"):
            s = s[2:]
        try:
            return bytes.fromhex(s)
        except ValueError:
            return b""
    return b""


def _normalize_address(value: object) -> str:
    """Lowercase + 0x-prefix-normalise an address string."""
    if not isinstance(value, str):
        return ""
    s = value.strip().lower()
    if not s:
        return ""
    if not s.startswith("0x"):
        s = "0x" + s
    return s


def _normalize_tx_hash(value: object) -> str:
    """Same shape as ``_normalize_address`` but conceptually a 32-byte
    hash. Kept separate for grep-ability."""
    return _normalize_address(value)


def _raw_tx_to_mempool_tx(raw: dict) -> Optional[MempoolTx]:
    """Convert a raw JSON-RPC tx dict into :class:`MempoolTx`.

    Returns ``None`` if mandatory fields (hash, from) are missing.
    """
    if not isinstance(raw, dict):
        return None
    tx_hash = _normalize_tx_hash(raw.get("hash"))
    from_wallet = _normalize_address(raw.get("from"))
    if not tx_hash or not from_wallet:
        return None
    # For EIP-1559 tx the field is ``maxFeePerGas``; legacy uses
    # ``gasPrice``. Either is monotonic enough for our nonce-chain
    # ordering check.
    gas_price = _hex_to_int(raw.get("gasPrice") or raw.get("maxFeePerGas"))
    return MempoolTx(
        tx_hash=tx_hash,
        from_wallet=from_wallet,
        to_contract=_normalize_address(raw.get("to")),
        gas_price=gas_price,
        gas_limit=_hex_to_int(raw.get("gas")),
        nonce=_hex_to_int(raw.get("nonce")),
        calldata=_hex_to_bytes(raw.get("input")),
        received_at=datetime.now(timezone.utc),
        replaces=None,
    )


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
    only filter.

    Lifecycle
    ---------
    The subscription is started lazily on the first ``async for``
    iteration. :meth:`close` flips an internal flag that the iterator
    checks on every wake; safe to call multiple times.
    """

    def __init__(
        self,
        rpc_client: "RPCClient",
        wallet_index: "WatchedWalletIndex",
    ) -> None:
        self._rpc = rpc_client
        self._wallet_index = wallet_index
        self._closed = False

    async def stream(self) -> AsyncIterator[MempoolTx]:
        """Yield decoded :class:`MempoolTx` objects.

        The Erigon ``newPendingTransactions`` subscription yields tx
        HASHES (not full tx bodies). We hydrate each with
        ``eth_getTransactionByHash`` before yielding.

        Per-tx exceptions (malformed payload, RPC blip while
        hydrating, etc.) are caught and logged at DEBUG — one bad tx
        must not stop the stream. The metric label is
        ``polybot_mempool_tx_received_total{source}`` (``source`` is
        the underlying provider; we record ``erigon`` here as the
        canonical local case).
        """
        # Build the subscription filter from the index snapshot. The
        # filter is sent as a list of addresses; Erigon accepts the
        # ``fromAddress`` form per its filtered-subscription docs.
        addresses = self._wallet_index.snapshot_addresses()
        filter_obj: dict = {}
        if addresses:
            filter_obj = {"fromAddress": list(addresses)}

        async for raw in self._rpc.eth_subscribe(
            filter_obj, subscription_type="newPendingTransactions"
        ):
            if self._closed:
                return
            try:
                # Erigon yields tx HASHES for newPendingTransactions;
                # some providers yield full tx objects directly.
                # Handle both shapes defensively.
                if isinstance(raw, str):
                    tx_hash = _normalize_tx_hash(raw)
                    if not tx_hash:
                        continue
                    full = await self._rpc.eth_getTransactionByHash(tx_hash)
                    if full is None:
                        # Tx has already been replaced / dropped from
                        # the node's view. Skip silently — this is
                        # normal under load.
                        continue
                    tx = _raw_tx_to_mempool_tx(full)
                elif isinstance(raw, dict):
                    tx = _raw_tx_to_mempool_tx(raw)
                else:
                    continue
                if tx is None:
                    continue
                try:
                    mempool_tx_received_total.labels(source="erigon").inc()
                except Exception:
                    pass
                # Bloom defense-in-depth: skip tx whose from-wallet
                # isn't in our watch set even if Erigon's filter
                # missed it.
                if tx.from_wallet not in self._wallet_index:
                    continue
                try:
                    mempool_wallet_matches_total.inc()
                except Exception:
                    pass
                yield tx
            except Exception as exc:
                # Catch ALL per-tx errors so one bad payload doesn't
                # tear down the stream. The subscription health is
                # paramount — we'd rather miss one intent than miss
                # every intent for the next 30 s.
                logger.debug(
                    "MempoolSubscription: per-tx error swallowed: {!r}", exc
                )
                continue

    async def close(self) -> None:
        """Cancel the in-flight subscription. Idempotent."""
        self._closed = True


class NonceTracker:
    """Per-wallet nonce-chain tracker for tx replacements.

    State
    -----
    ``_chains: dict[(wallet, nonce), list[(tx_hash, observed_at_s)]]``

    Per-wallet, per-nonce we keep the FULL list of tx hashes we've
    seen in arrival order. The LAST entry is the "live" tx; older
    ones are obsolete (replaced). We retain the full history so the
    replacement-chain-length histogram can report the count on
    :meth:`mark_confirmed`.

    Eviction
    --------
    On :meth:`mark_confirmed(wallet, nonce)` we drop the chain
    entirely. As a memory-bound safety valve we also evict chains
    whose youngest observation is older than ``_CHAIN_MAX_AGE_S``
    (~30 s) on every observe — tx that have been waiting that long
    have almost certainly been dropped from validator mempools.
    """

    def __init__(self) -> None:
        # Key: (wallet, nonce). Value: list of (tx_hash, observed_at).
        # observed_at is a monotonic seconds float, used for the age
        # eviction check only — not for ordering (arrival order in
        # the list is the truth).
        self._chains: dict[tuple[str, int], list[tuple[str, float]]] = {}

    def observe(self, tx: MempoolTx) -> Optional[str]:
        """Record ``tx`` in the chain for ``(tx.from_wallet, tx.nonce)``.

        Returns the tx_hash of the tx this one REPLACES, or ``None``
        if this is a fresh entry (first sighting of the nonce, OR a
        re-sighting of the same tx_hash that's already the head of
        the chain).
        """
        # Opportunistic age-based prune. Cheap (one walk over keys);
        # caps in-memory state at ``_CHAIN_MAX_AGE_S`` worth of
        # entries when ``mark_confirmed`` isn't being called.
        self._prune_stale()

        key = (tx.from_wallet, tx.nonce)
        now = time.monotonic()
        chain = self._chains.get(key)
        if chain is None:
            self._chains[key] = [(tx.tx_hash, now)]
            return None

        # Same tx hash re-seen → no replacement signal.
        head_hash = chain[-1][0] if chain else None
        if head_hash == tx.tx_hash:
            # Refresh the timestamp so we don't prematurely prune a
            # tx still being broadcast.
            chain[-1] = (head_hash, now)
            return None

        # A genuine replacement: the previous head is now obsolete.
        chain.append((tx.tx_hash, now))
        return head_hash

    def mark_confirmed(self, wallet: str, nonce: int) -> None:
        """Drop the chain for ``(wallet, nonce)``.

        Records the chain length to the histogram before purging so
        operators can see the gas-war fingerprint.
        """
        key = (_normalize_address(wallet), int(nonce))
        chain = self._chains.pop(key, None)
        # Also clear the un-normalized variant in case the caller
        # passed a checksummed address. observe() normalizes via the
        # MempoolTx.from_wallet field; mark_confirmed is called from
        # the on-chain listener which may pass either case.
        if chain is None:
            chain = self._chains.pop((wallet, nonce), None)
        if chain is None:
            return
        try:
            mempool_replacement_chain_length.observe(len(chain))
        except Exception:
            pass

    def is_live_for(self, wallet: str, nonce: int, tx_hash: str) -> bool:
        """Return True iff ``tx_hash`` is the CURRENT head of the
        (wallet, nonce) chain.

        Used by the IntentRouter as a final defence: if a replacement
        landed in our mempool AFTER the LeaderIntentPublisher emitted
        but BEFORE the router consumed the stream entry, we still
        want to refuse to fire.
        """
        key = (_normalize_address(wallet), int(nonce))
        chain = self._chains.get(key)
        if chain is None:
            chain = self._chains.get((wallet, nonce))
        if not chain:
            return False
        return chain[-1][0] == _normalize_tx_hash(tx_hash)

    def _prune_stale(self) -> None:
        """Drop chains whose youngest entry is older than
        ``_CHAIN_MAX_AGE_S``."""
        if not self._chains:
            return
        cutoff = time.monotonic() - _CHAIN_MAX_AGE_S
        to_drop: list[tuple[str, int]] = []
        for key, chain in self._chains.items():
            if not chain:
                to_drop.append(key)
                continue
            youngest = chain[-1][1]
            if youngest < cutoff:
                to_drop.append(key)
        for key in to_drop:
            self._chains.pop(key, None)


# ---------------------------------------------------------------------------
# Sprint 3.5 — Polymarket WS proxy subscription
# ---------------------------------------------------------------------------
#
# Decision rationale (recorded in docs/EXECUTION_PLAN_2026_05_12.md § 4
# Décision #5): Polymarket CLOB matching is OFF-CHAIN, so the Polygon
# mempool carries NO trade-intent transactions for us to subscribe to.
# Erigon's ``eth_subscribe('newPendingTransactions')`` (the
# :class:`MempoolSubscription` path above) is therefore moot in
# production. We don't even run Erigon on Hetzner.
#
# We re-aim the mempool daemon at the observer's existing
# ``trades:observed`` Redis pub/sub channel: every CONFIRMED trade
# observed via the WS+REST path is already published there with the
# ``is_leader`` flag pre-attributed. The mempool daemon becomes a
# downstream filter (leader-only, watched-wallet-only) that re-emits
# the trade as a synthetic :class:`MempoolTx` so the rest of the
# pipeline (decoder bypass + LeaderIntent publisher + IntentRouter)
# can stay unchanged.
#
# Trade-off: we lose the pre-confirmation latency edge (we now see
# trades AFTER they confirm, not before). This is acceptable because
# off-chain CLOB matching means the "pre-confirmation" alpha was
# illusory anyway — the trade is matched and final the moment the
# observer sees it on the WS firehose.

# Channel name MUST match the observer's :mod:`src.observer.trade_observer`
# publisher. Hardcoded as part of the public contract.
_TRADES_OBSERVED_CHANNEL: str = "trades:observed"

# Backoff schedule on Redis disconnects. Mirrors the schedule in
# :mod:`src.control.redis_pubsub` but kept local so this module doesn't
# import the Subscriber class (which carries handler-registration
# machinery we don't need here — we want an async iterator, not a
# callback dispatcher).
_PROXY_BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

# Polling timeout on ``get_message``. Short enough to wake on close,
# long enough to avoid busy-looping a quiet channel.
_PROXY_GET_MESSAGE_TIMEOUT_S: float = 1.0


def _stable_synthetic_hash(payload: dict) -> str:
    """Compute a stable hex hash for a ``trades:observed`` payload.

    Prefers an explicit ``dedup_key`` if the observer attached one
    (we don't depend on it being present; not all observer code paths
    set it). Falls back to an md5 of the canonical field tuple. The
    ``ws:`` prefix is added by the caller so the synthetic hash is
    distinguishable from a real 0x32-byte tx hash on the downstream
    stream.
    """
    dedup = payload.get("dedup_key")
    if isinstance(dedup, str) and dedup:
        return hashlib.md5(dedup.encode("utf-8")).hexdigest()
    # Canonical-tuple fallback: the same fields the observer uses to
    # dedup at insert time. ``time`` is included for stability across
    # close-in-time same-wallet trades.
    canonical = "|".join(
        str(payload.get(field_name, ""))
        for field_name in (
            "time",
            "wallet_address",
            "market_id",
            "token_id",
            "side",
            "price",
            "size_usdc",
        )
    )
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _trade_payload_to_mempool_tx(
    payload: dict,
    *,
    clob_contract: str,
) -> Optional[MempoolTx]:
    """Build a synthetic :class:`MempoolTx` from a ``trades:observed``
    payload.

    Returns ``None`` if the payload is missing the wallet address
    (without which there's no leader to attribute to).
    """
    wallet = payload.get("wallet_address")
    if not isinstance(wallet, str) or not wallet:
        return None
    from_wallet = _normalize_address(wallet)
    if not from_wallet:
        return None
    synthetic_hash = "ws:" + _stable_synthetic_hash(payload)
    return MempoolTx(
        tx_hash=synthetic_hash,
        from_wallet=from_wallet,
        to_contract=_normalize_address(clob_contract),
        gas_price=0,
        gas_limit=0,
        nonce=0,
        calldata=b"",
        received_at=datetime.now(timezone.utc),
        replaces=None,
        source_payload=payload,
    )


class LeaderTradeSubscription:
    """Redis pub/sub subscriber that masquerades as a mempool source.

    Wraps a dedicated ``redis.asyncio.Redis`` instance + a
    ``pubsub()`` session against ``trades:observed`` and yields
    synthetic :class:`MempoolTx` objects for every leader trade
    whose wallet sits in ``wallet_index``.

    The class deliberately mirrors :class:`MempoolSubscription`'s
    public contract — one ``async for`` over :meth:`stream` and a
    :meth:`close` method — so the daemon's stream loop in
    :mod:`src.mempool.main` can swap one for the other without
    branching on the subscription type.

    Reconnect strategy
    ------------------
    Reuses the exponential-backoff schedule from
    :class:`src.control.redis_pubsub.Subscriber`. On Redis disconnect
    we log, sleep, and rebuild the pubsub object — the dedicated
    Redis client is rebuilt too so a half-open socket doesn't survive
    the restart. Messages published during the reconnect window ARE
    LOST (the observer's pub/sub layer has no replay semantics);
    Phase-3 Redis Streams would close that gap but pub/sub is
    sufficient for the shadow-mode soak.
    """

    def __init__(
        self,
        redis_client: Any,
        wallet_index: "WatchedWalletIndex",
        *,
        clob_contract: Optional[str] = None,
        channel: str = _TRADES_OBSERVED_CHANNEL,
    ) -> None:
        # ``redis_client`` is a ``redis.asyncio.Redis`` instance
        # (production) or a fakeredis equivalent (tests). The caller
        # owns its lifetime — we don't close it on :meth:`close`.
        self._redis = redis_client
        self._wallet_index = wallet_index
        self._channel = channel
        # Lazy import to avoid a hard dependency on src.config at
        # import time (keeps the unit tests light).
        if clob_contract is None:
            try:
                from src.config import settings as _settings

                clob_contract = _settings.POLYMARKET_CLOB_CONTRACT_ADDRESS
            except Exception:
                clob_contract = ""
        self._clob_contract = clob_contract or ""
        self._closed = False

    async def stream(self) -> AsyncIterator[MempoolTx]:
        """Yield synthetic :class:`MempoolTx` for each matching trade.

        Reconnects on Redis errors with exponential backoff. Per-message
        exceptions (bad JSON, missing fields) are swallowed at DEBUG —
        one malformed message must not tear down the stream.
        """
        attempt = 0
        while not self._closed:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._channel)
                logger.debug(
                    "LeaderTradeSubscription: SUBSCRIBE ok on {}",
                    self._channel,
                )
                # Successful reconnect — reset the backoff counter.
                attempt = 0
                async for tx in self._consume(pubsub):
                    yield tx
                    if self._closed:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Treat ANY error inside the consume loop as
                # reconnect-worthy. The pre-fix observer pattern
                # would die silently here; we surface the reconnect
                # via WARNING + backoff.
                if self._closed:
                    return
                backoff = _PROXY_BACKOFF_SCHEDULE_S[
                    min(attempt, len(_PROXY_BACKOFF_SCHEDULE_S) - 1)
                ]
                attempt += 1
                logger.warning(
                    "LeaderTradeSubscription: reconnect #{} channel={} "
                    "backoff={:.1f}s err={!r}",
                    attempt,
                    self._channel,
                    backoff,
                    exc,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
            finally:
                # Best-effort unsubscribe + close. If the connection
                # is already broken we don't care — the outer loop
                # rebuilds on next iteration.
                if pubsub is not None:
                    try:
                        await asyncio.wait_for(
                            pubsub.unsubscribe(self._channel), timeout=2.0
                        )
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(pubsub.aclose(), timeout=2.0)
                    except Exception:
                        pass

    async def _consume(self, pubsub: Any) -> AsyncIterator[MempoolTx]:
        """Inner loop. Returns cleanly on ``self._closed``; raises on
        connection errors so the outer loop classifies + backs off."""
        while not self._closed:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=_PROXY_GET_MESSAGE_TIMEOUT_S,
            )
            if msg is None:
                continue
            if msg.get("type") != "message":
                continue
            tx = self._build_tx_from_message(msg)
            if tx is None:
                continue
            yield tx

    def _build_tx_from_message(self, msg: dict) -> Optional[MempoolTx]:
        """Decode one pub/sub message + apply the leader / watched-wallet
        filter. Returns ``None`` when the message must be dropped.

        Errors here are LOGGED at DEBUG and swallowed — one bad
        message must not stop the stream.
        """
        raw = msg.get("data")
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug(
                "LeaderTradeSubscription: bad JSON on {}: {!r}",
                self._channel,
                exc,
            )
            return None
        if not isinstance(payload, dict):
            return None
        # Filter #1: is_leader must be truthy. The observer already
        # stamps this per-trade so we don't have to consult any other
        # service.
        if not bool(payload.get("is_leader")):
            return None
        # Filter #2: the leader must be in the watched-wallet bloom.
        wallet = payload.get("wallet_address")
        if not isinstance(wallet, str) or wallet not in self._wallet_index:
            return None
        try:
            tx = _trade_payload_to_mempool_tx(
                payload, clob_contract=self._clob_contract
            )
        except Exception as exc:
            logger.debug(
                "LeaderTradeSubscription: build synthetic tx failed: {!r}",
                exc,
            )
            return None
        if tx is None:
            return None
        try:
            mempool_tx_received_total.labels(source="ws_proxy").inc()
        except Exception:
            pass
        try:
            mempool_wallet_matches_total.inc()
        except Exception:
            pass
        return tx

    async def close(self) -> None:
        """Flip the close flag. Safe to call multiple times.

        We do NOT close the underlying Redis client — the caller owns
        its lifetime (mirrors the convention in
        :class:`src.control.redis_pubsub.Subscriber` for the
        injected-client case).
        """
        self._closed = True
