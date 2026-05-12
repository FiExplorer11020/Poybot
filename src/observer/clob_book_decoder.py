"""Pure-Python decoder for Polymarket L3 WebSocket messages — Round 11.

Tested directly via :mod:`tests.test_observer.test_clob_book_decoder`
and :mod:`tests.test_observer.test_clob_book_observer`.

Two entry points coexist:

  * :func:`decode_ws_message`  — legacy single-event shapes
    (``order_placed`` / ``order_filled`` / etc.). Returns
    ``BookEvent | None``.
  * :func:`decode_ws_messages` — real Polymarket Market channel
    fan-out. Returns ``list[BookEvent]``. Handles ``price_change``
    (N changes per frame), ``last_trade_price`` (a fill), and
    snapshot frames (``book`` etc., which emit nothing). Use
    :func:`is_known_non_event_message` to distinguish valid-no-event
    frames from malformed payloads.

Wallet attribution caveat (spec § 3.1): the Market channel does NOT
ship wallets on placements / modifications / cancellations / fills —
NULL is preserved. The on-chain reconciler joins with
``trades_observed`` later.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# Event-type vocabulary. Reading order matters — the decoder maps raw
# WS event_type strings into this canonical set. The DB column
# event_type is VARCHAR(20); we keep the strings short.
EVENT_PLACED = "placed"
EVENT_MODIFIED = "modified"
EVENT_CANCELLED = "cancelled"
EVENT_PARTIAL_FILL = "partial_fill"
EVENT_FILLED = "filled"

_VALID_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_PLACED,
    EVENT_MODIFIED,
    EVENT_CANCELLED,
    EVENT_PARTIAL_FILL,
    EVENT_FILLED,
})

# Raw → canonical mapping. Polymarket's WS uses a few alternative names
# depending on channel; we normalise here so the rest of the pipeline
# only sees the canonical vocabulary.
_RAW_EVENT_TYPE_MAP: dict[str, str] = {
    "order_placed": EVENT_PLACED,
    "place": EVENT_PLACED,
    "placed": EVENT_PLACED,
    "new_order": EVENT_PLACED,
    "order_modified": EVENT_MODIFIED,
    "modify": EVENT_MODIFIED,
    "modified": EVENT_MODIFIED,
    "update": EVENT_MODIFIED,
    "order_cancelled": EVENT_CANCELLED,
    "cancel": EVENT_CANCELLED,
    "cancelled": EVENT_CANCELLED,
    "canceled": EVENT_CANCELLED,
    "order_partial_fill": EVENT_PARTIAL_FILL,
    "partial_fill": EVENT_PARTIAL_FILL,
    "partial": EVENT_PARTIAL_FILL,
    "order_filled": EVENT_FILLED,
    "fill": EVENT_FILLED,
    "filled": EVENT_FILLED,
    "trade": EVENT_FILLED,
}


def _canonicalize_event_type(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if not key:
        return None
    return _RAW_EVENT_TYPE_MAP.get(key)


def _to_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_timestamp(raw: Any) -> datetime | None:
    """Accept ms-epoch ints/floats, second-epoch ints/floats, and ISO 8601
    strings. Returns a tz-aware UTC datetime, or None.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if ts > 1e12:  # ms-epoch
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        # Try numeric-as-string first; fall back to ISO 8601.
        try:
            ts = float(raw)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class BookEvent:
    """In-memory record for the producer→consumer hand-off and the Redis
    Stream publish path. Maps 1:1 to a row in ``clob_book_events``.

    ``wallet_address`` is NULL except on fills — see spec § 3.1.
    """

    event_time: datetime
    market_id: str
    token_id: str
    event_type: str
    side: str
    price: Decimal | None
    size_delta: Decimal | None
    order_hash: str | None
    wallet_address: str | None
    source: str  # 'ws' | 'onchain_reconciled'
    raw_payload: dict | None = field(default=None)
    # Producer-side wall-clock timestamp for ws_latency observation.
    received_at: float = 0.0

    def to_stream_payload(self) -> dict[str, Any]:
        """Serialise into a JSON-safe dict for the Redis Stream publish.
        ``Decimal``s are stringified; ``datetime`` is ISO 8601.
        """
        return {
            "event_time": self.event_time.isoformat(),
            "market_id": self.market_id,
            "token_id": self.token_id,
            "event_type": self.event_type,
            "side": self.side,
            "price": str(self.price) if self.price is not None else None,
            "size_delta": (
                str(self.size_delta) if self.size_delta is not None else None
            ),
            "order_hash": self.order_hash,
            "wallet_address": self.wallet_address,
            "source": self.source,
            "received_at_ms": int(self.received_at * 1000),
        }


