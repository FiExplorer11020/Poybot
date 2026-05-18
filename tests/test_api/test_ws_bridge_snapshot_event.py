"""
WSBridge — snapshot_updated event tests (Agent D scope).

Covers the WebSocket fan-out triggered by the maintenance container's
``snapshot:live_summary:updated`` pub/sub publish. The bridge subscriber
side is exercised via the handler entry point ``_on_snapshot_updated``
to keep these tests independent of the actual Redis pub/sub layer
(already covered by ``tests/test_control/test_redis_pubsub.py``).

Run: pytest tests/test_api/test_ws_bridge_snapshot_event.py -v
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.api.ws_bridge import SNAPSHOT_PUBSUB_CHANNEL, WSBridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_running_bridge() -> WSBridge:
    """Return a WSBridge in the post-``start()`` state without touching Redis."""
    bridge = WSBridge()
    # The handler short-circuits on ``not self._running``; flip it manually
    # so we exercise the broadcast path without a real Subscriber.
    bridge._running = True
    return bridge


class _StubWebSocket:
    """Minimal stand-in for fastapi.WebSocket — captures sent text."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_updated_handler_broadcasts_event() -> None:
    """A snapshot pubsub message must trigger broadcast() exactly once."""
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_snapshot_updated(
        payload={"built_at": 123.0}, _channel=SNAPSHOT_PUBSUB_CHANNEL
    )

    assert bridge.broadcast.await_count == 1
    sent_payload = bridge.broadcast.await_args.args[0]
    assert sent_payload["type"] == "snapshot_updated"


@pytest.mark.asyncio
async def test_snapshot_updated_payload_includes_ts() -> None:
    """The broadcast payload must carry a numeric ``ts`` (wall-clock seconds).

    Clients use this to ignore stale events arriving out of order after a
    reconnect — see docstring on ``_on_snapshot_updated``.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)

    payload = bridge.broadcast.await_args.args[0]
    assert set(payload.keys()) == {"type", "ts"}
    assert payload["type"] == "snapshot_updated"
    assert isinstance(payload["ts"], float)
    assert payload["ts"] > 0.0


@pytest.mark.asyncio
async def test_snapshot_updated_debounced_within_2s(monkeypatch) -> None:
    """Two publishes 1s apart → only the first is broadcast.

    We pin ``time.monotonic`` because the debounce uses the monotonic
    clock to be immune to wall-clock jumps; freezing it gives us a
    deterministic interval.
    """
    bridge = _make_running_bridge()
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    # Patch the module-level reference — ws_bridge imports ``time`` so
    # the attribute is reachable as ``src.api.ws_bridge.time.monotonic``.
    import src.api.ws_bridge as ws_bridge_module

    monkeypatch.setattr(ws_bridge_module.time, "monotonic", fake_monotonic)

    # First publish — always passes (watermark is 0.0).
    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)
    # Second publish 1.0s later — inside the 2.0s window → debounced.
    fake_now[0] = 1001.0
    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)

    assert bridge.broadcast.await_count == 1, (
        "Second event within 2s must be dropped"
    )

    # Sanity: pushing past the window re-enables broadcasts.
    fake_now[0] = 1003.5
    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)
    assert bridge.broadcast.await_count == 2


@pytest.mark.asyncio
async def test_no_broadcast_if_no_clients_connected() -> None:
    """broadcast() is a no-op when ``_connections`` is empty.

    We call the real ``broadcast`` (not a mock) and assert it returns
    cleanly without raising. No WS clients means nothing to send to.
    """
    bridge = _make_running_bridge()
    assert bridge._connections == set()

    # Real broadcast path — must not raise even with zero clients.
    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)

    # And the watermark must still advance (so subsequent calls in this
    # same 2s window are debounced — confirming the debounce runs even
    # when nobody's listening, keeping behaviour predictable).
    assert bridge._last_snapshot_broadcast_ts > 0.0


@pytest.mark.asyncio
async def test_snapshot_updated_skipped_when_bridge_not_running() -> None:
    """Late messages arriving after ``stop()`` must NOT broadcast."""
    bridge = WSBridge()
    # _running stays False — simulates a message that lands in flight
    # while the bridge is being torn down.
    bridge.broadcast = AsyncMock()  # type: ignore[method-assign]

    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)

    bridge.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_updated_delivers_to_real_connections() -> None:
    """End-to-end: a real broadcast reaches every connected stub WS."""
    bridge = _make_running_bridge()
    client_a = _StubWebSocket()
    client_b = _StubWebSocket()
    bridge._connections.add(client_a)  # type: ignore[arg-type]
    bridge._connections.add(client_b)  # type: ignore[arg-type]

    await bridge._on_snapshot_updated(payload=None, _channel=SNAPSHOT_PUBSUB_CHANNEL)

    for client in (client_a, client_b):
        assert len(client.sent) == 1
        decoded = json.loads(client.sent[0])
        assert decoded["type"] == "snapshot_updated"
        assert "ts" in decoded
