"""
WSBridge — typed-delta fan-out tests (A8 scope).

Covers the refactor that replaces the bare ``snapshot_updated`` trigger
with real typed payloads sourced from the Pydantic event schemas in
``src/events/schemas.py``.

Run: pytest tests/test_api/test_ws_bridge.py -v
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.api.ws_bridge import (
    CHANNEL_TO_WS_TYPE,
    WSBridge,
)
from src.events.schemas import (
    CHANNEL_DECISIONS,
    CHANNEL_PAPER_CLOSED,
    CHANNEL_RECONCILIATION,
    CHANNEL_SCHEMA,
    CHANNEL_SYSTEM_STATUS,
    CHANNEL_TRADES_OBSERVED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_running_bridge() -> WSBridge:
    """Return a WSBridge in the post-``start()`` state without touching Redis.

    Mirrors the helper in test_ws_bridge_snapshot_event.py — the handler
    short-circuits on ``not self._running``, so flipping the flag is
    enough to exercise the fan-out path without spinning up Subscriber.
    """
    bridge = WSBridge()
    bridge._running = True
    return bridge


class _StubWebSocket:
    """Minimal stand-in for fastapi.WebSocket — captures sent text frames."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _valid_trade_payload() -> dict:
    """Minimal valid TradeObserved payload (dict shape, pre-validation)."""
    return {
        "time": "2026-05-18T12:34:56+00:00",
        "market_id": "0xmarket1",
        "wallet_address": "0xLEADER",
        "side": "BUY",
        "price": "0.65",
        "size_usdc": "100",
        "is_leader": True,
        "source": "websocket",
    }


def _valid_system_status_payload() -> dict:
    return {
        "time": "2026-05-18T12:00:00+00:00",
        "bot": "RUNNING",
        "ws": "LIVE",
        "ingest": {"websocket": "ok", "rest": "ok"},
        "killswitch": False,
    }


def _valid_decision_payload() -> dict:
    return {
        "time": "2026-05-18T12:35:01+00:00",
        "decision_id": "dec-001",
        "market_id": "0xmarket1",
        "action": "follow",
        "confidence": 0.71,
        "kelly": 0.015,
        "reason": "thompson_follow won the sample",
    }


def _valid_position_closed_payload() -> dict:
    return {
        "time": "2026-05-18T13:00:00+00:00",
        "position_id": "42",
        "wallet_address": "0xLEADER",
        "market_id": "0xmarket1",
        "pnl_usdc": 12.34,
        "close_method": "leader_exit",
        "holding_period_seconds": 3600,
    }


def _valid_reconciliation_payload() -> dict:
    return {
        "time": "2026-05-18T03:00:00+00:00",
        "verdict": "warn",
        "delta_abs": 125.5,
        "sample_size": 42,
    }


