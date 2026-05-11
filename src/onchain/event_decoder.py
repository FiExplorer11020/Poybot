"""Per-event-type ABI decoder for the Polymarket CLOB contract.

Wave-2 implementation. Built on ``eth_abi`` (not the full web3.py stack)
to keep the listener boot footprint small. The decoder only reads logs —
no contract calls, no transaction building — so a thin ABI decoder is
the right amount of dependency.

Decode flow
-----------

1. :meth:`EventDecoder.decode_any` reads ``log["topics"][0]`` and looks
   it up in :data:`src.onchain.clob_abi.TOPIC_TO_EVENT`.
2. The matched event name routes to the per-event decoder
   (``decode_order_filled`` / ``decode_orders_matched`` / ...).
3. Each per-event decoder:
     * Splits ABI inputs into indexed (one topic each) + non-indexed
       (concatenated into ``log.data``).
     * Decodes the non-indexed slice with ``eth_abi.decode``.
     * Decodes each indexed topic with ``eth_abi.decode`` on its 32-byte
       slot.
     * Pulls block_number / tx_hash / log_index / block_time off the
       raw log.
     * Returns the appropriate dataclass.
4. Malformed payload → catches ``Exception``, logs at DEBUG, increments
   ``polybot_chain_events_failed_decode_total{event_type, reason="malformed"}``,
   and returns ``None``. One bad event must not kill the listener.

Idempotency
-----------

The decoder is stateless. Calling ``decode_any(log)`` twice on the same
log returns equal dataclasses. The listener relies on this for the
``ON CONFLICT (tx_hash, log_index) DO NOTHING`` replay path.
"""

from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode
from loguru import logger

from src.onchain.clob_abi import (
    EVENT_INPUTS,
    EVENT_TOPICS,
    TOPIC_TO_EVENT,
)
from src.onchain.models import (
    DecodedEvent,
    FeeRateUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrdersMatchedEvent,
)

try:
    from src.monitoring.metrics import chain_events_failed_decode_total
except Exception:  # pragma: no cover — defensive: metrics optional
    chain_events_failed_decode_total = None  # type: ignore[assignment]


def _bump_failed_decode(event_type: str, reason: str) -> None:
    """Best-effort metric increment. Never let a metric error bubble out
    of the decoder — a bad metrics module shouldn't disable ingestion.
    """
    if chain_events_failed_decode_total is None:
        return
    try:
        chain_events_failed_decode_total.labels(
            event_type=event_type, reason=reason
        ).inc()
    except Exception:
        pass


def _to_int(num: Any) -> int:
    """Coerce a JSON-RPC int field. Providers return ``"0x..."`` hex strings
    for block_number / log_index; some test fixtures pass plain ints.
    """
    if isinstance(num, int):
        return num
    if isinstance(num, str):
        s = num.strip()
        if s.startswith(("0x", "0X")):
            return int(s, 16)
        return int(s)
    raise TypeError(f"can't coerce {num!r} to int")


def _strip_0x(s: str) -> str:
    return s[2:] if s.startswith(("0x", "0X")) else s


def _topic_to_address(topic: str) -> str:
    """Indexed address topics are 32-byte padded; the address is the
    last 20 bytes. Returns a 0x-prefixed lowercased hex address.
    """
    raw = _strip_0x(topic)
    if len(raw) < 64:
        raise ValueError(f"address topic too short: {topic!r}")
    return "0x" + raw[-40:].lower()


def _topic_to_bytes32(topic: str) -> str:
    """Bytes32 topics carry the raw 32 bytes directly. Return 0x-prefixed
    lowercased hex.
    """
    raw = _strip_0x(topic)
    if len(raw) != 64:
        raise ValueError(f"bytes32 topic wrong length: {topic!r}")
    return "0x" + raw.lower()


def _topic_to_uint(topic: str) -> int:
    """Uint topics carry the integer as 32-byte big-endian hex."""
    raw = _strip_0x(topic)
    return int(raw, 16) if raw else 0


def _decode_indexed(topic: str, abi_type: str) -> Any:
    """Decode one indexed topic against its ABI type."""
    if abi_type == "address":
        return _topic_to_address(topic)
    if abi_type == "bytes32":
        return _topic_to_bytes32(topic)
    if abi_type.startswith("uint") or abi_type.startswith("int"):
        return _topic_to_uint(topic)
    # Fallback: hex string. Indexed strings/bytes are hashed in topics —
    # not recoverable. We don't use them in the CLOB ABI.
    return topic


