"""
Tests for the Watchdog (S3.10).

Strategy:
    * Use fakeredis for the heartbeat keys + crash channel.
    * Register components whose factories yield short-lived coroutines
      we control (sleep, raise, never-end).
    * Manually call `tick()` after pre-conditions are set up — no
      reliance on APScheduler.

Each test pins the watchdog's restart bounds via constructor kwargs so
we don't depend on global settings.
"""

from __future__ import annotations

import asyncio
import json
import time

import fakeredis.aioredis
import pytest

from src.engine import watchdog as watchdog_module
from src.engine.watchdog import Watchdog, write_heartbeat


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


async def _drain_pubsub(redis_client, channel: str, *, timeout: float = 0.3):
    """Collect all messages published to channel within `timeout`. Sub
    BEFORE the publisher publishes for this to work."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    messages = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.05
            )
            if msg and msg.get("type") == "message":
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                try:
                    messages.append(json.loads(data))
                except Exception:
                    messages.append({"raw": data})
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
    return messages


# --------------------------------------------------------------------------- #
# Heartbeat helpers                                                            #
# --------------------------------------------------------------------------- #


async def test_write_and_read_heartbeat(redis_client):
    await write_heartbeat(redis_client, "comp", ttl_s=10)
    ts = await watchdog_module.read_heartbeat(redis_client, "comp")
    assert ts is not None
    assert abs(ts - time.time()) < 1.0


async def test_read_missing_heartbeat_returns_none(redis_client):
    assert await watchdog_module.read_heartbeat(redis_client, "nope") is None


# --------------------------------------------------------------------------- #
# Registration + autostart                                                     #
# --------------------------------------------------------------------------- #


async def test_register_autostart_creates_task(redis_client):
    stop = asyncio.Event()
    wd = Watchdog(redis_client=redis_client, stop_event=stop)
    started = asyncio.Event()

    async def comp():
        started.set()
        await stop.wait()

    await wd.register("comp", comp)
    # Task must be created and running
    assert "comp" in wd.names()
    await asyncio.wait_for(started.wait(), timeout=0.5)
    stop.set()
    await wd.stop_all()


async def test_register_no_autostart(redis_client):
    stop = asyncio.Event()
    wd = Watchdog(redis_client=redis_client, stop_event=stop)
    spawned = []

    async def comp():
        spawned.append("ran")
        await stop.wait()

    await wd.register("comp", comp, autostart=False)
    # Give the loop a turn
    await asyncio.sleep(0.05)
    assert spawned == []  # not yet started
    stop.set()


# --------------------------------------------------------------------------- #
# Crash detection + restart                                                    #
# --------------------------------------------------------------------------- #


async def test_crashed_task_is_restarted(redis_client):
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,  # no waiting in tests
        heartbeat_timeout_s=999,
    )
    runs = []

    async def flaky():
        runs.append(time.time())
        if len(runs) == 1:
            raise RuntimeError("boom")
        await stop.wait()

    await wd.register("flaky", flaky)
    # Let the first run crash
    await asyncio.sleep(0.05)
    # Tick — watchdog should detect the crash and restart
    await wd.tick()
    await asyncio.sleep(0.05)
    assert len(runs) == 2, f"expected restart, got runs={runs}"
    stop.set()
    await wd.stop_all()


async def test_max_restarts_trips_stop_event(redis_client):
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=2,
        backoff_s=0,
        heartbeat_timeout_s=999,
    )
    runs = []

    async def always_crashes():
        runs.append(1)
        raise RuntimeError("forever")

    await wd.register("doomed", always_crashes)
    # Tick repeatedly to drive through max_restarts
    for _ in range(5):
        await asyncio.sleep(0.02)
        await wd.tick()
        if stop.is_set():
            break
    assert stop.is_set(), "stop_event must be tripped after max restarts"


async def test_restart_publishes_engine_crash(redis_client):
    """When watchdog restarts a component, it must publish on
    engine:crash so Telegram can alert."""
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=999,
    )

    # Subscribe BEFORE we register (so we don't miss the publish)
    sub_task = asyncio.create_task(
        _drain_pubsub(redis_client, "engine:crash", timeout=0.5)
    )
    await asyncio.sleep(0.05)

    async def crasher():
        raise RuntimeError("kaboom")

    await wd.register("crasher", crasher)
    await asyncio.sleep(0.05)
    await wd.tick()

    msgs = await sub_task
    assert any(m.get("component") == "crasher" for m in msgs)
    stop.set()
    await wd.stop_all()


async def test_no_restart_when_stop_already_set(redis_client):
    """If stop_event is set, a finished task should NOT trigger a restart."""
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=999,
    )
    runs = []

    async def comp():
        runs.append(1)

    await wd.register("comp", comp)
    await asyncio.sleep(0.05)
    stop.set()
    await wd.tick()
    await asyncio.sleep(0.05)
    assert len(runs) == 1


# --------------------------------------------------------------------------- #
# Heartbeat freeze detection                                                   #
# --------------------------------------------------------------------------- #


async def test_heartbeat_freeze_triggers_restart(redis_client):
    """A long-running task with stale heartbeat should be killed and
    restarted."""
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=1,  # 1s
    )
    runs = []

    async def silent():
        # Never writes a heartbeat — frozen by definition.
        runs.append(1)
        await asyncio.sleep(60)

    await wd.register("silent", silent, heartbeat_interval_s=1)
    # Wait past the cold-start grace window (2 × interval = 2s)
    await asyncio.sleep(2.5)
    await wd.tick()
    await asyncio.sleep(0.1)
    assert len(runs) >= 2, f"frozen task must be restarted, got {runs}"
    stop.set()
    await wd.stop_all()


async def test_recent_heartbeat_avoids_restart(redis_client):
    """A live task that pings recently is not restarted."""
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=10,
    )
    runs = []

    async def healthy():
        runs.append(1)
        while not stop.is_set():
            await write_heartbeat(redis_client, "healthy", ttl_s=30)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    await wd.register("healthy", healthy, heartbeat_interval_s=1)
    # Wait past cold-start grace
    await asyncio.sleep(2.5)
    await wd.tick()
    await asyncio.sleep(0.05)
    # Should still be the original run
    assert len(runs) == 1
    stop.set()
    await wd.stop_all()


# --------------------------------------------------------------------------- #
# Restart counter forgiveness                                                  #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# QW3 (audit 2026-05-17) — alpha-component isolation from engine kill          #
# --------------------------------------------------------------------------- #


async def test_alpha_component_exhausts_restarts_without_killing_engine(
    redis_client,
):
    """QW3: ``intent_router`` is in ALPHA_COMPONENTS. When it exhausts
    its restart budget, the watchdog must disable it in place (set
    ``state.disabled = True``) and DO NOT trip ``stop_event``. The
    engine stays UP — the trading critical path never lost its
    decisions-channel consumer.

    Pre-fix (the production incident this QW closes): the engine
    auto-killed itself the moment intent_router crash-looped 4 times,
    opening multi-minute windows where the paper trader had no
    confidence-engine signals to act on.
    """
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=999,
    )
    runs = []

    async def always_crashes():
        runs.append(1)
        raise RuntimeError("alpha component is unstable")

    # Register under the canonical alpha name.
    await wd.register("intent_router", always_crashes)
    # Drive past max_restarts.
    for _ in range(6):
        await asyncio.sleep(0.02)
        await wd.tick()
    # The whole point of QW3 — engine must remain UP.
    assert not stop.is_set(), (
        "QW3 regression: alpha component exhausted restarts and the "
        "watchdog still tripped stop_event. The engine kill-switch on "
        "intent_router crashes is back."
    )
    # Component should be marked disabled and the in-flight task
    # cancelled / cleaned up.
    state = wd._components["intent_router"]  # type: ignore[attr-defined]
    assert state.disabled is True
    # Once disabled, further ticks must be no-ops — no new task spawns.
    runs_before = len(runs)
    await wd.tick()
    await asyncio.sleep(0.05)
    assert len(runs) == runs_before, (
        "Disabled alpha component restarted on next tick"
    )
    stop.set()
    await wd.stop_all()


async def test_non_alpha_component_still_kills_engine_on_max_restarts(
    redis_client,
):
    """QW3 mirror: components NOT in ALPHA_COMPONENTS keep the legacy
    behaviour. The paper_trader / confidence_engine / observer crashing
    loop-fast must still trip stop_event — those ARE the trading
    critical path.
    """
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=2,
        backoff_s=0,
        heartbeat_timeout_s=999,
    )

    async def always_crashes():
        raise RuntimeError("critical path failure")

    # Pick a clearly non-alpha name — this stands in for paper_trader /
    # confidence_engine.
    await wd.register("paper_trader", always_crashes)
    for _ in range(5):
        await asyncio.sleep(0.02)
        await wd.tick()
        if stop.is_set():
            break
    assert stop.is_set(), (
        "Non-alpha component exhausted restarts but engine kept "
        "running — the legacy safety behaviour was broken alongside "
        "the QW3 fix."
    )
    state = wd._components["paper_trader"]  # type: ignore[attr-defined]
    # Non-alpha components don't get the ``disabled`` flag; they trip
    # the global stop instead.
    assert state.disabled is False


async def test_restart_counter_resets_after_stable_period(redis_client):
    stop = asyncio.Event()
    wd = Watchdog(
        redis_client=redis_client,
        stop_event=stop,
        max_restarts=3,
        backoff_s=0,
        heartbeat_timeout_s=999,
        restart_reset_s=0,  # reset immediately
    )
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("transient")
        await stop.wait()

    await wd.register("flaky", flaky)
    await asyncio.sleep(0.05)
    await wd.tick()  # detects crash, restarts
    await asyncio.sleep(0.05)
    # Now the second run is stable — tick should reset restart_count
    await wd.tick()
    state = wd._components["flaky"]  # type: ignore[attr-defined]
    assert state.restart_count == 0
    stop.set()
    await wd.stop_all()
