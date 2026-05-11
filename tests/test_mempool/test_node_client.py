"""Tests for :mod:`src.mempool.node_client` — Round 7 Wave-2.

Covers:
  * :class:`MempoolSubscription.stream` hydrates and yields
    :class:`MempoolTx` from the underlying ``eth_subscribe`` +
    ``eth_getTransactionByHash`` mocks.
  * Per-tx exceptions are caught and the stream continues.
  * :class:`NonceTracker` replacement semantics: ``observe`` returns
    ``None`` on first sighting and the replaced hash on the second.
  * :class:`NonceTracker.is_live_for` honors the head of the chain.
  * :class:`NonceTracker.mark_confirmed` purges + records the chain
    length histogram.
  * Age-based eviction of chains older than 30 s.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mempool.node_client import (
    MempoolSubscription,
    MempoolTx,
    NonceTracker,
    _CHAIN_MAX_AGE_S,
    _raw_tx_to_mempool_tx,
)
from src.mempool.wallet_index import WatchedWalletIndex


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_raw_tx(
    *,
    tx_hash: str = "0xabc",
    from_addr: str = "0x1111111111111111111111111111111111111111",
    to: str = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    gas_price: int = 100,
    gas: int = 21000,
    nonce: int = 7,
    input_hex: str = "0x",
) -> dict:
    return {
        "hash": tx_hash,
        "from": from_addr,
        "to": to,
        "gasPrice": hex(gas_price),
        "gas": hex(gas),
        "nonce": hex(nonce),
        "input": input_hex,
    }


def _make_subscription_iter(items):
    """Return an async-generator callable suitable for use as
    ``rpc.eth_subscribe``'s side-effect. Yields the supplied items
    in order, then returns."""
    async def _gen(*_a, **_k):
        for item in items:
            yield item
    return _gen


def _make_rpc(raw_txs_by_hash: dict[str, dict], yield_items: list):
    """Build a MagicMock that quacks like an RPCClient for the subset
    of methods MempoolSubscription uses."""
    rpc = MagicMock()
    rpc.eth_subscribe = _make_subscription_iter(yield_items)

    async def _get_tx(tx_hash: str):
        return raw_txs_by_hash.get(tx_hash)

    rpc.eth_getTransactionByHash = AsyncMock(side_effect=_get_tx)
    return rpc


# --------------------------------------------------------------------------- #
# 1. MempoolSubscription.stream yields hydrated MempoolTx                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_yields_mempool_tx_from_hash():
    wallet = "0x1234000000000000000000000000000000000abc"
    index = WatchedWalletIndex()
    index.add(wallet)

    raw = _make_raw_tx(tx_hash="0xaaaa", from_addr=wallet)
    rpc = _make_rpc({"0xaaaa": raw}, ["0xaaaa"])

    sub = MempoolSubscription(rpc, index)
    out = []
    async for tx in sub.stream():
        out.append(tx)
        if len(out) >= 1:
            break
    assert len(out) == 1
    assert out[0].tx_hash == "0xaaaa"
    assert out[0].from_wallet == wallet
    assert out[0].nonce == 7


@pytest.mark.asyncio
async def test_stream_yields_from_dict_shape():
    """Providers that yield full tx dicts directly (instead of hash
    strings) must also work — defensive shape handling."""
    wallet = "0x2222000000000000000000000000000000000def"
    index = WatchedWalletIndex()
    index.add(wallet)

    raw = _make_raw_tx(tx_hash="0xbbbb", from_addr=wallet, nonce=42)
    rpc = _make_rpc({}, [raw])  # yield the dict directly

    sub = MempoolSubscription(rpc, index)
    out = []
    async for tx in sub.stream():
        out.append(tx)
        if len(out) >= 1:
            break
    assert len(out) == 1
    assert out[0].nonce == 42
    # Hash-by-hash lookup was NOT invoked since we yielded the dict.
    assert rpc.eth_getTransactionByHash.call_count == 0


@pytest.mark.asyncio
async def test_stream_skips_unknown_wallets():
    """A tx from a wallet not in the bloom index is dropped silently
    (defense-in-depth against Erigon filter drift)."""
    watched = "0x1111000000000000000000000000000000000aaa"
    intruder = "0x9999000000000000000000000000000000000bbb"
    index = WatchedWalletIndex()
    index.add(watched)

    raw = _make_raw_tx(tx_hash="0xc1", from_addr=intruder)
    rpc = _make_rpc({"0xc1": raw}, ["0xc1"])

    sub = MempoolSubscription(rpc, index)
    out = []
    async for tx in sub.stream():
        out.append(tx)
    assert out == []


@pytest.mark.asyncio
async def test_stream_continues_after_per_tx_exception():
    """A malformed payload that raises during hydration must NOT tear
    down the stream — the next tx should still be yielded."""
    wallet = "0x3333000000000000000000000000000000000bad"
    index = WatchedWalletIndex()
    index.add(wallet)

    good = _make_raw_tx(tx_hash="0xgood", from_addr=wallet, nonce=2)

    async def _get_tx(tx_hash: str):
        if tx_hash == "0xbad":
            raise RuntimeError("simulated hydrate failure")
        if tx_hash == "0xgood":
            return good
        return None

    rpc = MagicMock()
    rpc.eth_subscribe = _make_subscription_iter(["0xbad", "0xgood"])
    rpc.eth_getTransactionByHash = AsyncMock(side_effect=_get_tx)

    sub = MempoolSubscription(rpc, index)
    out = []
    async for tx in sub.stream():
        out.append(tx)
        if len(out) >= 1:
            break
    assert len(out) == 1
    assert out[0].tx_hash == "0xgood"


@pytest.mark.asyncio
async def test_stream_skips_when_hydrate_returns_none():
    """Tx dropped between subscribe-yield and hydrate (mempool churn)
    is a normal case — skip silently, don't crash."""
    wallet = "0x4444000000000000000000000000000000000eee"
    index = WatchedWalletIndex()
    index.add(wallet)

    rpc = MagicMock()
    rpc.eth_subscribe = _make_subscription_iter(["0xgone", "0xhere"])
    raw = _make_raw_tx(tx_hash="0xhere", from_addr=wallet)

    async def _get_tx(tx_hash: str):
        if tx_hash == "0xgone":
            return None
        return raw

    rpc.eth_getTransactionByHash = AsyncMock(side_effect=_get_tx)

    sub = MempoolSubscription(rpc, index)
    out = []
    async for tx in sub.stream():
        out.append(tx)
        break
    assert len(out) == 1
    assert out[0].tx_hash == "0xhere"


