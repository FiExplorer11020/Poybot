"""Polymarket CTF Exchange contract ABI — pinned definition.

Source
------
Polymarket CTF Exchange (NegRiskCtfExchange) on Polygon mainnet at
``0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E``. Event signatures derived
from the public Polymarket contract repository (``polymarket/ctf-exchange``)
and the Polygonscan-verified ABI on
``https://polygonscan.com/address/0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E``.

This file ships the MINIMAL event-only ABI needed by
:mod:`src.onchain.event_decoder` — just the four event entries our
listener subscribes to (OrderFilled, OrderCancelled, OrdersMatched, plus
the FeeRateUpdated infrastructure event). The full method ABI is not
required because the listener never builds outgoing transactions — it
only decodes inbound log events. A Wave-3 reviewer should flag if a
future caller needs eth_call against this contract (then the full ABI
must be pasted in).

Pinning rationale (per Wave-1 architect note):
  * Listener has zero external dependencies at boot beyond the RPC pool.
  * An ABI rotation (CLOB contract upgrade — rare governance event) is
    a visible code change, not a silent runtime state change.
  * Decoder gets a deterministic ABI shape for tests.

Event topic-0 hashes are precomputed at import time via ``eth_utils.keccak``.
The listener uses :data:`TRADE_EVENT_TOPICS` as the second element of an
``eth_subscribe`` ``topics`` filter — Polymarket emits all three trade
event types from the same contract, so a single subscription with an
OR'd topic-0 list covers the lot.
"""

from __future__ import annotations

from eth_utils import keccak

# ---------------------------------------------------------------------------
# Event-only ABI (subset of the full CTF Exchange ABI)
# ---------------------------------------------------------------------------
#
# Each entry is the standard solc event shape. The decoder reads:
#   * ``name``    — used to map topic-0 → handler.
#   * ``inputs``  — ordered list of {"name", "type", "indexed"}. The
#                   decoder splits indexed inputs (one topic each) from
#                   non-indexed (concatenated into ``log.data``).
#
# Type strings match Solidity ABI v2 exactly so ``eth_abi.decode`` can
# consume them directly.
POLYMARKET_CLOB_ABI: list[dict] = [
    {
        "type": "event",
        "anonymous": False,
        "name": "OrderFilled",
        "inputs": [
            {"name": "orderHash", "type": "bytes32", "indexed": True},
            {"name": "maker", "type": "address", "indexed": True},
            {"name": "taker", "type": "address", "indexed": True},
            {"name": "makerAssetId", "type": "uint256", "indexed": False},
            {"name": "takerAssetId", "type": "uint256", "indexed": False},
            {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
            {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
            {"name": "fee", "type": "uint256", "indexed": False},
        ],
    },
    {
        "type": "event",
        "anonymous": False,
        "name": "OrdersMatched",
        "inputs": [
            {"name": "takerOrderHash", "type": "bytes32", "indexed": True},
            {"name": "takerOrderMaker", "type": "address", "indexed": True},
            {"name": "makerAssetId", "type": "uint256", "indexed": False},
            {"name": "takerAssetId", "type": "uint256", "indexed": False},
            {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
            {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        ],
    },
    {
        "type": "event",
        "anonymous": False,
        "name": "OrderCancelled",
        "inputs": [
            {"name": "orderHash", "type": "bytes32", "indexed": True},
        ],
    },
    {
        "type": "event",
        "anonymous": False,
        "name": "FeeRateUpdated",
        "inputs": [
            {"name": "newFeeRateBps", "type": "uint256", "indexed": False},
        ],
    },
]


def _event_signature(entry: dict) -> str:
    """Build the canonical Solidity event signature string.

    ``EventName(type1,type2,...)`` — note: no spaces, no parameter names,
    indexed flag does NOT affect the keccak input. Matches the way
    web3.py / eth-abi compute topic-0 internally.
    """
    types = ",".join(inp["type"] for inp in entry["inputs"])
    return f"{entry['name']}({types})"


def _topic0(entry: dict) -> str:
    """Compute the topic-0 hex string (0x-prefixed) for an event entry."""
    sig = _event_signature(entry)
    return "0x" + keccak(text=sig).hex()


# Lookup tables built once at import.
#   EVENT_TOPICS    : event-name → topic-0 hex
#   TOPIC_TO_EVENT  : topic-0 hex (lowercased) → event-name  (decoder dispatch)
#   EVENT_INPUTS    : event-name → ordered inputs list (decoder)
EVENT_TOPICS: dict[str, str] = {
    entry["name"]: _topic0(entry) for entry in POLYMARKET_CLOB_ABI
}

TOPIC_TO_EVENT: dict[str, str] = {
    topic.lower(): name for name, topic in EVENT_TOPICS.items()
}

EVENT_INPUTS: dict[str, list[dict]] = {
    entry["name"]: entry["inputs"] for entry in POLYMARKET_CLOB_ABI
}


# Trade event topics — the listener OR's these into a single eth_subscribe
# filter so one subscription captures every fill / cancel / match.
TRADE_EVENT_TOPICS: list[str] = [
    EVENT_TOPICS["OrderFilled"],
    EVENT_TOPICS["OrdersMatched"],
    EVENT_TOPICS["OrderCancelled"],
]