# ---------------------------------------------------------------------------
# Test 1 — typed delta is emitted with full Pydantic-serialised payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_observed_emits_typed_envelope() -> None:
    """A valid TradeObserved event must be broadcast as a typed envelope:

        {type: "trade", channel: "trades:observed", ts: <float>, data: {...}}

    This is the core DoD — the front no longer has to refetch HTTP on
    each event because ``data`` carries the full payload.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(_valid_trade_payload(), CHANNEL_TRADES_OBSERVED)

    assert bridge.broadcast.await_count == 1
    sent = bridge.broadcast.await_args.args[0]
    assert sent["type"] == "trade"
    assert sent["channel"] == CHANNEL_TRADES_OBSERVED
    assert isinstance(sent["ts"], float) and sent["ts"] > 0.0
    assert isinstance(sent["data"], dict)
    # Core fields survive the Pydantic round-trip.
    assert sent["data"]["market_id"] == "0xmarket1"
    assert sent["data"]["wallet_address"] == "0xLEADER"
    assert sent["data"]["side"] == "BUY"
    # ``price`` / ``size_usdc`` are serialised as strings (legacy contract,
    # see TradeObserved._ser_price). We assert presence + type to lock that.
    assert isinstance(sent["data"]["price"], str)
    assert isinstance(sent["data"]["size_usdc"], str)


# ---------------------------------------------------------------------------
# Test 2 — malformed event is dropped without crashing the bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_event_is_dropped(caplog) -> None:
    """A payload missing a required field (drift) must NOT broadcast and
    MUST NOT crash the bridge. The Subscriber loop should keep running.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    bad_payload = _valid_trade_payload()
    del bad_payload["wallet_address"]  # required field — Pydantic must reject

    # Bridge must not raise.
    await bridge._on_typed_event(bad_payload, CHANNEL_TRADES_OBSERVED)

    bridge.broadcast.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3 — 200 events in 1s → ~100 dropped by the rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_drops_excess_events(monkeypatch) -> None:
    """The 100 msg/s token bucket on ``trades:observed`` must let 100
    events through and drop the next 100 within the same window.

    We pin ``time.monotonic`` so the test is deterministic (the bucket
    is keyed off the monotonic clock).
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    fake_mono = [1000.0]

    def fake_monotonic() -> float:
        return fake_mono[0]

    import src.api.ws_bridge as ws_bridge_module

    monkeypatch.setattr(ws_bridge_module.time, "monotonic", fake_monotonic)

    payload = _valid_trade_payload()
    # 200 events within the same 1s window.
    for _ in range(200):
        await bridge._on_typed_event(payload, CHANNEL_TRADES_OBSERVED)

    # Capacity is 100/s — only the first 100 should broadcast.
    assert bridge.broadcast.await_count == 100

    # The other 100 should be tallied in the drop counter.
    assert bridge._drop_counts[CHANNEL_TRADES_OBSERVED] == 100

    # Roll the clock past the window — next event must pass again, proving
    # the bucket refilled.
    fake_mono[0] += 1.1
    await bridge._on_typed_event(payload, CHANNEL_TRADES_OBSERVED)
    assert bridge.broadcast.await_count == 101


# ---------------------------------------------------------------------------
# Test 4 — system:status event is forwarded with the right WS type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_status_event_forwarded() -> None:
    """A SystemStatusChanged event must reach the front as type="system_status"
    with the full health payload — this is what unblocks the BotHealth
    panel from polling.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(
        _valid_system_status_payload(), CHANNEL_SYSTEM_STATUS
    )

    assert bridge.broadcast.await_count == 1
    sent = bridge.broadcast.await_args.args[0]
    assert sent["type"] == "system_status"
    assert sent["channel"] == CHANNEL_SYSTEM_STATUS
    assert sent["data"]["bot"] == "RUNNING"
    assert sent["data"]["ws"] == "LIVE"
    assert sent["data"]["killswitch"] is False
    assert sent["data"]["ingest"] == {"websocket": "ok", "rest": "ok"}


# ---------------------------------------------------------------------------
# Test 5 — legacy snapshot_updated channel still emits in parallel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_snapshot_updated_still_emitted() -> None:
    """The legacy ``snapshot_updated`` event must keep working during the
    A9 transition. We pump the snapshot handler and assert the legacy
    envelope shape ``{type, ts}`` is unchanged.
    """
    from src.api.ws_bridge import SNAPSHOT_PUBSUB_CHANNEL

    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_snapshot_updated(
        payload={"built_at": 1234.0}, _channel=SNAPSHOT_PUBSUB_CHANNEL
    )

    assert bridge.broadcast.await_count == 1
    sent = bridge.broadcast.await_args.args[0]
    # Legacy contract: only ``type`` and ``ts`` keys — no ``data`` payload.
    # If A9 needs to change this, both this test and the front consumer
    # must move in lockstep.
    assert set(sent.keys()) == {"type", "ts"}
    assert sent["type"] == "snapshot_updated"
    assert isinstance(sent["ts"], float)


# ---------------------------------------------------------------------------
# Bonus — coverage / drift guards
# ---------------------------------------------------------------------------


