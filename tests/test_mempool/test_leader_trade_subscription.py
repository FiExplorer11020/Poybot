"""Tests for :class:`src.mempool.node_client.LeaderTradeSubscription`.

Sprint 3.5 — EXECUTION_PLAN § 4 Décision #5.

Coverage:

* Subscription yields a synthetic :class:`MempoolTx` for a
  ``trades:observed`` payload with ``is_leader=true`` whose wallet
  sits in the watched-wallet bloom.
* ``is_leader=false`` payloads are dropped on the floor.
* Wallets that the bloom doesn't know about are dropped.
* Malformed payloads (bad JSON, missing fields, wrong types) are
  swallowed with a DEBUG log — the stream MUST keep running.

The Redis pub/sub layer is fakerised with a minimal stub: we don't
take a dependency on fakeredis here because the surface we exercise
is tiny (one method: ``pubsub()`` → an object with ``subscribe`` /
``unsubscribe`` / ``get_message`` / ``aclose``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable

import pytest

from src.mempool.node_client import (
    LeaderTradeSubscription,
    MempoolTx,
    _stable_synthetic_hash,
    _trade_payload_to_mempool_tx,
)
from src.mempool.wallet_index import WatchedWalletIndex


# --------------------------------------------------------------------------- #
# Minimal Redis pub/sub stub                                                   #
# --------------------------------------------------------------------------- #

WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CLOB = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"


class _FakePubSub:
    """In-memory stand-in for ``redis.asyncio.Redis.pubsub()``.

    Drains a pre-seeded queue of pub/sub frames, then signals "no more
    data" by returning ``None`` from ``get_message`` on every
    subsequent call (the real client does the same on a quiet channel).
    """

    def __init__(self, messages: Iterable[dict]) -> None:
        # Copy into a list so reuse across tests is straightforward.
        self._messages: list[dict] = list(messages)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> dict | None:
        # Pop one frame per call. When the queue is empty, sleep briefly
        # so the caller's outer loop has a chance to honor close() and
        # return ``None`` to mimic the real client's quiet-channel
        # behaviour.
        if self._messages:
            return self._messages.pop(0)
        # Small yield so the test loop can observe stream close().
        await asyncio.sleep(0)
        return None


class _FakeRedis:
    """Minimal stand-in for ``redis.asyncio.Redis``."""

    def __init__(self, messages: Iterable[dict]) -> None:
        self._pubsub = _FakePubSub(messages)

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


def _frame(payload: Any) -> dict:
    """Wrap ``payload`` in a ``message``-typed pub/sub frame."""
    if isinstance(payload, (bytes, bytearray, str)):
        data = payload
    else:
        data = json.dumps(payload)
    return {
        "type": "message",
        "channel": "trades:observed",
        "data": data,
    }


def _build_index(*wallets: str) -> WatchedWalletIndex:
    idx = WatchedWalletIndex()
    for w in wallets:
        idx.add(w)
    return idx


def _make_trade_payload(
    *,
    wallet: str = WALLET_A,
    is_leader: bool = True,
    market_id: str = "market-1",
    token_id: str = "token-yes",
    side: str = "buy",
    price: str = "0.55",
    size_usdc: str = "1500.00",
    time: str = "2026-05-13T12:34:56+00:00",
) -> dict:
    return {
        "time": time,
        "market_id": market_id,
        "token_id": token_id,
        "wallet_address": wallet,
        "side": side,
        "price": price,
        "size_usdc": size_usdc,
        "is_leader": is_leader,
        "source": "websocket",
    }


async def _drain(sub: LeaderTradeSubscription, *, max_items: int = 8):
    """Pull up to ``max_items`` MempoolTx from the subscription, then
    close it. Returns the list of yielded items."""
    out: list[MempoolTx] = []
    agen = sub.stream()
    try:
        for _ in range(max_items):
            try:
                tx = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            out.append(tx)
    finally:
        await sub.close()
        try:
            await agen.aclose()
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------- #
# 1. Leader trade is yielded as a synthetic MempoolTx                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leader_trade_yields_synthetic_mempool_tx():
    payload = _make_trade_payload(wallet=WALLET_A, is_leader=True)
    redis_stub = _FakeRedis([_frame(payload)])
    idx = _build_index(WALLET_A)

    sub = LeaderTradeSubscription(redis_stub, idx, clob_contract=CLOB)
    out = await _drain(sub, max_items=2)

    assert len(out) == 1
    tx = out[0]
    assert isinstance(tx, MempoolTx)
    assert tx.from_wallet == WALLET_A.lower()
    assert tx.to_contract == CLOB.lower()
    assert tx.tx_hash.startswith("ws:")
    assert tx.nonce == 0
    assert tx.gas_price == 0
    assert tx.calldata == b""
    # The source payload is preserved verbatim for the daemon to
    # build a LeaderIntent without going through the ABI decoder.
    assert tx.source_payload == payload


# --------------------------------------------------------------------------- #
# 2. is_leader=False payloads are filtered out                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_leader_trade_is_filtered_out():
    payload = _make_trade_payload(wallet=WALLET_A, is_leader=False)
    redis_stub = _FakeRedis([_frame(payload)])
    idx = _build_index(WALLET_A)

    sub = LeaderTradeSubscription(redis_stub, idx, clob_contract=CLOB)
    out = await _drain(sub, max_items=2)

    assert out == []


# --------------------------------------------------------------------------- #
# 3. Wallets not in the watched-wallet index are filtered                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unwatched_wallet_is_filtered_out():
    payload = _make_trade_payload(wallet=WALLET_B, is_leader=True)
    redis_stub = _FakeRedis([_frame(payload)])
    # Index only watches WALLET_A; WALLET_B's leader trade should drop.
    idx = _build_index(WALLET_A)

    sub = LeaderTradeSubscription(redis_stub, idx, clob_contract=CLOB)
    out = await _drain(sub, max_items=2)

    assert out == []


# --------------------------------------------------------------------------- #
# 4. Malformed payloads are swallowed; the stream keeps running                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_malformed_payload_is_swallowed_stream_continues():
    good = _make_trade_payload(wallet=WALLET_A, is_leader=True)
    # Mix of broken frames around a good one. We want to prove that NONE
    # of these tear down the stream and the good trade still arrives.
    redis_stub = _FakeRedis(
        [
            # Frame whose data isn't JSON at all.
            _frame("{this is not json"),
            # Frame whose decoded JSON is a list, not a dict.
            _frame([1, 2, 3]),
            # Frame missing wallet_address.
            _frame({"is_leader": True, "side": "buy"}),
            # Frame with wrong wallet_address type.
            _frame({"is_leader": True, "wallet_address": 12345}),
            # The legitimate trade.
            _frame(good),
        ]
    )
    idx = _build_index(WALLET_A)

    sub = LeaderTradeSubscription(redis_stub, idx, clob_contract=CLOB)
    out = await _drain(sub, max_items=8)

    # Only the good payload yields a tx — the four broken frames are
    # silently swallowed.
    assert len(out) == 1
    assert out[0].source_payload == good


# --------------------------------------------------------------------------- #
# Helpers also worth covering directly                                         #
# --------------------------------------------------------------------------- #


def test_stable_synthetic_hash_is_deterministic_with_dedup_key():
    payload = {"dedup_key": "wallet:market:1700000000:buy:0.55:1500.0"}
    h1 = _stable_synthetic_hash(payload)
    h2 = _stable_synthetic_hash(payload)
    assert h1 == h2 and len(h1) == 32


def test_stable_synthetic_hash_canonical_fallback():
    # No dedup_key → fall back to canonical tuple; same content → same hash.
    p = _make_trade_payload()
    h1 = _stable_synthetic_hash(p)
    h2 = _stable_synthetic_hash(p)
    assert h1 == h2 and len(h1) == 32


def test_trade_payload_to_mempool_tx_returns_none_when_wallet_missing():
    assert _trade_payload_to_mempool_tx({}, clob_contract=CLOB) is None
    assert (
        _trade_payload_to_mempool_tx(
            {"wallet_address": ""}, clob_contract=CLOB
        )
        is None
    )