class EventDecoder:
    """ABI decoder for the Polymarket CTF Exchange events we care about.

    Construction is cheap — the decoder caches the per-event ABI input
    layout from :mod:`src.onchain.clob_abi` at init. Every method is
    pure: no DB, no Redis, no network. The listener is the only stateful
    consumer.
    """

    def __init__(self) -> None:
        # Cache: event-name → (indexed_inputs, non_indexed_inputs).
        # Done once at construction so the hot path doesn't re-split.
        self._indexed: dict[str, list[dict]] = {}
        self._non_indexed: dict[str, list[dict]] = {}
        for name, inputs in EVENT_INPUTS.items():
            self._indexed[name] = [i for i in inputs if i.get("indexed")]
            self._non_indexed[name] = [i for i in inputs if not i.get("indexed")]

    # ------------------------------------------------------------------ #
    # Dispatch                                                            #
    # ------------------------------------------------------------------ #

    def decode_any(self, raw_log: dict) -> DecodedEvent | None:
        """Match topic-0 → event name and delegate to the typed decoder.

        Returns:
            One of the four event dataclasses on success, ``None`` if:
              * topic[0] isn't a known event (silent skip, no metric).
              * the underlying decode raises (logs DEBUG, increments the
                failed-decode metric, returns None).
        """
        topics = raw_log.get("topics") or []
        if not topics:
            return None
        topic0 = str(topics[0]).lower()
        event_name = TOPIC_TO_EVENT.get(topic0)
        if event_name is None:
            return None
        try:
            if event_name == "OrderFilled":
                return self.decode_order_filled(raw_log)
            if event_name == "OrdersMatched":
                return self.decode_orders_matched(raw_log)
            if event_name == "OrderCancelled":
                return self.decode_order_cancelled(raw_log)
            if event_name == "FeeRateUpdated":
                return self.decode_fee_rate_updated(raw_log)
        except Exception as exc:
            logger.debug(
                f"EventDecoder: failed to decode {event_name} log: {exc!r}"
            )
            _bump_failed_decode(event_name, "malformed")
            return None
        return None

    # Back-compat alias — the architect skeleton referenced ``decode_log``.
    def decode_log(self, raw_log: dict) -> DecodedEvent | None:
        return self.decode_any(raw_log)

    # ------------------------------------------------------------------ #
    # Per-event decoders                                                  #
    # ------------------------------------------------------------------ #

    def decode_order_filled(self, raw_log: dict) -> OrderFilledEvent | None:
        """Decode an ``OrderFilled(bytes32, address, address, uint256,
        uint256, uint256, uint256, uint256)`` log.

        Indexed topics (3): orderHash, maker, taker.
        Non-indexed (5): makerAssetId, takerAssetId, makerAmountFilled,
        takerAmountFilled, fee.
        """
        if not self._is_event(raw_log, "OrderFilled"):
            return None
        try:
            indexed, non_indexed = self._decode_split(raw_log, "OrderFilled")
            ctx = self._tx_context(raw_log)
            return OrderFilledEvent(
                order_hash=indexed["orderHash"],
                maker=indexed["maker"],
                taker=indexed["taker"],
                maker_asset_id=non_indexed["makerAssetId"],
                taker_asset_id=non_indexed["takerAssetId"],
                maker_amount_filled=non_indexed["makerAmountFilled"],
                taker_amount_filled=non_indexed["takerAmountFilled"],
                fee=non_indexed["fee"],
                **ctx,
            )
        except Exception as exc:
            logger.debug(f"decode_order_filled failed: {exc!r}")
            _bump_failed_decode("OrderFilled", "malformed")
            return None

    def decode_orders_matched(self, raw_log: dict) -> OrdersMatchedEvent | None:
        """Decode an ``OrdersMatched(bytes32, address, uint256, uint256,
        uint256, uint256)`` log.

        Indexed (2): takerOrderHash, takerOrderMaker.
        Non-indexed (4): makerAssetId, takerAssetId, makerAmountFilled,
        takerAmountFilled.
        """
        if not self._is_event(raw_log, "OrdersMatched"):
            return None
        try:
            indexed, non_indexed = self._decode_split(raw_log, "OrdersMatched")
            ctx = self._tx_context(raw_log)
            return OrdersMatchedEvent(
                taker_order_hash=indexed["takerOrderHash"],
                taker_order_maker=indexed["takerOrderMaker"],
                maker_asset_id=non_indexed["makerAssetId"],
                taker_asset_id=non_indexed["takerAssetId"],
                maker_amount_filled=non_indexed["makerAmountFilled"],
                taker_amount_filled=non_indexed["takerAmountFilled"],
                **ctx,
            )
        except Exception as exc:
            logger.debug(f"decode_orders_matched failed: {exc!r}")
            _bump_failed_decode("OrdersMatched", "malformed")
            return None

    def decode_order_cancelled(self, raw_log: dict) -> OrderCancelledEvent | None:
        """Decode an ``OrderCancelled(bytes32)`` log."""
        if not self._is_event(raw_log, "OrderCancelled"):
            return None
        try:
            indexed, _ = self._decode_split(raw_log, "OrderCancelled")
            ctx = self._tx_context(raw_log)
            return OrderCancelledEvent(
                order_hash=indexed["orderHash"],
                **ctx,
            )
        except Exception as exc:
            logger.debug(f"decode_order_cancelled failed: {exc!r}")
            _bump_failed_decode("OrderCancelled", "malformed")
            return None

    def decode_fee_rate_updated(self, raw_log: dict) -> FeeRateUpdatedEvent | None:
        """Decode a ``FeeRateUpdated(uint256)`` log."""
        if not self._is_event(raw_log, "FeeRateUpdated"):
            return None
        try:
            _, non_indexed = self._decode_split(raw_log, "FeeRateUpdated")
            ctx = self._tx_context(raw_log)
            return FeeRateUpdatedEvent(
                new_fee_rate_bps=non_indexed["newFeeRateBps"],
                **ctx,
            )
        except Exception as exc:
            logger.debug(f"decode_fee_rate_updated failed: {exc!r}")
            _bump_failed_decode("FeeRateUpdated", "malformed")
            return None

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _is_event(self, raw_log: dict, event_name: str) -> bool:
        """Cheap topic-0 check before doing real decode work."""
        topics = raw_log.get("topics") or []
        if not topics:
            return False
        return str(topics[0]).lower() == EVENT_TOPICS[event_name].lower()

    def _decode_split(
        self, raw_log: dict, event_name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Split + decode the log's indexed topics and data blob.

        Returns:
            ``(indexed_values, non_indexed_values)`` — both name-keyed
            dicts. The caller picks fields by name to build the typed
            dataclass.

        Raises:
            ValueError when the topic count doesn't match the indexed
            input count, or eth_abi raises on a malformed data blob.
        """
        topics = raw_log.get("topics") or []
        indexed_inputs = self._indexed[event_name]
        non_indexed_inputs = self._non_indexed[event_name]

        # topics[0] is the event signature; topics[1:] are the indexed
        # values in declaration order.
        if len(topics) - 1 != len(indexed_inputs):
            raise ValueError(
                f"{event_name}: expected {len(indexed_inputs)} indexed "
                f"topics, got {len(topics) - 1}"
            )
        indexed_values: dict[str, Any] = {}
        for inp, topic in zip(indexed_inputs, topics[1:]):
            indexed_values[inp["name"]] = _decode_indexed(str(topic), inp["type"])

        # ``data`` is hex-encoded ABI-encoded non-indexed inputs.
        data = raw_log.get("data") or "0x"
        data_bytes = bytes.fromhex(_strip_0x(data))
        non_indexed_types = [inp["type"] for inp in non_indexed_inputs]
        if non_indexed_types:
            decoded = abi_decode(non_indexed_types, data_bytes)
        else:
            decoded = ()
        non_indexed_values = {
            inp["name"]: value
            for inp, value in zip(non_indexed_inputs, decoded)
        }
        return indexed_values, non_indexed_values

    @staticmethod
    def _tx_context(raw_log: dict) -> dict[str, Any]:
        """Build the (block_number, tx_hash, log_index, block_time) kwargs
        for a dataclass constructor. Tolerant of provider quirks:
          * block_number / log_index may arrive as hex strings or ints.
          * block_time may be absent (some providers omit it); the
            listener fills it in later via eth_getBlockByNumber.
        """
        block_number_raw = raw_log.get("blockNumber") or raw_log.get("block_number") or 0
        log_index_raw = raw_log.get("logIndex") or raw_log.get("log_index") or 0
        tx_hash = (
            raw_log.get("transactionHash")
            or raw_log.get("transaction_hash")
            or ""
        )
        block_time = raw_log.get("blockTimestamp") or raw_log.get("block_time") or 0
        try:
            block_number = _to_int(block_number_raw)
        except Exception:
            block_number = 0
        try:
            log_index = _to_int(log_index_raw)
        except Exception:
            log_index = 0
        try:
            block_time_val = float(_to_int(block_time)) if isinstance(block_time, str) and block_time.startswith(("0x", "0X")) else float(block_time)
        except Exception:
            block_time_val = 0.0
        return {
            "block_number": block_number,
            "tx_hash": str(tx_hash),
            "log_index": log_index,
            "block_time": block_time_val,
        }
