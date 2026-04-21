"""
Unit tests for src/observer/websocket_client.py
"""

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from src.observer.websocket_client import PolymarketWSClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws_mock(messages=None, close_code=None):
    """
    Build an async context manager mock that acts like a websockets connection.
    `messages` is a list of raw string/bytes to yield from the async iterator.
    """
    ws = AsyncMock()
    ws.ping = AsyncMock(return_value=asyncio.get_event_loop().create_future())
    ws.send = AsyncMock()
    ws.close = AsyncMock()

    async def _aiter():
        for m in messages or []:
            yield m

    ws.__aiter__ = lambda self: _aiter()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=ws)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, ws


# ---------------------------------------------------------------------------
# 1. Subscribe sends correct message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_sends_correct_message():
    markets = {"token_a", "token_b"}
    received_messages: list[dict] = []

    async def on_msg(msg):
        received_messages.append(msg)

    client = PolymarketWSClient(on_message=on_msg, markets=markets)

    cm, ws = _make_ws_mock(messages=[])

    with patch("src.observer.websocket_client.websockets.connect", return_value=cm):
        # Run _connect_and_run — it will exit cleanly because iterator is empty
        await client._connect_and_run()

    ws.send.assert_awaited_once()
    sent = json.loads(ws.send.call_args[0][0])
    assert sent["type"] == "market"
    assert sent["custom_feature_enabled"] is True
    assert set(sent["assets_ids"]) == markets


# ---------------------------------------------------------------------------
# 2. Reconnect on ConnectionClosed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_on_connection_closed():
    async def on_msg(msg):
        pass

    client = PolymarketWSClient(on_message=on_msg)
    client._running = True

    call_count = 0

    async def fake_connect_and_run():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionClosed(None, None)
        # Second call: stop cleanly
        client._running = False

    async def raise_timeout(awaitable, timeout):
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError

    with patch.object(client, "_connect_and_run", side_effect=fake_connect_and_run):
        with patch("asyncio.wait_for", side_effect=raise_timeout):
            await client._connect_loop()

    assert client.reconnect_count == 1
    assert call_count == 2


# ---------------------------------------------------------------------------
# 3. Ping loop closes connection on pong timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_loop_pong_timeout():
    async def on_msg(msg):
        pass

    client = PolymarketWSClient(on_message=on_msg)

    ws = AsyncMock()
    ws.close = AsyncMock()

    future = asyncio.get_event_loop().create_future()
    # ping() returns a future that never completes → simulates timeout
    ws.ping = AsyncMock(return_value=future)

    with patch("src.observer.websocket_client.settings.WEBSOCKET_PING_INTERVAL_S", 0):
        with patch("src.observer.websocket_client.settings.WEBSOCKET_PONG_TIMEOUT_S", 0):
            # asyncio.sleep(0) yields immediately; wait_for with timeout=0 times out
            await client._ping_loop(ws)

    ws.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. on_message called for a trade event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_called_for_trade_event():
    received: list[dict] = []

    async def on_msg(msg):
        received.append(msg)

    client = PolymarketWSClient(on_message=on_msg)

    trade_msg = json.dumps(
        {
            "event_type": "trade",
            "asset_id": "token_x",
            "price": "0.65",
            "size": "1000",
            "side": "BUY",
            "maker_address": "0xabc",
            "taker_address": "0xdef",
            "timestamp": "1700000000000",
        }
    )

    cm, ws = _make_ws_mock(messages=[trade_msg])

    with patch("src.observer.websocket_client.websockets.connect", return_value=cm):
        await client._connect_and_run()

    assert len(received) == 1
    assert received[0]["event_type"] == "trade"
    assert received[0]["asset_id"] == "token_x"


# ---------------------------------------------------------------------------
# 5. on_message called for each item in a list message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_called_for_list_message():
    received: list[dict] = []

    async def on_msg(msg):
        received.append(msg)

    client = PolymarketWSClient(on_message=on_msg)

    list_msg = json.dumps(
        [
            {"event_type": "trade", "asset_id": "t1"},
            {"event_type": "trade", "asset_id": "t2"},
            {"event_type": "trade", "asset_id": "t3"},
        ]
    )

    cm, ws = _make_ws_mock(messages=[list_msg])

    with patch("src.observer.websocket_client.websockets.connect", return_value=cm):
        await client._connect_and_run()

    assert len(received) == 3
    asset_ids = {m["asset_id"] for m in received}
    assert asset_ids == {"t1", "t2", "t3"}


# ---------------------------------------------------------------------------
# 6. update_markets updates the internal set
# ---------------------------------------------------------------------------


def test_update_markets():
    async def on_msg(msg):
        pass

    client = PolymarketWSClient(on_message=on_msg, markets={"old_token"})
    assert "old_token" in client._markets

    client.update_markets({"new_token_a", "new_token_b"})
    assert client._markets == {"new_token_a", "new_token_b"}
    assert "old_token" not in client._markets


# ---------------------------------------------------------------------------
# 7. stop() sets _running = False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_stops_running():
    async def on_msg(msg):
        pass

    client = PolymarketWSClient(on_message=on_msg)
    client._running = True

    # No real WS — just call stop
    await client.stop()

    assert client._running is False
    assert client._stop_event.is_set()
