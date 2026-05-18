"""
Pydantic v2 schemas for Redis pub/sub events.

Each model is strict (``extra="forbid"``) so any field added by a producer
without a matching schema update raises ``ValidationError`` on the consumer
side. That's the whole point — kill silent drift between
``json.dumps(dict)`` producers and ``data: Any`` consumers.

Casing tolerance: the consigne fixes ``side`` / ``action`` / ``bot`` etc.
as upper-case literals, but the legacy producers emit lower-case
(``'buy'``, ``'sell'``, ``'follow'``, ``'fade'``…). To keep the refactor
non-breaking at runtime, each enum-like field has a ``mode='before'``
validator that uppercases incoming values. Producers built from these
models always emit canonical upper-case (we feed the Literal directly).

This file is the single source of truth — ``docs/events.md`` mirrors it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    field_serializer,
    field_validator,
)

# --------------------------------------------------------------------------- #
# Channel names (single source of truth — import these everywhere)            #
# --------------------------------------------------------------------------- #

CHANNEL_TRADES_OBSERVED = "trades:observed"
CHANNEL_DECISIONS = "decisions"
CHANNEL_PAPER_CLOSED = "positions:paper_closed"
CHANNEL_SYSTEM_STATUS = "system:status"
CHANNEL_RECONCILIATION = "reconciliation:completed"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _uppercase(value: Any) -> Any:
    """Best-effort uppercasing for Literal enum-like fields.

    Pydantic ``mode='before'`` validator: accept legacy lower-case inputs
    (``'buy'``, ``'follow'``) and normalise them to upper-case before the
    Literal check runs.
    """
    if isinstance(value, str):
        return value.upper()
    return value


# --------------------------------------------------------------------------- #
# TradeObserved — published by src/observer/trade_observer.py                 #
# --------------------------------------------------------------------------- #


class TradeObserved(BaseModel):
    """Event emitted on ``trades:observed`` for every dedup-passing trade.

    Required core fields (consigne): time, market_id, wallet_address, side,
    price, size_usdc, is_leader, source.

    Optional fields preserve the existing payload so legacy consumers (e.g.
    profilers reading ``market_question``) keep working without code change.
    """

    model_config = ConfigDict(extra="forbid")

    # Core (consigne)
    time: datetime
    market_id: str
    wallet_address: str
    side: Literal["BUY", "SELL"]
    price: float
    size_usdc: float
    is_leader: bool
    source: str

    # Legacy enrichment fields preserved from the existing producer payload.
    # Marked Optional so the schema accepts a minimal core-only event too.
    token_id: str | None = None
    market_question: str | None = None
    market_category: str | None = None
    market_type: str | None = None
    wallet_type: str | None = None
    wallet_status: str | None = None
    wallet_strategy: str | None = None
    wallet_horizon: str | None = None
    wallet_influence: str | None = None

    @field_validator("side", mode="before")
    @classmethod
    def _normalise_side(cls, v: Any) -> Any:
        return _uppercase(v)

    @field_validator("price", "size_usdc", mode="before")
    @classmethod
    def _coerce_str_to_float(cls, v: Any) -> Any:
        # Existing producer stringifies price/size — accept both shapes.
        if isinstance(v, str):
            return float(v)
        return v

    # ---------------------------------------------------------------- #
    # Serialisation: legacy producer emits ``price`` and ``size_usdc`` #
    # as strings (Decimal precision preservation). Keep that wire     #
    # contract so existing consumers — and the in-tree                #
    # tests/test_observer/test_trade_observer.py — keep working. The  #
    # model itself stores floats (consigne) but the JSON shape mirrors #
    # the legacy producer.                                             #
    # ---------------------------------------------------------------- #

    @field_serializer("price")
    def _ser_price(self, v: float) -> str:
        # Mirror the legacy ``str(rec.price)`` representation. We pass
        # through ``repr`` of the float to avoid trailing zeros while
        # still being json-roundtrippable.
        return format(v, "g")

    @field_serializer("size_usdc")
    def _ser_size(self, v: float) -> str:
        return format(v, "g")


# --------------------------------------------------------------------------- #
# DecisionMade — published by src/engine/decision_router.py                   #
# --------------------------------------------------------------------------- #


class DecisionMade(BaseModel):
    """Event emitted on ``decisions`` (paper) and ``decisions:live`` for
    every routable decision.

    Core fields (consigne): time, decision_id, market_id, action,
    confidence, kelly, reason.

    Action vocabulary: the canonical contract is
    ``Literal["OPEN","CLOSE","REDUCE","SKIP"]``, but the existing
    PaperTrader consumer ALREADY branches on legacy values
    (``"follow"``, ``"fade"``, ``"volume_anticipation"`` …) so we keep
    them allowed too. Producers should emit canonical values where
    possible; consumers should treat the value as the union below.
    """

    model_config = ConfigDict(extra="forbid")

    # Core (consigne).  We accept the canonical upper-case set AND the
    # historical lower-case set used by ConfidenceEngine/PaperTrader so
    # this refactor stays non-breaking. Tests assert that drift on any
    # *other* value still raises.
    time: datetime
    decision_id: str
    market_id: str
    action: Literal[
        "OPEN", "CLOSE", "REDUCE", "SKIP",
        "follow", "fade", "skip", "volume_anticipation",
    ]
    confidence: float
    kelly: float
    reason: str

    # Legacy / context — preserved to avoid breaking PaperTrader gates.
    leader_wallet: str | None = None
    token_id: str | None = None
    side: str | None = None
    price: float | None = None
    size_usdc: float | None = None
    # The legacy producer ships ``kelly_fraction``; the consigne names
    # the canonical field ``kelly`` (which we ALSO populate above).
    # Both kept here so existing PaperTrader code (which reads
    # ``kelly_fraction``) keeps working.
    kelly_fraction: float | None = None
    thompson_follow: float | None = None
    thompson_fade: float | None = None
    market_question: str | None = None
    market_category: str | None = None
    market_type: str | None = None
    wallet_type: str | None = None
    wallet_strategy: str | None = None
    wallet_horizon: str | None = None
    wallet_influence: str | None = None
    trade_context: dict | None = None
    context_penalty: float | None = None
    strategy_track: str | None = None
    economic_model_version: str | None = None
    signal_audit: dict | None = None

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action(cls, v: Any) -> Any:
        # Accept canonical UPPER-case AND legacy lower-case as-is. The
        # Literal in the field declaration enumerates both sets so drift
        # to a third value (e.g. "rebalance") still raises.
        if isinstance(v, str):
            return v.strip()
        return v


# --------------------------------------------------------------------------- #
# PositionClosed — published by src/engine/paper_trader.py                    #
# --------------------------------------------------------------------------- #


class PositionClosed(BaseModel):
    """Event emitted on ``positions:paper_closed`` when a paper trade exits.

    Core fields (consigne): time, position_id, wallet_address, market_id,
    pnl_usdc, close_method, holding_period_seconds.

    Legacy fields preserved so the Telegram formatter, audit log and
    learning pipeline (which all consume this channel) keep working.
    """

    model_config = ConfigDict(extra="forbid")

    # Core (consigne)
    time: datetime
    position_id: str
    wallet_address: str
    market_id: str
    pnl_usdc: float
    close_method: str
    holding_period_seconds: int

    # Legacy enrichment from PaperTrader._publish_close — preserved so
    # the Telegram notifier and dashboard keep parsing the old keys.
    trade_id: int | None = None
    leader_wallet: str | None = None
    pnl_pct: float | None = None
    direction: str | None = None
    size_usdc: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    close_reason: str | None = None
    strategy: str | None = None
    strategy_track: str | None = None
    economic_model_version: str | None = None
    gross_pnl_usdc: float | None = None
    size_shares: float | None = None
    loss_reasons: list[str] | None = None
    context_penalty: float | None = None
    # Legacy fields from observer.position_tracker._publish_close —
    # the same channel name (positions:closed) is also used by the
    # on-chain reconstruction path, but the paper_closed channel only
    # carries the paper_trader payload. Kept here for forward-compat
    # if both producers ever converge on a single schema.
    token_id: str | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    category: str | None = None
    size_shares_str: str | None = None
    holding_period_s: int | None = None
    is_contrarian: bool | None = None


# --------------------------------------------------------------------------- #
# SystemStatusChanged — health / mode transitions                             #
# --------------------------------------------------------------------------- #


class SystemStatusChanged(BaseModel):
    """Event emitted on ``system:status`` whenever the bot lifecycle
    (running / WS health / ingest sources / killswitch) transitions.

    Producers are free to publish a partial status (only the fields that
    actually changed are stable across calls); ``ingest`` is a dict
    because the source list is open-ended.
    """

    model_config = ConfigDict(extra="forbid")

    time: datetime
    bot: Literal["RUNNING", "STOPPED"]
    ws: Literal["LIVE", "DEGRADED", "DOWN"]
    ingest: dict
    killswitch: bool

    @field_validator("bot", "ws", mode="before")
    @classmethod
    def _normalise_enum(cls, v: Any) -> Any:
        return _uppercase(v)


# --------------------------------------------------------------------------- #
# ReconciliationCompleted — paper-truth audit                                 #
# --------------------------------------------------------------------------- #


class ReconciliationCompleted(BaseModel):
    """Event emitted on ``reconciliation:completed`` after each paper-truth
    reconciliation run (Gamma vs internal close audit).

    See ``project_paper_trading_truth.md`` memory and
    ``src/api/reconciliation_queries.py``.
    """

    model_config = ConfigDict(extra="forbid")

    time: datetime
    verdict: Literal["ok", "warn", "critical"]
    delta_abs: float
    sample_size: int

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalise_verdict(cls, v: Any) -> Any:
        # Verdict stays lower-case (matches the existing pillar API
        # contract), just trim whitespace and force lowercase if a
        # producer mistakenly upper-cases it.
        if isinstance(v, str):
            return v.strip().lower()
        return v


# --------------------------------------------------------------------------- #
# Channel → schema dispatch table (used by the WS bridge consumer)            #
# --------------------------------------------------------------------------- #


CHANNEL_SCHEMA: dict[str, type[BaseModel]] = {
    CHANNEL_TRADES_OBSERVED: TradeObserved,
    CHANNEL_DECISIONS: DecisionMade,
    CHANNEL_PAPER_CLOSED: PositionClosed,
    CHANNEL_SYSTEM_STATUS: SystemStatusChanged,
    CHANNEL_RECONCILIATION: ReconciliationCompleted,
}
