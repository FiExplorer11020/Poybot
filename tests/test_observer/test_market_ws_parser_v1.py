from src.observer.market_events import parse_market_event


def _payload(event_type: str) -> dict:
    return {
        "event_type": event_type,
        "market": "market-1",
        "asset_id": "token-1",
        "timestamp": "1774907005123",
        "extra": {"raw": True},
    }


def test_v1_market_event_types_are_recognized():
    for event_type in (
        "book",
        "price_change",
        "last_trade_price",
        "best_bid_ask",
        "tick_size_change",
        "market_resolved",
    ):
        parsed = parse_market_event(_payload(event_type))

        assert parsed.event_type == event_type
        assert parsed.token_id == "token-1"
        assert parsed.market_id == "market-1"
        assert parsed.exchange_ts == "1774907005123"
        assert parsed.raw_payload["extra"] == {"raw": True}
        assert parsed.reject_reason is None


def test_unknown_market_event_is_rejected_with_reason():
    parsed = parse_market_event(_payload("unknown_event"))

    assert parsed.event_type == "unknown_event"
    assert parsed.token_id == "token-1"
    assert parsed.reject_reason == "unsupported_event_type"


def test_market_event_missing_token_is_rejected():
    payload = _payload("book")
    payload.pop("asset_id")

    parsed = parse_market_event(payload)

    assert parsed.reject_reason == "missing_token_id"
