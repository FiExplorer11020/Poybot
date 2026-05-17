from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SUPPORTED_MARKET_EVENTS = {
    "book",
    "price_change",
    "last_trade_price",
    "best_bid_ask",
    "tick_size_change",
    "market_resolved",
}


@dataclass(frozen=True)
class ParsedMarketEvent:
    event_type: str
    token_id: str | None
    market_id: str | None
    exchange_ts: str | int | float | None
    observed_ts: datetime
    raw_payload: dict[str, Any]
    reject_reason: str | None = None


def parse_market_event(payload: dict[str, Any]) -> ParsedMarketEvent:
    event_type = str(payload.get("event_type") or payload.get("type") or "")
    token_id = payload.get("asset_id") or payload.get("token_id") or payload.get("asset")
    market_id = payload.get("market") or payload.get("market_id")
    exchange_ts = payload.get("timestamp") or payload.get("time") or payload.get("ts")
    reject_reason = None

    if event_type not in SUPPORTED_MARKET_EVENTS:
        reject_reason = "unsupported_event_type"
    elif not token_id:
        reject_reason = "missing_token_id"

    return ParsedMarketEvent(
        event_type=event_type,
        token_id=str(token_id) if token_id else None,
        market_id=str(market_id) if market_id else None,
        exchange_ts=exchange_ts,
        observed_ts=datetime.now(tz=timezone.utc),
        raw_payload=dict(payload),
        reject_reason=reject_reason,
    )


def extract_resolution_outcome(payload: dict[str, Any]) -> str | None:
    """Return ``"yes"`` / ``"no"`` for a Polymarket ``market_resolved`` WS
    frame, or ``None`` when the outcome cannot be inferred.

    The Polymarket WS schema is loosely typed and several keys carry the
    same information depending on the upstream version: ``outcome``,
    ``winning_outcome``, ``winner`` (string) or ``outcomePrices`` /
    ``outcome_prices`` (list of two numerics — index 0 is YES). We accept
    any of them, normalise to lowercase, and clamp to ``{"yes", "no"}``.
    Returning ``None`` is the safe default — callers must treat unknown
    outcomes as "defer the close" rather than guess.
    """
    if not isinstance(payload, dict):
        return None

    for key in ("outcome", "winning_outcome", "winner", "resolution"):
        raw = payload.get(key)
        if raw is None:
            continue
        text = str(raw).strip().lower()
        if text in ("yes", "1", "true"):
            return "yes"
        if text in ("no", "0", "false"):
            return "no"

    for key in ("outcomePrices", "outcome_prices", "prices"):
        prices = payload.get(key)
        if isinstance(prices, str):
            # Some publishers ship JSON-encoded arrays inside a string.
            import json
            try:
                prices = json.loads(prices)
            except Exception:
                prices = None
        if isinstance(prices, (list, tuple)) and len(prices) >= 1:
            try:
                yes_terminal = float(prices[0])
            except (TypeError, ValueError):
                continue
            return "yes" if yes_terminal > 0.5 else "no"

    return None
