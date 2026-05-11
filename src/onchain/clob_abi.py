"""Polymarket CTF Exchange contract ABI — pinned definition.

WAVE-1 ARCHITECT SKELETON. The actual ABI payload is left empty here;
Wave 2 pastes the official ABI verbatim from the operator-supplied
artefact (Polymarket publishes it via the Etherscan verification page
for the CTF Exchange contract, or via their GitHub).

We pin the ABI inline rather than fetching at runtime so:
  * The listener has zero external dependencies at boot beyond the RPC
    pool itself.
  * An ABI rotation (rare; CLOB contract upgrades are governance events)
    is a code change visible in a code review, not a silent runtime
    state-change.
  * The decoder in :mod:`src.onchain.event_decoder` can rely on a
    deterministic ABI shape during tests.

Contract address: see ``settings.POLYMARKET_CLOB_CONTRACT_ADDRESS``.

Events we care about (full list goes in the ABI):
  * ``OrderFilled(maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee)``
    — the primary trade event; one per individual fill.
  * ``OrdersMatched(takerOrderHash, takerOrderMaker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled)``
    — emitted when two limit orders match each other on-chain.
  * ``OrderCancelled(orderHash)`` — order removed before fill.
  * ``FeeRateUpdated(feeRate)`` — global fee-rate change.
  * ``TradingStatusUpdated(paused)`` — exchange paused / resumed.
"""

from __future__ import annotations

# TODO (Wave 2): paste the official Polymarket CTF Exchange ABI as a JSON
# array of dicts here. The expected shape is the standard solc output:
#   [
#       {"type": "event", "name": "OrderFilled", "inputs": [...], "anonymous": False},
#       {"type": "event", "name": "OrdersMatched", "inputs": [...], "anonymous": False},
#       ...
#   ]
# The decoder in event_decoder.py expects each event entry to have:
#   * ``type == "event"``
#   * ``name`` matches one of the documented event names
#   * ``inputs`` is a list of {"name", "type", "indexed"} dicts
POLYMARKET_CLOB_ABI: list[dict] = []  # TODO: paste the contract ABI here

# Event-name → topic-0 keccak (filled in by Wave 2 once ABI is pasted).
# The listener uses this to build the eth_subscribe filter without having
# to recompute the keccak on every boot. Computed at module import time
# by Wave 2 via web3.utils.keccak or eth-abi.event.encode_event_topic.
EVENT_TOPICS: dict[str, str] = {
    # "OrderFilled":      "0x...",
    # "OrdersMatched":    "0x...",
    # "OrderCancelled":   "0x...",
    # "FeeRateUpdated":   "0x...",
    # "TradingStatusUpdated": "0x...",
}

# Convenience: just the topic-0 list, ordered for use as the second
# element of an eth_subscribe ``topics`` filter (which expects a list
# of allowed topic-0 values).
TRADE_EVENT_TOPICS: list[str] = [
    # Populated by Wave 2 once EVENT_TOPICS is filled. Used by the
    # listener as ``topics=[TRADE_EVENT_TOPICS]`` to OR together every
    # trade-related event in one subscription.
]