@pytest.mark.asyncio
async def test_stream_close_is_idempotent():
    """close() can be called more than once without raising."""
    index = WatchedWalletIndex()
    rpc = _make_rpc({}, [])
    sub = MempoolSubscription(rpc, index)
    await sub.close()
    await sub.close()


# --------------------------------------------------------------------------- #
# 2. NonceTracker semantics                                                    #
# --------------------------------------------------------------------------- #


def _tx(wallet: str, nonce: int, tx_hash: str) -> MempoolTx:
    return MempoolTx(
        tx_hash=tx_hash,
        from_wallet=wallet,
        to_contract="0xdead",
        gas_price=100,
        gas_limit=21000,
        nonce=nonce,
        calldata=b"",
        received_at=datetime.now(timezone.utc),
    )


def test_nonce_tracker_returns_none_on_first_observe():
    tracker = NonceTracker()
    wallet = "0x" + "11" * 20
    assert tracker.observe(_tx(wallet, 1, "0xa")) is None


def test_nonce_tracker_returns_replaced_hash():
    tracker = NonceTracker()
    wallet = "0x" + "22" * 20
    assert tracker.observe(_tx(wallet, 1, "0xa")) is None
    replaced = tracker.observe(_tx(wallet, 1, "0xb"))
    assert replaced == "0xa"
    # Now a third observation should report "0xb" as the replaced one.
    replaced = tracker.observe(_tx(wallet, 1, "0xc"))
    assert replaced == "0xb"


def test_nonce_tracker_same_hash_re_observed_returns_none():
    """Idempotent: re-seeing the same tx_hash at the head of the chain
    is not a replacement."""
    tracker = NonceTracker()
    wallet = "0x" + "33" * 20
    assert tracker.observe(_tx(wallet, 1, "0xa")) is None
    assert tracker.observe(_tx(wallet, 1, "0xa")) is None


