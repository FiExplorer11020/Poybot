"""
Round-trip and negative-drift tests for src/events/schemas.py.

Each model has:
  * a happy-path round-trip (build → JSON → parse → equal)
  * a Literal coercion check (legacy lower-case inputs accepted)
  * a negative test confirming ``extra="forbid"`` catches drift

If any of these fail, the producer/consumer contract is broken — fix the
schema or the producer, do NOT relax ``extra``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.events.schemas import (
    CHANNEL_DECISIONS,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_RECONCILIATION,
    CHANNEL_SCHEMA,
    CHANNEL_SYSTEM_STATUS,
    CHANNEL_TRADES_OBSERVED,
    DecisionMade,
    PositionClosed,
    ReconciliationCompleted,
    SystemStatusChanged,
    TradeObserved,
)

# --------------------------------------------------------------------------- #
# TradeObserved                                                               #
# --------------------------------------------------------------------------- #


def test_trade_observed_round_trip():
    event = TradeObserved(
        time=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
        market_id="market-1",
        wallet_address="0xLEADER",
        side="BUY",
        price=0.42,
        size_usdc=123.45,
        is_leader=True,
        source="websocket",
    )
    payload = event.model_dump_json()
    parsed = TradeObserved.model_validate_json(payload)
    assert parsed == event


def test_trade_observed_accepts_legacy_lowercase_side_and_str_numbers():
    # Existing producer stringifies price/size and emits lower-case side.
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "market_id": "market-1",
        "wallet_address": "0xLEADER",
        "side": "sell",
        "price": "0.42",
        "size_usdc": "123.45",
        "is_leader": False,
        "source": "api_market",
    }
    event = TradeObserved.model_validate(raw)
    assert event.side == "SELL"
    assert event.price == pytest.approx(0.42)
    assert event.size_usdc == pytest.approx(123.45)


def test_trade_observed_rejects_unknown_field():
    # extra="forbid" → drift surfaces here.
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "market_id": "market-1",
        "wallet_address": "0xLEADER",
        "side": "BUY",
        "price": 0.42,
        "size_usdc": 10.0,
        "is_leader": True,
        "source": "websocket",
        "this_field_does_not_exist": "drift!",
    }
    with pytest.raises(ValidationError):
        TradeObserved.model_validate(raw)


# --------------------------------------------------------------------------- #
# DecisionMade                                                                #
# --------------------------------------------------------------------------- #


def test_decision_made_round_trip():
    event = DecisionMade(
        time=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
        decision_id="dec-001",
        market_id="market-1",
        action="OPEN",
        confidence=0.71,
        kelly=0.015,
        reason="thompson_follow_won_sample",
    )
    payload = event.model_dump_json()
    parsed = DecisionMade.model_validate_json(payload)
    assert parsed == event


def test_decision_made_accepts_legacy_lowercase_actions():
    """Legacy producers emit ``follow``/``fade``/``skip``/``volume_anticipation``
    and the existing PaperTrader branches on these exact strings. The
    schema MUST accept both vocabularies as-is to stay non-breaking.
    """
    for legacy in ("follow", "fade", "skip", "volume_anticipation"):
        raw = {
            "time": "2026-05-18T12:00:00+00:00",
            "decision_id": "dec-002",
            "market_id": "market-1",
            "action": legacy,
            "confidence": 0.71,
            "kelly": 0.015,
            "reason": "legacy",
        }
        event = DecisionMade.model_validate(raw)
        assert event.action == legacy


def test_decision_made_rejects_unknown_action():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "decision_id": "dec-002",
        "market_id": "market-1",
        "action": "rebalance",  # not in the Literal set
        "confidence": 0.71,
        "kelly": 0.015,
        "reason": "rogue",
    }
    with pytest.raises(ValidationError):
        DecisionMade.model_validate(raw)


def test_decision_made_rejects_unknown_field():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "decision_id": "dec-003",
        "market_id": "market-1",
        "action": "SKIP",
        "confidence": 0.5,
        "kelly": 0.0,
        "reason": "ok",
        "rogue_field": "drift",
    }
    with pytest.raises(ValidationError):
        DecisionMade.model_validate(raw)


# --------------------------------------------------------------------------- #
# PositionClosed                                                              #
# --------------------------------------------------------------------------- #


def test_position_closed_round_trip():
    event = PositionClosed(
        time=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
        position_id="42",
        wallet_address="bot",
        market_id="market-1",
        pnl_usdc=12.34,
        close_method="leader_exit",
        holding_period_seconds=3600,
    )
    payload = event.model_dump_json()
    parsed = PositionClosed.model_validate_json(payload)
    assert parsed == event


def test_position_closed_rejects_unknown_field():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "position_id": "42",
        "wallet_address": "bot",
        "market_id": "market-1",
        "pnl_usdc": 1.0,
        "close_method": "leader_exit",
        "holding_period_seconds": 60,
        "phantom_field": True,
    }
    with pytest.raises(ValidationError):
        PositionClosed.model_validate(raw)


# --------------------------------------------------------------------------- #
# SystemStatusChanged                                                         #
# --------------------------------------------------------------------------- #


def test_system_status_round_trip():
    event = SystemStatusChanged(
        time=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
        bot="RUNNING",
        ws="LIVE",
        ingest={"websocket": "ok", "rest": "ok"},
        killswitch=False,
    )
    payload = event.model_dump_json()
    parsed = SystemStatusChanged.model_validate_json(payload)
    assert parsed == event


def test_system_status_accepts_lowercase_enums():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "bot": "stopped",
        "ws": "degraded",
        "ingest": {},
        "killswitch": True,
    }
    event = SystemStatusChanged.model_validate(raw)
    assert event.bot == "STOPPED"
    assert event.ws == "DEGRADED"


def test_system_status_rejects_unknown_field():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "bot": "RUNNING",
        "ws": "LIVE",
        "ingest": {},
        "killswitch": False,
        "uptime_hint": "should_be_ingest_key",
    }
    with pytest.raises(ValidationError):
        SystemStatusChanged.model_validate(raw)


# --------------------------------------------------------------------------- #
# ReconciliationCompleted                                                     #
# --------------------------------------------------------------------------- #


def test_reconciliation_round_trip():
    event = ReconciliationCompleted(
        time=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
        verdict="warn",
        delta_abs=125.5,
        sample_size=42,
    )
    payload = event.model_dump_json()
    parsed = ReconciliationCompleted.model_validate_json(payload)
    assert parsed == event


def test_reconciliation_rejects_unknown_field():
    raw = {
        "time": "2026-05-18T12:00:00+00:00",
        "verdict": "ok",
        "delta_abs": 0.0,
        "sample_size": 0,
        "stale_attr": "drift",
    }
    with pytest.raises(ValidationError):
        ReconciliationCompleted.model_validate(raw)


# --------------------------------------------------------------------------- #
# Channel dispatch table                                                      #
# --------------------------------------------------------------------------- #


def test_channel_dispatch_table_covers_all_channels():
    expected = {
        CHANNEL_TRADES_OBSERVED: TradeObserved,
        CHANNEL_DECISIONS: DecisionMade,
        CHANNEL_PAPER_CLOSED: PositionClosed,
        CHANNEL_SYSTEM_STATUS: SystemStatusChanged,
        CHANNEL_RECONCILIATION: ReconciliationCompleted,
    }
    assert CHANNEL_SCHEMA == expected


# --------------------------------------------------------------------------- #
# Negative producer/consumer drift — the DoD requirement                      #
# --------------------------------------------------------------------------- #


def test_producer_consumer_drift_is_caught_at_runtime():
    """A producer that forgets a required field (silent drift) must
    raise ValidationError at the consumer side.

    This is the DoD check: the whole module exists to turn this silent
    failure into a loud one.
    """
    # Producer "forgets" to send the required ``reason`` field.
    rogue_payload = json.dumps(
        {
            "time": "2026-05-18T12:00:00+00:00",
            "decision_id": "dec-x",
            "market_id": "market-1",
            "action": "OPEN",
            "confidence": 0.7,
            "kelly": 0.01,
        }
    )
    with pytest.raises(ValidationError):
        DecisionMade.model_validate_json(rogue_payload)
