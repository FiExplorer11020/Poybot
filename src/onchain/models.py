"""Dataclasses for decoded Polymarket CTF Exchange events.

Each dataclass is the typed output of one
:class:`src.onchain.event_decoder.EventDecoder` method. The shape mirrors
the canonical "event dict" documented in
:mod:`src.onchain.event_decoder`, but a slotted dataclass gives us:

  * Cheaper memory (no per-instance ``__dict__``).
  * IDE / typecheck-friendly field access.
  * A natural ``to_dict()`` for the Redis-stream JSON payload.

The listener consumes these via :meth:`CLOBChainListener._publish_event`.
Conversion to dict happens once at publish time; the stream payload is
JSON-encoded so dataclass identity doesn't survive the wire.

Tx context (block_number, tx_hash, log_index, block_time) is repeated on
every event. This is intentional — downstream consumers don't have a
shared event metadata table, and the (tx_hash, log_index) tuple is the
idempotency key for the partial UNIQUE INDEX from migration 021.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class _TxContext:
    """Common transaction-level fields. Composed into every event so
    consumers don't need to reach into a nested dict to dedup.
    """

    block_number: int
    tx_hash: str
    log_index: int
    # Unix epoch seconds. The listener fills this either from the raw log
    # (some providers attach ``blockTimestamp``) or via a single
    # ``eth_getBlockByNumber`` call. NaN-safe: 0.0 means "unknown".
    block_time: float = 0.0


@dataclass(slots=True)
class OrderFilledEvent:
    """Decoded ``OrderFilled`` event.

    Polymarket emits one of these per individual fill. ``maker`` and
    ``taker`` are address topics — they're the canonical wallet
    attribution for the trade (no REST cross-reference needed).
    """

    order_hash: str
    maker: str
    taker: str
    maker_asset_id: int
    taker_asset_id: int
    maker_amount_filled: int
    taker_amount_filled: int
    fee: int

    # Transaction context (block_number / tx_hash / log_index / block_time).
    # We inline rather than compose because slotted dataclasses can't
    # cleanly compose another slotted dataclass without extra metaclass
    # plumbing; keeping the fields flat trades a tiny duplication for
    # zero-import simplicity at every call site.
    block_number: int = 0
    tx_hash: str = ""
    log_index: int = 0
    block_time: float = 0.0

    event_type: str = "OrderFilled"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class OrdersMatchedEvent:
    """Decoded ``OrdersMatched`` event.

    Emitted when two limit orders match on-chain; often paired with one
    or more :class:`OrderFilledEvent` instances in the same tx. The
    ``(tx_hash, log_index)`` UNIQUE INDEX keeps downstream consumers
    safe even if they receive both.
    """

    taker_order_hash: str
    taker_order_maker: str
    maker_asset_id: int
    taker_asset_id: int
    maker_amount_filled: int
    taker_amount_filled: int

    block_number: int = 0
    tx_hash: str = ""
    log_index: int = 0
    block_time: float = 0.0

    event_type: str = "OrdersMatched"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class OrderCancelledEvent:
    """Decoded ``OrderCancelled`` event.

    Carries only the order hash — no wallet attribution. Downstream
    consumers JOIN by ``order_hash`` against past OrderFilled rows to
    recover the maker.
    """

    order_hash: str

    block_number: int = 0
    tx_hash: str = ""
    log_index: int = 0
    block_time: float = 0.0

    event_type: str = "OrderCancelled"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class FeeRateUpdatedEvent:
    """Decoded ``FeeRateUpdated`` event.

    Carries the new fee rate in basis points. Bot impact: the
    PaperTrader snapshots fee rate at trade time; this event signals an
    operator that markets.fee_rate_pct needs a refresh.
    """

    new_fee_rate_bps: int

    block_number: int = 0
    tx_hash: str = ""
    log_index: int = 0
    block_time: float = 0.0

    event_type: str = "FeeRateUpdated"

    def to_dict(self) -> dict:
        return asdict(self)


# Convenience union — for type hints + runtime instanceof checks in the
# listener's publish path.
DecodedEvent = (
    OrderFilledEvent
    | OrdersMatchedEvent
    | OrderCancelledEvent
    | FeeRateUpdatedEvent
)

# Set of event types that produce a row in ``trades_observed`` (vs
# infrastructure events that only flow through Redis).
TRADE_EVENT_TYPES: set[str] = {"OrderFilled", "OrdersMatched"}
