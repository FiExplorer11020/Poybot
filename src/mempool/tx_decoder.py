"""Polymarket CTF Exchange calldata decoder.

Round 7 / The Front Door — § 3.2.

The mempool subscription hands us raw :class:`src.mempool.node_client.MempoolTx`
objects with un-decoded ``calldata`` bytes. This module decodes those
bytes against the Polymarket CLOB function ABI to extract a
:class:`LeaderIntent`: the market, the token, the side (buy/sell), the
size in USDC, and the price.

Functions we decode (per R7 § 3.2)::

    fillOrder(order, signature, fillAmount, salt)
    matchOrders(takerOrder, makerOrders, ...)
    cancelOrder(order)

If the tx's function selector doesn't match any of these (e.g. an
unrelated ERC-20 approve, a proxy admin call, a CTF mint/burn) we
return ``None`` — the caller drops the tx without further work.

ABI source
----------
:mod:`src.onchain.clob_abi` ships an EVENT-ONLY ABI for R6 (the
on-chain listener only decodes inbound log events, not transactions
it sends). Wave-2 will need to EXTEND the ABI with FUNCTION
signatures for ``fillOrder`` / ``matchOrders`` / ``cancelOrder`` —
the on-chain decoder doesn't need them, so they weren't shipped in
R6. The expansion is a Wave-2 implementer concern; the docstring at
the top of ``src/onchain/clob_abi.py`` already calls this out as the
trigger condition.

  TODO (Wave 2): expand src.onchain.clob_abi.POLYMARKET_CLOB_ABI with
  function entries for fillOrder / matchOrders / cancelOrder. The
  authoritative source is the Polygonscan-verified ABI at the contract
  address (``settings.POLYMARKET_CLOB_CONTRACT_ADDRESS``).

Edge case: proxy contracts
--------------------------
Polymarket uses adapter / proxy contracts (UMA dispute resolution,
the negRiskAdapter, the CTF). A "fillOrder" call may arrive wrapped
in a proxy's ``call(target, data)`` envelope. Wave-2 will need to
peel the outer call and decode the inner calldata. Tracked as a
known-limitation TODO; the FIRST iteration can simply skip
unrecognised selectors and rely on ``polybot_mempool_tx_decoded_total
{result="not_clob"}`` to surface the miss rate.

Edge case: contract upgrade
---------------------------
A CLOB contract upgrade (rare governance event) rotates function
selectors and will cause a sudden ``decode_failed`` spike. The
operator alert is wired off ``polybot_mempool_tx_decoded_total
{result="decode_failed"}`` — runbook is "pin the new ABI, redeploy
mempool service".

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.2 for the full spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.node_client import MempoolTx


_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.2"


# The order-type set we surface to the IntentRouter. ``FOK`` (fill-or-
# kill) and ``GTC`` (good-til-cancelled) are the two we expect to see
# on Polymarket; ``GTD`` is "good-til-date". The CLOB encodes these in
# the order struct's ``orderType`` field — Wave-2 maps the on-chain
# uint8 enum to these strings.
OrderType = Literal["FOK", "GTC", "GTD"]


@dataclass(slots=True)
class LeaderIntent:
    """Decoded Polymarket trading intent — the unit the IntentRouter sees.

    This is the canonical event type published to
    ``mempool:leader_intent`` (Redis Stream). The JSON-on-the-wire
    schema is shaped by :meth:`src.mempool.event_emitter.LeaderIntentPublisher.publish`
    — see R7 § 3.4 for the contract.

    Attributes
    ----------
    intent_id
        A fresh UUID4 minted at decode time. PRIMARY KEY of the
        ``mempool_observations`` table (migration 024) and the
        correlation handle for every downstream log line.
    wallet
        The signer EOA (mirrors :attr:`MempoolTx.from_wallet`).
    market_id
        Polymarket market id (the condition id / questionId on the
        CTF). Decoded by mapping ``makerAssetId`` ↔ ``takerAssetId``
        against the CTF token → market lookup.
    token_id
        The specific outcome token (YES or NO) the leader is targeting.
    side
        ``"buy"`` if the leader is acquiring shares, ``"sell"`` if
        selling. For ``matchOrders`` / ``fillOrder`` we read this from
        the order struct's ``side`` field.
    size_usdc
        Notional in USDC for the trade (size_shares * price). We use
        ``Decimal`` end-to-end on the money path so we never lose
        rounding precision before the DB row writes (the DB column is
        ``NUMERIC(20,2)``).
    price
        Limit price per share, in [0, 1]. For ``fillOrder`` this is
        ``makerAmount / takerAmount`` (or the inverse, depending on
        side). ``Decimal`` for the same reason as ``size_usdc``.
    order_type
        One of ``"FOK"`` / ``"GTC"`` / ``"GTD"``. See :data:`OrderType`.
    intent_received_at
        Wall-clock time the SUBSCRIPTION saw the tx (mirrors
        :attr:`MempoolTx.received_at`). This is t=0 for the
        ``polybot_intent_router_latency_seconds`` histogram.
    expected_block
        The next Polygon block number — i.e., the earliest block this
        tx can mine in. The decoder reads it from
        ``RPCClient.eth_getBlockByNumber('latest')['number'] + 1``.
        Used to bound staleness on the IntentRouter side and to populate
        the ``expected_block`` column of ``mempool_observations``.
    tx_hash
        Mirrors :attr:`MempoolTx.tx_hash`.
    nonce
        Mirrors :attr:`MempoolTx.nonce`.
    replaces
        Mirrors :attr:`MempoolTx.replaces` — the tx_hash this one
        displaces, or ``None``.
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


