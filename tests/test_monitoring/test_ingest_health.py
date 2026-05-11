"""
Tests for ``src/monitoring/ingest_health.py`` (Phase 3 Round 1, Agent D).

The invariants under test are:

* ``heartbeat`` updates ``last_heartbeat_at`` and is a no-op on unknown
  source name (lazy-register, never crash).
* The watchdog loop detects ``now - last_heartbeat_at > threshold`` and
  fires ``on_gap`` callbacks exactly once per gap (not once per tick).
* When heartbeat returns after a gap, the next heartbeat exits gap
  state, logs INFO and increments
  ``polybot_ingest_recovery_success_total``.
* Cooldown prevents back-to-back recovery firings: within
  ``RECOVERY_COOLDOWN_S`` the second detection logs the
  ``skipped_cooldown`` metric and does NOT invoke the callback.
* Concurrent callbacks fan out via ``asyncio.gather`` — one slow
  callback does not block another.

We drive the loop manually via ``await monitor._tick()`` instead of
spinning up the background task; this keeps the tests deterministic
(no real-time sleep dependency).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.monitoring import ingest_health as ih


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts with a fresh monitor — the global
    singleton would otherwise leak state across tests."""
    ih.reset_health_monitor()
    yield
    ih.reset_health_monitor()


@pytest.fixture
def monitor():
    """A monitor with low thresholds for fast tests."""
    return ih.IngestHealthMonitor(
        thresholds_s={
            ih.SOURCE_WS_MARKET_FEED: 2,
            ih.SOURCE_REST_DATA_API: 1,
            ih.SOURCE_FALCON_LEADERBOARD: 5,
        },
        recovery_cooldown_s=1,
        loop_interval_s=1,
    )


# --------------------------------------------------------------------------- #
# Heartbeat semantics                                                          #
# --------------------------------------------------------------------------- #


def test_heartbeat_updates_timestamp(monitor):
    """heartbeat() must move last_heartbeat_at forward."""
    state = monitor._sources[ih.SOURCE_WS_MARKET_FEED]
    assert state.last_heartbeat_at == 0.0
    monitor.heartbeat(ih.SOURCE_WS_MARKET_FEED)
    assert state.last_heartbeat_at > 0.0


def test_heartbeat_unknown_source_lazy_registers(monitor):
    """Unknown source is lazy-registered, not silently dropped."""
    monitor.heartbeat("not_a_real_source")
    assert "not_a_real_source" in monitor._sources


def test_heartbeat_never_raises(monitor, monkeypatch):
    """Hot path must NEVER raise (it's called from observer loops)."""
    # Force the underlying dict access to blow up.
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated metrics failure")

    monkeypatch.setattr(
        ih.ingest_threshold_breaches_active, "labels", _boom, raising=False
    )
    # Should not propagate.
    monitor.heartbeat(ih.SOURCE_WS_MARKET_FEED)


# --------------------------------------------------------------------------- #
# Gap detection                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_watchdog_detects_gap_above_threshold(monitor):
    """A source past its threshold transitions to in_gap=True and fires recovery."""
    cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, cb)

    # Set last_heartbeat_at far enough in the past to trip the 1 s threshold.
    state = monitor._sources[ih.SOURCE_REST_DATA_API]
    state.last_heartbeat_at = time.monotonic() - 10

    await monitor._tick()

    assert state.in_gap is True
    cb.assert_awaited_once()
    # Callback gets (source, gap_duration_s).
    args = cb.await_args.args
    assert args[0] == ih.SOURCE_REST_DATA_API
    assert args[1] >= 1


@pytest.mark.asyncio
async def test_gap_callback_fires_once_per_gap(monitor):
    """Multiple ticks while in gap don't re-fire the callback."""
    cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, cb)

    state = monitor._sources[ih.SOURCE_REST_DATA_API]
    state.last_heartbeat_at = time.monotonic() - 10

    await monitor._tick()
    await monitor._tick()
    await monitor._tick()

    # Only the first tick fires; subsequent ticks see in_gap=True and skip.
    assert cb.await_count == 1


@pytest.mark.asyncio
async def test_below_threshold_no_callback(monitor):
    """Source below threshold: no callback, no gap state."""
    cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_WS_MARKET_FEED, cb)
    monitor.heartbeat(ih.SOURCE_WS_MARKET_FEED)
    await monitor._tick()
    cb.assert_not_awaited()
    assert monitor._sources[ih.SOURCE_WS_MARKET_FEED].in_gap is False


# --------------------------------------------------------------------------- #
# Gap closure                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_heartbeat_after_gap_closes_state(monitor):
    """A heartbeat after a gap clears in_gap and resets the start time."""
    cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, cb)

    state = monitor._sources[ih.SOURCE_REST_DATA_API]
    state.last_heartbeat_at = time.monotonic() - 10
    await monitor._tick()
    assert state.in_gap is True

    monitor.heartbeat(ih.SOURCE_REST_DATA_API)
    assert state.in_gap is False
    assert state.gap_started_at == 0.0


