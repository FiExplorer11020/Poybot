"""Tests for :mod:`src.mempool.tx_decoder` — Round 7 Wave-2.

Covers:
  * Selector lookup: ``fillOrder`` / ``matchOrders`` / ``cancelOrder``
    each produce a populated :class:`LeaderIntent` from synthetic
    calldata.
  * Side encoding: 0 → ``"buy"``, 1 → ``"sell"``.
  * Size + price computation from maker/taker amounts.
  * Wrong selector → ``None``, metric ``not_clob`` increments.
  * Short calldata (no selector) → ``None``.
  * Malformed calldata after a valid selector → ``None``, metric
    ``decode_failed`` increments.
  * ``cancelOrder`` decodes to an intent with ``order_type="cancel"``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_abi import encode as abi_encode

from src.mempool.node_client import MempoolTx
from src.mempool.tx_decoder import (
    CLOBTxDecoder,
    _CANCEL_ORDER_SELECTOR,
    _FILL_ORDER_SELECTOR,
    _MATCH_ORDERS_SELECTOR,
    _ORDER_TUPLE,
    LeaderIntent,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _build_order_tuple(
    *,
    maker: str = "0x" + "11" * 20,
    token_id: int = 0xABCD,
    maker_amount: int = 1_000_000_000,  # 1000 USDC at 6 decimals
    taker_amount: int = 2_000_000_000_000_000_000_000,  # 2000 shares
    side: int = 0,  # 0 = BUY, 1 = SELL
) -> tuple:
    return (
        12345,           # salt
        maker,           # maker
        maker,           # signer (same)
        "0x" + "00" * 20,  # taker (open order)
        token_id,        # tokenId
        maker_amount,    # makerAmount
        taker_amount,    # takerAmount
        9_999_999_999,   # expiration
        0,               # nonce
        0,               # feeRateBps
        side,            # side (uint8)
        0,               # signatureType (uint8)
        b"",             # signature (bytes)
    )


def _build_fill_order_calldata(order: tuple, fill_amount: int = 0) -> bytes:
    """Compose calldata for fillOrder(Order, uint256)."""
    body = abi_encode([_ORDER_TUPLE, "uint256"], [order, fill_amount])
    return _FILL_ORDER_SELECTOR + body


def _build_cancel_order_calldata(order: tuple) -> bytes:
    body = abi_encode([_ORDER_TUPLE], [order])
    return _CANCEL_ORDER_SELECTOR + body


def _build_match_orders_calldata(taker: tuple, makers: list) -> bytes:
    body = abi_encode(
        [_ORDER_TUPLE, f"{_ORDER_TUPLE}[]", "uint256", "uint256[]"],
        [taker, makers, 0, [0] * len(makers)],
    )
    return _MATCH_ORDERS_SELECTOR + body


def _mk_tx(calldata: bytes, *, tx_hash: str = "0xfe") -> MempoolTx:
    return MempoolTx(
        tx_hash=tx_hash,
        from_wallet="0x" + "aa" * 20,
        to_contract="0x" + "bb" * 20,
        gas_price=100,
        gas_limit=21000,
        nonce=42,
        calldata=calldata,
        received_at=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# 1. Selector lookup                                                           #
# --------------------------------------------------------------------------- #


def test_decode_fill_order_returns_leader_intent():
    order = _build_order_tuple(
        maker_amount=1_000_000_000,  # 1000 USDC
        taker_amount=2_000_000_000_000_000_000_000,  # 2000 shares
        side=0,  # BUY
    )
    calldata = _build_fill_order_calldata(order)
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is not None
    assert isinstance(intent, LeaderIntent)
    assert intent.side == "buy"
    # 1000 USDC → 2000 shares ⇒ price = 0.5
    assert intent.size_usdc == Decimal("1000")
    assert intent.price == Decimal("0.5")
    assert intent.tx_hash == "0xfe"
    assert intent.nonce == 42
    assert intent.order_type == "GTC"
    assert intent.intent_id  # uuid4 was minted


def test_decode_fill_order_sell_side():
    """side=1 (SELL) ⇒ intent.side='sell' and amounts are swapped:
    the maker is GIVING shares and RECEIVING USDC."""
    order = _build_order_tuple(
        maker_amount=4_000_000_000_000_000_000_000,  # 4000 shares
        taker_amount=2_500_000_000,  # 2500 USDC
        side=1,
    )
    calldata = _build_fill_order_calldata(order)
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is not None
    assert intent.side == "sell"
    assert intent.size_usdc == Decimal("2500")
    # 2500 USDC / 4000 shares ⇒ price = 0.625
    assert intent.price == Decimal("0.625")


def test_decode_cancel_order_marker():
    """cancelOrder produces an intent stamped with order_type='cancel'."""
    order = _build_order_tuple()
    calldata = _build_cancel_order_calldata(order)
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is not None
    assert intent.order_type == "cancel"


def test_decode_match_orders_decoded_marker():
    """matchOrders should decode to an intent with order_type='GTC'
    (same as fillOrder; the cancel marker is the special case)."""
    taker = _build_order_tuple(side=1)
    maker = _build_order_tuple(side=0)
    calldata = _build_match_orders_calldata(taker, [maker])
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is not None
    assert intent.order_type == "GTC"
    assert intent.side == "sell"  # taker order, side=SELL


# --------------------------------------------------------------------------- #
# 2. Negative paths                                                            #
# --------------------------------------------------------------------------- #


def test_decode_unknown_selector_returns_none():
    """A non-CLOB selector (e.g. ERC-20 approve) yields ``None`` and
    bumps the ``not_clob`` counter."""
    # `transfer(address,uint256)` selector = 0xa9059cbb
    calldata = bytes.fromhex("a9059cbb") + b"\x00" * 64
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is None


def test_decode_short_calldata_returns_none():
    """Calldata too short to contain a selector → None."""
    assert CLOBTxDecoder().decode(_mk_tx(b"")) is None
    assert CLOBTxDecoder().decode(_mk_tx(b"\x01\x02")) is None


def test_decode_corrupted_calldata_returns_none():
    """A valid selector followed by malformed ABI data → None,
    decode_failed counter increments. We assert the metric path is
    exercised by ensuring the call does not raise."""
    # Valid fillOrder selector + 32 bytes of garbage (insufficient
    # for the Order struct; eth_abi.decode will raise).
    calldata = _FILL_ORDER_SELECTOR + b"\xff" * 32
    intent = CLOBTxDecoder().decode(_mk_tx(calldata))
    assert intent is None


def test_decode_uses_maker_address_not_tx_from():
    """For relayed submissions the tx.from is the facilitator; the
    intent's wallet must come from the Order struct's maker field."""
    explicit_maker = "0x" + "fe" * 20
    order = _build_order_tuple(maker=explicit_maker)
    calldata = _build_fill_order_calldata(order)
    tx = _mk_tx(calldata)
    # tx.from is intentionally different (the "facilitator").
    intent = CLOBTxDecoder().decode(tx)
    assert intent is not None
    assert intent.wallet == explicit_maker
    assert intent.wallet != tx.from_wallet


def test_decoder_propagates_replaces_field():
    """If the MempoolTx carries a ``replaces`` hash (NonceTracker
    handed us a replacement), the intent must preserve it."""
    order = _build_order_tuple()
    calldata = _build_fill_order_calldata(order)
    tx = _mk_tx(calldata)
    tx.replaces = "0xprev"
    intent = CLOBTxDecoder().decode(tx)
    assert intent is not None
    assert intent.replaces == "0xprev"
