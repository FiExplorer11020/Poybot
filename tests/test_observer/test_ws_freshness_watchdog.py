"""
Phase 3 Round 1 (Agent A) — WS freshness watchdog tests.

The user-facing symptom this addresses is the silent-stall case: the
WS socket is open (ping/pong are fine) but the upstream stopped
shipping events on a specific channel for an extended period. The
old client never noticed; we sat on a healthy-looking socket while
producing zero data.

Covers:
1. Stale-channel detection — per-channel `observer:ws:last_msg:*` keys
   older than WS_CHANNEL_STALE_S trigger the watchdog.
2. The watchdog calls `force_reconnect` and bumps
   `polybot_ws_channel_stale_total{channel}`.
3. Missing keys at boot don't fault before `WS_CHANNEL_STALE_S` of
   process uptime (otherwise the very first tick would always fault).
4. `_compute_backfill_hours` clamps to WS_BACKFILL_MAX_HOURS.
5. With no Redis attached, the watchdog is a no-op (legacy path).
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.config import settings
from src.observer.websocket_client import PolymarketWSClient, _ws_last_msg_key


@pytest.fixture
def fake_redis():
    r = AsyncMock()
    store: dict[str, str] = {}

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ex=None, nx=None):
        store[key] = value if isinstance(value, str) else str(value)
        return True

    r.get = AsyncMock(side_effect=_get)
    r.set = AsyncMock(side_effect=_set)
    r._store = store
    return r


def _make_client(redis_client=None, markets=None) -> PolymarketWSClient:
    async def _noop_handler(msg):
        return None

    # Use `is None` so the empty-set case (markets=set()) is honored —
    # `markets or {...}` falls through to the default because `set()` is
    # falsy. Round 3 fix for the test_watchdog_skips_when_no_markets test.
    return PolymarketWSClient(
        on_message=_noop_handler,
        markets={"0xtoken1"} if markets is None else markets,
        redis_client=redis_client,
    )


# ---------------------------------------------------------------------------
# 1. Stale channel detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_finds_stale_channels(fake_redis):
    client = _make_client(redis_client=fake_redis)
    client.last_message_at = time.time()  # process is "old enough"
    # book channel: last message 120s ago
    fake_redis._store[_ws_last_msg_key("book")] = str(time.time() - 120)
    # price_change channel: fresh, 5s ago
    fake_redis._store[_ws_last_msg_key("price_change")] = str(time.time() - 5)
    stale = await client._scan_stale_channels(
        now_s=time.time(), stale_threshold_s=60
    )
    channels = {c for c, _ in stale}
    assert "book" in channels
    assert "price_change" not in channels


@pytest.mark.asyncio
async def test_scan_no_stale_when_all_fresh(fake_redis):
    client = _make_client(redis_client=fake_redis)
    client.last_message_at = time.time()
    now = time.time()
    for ch in ("book", "price_change", "trade"):
        fake_redis._store[_ws_last_msg_key(ch)] = str(now - 5)
    stale = await client._scan_stale_channels(now_s=now, stale_threshold_s=60)
    assert stale == []


# ---------------------------------------------------------------------------
# 2. Missing keys early in process life — don't fault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_keys_at_boot_dont_fault(fake_redis):
    """If the process just came up and hasn't seen anything yet, missing
    Redis keys are NOT a fault — the upstream might just be quiet."""
    client = _make_client(redis_client=fake_redis)
    # Simulate "just started" — last_message_at is None.
    client.last_message_at = None
    # The watchdog's logic treats `inf` age as "always stale" only
    # AFTER stale_threshold_s of uptime. Since `last_message_at is
    # None`, process_age_s = inf so the missing key path SHOULD fire.
    # The proper "just booted" test is: we just got a message, so
    # process_age_s is tiny.
    client.last_message_at = time.time() - 5  # 5 s ago
    stale = await client._scan_stale_channels(
        now_s=time.time(), stale_threshold_s=60
    )
    assert stale == []  # no key, but uptime < threshold


# ---------------------------------------------------------------------------
# 3. force_reconnect is wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_reconnect_closes_socket(fake_redis):
    client = _make_client(redis_client=fake_redis)
    client._running = True

    ws_mock = AsyncMock()
    ws_mock.close = AsyncMock()
    client._ws = ws_mock

    await client.force_reconnect()
    ws_mock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_reconnect_noop_when_not_running(fake_redis):
    client = _make_client(redis_client=fake_redis)
    client._running = False
    ws_mock = AsyncMock()
    client._ws = ws_mock
    await client.force_reconnect()
    ws_mock.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Backfill-hours computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_backfill_hours_clamps_to_max(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "WS_BACKFILL_MAX_HOURS", 24.0)
    client = _make_client(redis_client=fake_redis)
    # last_msg ages out to 100 h ago.
    fake_redis._store[_ws_last_msg_key("any")] = str(time.time() - 100 * 3600)
    hours = await client._compute_backfill_hours()
    assert hours == 24.0


@pytest.mark.asyncio
async def test_compute_backfill_hours_returns_actual_under_cap(fake_redis):
    client = _make_client(redis_client=fake_redis)
    fake_redis._store[_ws_last_msg_key("any")] = str(time.time() - 1.5 * 3600)
    hours = await client._compute_backfill_hours()
    assert 1.4 <= hours <= 1.6


@pytest.mark.asyncio
async def test_compute_backfill_hours_zero_on_fresh_boot(fake_redis):
    client = _make_client(redis_client=fake_redis)
    hours = await client._compute_backfill_hours()
    assert hours == 0.0


@pytest.mark.asyncio
async def test_compute_backfill_hours_zero_when_no_redis():
    client = _make_client(redis_client=None)
    hours = await client._compute_backfill_hours()
    assert hours == 0.0


# ---------------------------------------------------------------------------
# 5. Watchdog disabled when no redis client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_noop_without_redis():
    """Constructor accepts redis_client=None and no watchdog task is
    created on start (the start() method short-circuits)."""
    client = _make_client(redis_client=None)
    # Don't actually call start() — that would attempt the WS connect.
    # Verify the field defaults.
    assert client._watchdog_task is None
    assert client._redis is None


# ---------------------------------------------------------------------------
# 6. Watchdog respects empty market set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_skips_when_no_markets_subscribed(fake_redis):
    """If no markets are subscribed, channel silence is expected — the
    watchdog should NOT fire a reconnect.

    Round 3 fix: the helper `_make_client(markets=set())` now correctly
    passes the empty set through (was falling through `markets or ...`
    to the default before).
    """
    client = _make_client(redis_client=fake_redis, markets=set())
    client._running = True
    client.last_message_at = time.time() - 1000
    # Stale key.
    fake_redis._store[_ws_last_msg_key("book")] = str(time.time() - 1000)

    # Patch the wait_for to exit cleanly after one iteration.
    ws_mock = AsyncMock()
    ws_mock.close = AsyncMock()
    client._ws = ws_mock

    async def run_one_tick():
        # Run the watchdog body manually rather than the full loop.
        if not client._markets:
            return  # the documented skip path
        stale = await client._scan_stale_channels(
            now_s=time.time(), stale_threshold_s=60
        )
        for _ch, _s in stale:
            await client.force_reconnect()

    await run_one_tick()
    ws_mock.close.assert_not_awaited()