class CLOBTxDecoder:
    """Decode :class:`MempoolTx.calldata` against the Polymarket CLOB ABI.

    Single public method :meth:`decode` returns a :class:`LeaderIntent`
    on a successful decode or ``None`` if the tx targets a function we
    don't care about / can't decode.

    Wave-2 implementation outline
    -----------------------------
    1. Build an in-memory selector → method map at construction time::

           selector(0xabcdef12) -> ("fillOrder", inputs_list)
           selector(0x...)      -> ("matchOrders", inputs_list)
           selector(0x...)      -> ("cancelOrder", inputs_list)

       where ``selector`` is ``keccak256(method_signature)[:4]``.
       The method signature is ``methodName(type1,type2,...)`` per
       Solidity ABI v2.

    2. In :meth:`decode`:

       a. Extract the 4-byte selector from ``calldata[:4]``.
       b. Look it up. Miss → ``polybot_mempool_tx_decoded_total
          {result="not_clob"}.inc()``, return ``None``.
       c. ABI-decode ``calldata[4:]`` against the inputs list using
          ``eth_abi.decode(types, data)`` (already a R6 dep).
       d. Build the :class:`LeaderIntent` from the decoded fields.
       e. ``polybot_mempool_tx_decoded_total{result="decoded"}.inc()``.
       f. Return.

    3. ABI-decode exception → ``decode_failed`` counter and ``None``.
       Do NOT raise — the subscription must stay healthy regardless of
       individual decode failures.
    """

    def __init__(self, abi: Optional[list[dict]] = None) -> None:
        """Construct with an optional ABI override.

        Parameters
        ----------
        abi
            If supplied, overrides the default :data:`src.onchain.clob_abi.POLYMARKET_CLOB_ABI`.
            Useful for tests that pin a known function set. In
            production we read from the on-chain module.
        """
        raise NotImplementedError(_WAVE_2_REF)

    def decode(self, tx: "MempoolTx") -> Optional[LeaderIntent]:
        """Decode ``tx.calldata`` into a :class:`LeaderIntent`.

        Returns ``None`` when:
          * ``tx.calldata`` is shorter than 4 bytes (selector-less call).
          * The selector is not in our method map (not a CLOB call we
            care about).
          * ABI decoding raises.
          * The decoded order targets a market / token we don't have a
            mapping for (asset_id → market_id lookup miss — same
            problem the on-chain listener has at R6, deferred to a
            Wave-2 join).

        On success returns a fully-populated :class:`LeaderIntent`
        with a fresh ``intent_id`` (uuid4) and ``intent_received_at``
        copied from ``tx.received_at``.

        Wave-2 must also fill in ``expected_block`` — most callers
        will inject an async ``RPCClient`` reference at construction
        time and look up ``eth_getBlockByNumber('latest')`` lazily;
        the OWNED-rpc-client variant is what we expect to use in
        ``src.mempool.main``.
        """
        raise NotImplementedError(_WAVE_2_REF)