def test_channel_to_ws_type_covers_all_schemas() -> None:
    """Every channel in CHANNEL_SCHEMA must have a WS-type mapping
    (otherwise a Pydantic-valid event would silently become unknown).
    The bridge enforces this at ``start()`` via _assert_channel_coverage,
    but having the assertion at unit-test time gives a faster failure
    when someone forgets to add a new channel.
    """
    assert set(CHANNEL_TO_WS_TYPE.keys()) == set(CHANNEL_SCHEMA.keys())


def test_channel_to_ws_type_values_are_unique() -> None:
    """WS type strings must be unique so the front dispatch is unambiguous."""
    values = list(CHANNEL_TO_WS_TYPE.values())
    assert len(values) == len(set(values))


@pytest.mark.asyncio
async def test_decision_event_forwarded() -> None:
    """Sanity: DecisionMade also follows the typed-envelope path."""
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(_valid_decision_payload(), CHANNEL_DECISIONS)

    sent = bridge.broadcast.await_args.args[0]
    assert sent["type"] == "decision"
    assert sent["channel"] == CHANNEL_DECISIONS
    # Legacy lower-case action is preserved (see DecisionMade.action Literal).
    assert sent["data"]["action"] == "follow"
    assert sent["data"]["decision_id"] == "dec-001"


@pytest.mark.asyncio
async def test_position_closed_event_forwarded() -> None:
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(
        _valid_position_closed_payload(), CHANNEL_PAPER_CLOSED
    )

    sent = bridge.broadcast.await_args.args[0]
    assert sent["type"] == "position_closed"
    assert sent["channel"] == CHANNEL_PAPER_CLOSED
    assert sent["data"]["pnl_usdc"] == 12.34
    assert sent["data"]["close_method"] == "leader_exit"


@pytest.mark.asyncio
async def test_reconciliation_event_forwarded() -> None:
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(
        _valid_reconciliation_payload(), CHANNEL_RECONCILIATION
    )

    sent = bridge.broadcast.await_args.args[0]
    assert sent["type"] == "reconciliation"
    assert sent["channel"] == CHANNEL_RECONCILIATION
    assert sent["data"]["verdict"] == "warn"
    assert sent["data"]["sample_size"] == 42


@pytest.mark.asyncio
async def test_unknown_channel_does_not_crash() -> None:
    """If a producer publishes on a channel we subscribed to but for
    which CHANNEL_SCHEMA has no entry (impossible at start() time, but
    defensive), the handler must log + skip — never raise.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event({"foo": "bar"}, "channel:does_not_exist")

    bridge.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_event_skipped_when_bridge_not_running() -> None:
    """Late messages arriving after stop() must NOT broadcast (parallels
    the equivalent guard for snapshot_updated in
    test_ws_bridge_snapshot_event.py).
    """
    bridge = WSBridge()
    # _running stays False — simulates a message landing during teardown.
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_typed_event(_valid_trade_payload(), CHANNEL_TRADES_OBSERVED)

    bridge.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_event_delivered_to_real_connections() -> None:
    """End-to-end via the real ``broadcast``: every connected stub
    receives the same JSON-encoded typed envelope.
    """
    bridge = _make_running_bridge()
    client_a = _StubWebSocket()
    client_b = _StubWebSocket()
    bridge._connections.add(client_a)  # type: ignore[arg-type]
    bridge._connections.add(client_b)  # type: ignore[arg-type]

    await bridge._on_typed_event(_valid_trade_payload(), CHANNEL_TRADES_OBSERVED)

    for client in (client_a, client_b):
        assert len(client.sent) == 1
        decoded = json.loads(client.sent[0])
        assert decoded["type"] == "trade"
        assert decoded["channel"] == CHANNEL_TRADES_OBSERVED
        assert decoded["data"]["market_id"] == "0xmarket1"


@pytest.mark.asyncio
async def test_assert_channel_coverage_passes() -> None:
    """The runtime drift guard must pass with the current channel map."""
    # Calling the staticmethod directly — must not raise.
    WSBridge._assert_channel_coverage()
