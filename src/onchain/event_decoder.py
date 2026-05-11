"""Per-event-type ABI decoder for the Polymarket CLOB contract.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.3.

Mirrors the structure of the trade_observer's existing decode helpers
(see src/observer/trade_observer.py and src/observer/websocket_client.py
for the project's house style of "one decoder method per event class").

The output of every decode_* method is a canonical dict ready for the
listener's downstream UPSERT into trades_observed and XADD into
``chain:trades:stream``::

    {
        "event_type": "OrderFilled" | "OrdersMatched" | ...,
        "wallet_address": "0x...",          # canonical maker for trade events
        "counterparty": "0x..." | None,     # taker if known
        "market_id": "0x...",               # the Polymarket conditionId / question hash
        "token_id": "0x...",                # outcome token id (YES or NO)
        "side": "buy" | "sell",
        "price": float,                     # decoded from amount ratios
        "size_usdc": float,                 # USDC notional of the fill
        "block_number": int,
        "tx_hash": "0x...",
        "log_index": int,
        "block_timestamp": float,           # unix epoch seconds
        "raw": dict,                        # original log dict for forensics
    }

For non-trade events (FeeRateUpdated, TradingStatusUpdated), the output
shape differs and is published to a separate channel — those are
infrastructure events that affect the bot's pricing assumptions.
"""

from __future__ import annotations

from typing import Any


class EventDecoder:
    """ABI decoder for the Polymarket CTF Exchange events we care about.

    One method per event type. Each method takes the raw eth_subscribe
    log dict and returns either:
      * A canonical event dict (see module docstring), OR
      * None if the event doesn't match this decoder (cheap defensive
        check on topic[0] before delegating to the typed decoder).

    Wave 2 uses ``eth_abi.codec.ABICodec`` (the standard library for
    ABI decode in Python's web3 stack) keyed off ``clob_abi.POLYMARKET_CLOB_ABI``.
    """

    def __init__(self) -> None:
        """Build the codec from POLYMARKET_CLOB_ABI at construction.

        Wave 2 caches:
          * Per-event signature → input-type list for fast decode.
          * Per-topic-0 → event-name lookup for the dispatch path.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.3
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_log(self, raw_log: dict) -> dict | None:
        """Dispatch a raw log to the appropriate per-event decoder.

        This is the single entry point used by CLOBChainListener:

            for raw in rpc_client.eth_subscribe(...):
                event = decoder.decode_log(raw)
                if event is None:
                    continue   # not one of ours
                ...

        Args:
            raw_log: Raw JSON-RPC log dict. Has keys:
                ``address``, ``topics``, ``data``, ``blockNumber``,
                ``transactionHash``, ``logIndex``,
                ``blockTimestamp`` (some providers; otherwise
                eth_getBlockByNumber on cache-miss).

        Returns:
            Canonical event dict (see module docstring), or None when:
              * topic[0] doesn't match any tracked event
              * decode raises (logs WARN; treat as "skip" so one bad
                event doesn't kill the listener)
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_order_filled(self, raw_log: dict) -> dict | None:
        """Decode an ``OrderFilled`` event.

        Event signature (per the contract spec)::

            OrderFilled(
                bytes32 orderHash,
                address indexed maker,
                address indexed taker,
                uint256 makerAssetId,
                uint256 takerAssetId,
                uint256 makerAmountFilled,
                uint256 takerAmountFilled,
                uint256 fee
            )

        Implementation notes:
          * ``maker`` and ``taker`` are indexed topics — they live in
            log.topics[1] and log.topics[2] respectively.
          * ``makerAssetId`` / ``takerAssetId`` resolve to either USDC
            (the cash leg) or an outcome token. Whichever is the
            non-USDC asset is the position token; the side ('buy' or
            'sell') is determined by whether maker or taker is paying USDC.
          * Price = USDC notional / outcome-token shares (both decoded
            from the amount fields).
          * Wave 2 needs the ConditionalTokens contract's position-id
            → (conditionId, outcomeIndex) mapping. Either a cached
            eth_call against ConditionalTokens.payoutDenominator /
            getCondition, or a precomputed lookup table.

        Returns:
            Canonical event dict, or None on decode failure.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_orders_matched(self, raw_log: dict) -> dict | None:
        """Decode an ``OrdersMatched`` event.

        Event signature::

            OrdersMatched(
                bytes32 indexed takerOrderHash,
                address indexed takerOrderMaker,
                uint256 makerAssetId,
                uint256 takerAssetId,
                uint256 makerAmountFilled,
                uint256 takerAmountFilled
            )

        Often paired with one or more OrderFilled events in the same
        transaction. The listener emits both — downstream consumers
        deduplicate via the (tx_hash, log_index) UNIQUE INDEX from
        migration 021.

        Returns:
            Canonical event dict, or None on decode failure.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_order_cancelled(self, raw_log: dict) -> dict | None:
        """Decode an ``OrderCancelled`` event.

        Event signature::

            OrderCancelled(bytes32 indexed orderHash)

        Returned dict diverges from the trade-event shape — there's no
        wallet attribution or size info in this event alone (the
        listener can resolve orderHash → maker via a cached lookup
        table populated from prior OrderFilled events). Wave 2 either:
          * Maintains a Redis-backed orderHash → wallet cache, or
          * Defers attribution to a downstream consumer that does the
            join against trades_observed by orderHash.

        Returns:
            ``{"event_type": "OrderCancelled", "order_hash": ...,
              "block_number": ..., "tx_hash": ..., "log_index": ...}``,
            or None on decode failure.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_fee_rate_updated(self, raw_log: dict) -> dict | None:
        """Decode a ``FeeRateUpdated`` event — a governance action that
        changes the global fee rate.

        Bot impact: the PaperTrader's PnL calculation snapshots the fee
        rate at trade time; future trades pick up the new rate via
        the markets.fee_rate_pct refresh path. The listener publishes
        a high-signal alert on the dedicated ``chain:gov:stream`` so
        operators see the change immediately.

        Returns:
            ``{"event_type": "FeeRateUpdated", "fee_rate": float,
              "block_number": ..., "tx_hash": ..., "log_index": ...}``,
            or None on decode failure.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def decode_trading_status_updated(self, raw_log: dict) -> dict | None:
        """Decode a ``TradingStatusUpdated`` event — the exchange has
        been paused or resumed.

        Bot impact: paper trades may still flow during a pause, but
        live trades cannot fill. The listener publishes to
        ``chain:gov:stream``; engine/live_trader consumes and respects
        the paused state by gating live orders.

        Returns:
            ``{"event_type": "TradingStatusUpdated", "paused": bool, ...}``,
            or None on decode failure.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")

    def _resolve_market_and_token(
        self,
        maker_asset_id: int,
        taker_asset_id: int,
    ) -> tuple[str | None, str | None, str | None]:
        """Helper: figure out which side is USDC vs an outcome token,
        and which market the outcome token belongs to.

        Returns:
            ``(market_id, token_id, side)``. ``side`` is from the
            taker's perspective: 'buy' if taker pays USDC for outcome
            token, 'sell' otherwise. Any element may be None if the
            asset IDs don't map to a known market (logged as warning;
            event is dropped).

        Wave 2 owns the asset-id → market/token mapping logic; likely
        a cached eth_call against ConditionalTokens contract +
        precomputed dictionary persisted in Redis.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.3")