def decode_ws_message(msg: dict[str, Any], *, now_s: float | None = None) -> BookEvent | None:
    """Decode a raw Polymarket L3 WS message into a :class:`BookEvent`.

    Returns ``None`` if the message is malformed or doesn't match any of
    the five canonical event types. The caller is expected to count
    ``invalid`` drops via the dropped_total metric.

    The function is **pure** so it can be unit-tested without a running
    event loop or Redis client.
    """
    if not isinstance(msg, dict):
        return None
    event_type = _canonicalize_event_type(
        msg.get("event_type") or msg.get("type") or msg.get("kind")
    )
    if event_type is None or event_type not in _VALID_EVENT_TYPES:
        return None

    market_id = str(
        msg.get("market_id") or msg.get("market") or msg.get("condition_id") or ""
    ).strip()
    token_id = str(
        msg.get("token_id")
        or msg.get("asset_id")
        or msg.get("asset")
        or msg.get("token")
        or ""
    ).strip()
    if not market_id or not token_id:
        return None

    side_raw = str(msg.get("side") or "").strip().lower()
    # Normalise to 'buy' / 'sell'. Some feeds use BID/ASK; treat
    # BID = buy, ASK = sell.
    if side_raw in ("buy", "bid"):
        side = "buy"
    elif side_raw in ("sell", "ask"):
        side = "sell"
    else:
        # Side is essential for OFI / spoof / iceberg signal correctness;
        # an unknown side makes the event useless for the deriver.
        return None

    price = _to_decimal(msg.get("price"))
    # size_delta: prefer the explicit field; fall back to size/quantity.
    size_delta = _to_decimal(
        msg.get("size_delta")
        if msg.get("size_delta") is not None
        else (msg.get("size") or msg.get("quantity") or msg.get("amount"))
    )
    # For cancel events, size is the REMAINING size being withdrawn.
    # Express that as a negative delta so OFI math works downstream
    # without a special case.
    if event_type == EVENT_CANCELLED and size_delta is not None and size_delta > 0:
        size_delta = -size_delta

    order_hash_raw = msg.get("order_hash") or msg.get("order_id") or msg.get("id")
    order_hash = str(order_hash_raw).strip() if order_hash_raw else None
    if order_hash == "":
        order_hash = None

    # Wallet attribution: only on fills (spec § 3.1). We still honour an
    # explicit wallet field on any event if upstream provides it — that
    # keeps the door open for the on-chain reconciler to enrich rows.
    wallet_raw = (
        msg.get("wallet_address")
        or msg.get("wallet")
        or msg.get("owner")
        or msg.get("maker")  # 'maker' is the resting-side wallet on fills
    )
    wallet_address = (
        str(wallet_raw).strip().lower() if wallet_raw else None
    )
    if wallet_address == "":
        wallet_address = None

    event_time = _parse_timestamp(
        msg.get("event_time")
        or msg.get("timestamp")
        or msg.get("time")
        or msg.get("ts")
    )
    if event_time is None:
        # Fall back to "now" — better than dropping the event when the
        # upstream is mid-replay and ships a payload without a clock.
        event_time = datetime.now(tz=timezone.utc)

    source = str(msg.get("source") or "ws").strip().lower() or "ws"

    return BookEvent(
        event_time=event_time,
        market_id=market_id,
        token_id=token_id,
        event_type=event_type,
        side=side,
        price=price,
        size_delta=size_delta,
        order_hash=order_hash,
        wallet_address=wallet_address,
        source=source,
        raw_payload=msg,
        received_at=float(now_s) if now_s is not None else time.time(),
    )


