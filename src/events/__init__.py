"""
Typed Redis pub/sub event schemas (Pydantic v2).

Producers must serialise events via the model's ``.model_dump_json()`` and
consumers must validate via ``.model_validate()`` (already-decoded dict from
the Subscriber utility) or ``.model_validate_json()`` (raw str).

Each model uses ``extra="forbid"`` so producer/consumer drift surfaces as a
``ValidationError`` at runtime — the whole point of this module.

Channel ↔ schema mapping (single source of truth):

    trades:observed                  → TradeObserved
    decisions                        → DecisionMade
    positions:paper_closed           → PositionClosed
    system:status                    → SystemStatusChanged
    reconciliation:completed         → ReconciliationCompleted

See ``docs/events.md`` for payload examples and ``tests/events/`` for
round-trip + negative-drift tests.
"""

from src.events.schemas import (
    CHANNEL_DECISIONS,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_RECONCILIATION,
    CHANNEL_SYSTEM_STATUS,
    CHANNEL_TRADES_OBSERVED,
    DecisionMade,
    PositionClosed,
    ReconciliationCompleted,
    SystemStatusChanged,
    TradeObserved,
)

__all__ = [
    "CHANNEL_DECISIONS",
    "CHANNEL_PAPER_CLOSED",
    "CHANNEL_RECONCILIATION",
    "CHANNEL_SYSTEM_STATUS",
    "CHANNEL_TRADES_OBSERVED",
    "DecisionMade",
    "PositionClosed",
    "ReconciliationCompleted",
    "SystemStatusChanged",
    "TradeObserved",
]
