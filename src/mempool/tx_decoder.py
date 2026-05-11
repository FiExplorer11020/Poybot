"""Polymarket CTF Exchange calldata decoder.

Round 7 / The Front Door — § 3.2.

The mempool subscription hands us raw :class:`src.mempool.node_client.MempoolTx`
objects with un-decoded ``calldata`` bytes. This module decodes those
bytes against the Polymarket CLOB function ABI to extract a
:class:`LeaderIntent`: the market, the token, the side (buy/sell), the
size in USDC, and the price.

Function selector strategy (Wave-2 decision)
--------------------------------------------
The R6-pinned ABI in :mod:`src.onchain.clob_abi` only ships event
signatures (the on-chain listener only decodes inbound logs). We need
function signatures for ``fillOrder`` / ``matchOrders`` / ``cancelOrder``
to map the 4-byte selector at ``calldata[:4]`` to a parameter type
list. Per Wave-2 implementer note in the architect docstring, we
**hardcode the selectors and input-type tuples in this module** as
constants rather than cross-edit ``clob_abi.py`` (which other agents
in this wave are NOT touching). The TODO below tracks unifying the
two ABI surfaces in a Round-7 follow-up.

  TODO(round-7-followup): unify with src.onchain.clob_abi.POLYMARKET_CLOB_ABI
  when convenient. The two ABI definitions should live in one place so
  a future contract upgrade is a single edit.

Source of selectors
-------------------
Selectors are computed at import time via ``eth_utils.keccak`` of the
canonical Solidity function signature (the same way the event topic-0
hashes are computed in :mod:`src.onchain.clob_abi`). The Order struct
type tuple matches the Polygon-mainnet CTF Exchange's verified ABI at
``0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E``:

    struct Order {
        uint256 salt;
        address maker;
        address signer;
        address taker;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint256 expiration;
        uint256 nonce;
        uint256 feeRateBps;
        uint8   side;
        uint8   signatureType;
        bytes   signature;
    }

Decoded side encoding: 0 = BUY, 1 = SELL (per the contract).

V1 limitations
--------------
* The decoder reads the order struct directly; it does NOT unwrap
  proxy / negRiskAdapter envelopes (per the architect's edge-case
  note). Proxy calls produce a ``not_clob`` outcome until a Round-7
  follow-up adds the wrapper handling.
* ``expected_block`` is filled by the IntentRouter at consume time
  (not by the decoder here). The :class:`LeaderIntent` carries 0
  until then; the publisher emits 0 and the router upgrades it.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.2 for the full spec.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Optional

from eth_abi import decode as abi_decode
from eth_utils import keccak
from loguru import logger

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.node_client import MempoolTx


# Defensive metrics import.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        mempool_tx_decoded_total,
    )
except Exception:  # pragma: no cover — defensive fallback

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    mempool_tx_decoded_total = _NoOpLabel()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Function signatures + selectors
# ---------------------------------------------------------------------------
#
# Each entry: (selector_4bytes, parameter_type_tuple, order_arg_index).
# ``order_arg_index`` is the position of the Order struct in the
# decoded args tuple — we use it to pull side / tokenId / amounts.
# matchOrders and fillOrder both take the order as arg 0; cancelOrder
# is just the order.
#
# Order tuple type, matching the struct above. ``eth_abi.decode``
# accepts the parenthesised Solidity-tuple form.
_ORDER_TUPLE: str = (
    "(uint256,address,address,address,uint256,uint256,uint256,"
    "uint256,uint256,uint256,uint8,uint8,bytes)"
)

# Signatures used to compute the 4-byte selectors. The selector is
# keccak256(signature)[:4].
_FILL_ORDER_SIG = f"fillOrder({_ORDER_TUPLE},uint256)"
_MATCH_ORDERS_SIG = (
    f"matchOrders({_ORDER_TUPLE},{_ORDER_TUPLE}[],uint256,uint256[])"
)
_CANCEL_ORDER_SIG = f"cancelOrder({_ORDER_TUPLE})"


def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


_FILL_ORDER_SELECTOR: bytes = _selector(_FILL_ORDER_SIG)
_MATCH_ORDERS_SELECTOR: bytes = _selector(_MATCH_ORDERS_SIG)
_CANCEL_ORDER_SELECTOR: bytes = _selector(_CANCEL_ORDER_SIG)


# Param type lists in eth_abi.decode form (a flat list of types; the
# Order struct is one element typed as the tuple string above).
_FILL_ORDER_PARAMS: list[str] = [_ORDER_TUPLE, "uint256"]
_MATCH_ORDERS_PARAMS: list[str] = [
    _ORDER_TUPLE,
    f"{_ORDER_TUPLE}[]",
    "uint256",
    "uint256[]",
]
_CANCEL_ORDER_PARAMS: list[str] = [_ORDER_TUPLE]


# Selector → (method_name, param_types, order_arg_index)
_SELECTOR_MAP: dict[bytes, tuple[str, list[str], int]] = {
    _FILL_ORDER_SELECTOR: ("fillOrder", _FILL_ORDER_PARAMS, 0),
    _MATCH_ORDERS_SELECTOR: ("matchOrders", _MATCH_ORDERS_PARAMS, 0),
    _CANCEL_ORDER_SELECTOR: ("cancelOrder", _CANCEL_ORDER_PARAMS, 0),
}


# Order-tuple positional layout (mirrors _ORDER_TUPLE above).
_ORDER_SALT = 0
_ORDER_MAKER = 1
_ORDER_SIGNER = 2
_ORDER_TAKER = 3
_ORDER_TOKEN_ID = 4
_ORDER_MAKER_AMOUNT = 5
_ORDER_TAKER_AMOUNT = 6
_ORDER_EXPIRATION = 7
_ORDER_NONCE = 8
_ORDER_FEE_RATE_BPS = 9
_ORDER_SIDE = 10  # 0 = BUY, 1 = SELL


OrderType = Literal["FOK", "GTC", "GTD"]


@dataclass(slots=True)
class LeaderIntent:
    """Decoded Polymarket trading intent — the unit the IntentRouter sees.

    See module docstring + the architect's Wave-1 docstring for the
    per-field contract.
    """

    intent_id: str
    wallet: str
    market_id: str
    token_id: str
    side: Literal["buy", "sell"]
    size_usdc: Decimal
    price: Decimal
    order_type: str  # one of OrderType
    intent_received_at: datetime
    expected_block: int
    tx_hash: str
    nonce: int
    replaces: Optional[str] = None


def _bump_decoded(result: str) -> None:
    try:
        mempool_tx_decoded_total.labels(result=result).inc()
    except Exception:
        pass


class CLOBTxDecoder:
    """Decode :class:`MempoolTx.calldata` against the Polymarket CLOB ABI.

    Single public method :meth:`decode` returns a :class:`LeaderIntent`
    on a successful decode or ``None`` if the tx targets a function we
    don't care about / can't decode.
    """

    def __init__(self, abi: Optional[list[dict]] = None) -> None:
        # ``abi`` is accepted for API parity with the architect contract
        # but the V1 decoder reads the hardcoded selector map above. A
        # future round-7 follow-up will move the selectors into the
        # shared ABI and honour the override.
        self._abi_override = abi

    def decode(self, tx: "MempoolTx") -> Optional[LeaderIntent]:
        """Decode ``tx.calldata`` into a :class:`LeaderIntent`.

        Returns ``None`` when:
          * ``tx.calldata`` is shorter than 4 bytes (no selector).
          * Selector is not in the CLOB function map.
          * ABI decode raises.

        On success, ``mempool_tx_decoded_total{result="decoded"}``
        increments and a fully-populated :class:`LeaderIntent` is
        returned with a fresh UUID ``intent_id``.
        """
        calldata = tx.calldata
        if not calldata or len(calldata) < 4:
            _bump_decoded("not_clob")
            return None
        selector = bytes(calldata[:4])
        entry = _SELECTOR_MAP.get(selector)
        if entry is None:
            _bump_decoded("not_clob")
            return None
        method_name, param_types, order_idx = entry
        try:
            decoded = abi_decode(param_types, calldata[4:])
        except Exception as exc:
            logger.debug(
                "CLOBTxDecoder: ABI decode failed for {} tx={}: {!r}",
                method_name,
                tx.tx_hash,
                exc,
            )
            _bump_decoded("decode_failed")
            return None
        try:
            intent = self._build_intent(tx, method_name, decoded, order_idx)
        except Exception as exc:
            logger.debug(
                "CLOBTxDecoder: intent build failed for {} tx={}: {!r}",
                method_name,
                tx.tx_hash,
                exc,
            )
            _bump_decoded("decode_failed")
            return None
        if intent is None:
            _bump_decoded("decode_failed")
            return None
        _bump_decoded("decoded")
        return intent

    def _build_intent(
        self,
        tx: "MempoolTx",
        method_name: str,
        decoded: tuple,
        order_idx: int,
    ) -> Optional[LeaderIntent]:
        """Pull the Order struct out of ``decoded`` and assemble a
        :class:`LeaderIntent`.

        For ``cancelOrder``: the order is the whole payload, the
        intent's ``order_type`` field carries the marker ``"cancel"``
        and size/price are read from the order struct itself (the
        signed maker/taker amounts that the cancellation refers to).
        """
        if order_idx >= len(decoded):
            return None
        order = decoded[order_idx]
        if not isinstance(order, (tuple, list)) or len(order) < 12:
            return None

        maker_addr = order[_ORDER_MAKER]
        token_id_int = int(order[_ORDER_TOKEN_ID])
        maker_amount = int(order[_ORDER_MAKER_AMOUNT])
        taker_amount = int(order[_ORDER_TAKER_AMOUNT])
        side_uint = int(order[_ORDER_SIDE])

        # side: 0=BUY, 1=SELL per Polymarket CTF Exchange.
        side: Literal["buy", "sell"] = "buy" if side_uint == 0 else "sell"

        # USDC has 6 decimals; outcome tokens are 18-decimal CTF shares.
        # On Polymarket the convention is:
        #   side=BUY  → maker spends USDC (makerAmount in USDC base
        #               units), receives takerAmount shares.
        #               price = makerAmount / takerAmount, size_usdc =
        #               makerAmount / 1e6.
        #   side=SELL → maker spends shares (makerAmount in shares),
        #               receives takerAmount USDC.
        #               price = takerAmount / makerAmount, size_usdc
        #               = takerAmount / 1e6.
        # Defensive: if either side is zero, fall back to a price of 0
        # and a size of 0 — the decoded intent is still useful to the
        # IntentRouter as a directional signal even if the numerics
        # are degenerate (cancelOrder, for instance, often has
        # zero-ish amounts in practice).
        if side == "buy":
            usdc_raw = maker_amount
            shares_raw = taker_amount
        else:
            usdc_raw = taker_amount
            shares_raw = maker_amount

        size_usdc = Decimal(usdc_raw) / Decimal(10**6)
        if shares_raw > 0:
            # Shares are 18-decimal CTF tokens; USDC is 6-decimal.
            # price = USDC per share, scaled to the [0, 1] range
            # Polymarket uses.
            # price (USDC/share) = (usdc_raw / 1e6) / (shares_raw / 1e18)
            #                    = usdc_raw * 1e12 / shares_raw
            price = (
                Decimal(usdc_raw) * Decimal(10**12) / Decimal(shares_raw)
            )
        else:
            price = Decimal(0)

        # Order type: V1 stamps a marker for cancels and "GTC" for
        # fills / matches. The contract carries no explicit order_type
        # in the struct above — Polymarket has variants (FOK / GTC /
        # GTD) encoded via the expiration field semantics, which is
        # too tangled for V1. The TODO below tracks promoting this to
        # a real enum once the IntentRouter actually needs to
        # distinguish.
        if method_name == "cancelOrder":
            order_type = "cancel"
        else:
            order_type = "GTC"  # default for fills/matches

        # market_id ↔ token mapping: the on-chain tokenId is the YES
        # or NO CTF token id. Mapping back to the Polymarket market_id
        # + outcome is a DB join (markets.token_yes / token_no), which
        # we can NOT do synchronously in the decode hot path. V1
        # stamps the token_id (lowercased 0x-prefixed hex of the
        # uint256) and lets the IntentRouter resolve to market_id at
        # consume time. The architect's doc note in tx_decoder.py
        # § "asset_id → market_id lookup miss" captures the same
        # deferral.
        token_id_hex = f"0x{token_id_int:064x}"

        # Normalise the maker address for the wallet field. Use the
        # maker (the leader signing the order), NOT tx.from_wallet:
        # for relayed / proxy submissions the tx may come from a
        # facilitator while the order itself is signed by the leader.
        # V1 fallback: if the maker address is the zero address, use
        # tx.from_wallet.
        if isinstance(maker_addr, str) and maker_addr:
            wallet = maker_addr.lower()
            if not wallet.startswith("0x"):
                wallet = "0x" + wallet
        else:
            wallet = tx.from_wallet

        return LeaderIntent(
            intent_id=str(uuid.uuid4()),
            wallet=wallet,
            market_id=token_id_hex,  # V1 placeholder — IntentRouter resolves
            token_id=token_id_hex,
            side=side,
            size_usdc=size_usdc,
            price=price,
            order_type=order_type,
            intent_received_at=tx.received_at,
            expected_block=0,  # filled by router at consume time
            tx_hash=tx.tx_hash,
            nonce=tx.nonce,
            replaces=tx.replaces,
        )