def test_nonce_tracker_is_live_for():
    tracker = NonceTracker()
    wallet = "0x" + "44" * 20
    tracker.observe(_tx(wallet, 1, "0xa"))
    tracker.observe(_tx(wallet, 1, "0xb"))
    # 0xb is the head; 0xa is obsolete.
    assert tracker.is_live_for(wallet, 1, "0xb") is True
    assert tracker.is_live_for(wallet, 1, "0xa") is False
    # Unknown wallet/nonce → False.
    assert tracker.is_live_for("0xnope", 99, "0xa") is False


def test_nonce_tracker_mark_confirmed_purges_chain():
    tracker = NonceTracker()
    wallet = "0x" + "55" * 20
    tracker.observe(_tx(wallet, 1, "0xa"))
    tracker.observe(_tx(wallet, 1, "0xb"))
    tracker.mark_confirmed(wallet, 1)
    # After confirmation the chain is gone — neither hash is live.
    assert tracker.is_live_for(wallet, 1, "0xb") is False
    assert tracker.is_live_for(wallet, 1, "0xa") is False
    # A new observation at the same (wallet, nonce) starts fresh.
    assert tracker.observe(_tx(wallet, 1, "0xc")) is None


def test_nonce_tracker_prunes_stale_chains(monkeypatch):
    """Chains older than ``_CHAIN_MAX_AGE_S`` (30 s) are dropped on
    the next observe() to bound memory in the absence of confirm
    feedback."""
    tracker = NonceTracker()
    wallet = "0x" + "66" * 20

    # Patch time.monotonic to step forward deterministically.
    now = [1000.0]
    monkeypatch.setattr(
        "src.mempool.node_client.time.monotonic", lambda: now[0]
    )

    tracker.observe(_tx(wallet, 1, "0xa"))
    assert (wallet, 1) in tracker._chains

    # Jump past the eviction threshold.
    now[0] += _CHAIN_MAX_AGE_S + 5.0
    # Trigger a prune via an unrelated observation.
    tracker.observe(_tx(wallet, 99, "0xz"))
    # The (wallet, 1) chain is gone, (wallet, 99) lives.
    assert (wallet, 1) not in tracker._chains
    assert (wallet, 99) in tracker._chains


# --------------------------------------------------------------------------- #
# 3. Raw-tx coercion helpers (smoke)                                           #
# --------------------------------------------------------------------------- #


def test_raw_tx_to_mempool_tx_handles_hex_fields():
    """The coercion helper turns 0x-hex JSON-RPC fields into ints/bytes
    and normalises addresses to lowercase."""
    raw = {
        "hash": "0xDEADBEEF",
        "from": "0xABCdef0000000000000000000000000000000001",
        "to": "0x4BFB41D5B3570DEFD03C39A9A4D8DE6BD8B8982E",
        "gasPrice": "0x64",  # 100
        "gas": "0x5208",  # 21000
        "nonce": "0x7",
        "input": "0xfe729aaf",  # fillOrder selector
    }
    tx = _raw_tx_to_mempool_tx(raw)
    assert tx is not None
    assert tx.tx_hash == "0xdeadbeef"
    assert tx.from_wallet == "0xabcdef0000000000000000000000000000000001"
    assert tx.gas_price == 100
    assert tx.gas_limit == 21000
    assert tx.nonce == 7
    assert tx.calldata == bytes.fromhex("fe729aaf")


def test_raw_tx_to_mempool_tx_rejects_missing_hash():
    assert _raw_tx_to_mempool_tx({"from": "0x1234"}) is None
    assert _raw_tx_to_mempool_tx(None) is None  # type: ignore[arg-type]


def test_raw_tx_to_mempool_tx_handles_eip1559_max_fee():
    """EIP-1559 tx have no ``gasPrice``; fall through to ``maxFeePerGas``."""
    raw = {
        "hash": "0xeeff",
        "from": "0x" + "ab" * 20,
        "maxFeePerGas": "0xff",
        "gas": "0x5208",
        "nonce": "0x0",
        "input": "0x",
    }
    tx = _raw_tx_to_mempool_tx(raw)
    assert tx is not None
    assert tx.gas_price == 255