# --------------------------------------------------------------------------- #
# Sprint 3 — Polymarket Market channel real wire format                        #
# --------------------------------------------------------------------------- #

# Valid event_types that carry no BookEvent delta (snapshot / ticker /
# control plane). The observer treats them as "valid but no event" so
# they don't bump the ``invalid`` drop counter.
_NON_EVENT_MESSAGE_TYPES: frozenset[str] = frozenset({
    "book", "best_bid_ask", "new_market", "tick_size_change", "market_resolved",
})

# WS-feed side normalisation. The Market channel ships BUY/SELL; the
# legacy decoder also accepts BID/ASK — same table.
_SIDE_NORMALISATION: dict[str, str] = {
    "buy": "buy", "bid": "buy", "sell": "sell", "ask": "sell",
}


def _normalise_side(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return _SIDE_NORMALISATION.get(raw.strip().lower())


def is_known_non_event_message(msg: dict[str, Any]) -> bool:
    """True when ``msg`` is a valid Polymarket WS frame that carries no
    book-event delta (snapshot / ticker / control plane). The observer
    uses this to avoid double-counting them under ``invalid``.
    """
    if not isinstance(msg, dict):
        return False
    raw_type = msg.get("event_type") or msg.get("type") or msg.get("kind")
    if not isinstance(raw_type, str):
        return False
    return raw_type.strip().lower() in _NON_EVENT_MESSAGE_TYPES


def _decode_last_trade_price(
    msg: dict[str, Any], *, now_s: float | None
) -> BookEvent | None:
    """Map a ``last_trade_price`` frame (a real fill) to a ``filled``
    :class:`BookEvent`. The Polymarket Market channel does NOT ship the
    maker/taker wallets on this frame — the wallet stays NULL just like
    on placements. Downstream readers join with ``trades_observed`` for
    attribution.
    """
    market_id = str(msg.get("market") or msg.get("market_id") or "").strip()
    token_id = str(
        msg.get("asset_id") or msg.get("token_id") or msg.get("asset") or ""
    ).strip()
    if not market_id or not token_id:
        return None

    side = _normalise_side(msg.get("side"))
    if side is None:
        return None

    price = _to_decimal(msg.get("price"))
    size_delta = _to_decimal(msg.get("size"))
    if price is None or size_delta is None:
        return None

    event_time = _parse_timestamp(
        msg.get("event_time") or msg.get("timestamp") or msg.get("time") or msg.get("ts")
    ) or datetime.now(tz=timezone.utc)

    # ``transaction_hash`` is the on-chain settlement hash — use it as the
    # order_hash so downstream joins with the on-chain reconciler line up.
    tx_hash = msg.get("transaction_hash") or msg.get("hash") or msg.get("order_hash")
    order_hash = str(tx_hash).strip() if tx_hash else None
    if order_hash == "":
        order_hash = None

    return BookEvent(
        event_time=event_time,
        market_id=market_id,
        token_id=token_id,
        event_type=EVENT_FILLED,
        side=side,
        price=price,
        size_delta=size_delta,
        order_hash=order_hash,
        wallet_address=None,  # spec § 3.1: WS Market channel doesn't ship wallets
        source="ws",
        raw_payload=msg,
        received_at=float(now_s) if now_s is not None else time.time(),
    )


def _decode_price_change(
    msg: dict[str, Any],
    *,
    now_s: float | None,
    level_state: dict[tuple[str, str, str], Decimal] | None,
) -> list[BookEvent]:
    """Fan out a ``price_change`` frame into N :class:`BookEvent` rows.

    Polymarket packs every level update for a market into one WS frame
    under ``price_changes``. Each entry carries the NEW resting size at
    ``(asset_id, price, side)`` — NOT a delta. With ``level_state`` the
    decoder synthesises signed deltas: positive → ``placed``, negative
    or zero-clear → ``cancelled``. Without cache, treats wire size as
    positive delta and ``size==0`` as a clear.
    """
    market_id = str(msg.get("market") or msg.get("market_id") or "").strip()
    if not market_id:
        return []

    changes = msg.get("price_changes") or msg.get("changes") or []
    if not isinstance(changes, list) or not changes:
        return []

    base_ts = _parse_timestamp(
        msg.get("timestamp") or msg.get("event_time") or msg.get("time") or msg.get("ts")
    ) or datetime.now(tz=timezone.utc)

    out: list[BookEvent] = []
    for entry in changes:
        if not isinstance(entry, dict):
            continue
        token_id = str(
            entry.get("asset_id") or entry.get("token_id") or entry.get("asset") or ""
        ).strip()
        if not token_id:
            continue
        side = _normalise_side(entry.get("side"))
        if side is None:
            continue
        price = _to_decimal(entry.get("price"))
        new_size = _to_decimal(entry.get("size"))
        if price is None or new_size is None:
            continue

        cache_key = (token_id, str(price), side)
        if level_state is not None:
            prev_size = level_state.get(cache_key, Decimal("0"))
            delta = new_size - prev_size
            level_state[cache_key] = new_size
        else:
            # No cache → treat the wire size as the (positive) magnitude;
            # use ``new_size == 0`` as the unambiguous "level cleared"
            # marker.
            prev_size = Decimal("0")
            delta = -prev_size if new_size == 0 else new_size

        if delta == 0:
            # No-op event (size unchanged or both zero). Skip rather than
            # emit a noisy zero-delta row.
            continue
        if delta > 0:
            event_type = EVENT_PLACED
            size_delta = delta
        else:
            event_type = EVENT_CANCELLED
            size_delta = delta  # already negative — matches legacy convention

        order_hash_raw = entry.get("hash") or entry.get("order_hash") or entry.get("order_id")
        order_hash = str(order_hash_raw).strip() if order_hash_raw else None
        if order_hash == "":
            order_hash = None

        out.append(
            BookEvent(
                event_time=base_ts,
                market_id=market_id,
                token_id=token_id,
                event_type=event_type,
                side=side,
                price=price,
                size_delta=size_delta,
                order_hash=order_hash,
                wallet_address=None,  # spec § 3.1: no wallet on non-fill
                source="ws",
                raw_payload=msg,
                received_at=float(now_s) if now_s is not None else time.time(),
            )
        )
    return out


def decode_ws_messages(
    msg: dict[str, Any],
    *,
    now_s: float | None = None,
    level_state: dict[tuple[str, str, str], Decimal] | None = None,
) -> list[BookEvent]:
    """Fan-out decoder for the real Polymarket Market channel.

    Returns ``list[BookEvent]`` (possibly empty). Callers MUST use
    :func:`is_known_non_event_message` to distinguish valid-no-event
    frames (snapshot / ticker / control plane) from malformed
    payloads — both return ``[]``, but only the former should NOT
    bump the ``invalid`` drop counter.

    Pure modulo optional ``level_state`` mutation: reads previous
    resting size at ``(token_id, price, side)`` and writes the new one.
    Legacy single-event shapes are delegated to :func:`decode_ws_message`
    so existing consumers don't break.
    """
    if not isinstance(msg, dict):
        return []

    raw_type = msg.get("event_type") or msg.get("type") or msg.get("kind")
    raw_key = raw_type.strip().lower() if isinstance(raw_type, str) else ""

    if raw_key == "price_change":
        return _decode_price_change(msg, now_s=now_s, level_state=level_state)

    if raw_key == "last_trade_price":
        evt = _decode_last_trade_price(msg, now_s=now_s)
        return [evt] if evt is not None else []

    if raw_key in _NON_EVENT_MESSAGE_TYPES:
        # ``book`` snapshot, best_bid_ask ticker, etc. — valid but no
        # deltas to emit. The caller knows via
        # ``is_known_non_event_message`` to skip the invalid-drop count.
        return []

    # Legacy per-order shapes (or any other shape the original decoder
    # understands) — delegate. Keeps backward compat with tests that
    # drive the observer with synthetic ``order_placed``/``order_filled``
    # payloads.
    evt = decode_ws_message(msg, now_s=now_s)
    return [evt] if evt is not None else []