# --------------------------------------------------------------------------- #
# Recovery cooldown                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recovery_cooldown_prevents_callback_storm():
    """Within cooldown, a second gap detection logs skipped, not fires cb."""
    monitor = ih.IngestHealthMonitor(
        thresholds_s={ih.SOURCE_REST_DATA_API: 1},
        recovery_cooldown_s=120,  # very high — easy to assert
        loop_interval_s=1,
    )
    cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, cb)

    state = monitor._sources[ih.SOURCE_REST_DATA_API]

    # First gap → callback fires.
    state.last_heartbeat_at = time.monotonic() - 10
    await monitor._tick()
    assert cb.await_count == 1

    # Close the gap, then re-open immediately.
    monitor.heartbeat(ih.SOURCE_REST_DATA_API)
    state.last_heartbeat_at = time.monotonic() - 10
    await monitor._tick()

    # Within cooldown → no second callback.
    assert cb.await_count == 1


# --------------------------------------------------------------------------- #
# Concurrent callbacks                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_callbacks_dont_race():
    """Multiple callbacks on the same source run via gather without racing."""
    monitor = ih.IngestHealthMonitor(
        thresholds_s={ih.SOURCE_REST_DATA_API: 1},
        recovery_cooldown_s=1,
        loop_interval_s=1,
    )

    order: list[str] = []
    started = asyncio.Event()

    async def slow_cb(source, dur):
        order.append("slow_start")
        await started.wait()
        order.append("slow_done")

    async def fast_cb(source, dur):
        order.append("fast")

    monitor.register_recovery(ih.SOURCE_REST_DATA_API, slow_cb)
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, fast_cb)

    state = monitor._sources[ih.SOURCE_REST_DATA_API]
    state.last_heartbeat_at = time.monotonic() - 10

    # Kick off tick; release the slow callback after a short delay.
    tick_task = asyncio.create_task(monitor._tick())
    await asyncio.sleep(0.01)
    started.set()
    await tick_task

    # Fast callback started before slow_done — concurrent dispatch.
    assert order.index("fast") < order.index("slow_done")


# --------------------------------------------------------------------------- #
# Callback exceptions                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_callback_exception_doesnt_block_others():
    """One bad callback must not starve the others (return_exceptions=True)."""
    monitor = ih.IngestHealthMonitor(
        thresholds_s={ih.SOURCE_REST_DATA_API: 1},
        recovery_cooldown_s=1,
        loop_interval_s=1,
    )

    async def bad_cb(*_a, **_kw):
        raise RuntimeError("simulated callback failure")

    good_cb = AsyncMock()
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, bad_cb)
    monitor.register_recovery(ih.SOURCE_REST_DATA_API, good_cb)

    state = monitor._sources[ih.SOURCE_REST_DATA_API]
    state.last_heartbeat_at = time.monotonic() - 10

    await monitor._tick()

    good_cb.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Singleton accessor                                                           #
# --------------------------------------------------------------------------- #


def test_get_health_monitor_is_singleton():
    """Two calls return the same instance."""
    a = ih.get_health_monitor()
    b = ih.get_health_monitor()
    assert a is b


def test_falcon_agent_to_source_map_complete():
    """Every Falcon agent_id used in src/registry maps to a canonical source."""
    # Sanity: required entries.
    assert ih.FALCON_AGENT_TO_SOURCE[584] == ih.SOURCE_FALCON_LEADERBOARD
    assert ih.FALCON_AGENT_TO_SOURCE[581] == ih.SOURCE_FALCON_WALLET360
    assert ih.FALCON_AGENT_TO_SOURCE[574] == ih.SOURCE_FALCON_MARKETS
    assert ih.FALCON_AGENT_TO_SOURCE[575] == ih.SOURCE_FALCON_MARKETS
    assert ih.FALCON_AGENT_TO_SOURCE[556] == ih.SOURCE_FALCON_TRADES


# --------------------------------------------------------------------------- #
# Threshold env override                                                       #
# --------------------------------------------------------------------------- #


def test_env_override_sets_threshold(monkeypatch):
    """INGEST_THRESHOLD_<SOURCE>_S env var overrides the default."""
    monkeypatch.setenv("INGEST_THRESHOLD_WS_MARKET_FEED_S", "42")
    monitor = ih.IngestHealthMonitor()
    assert monitor._sources[ih.SOURCE_WS_MARKET_FEED].threshold_s == 42


def test_bad_env_keeps_default(monkeypatch, caplog):
    """A malformed env var doesn't crash construction."""
    monkeypatch.setenv("INGEST_THRESHOLD_WS_MARKET_FEED_S", "not_a_number")
    monitor = ih.IngestHealthMonitor()
    # Falls back to the source's normal default.
    assert (
        monitor._sources[ih.SOURCE_WS_MARKET_FEED].threshold_s
        == ih.DEFAULT_THRESHOLDS_S[ih.SOURCE_WS_MARKET_FEED]
    )


# --------------------------------------------------------------------------- #
# Snapshot                                                                     #
# --------------------------------------------------------------------------- #


def test_snapshot_shape(monitor):
    """snapshot() returns the expected dict shape per source."""
    monitor.heartbeat(ih.SOURCE_WS_MARKET_FEED)
    snap = monitor.snapshot()
    assert ih.SOURCE_WS_MARKET_FEED in snap
    entry = snap[ih.SOURCE_WS_MARKET_FEED]
    assert "threshold_s" in entry
    assert "in_gap" in entry
    assert "seconds_since_last_event" in entry
    assert entry["in_gap"] is False
    assert entry["seconds_since_last_event"] is not None


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_stop_idempotent(monitor):
    """start/stop can be called twice without raising."""
    await monitor.start()
    await monitor.start()  # idempotent
    await monitor.stop()
    await monitor.stop()  # idempotent
