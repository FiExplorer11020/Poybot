"""
Tests for ``src/control/redis_pubsub.py`` — the centralized Subscriber.

Covers the seven contractual properties demanded by Phase 2 Task D:

  1. ``register()`` and the decorator form work and reject duplicate
     channel registration.
  2. ``start()`` issues SUBSCRIBE for every registered channel.
  3. A simulated ``ConnectionError`` mid-listen triggers reconnect AND
     re-subscribe — this is the core F-04 fix.
  4. A handler raising an exception increments the error counter but
     does NOT kill the loop; the next message is still delivered.
  5. ``stop()`` cleanly cancels the task and closes the connection.
  6. Backoff is bounded — it never hits 1-second-forever (it grows) and
     never grows unboundedly (it caps at 30s).
  7. Two Subscribers on the same channel both receive every message.

Most tests use a shared fakeredis instance for the publisher + the
subscriber (the Subscriber accepts a ``redis_client=`` injection for
exactly this case). The reconnect test uses a hand-rolled fake that
exposes a ``trigger_disconnect()`` knob so we can deterministically
fire a ConnectionError without relying on fakeredis internals.
"""

from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis
import pytest
import redis.asyncio as redis_async

from src.control import redis_pubsub
from src.control.redis_pubsub import Subscriber, _BACKOFF_SCHEDULE_S


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.01) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _await_connected(sub: Subscriber, timeout: float = 2.0) -> None:
    """Wait until the subscriber's listen loop has issued SUBSCRIBE.

    fakeredis pub/sub has no message retention — if we publish before
    the subscriber's pubsub.subscribe() coroutine has resolved server
    side, the message is dropped. The Subscriber flips `is_connected`
    to True only after SUBSCRIBE succeeds, so it's the right barrier.
    """
    ok = await _wait_for(lambda: sub.is_connected, timeout=timeout)
    assert ok, "subscriber failed to reach is_connected=True"


# --------------------------------------------------------------------------- #
# 1. Registration                                                              #
# --------------------------------------------------------------------------- #


async def test_register_binds_handler_to_channel():
    sub = Subscriber("redis://ignored", name="test.register")
    received: list = []

    async def handler(payload, channel):
        received.append((channel, payload))

    sub.register("foo:bar", handler)
    assert "foo:bar" in sub.channels
    assert sub.channels == ("foo:bar",)


async def test_handler_decorator_form_registers():
    sub = Subscriber("redis://ignored", name="test.decorator")

    @sub.handler("foo:bar")
    async def on_foo(payload, channel):
        return None

    assert sub.channels == ("foo:bar",)


async def test_duplicate_channel_registration_raises():
    sub = Subscriber("redis://ignored", name="test.dup")

    async def h(payload, channel):
        return None

    sub.register("foo", h)
    with pytest.raises(ValueError, match="already registered"):
        sub.register("foo", h)


async def test_register_after_start_raises(redis_client):
    sub = Subscriber("redis://ignored", name="test.late")

    async def h(payload, channel):
        return None

    sub.register("foo", h)
    await sub.start(redis_client=redis_client)
    try:
        with pytest.raises(RuntimeError, match="before start"):
            sub.register("bar", h)
    finally:
        await sub.stop()


async def test_start_without_handlers_raises(redis_client):
    sub = Subscriber("redis://ignored", name="test.empty")
    with pytest.raises(RuntimeError, match="no handlers"):
        await sub.start(redis_client=redis_client)


# --------------------------------------------------------------------------- #
# 2. SUBSCRIBE is issued for every registered channel                          #
# --------------------------------------------------------------------------- #


async def test_start_subscribes_to_every_channel(redis_client):
    sub = Subscriber("redis://ignored", name="test.multi")
    received: list[tuple[str, dict]] = []

    async def h(payload, channel):
        received.append((channel, payload))

    sub.register("ch1", h)
    sub.register("ch2", h)
    sub.register("ch3", h)
    await sub.start(redis_client=redis_client)
    await _await_connected(sub)
    try:
        # Publish one message on each channel; if subscribe didn't go
        # through for any of them, we'd miss it here.
        for ch in ("ch1", "ch2", "ch3"):
            await redis_client.publish(ch, json.dumps({"ch": ch}))
        ok = await _wait_for(lambda: len(received) >= 3, timeout=2.0)
        assert ok, f"expected 3 messages, got {len(received)}"
        assert sorted(c for c, _ in received) == ["ch1", "ch2", "ch3"]
    finally:
        await sub.stop()


async def test_json_payload_is_decoded(redis_client):
    sub = Subscriber("redis://ignored", name="test.json")
    received: list = []

    async def h(payload, channel):
        received.append(payload)

    sub.register("ch", h)
    await sub.start(redis_client=redis_client)
    await _await_connected(sub)
    try:
        await redis_client.publish("ch", json.dumps({"hello": "world", "n": 7}))
        ok = await _wait_for(lambda: len(received) >= 1, timeout=1.0)
        assert ok
        assert received[0] == {"hello": "world", "n": 7}
    finally:
        await sub.stop()


# --------------------------------------------------------------------------- #
# 3. Reconnect + resubscribe on ConnectionError                                #
# --------------------------------------------------------------------------- #


class _FakePubsub:
    """Hand-rolled pubsub that raises ConnectionError on demand.

    The queue is partitioned into 'sessions' — a list per consume_once
    call. ``subscribe()`` advances the session pointer, so messages
    queued under index 1 are only delivered AFTER a reconnect (which is
    the only way ``subscribe()`` runs a second time).
    """

    def __init__(self, fail_event: asyncio.Event, msg_sessions: list[list[dict]]):
        self._fail = fail_event
        self._msg_sessions = msg_sessions
        self._session_idx = -1  # incremented by subscribe()
        self.subscribe_call_count = 0

    async def subscribe(self, *channels):
        self.subscribe_call_count += 1
        self._session_idx += 1

    async def unsubscribe(self, *channels):
        return None

    async def aclose(self):
        return None

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        if self._fail.is_set():
            self._fail.clear()
            raise redis_async.ConnectionError("simulated disconnect")
        if 0 <= self._session_idx < len(self._msg_sessions):
            queue = self._msg_sessions[self._session_idx]
            if queue:
                return queue.pop(0)
        await asyncio.sleep(0.01)
        return None


class _FakeRedis:
    """Just enough redis.asyncio surface to drive Subscriber."""

    def __init__(self, pubsub: _FakePubsub):
        self._pubsub = pubsub
        self.aclose_called = False

    def pubsub(self):
        return self._pubsub

    async def aclose(self):
        self.aclose_called = True


async def test_reconnect_resubscribes_after_connection_error(monkeypatch):
    """The F-04 fix: when the listen loop raises ConnectionError, the
    subscriber must reconnect AND re-issue SUBSCRIBE for every channel."""
    # Shrink backoff to ~0 so the test runs fast.
    monkeypatch.setattr(redis_pubsub, "_BACKOFF_SCHEDULE_S", (0.0, 0.0, 0.0))

    fail = asyncio.Event()
    msg1 = {"type": "message", "channel": "ch", "data": '{"v": 1}'}
    msg2 = {"type": "message", "channel": "ch", "data": '{"v": 2}'}
    # Session 0 (first subscribe) yields msg1; we trigger fail; after
    # reconnect session 1 (second subscribe) yields msg2. The session
    # boundary advances ONLY on subscribe(), so msg2 is unreachable
    # unless the reconnect path actually re-subscribes.
    pubsub = _FakePubsub(fail, msg_sessions=[[msg1], [msg2]])
    fake_redis = _FakeRedis(pubsub)

    received: list = []
    sub = Subscriber("redis://ignored", name="test.reconnect")

    async def h(payload, channel):
        received.append(payload)

    sub.register("ch", h)
    await sub.start(redis_client=fake_redis)
    try:
        # Wait for the first message
        ok = await _wait_for(lambda: len(received) >= 1, timeout=2.0)
        assert ok, "first message must arrive"
        assert received[0] == {"v": 1}

        # Trigger the disconnect.
        fail.set()

        # Wait for the second message AFTER reconnect.
        ok = await _wait_for(lambda: len(received) >= 2, timeout=2.0)
        assert ok, "second message must arrive after reconnect"
        assert received[1] == {"v": 2}

        # SUBSCRIBE must have been called at least twice (initial + reconnect).
        assert pubsub.subscribe_call_count >= 2, (
            f"SUBSCRIBE was only issued {pubsub.subscribe_call_count} times; "
            "reconnect path should re-issue it"
        )
        # The reconnect counter must be at least 1.
        assert sub.total_reconnects >= 1
    finally:
        await sub.stop()


# --------------------------------------------------------------------------- #
# 4. Handler exceptions don't kill the loop                                    #
# --------------------------------------------------------------------------- #


async def test_handler_exception_does_not_kill_loop(redis_client):
    sub = Subscriber("redis://ignored", name="test.handler_err")
    received: list = []
    raised = {"count": 0}

    async def flaky(payload, channel):
        if payload.get("kaboom"):
            raised["count"] += 1
            raise RuntimeError("synthetic handler error")
        received.append(payload)

    sub.register("ch", flaky)
    await sub.start(redis_client=redis_client)
    await _await_connected(sub)
    try:
        # First message raises
        await redis_client.publish("ch", json.dumps({"kaboom": True}))
        # Subsequent message must still land
        await redis_client.publish("ch", json.dumps({"ok": True}))
        ok = await _wait_for(lambda: len(received) >= 1, timeout=2.0)
        assert ok, "subscriber must keep running after handler exception"
        assert received[0] == {"ok": True}
        assert raised["count"] == 1
        assert sub.handler_errors == 1
        # Reconnect counter must NOT be touched — handler errors are not
        # reconnects.
        assert sub.total_reconnects == 0
    finally:
        await sub.stop()


async def test_bad_json_does_not_kill_loop(redis_client):
    sub = Subscriber("redis://ignored", name="test.bad_json")
    received: list = []

    async def h(payload, channel):
        received.append(payload)

    sub.register("ch", h)
    await sub.start(redis_client=redis_client)
    await _await_connected(sub)
    try:
        # Garbage payload — must be skipped, not crash the loop.
        await redis_client.publish("ch", "{not json")
        # Then a good one — proves the loop is still alive.
        await redis_client.publish("ch", json.dumps({"ok": True}))
        ok = await _wait_for(lambda: len(received) >= 1, timeout=2.0)
        assert ok
        assert received[0] == {"ok": True}
    finally:
        await sub.stop()


# --------------------------------------------------------------------------- #
# 5. stop() cleanly cancels the task                                           #
# --------------------------------------------------------------------------- #


async def test_stop_cancels_task_cleanly(redis_client):
    sub = Subscriber("redis://ignored", name="test.stop")

    async def h(payload, channel):
        return None

    sub.register("ch", h)
    await sub.start(redis_client=redis_client)
    assert sub._task is not None and not sub._task.done()
    await sub.stop()
    # The task must be done and the subscriber marked not running.
    assert sub._task is None
    assert sub._running is False


async def test_stop_is_idempotent():
    sub = Subscriber("redis://ignored", name="test.stop2")

    async def h(payload, channel):
        return None

    sub.register("ch", h)
    # Stop before start — should not blow up.
    await sub.stop()


async def test_stop_closes_owned_redis_client(monkeypatch):
    """When Subscriber opens its own client, stop() must close it."""
    closed = {"called": False}

    class _Tracked:
        def __init__(self):
            self.aclose_called = False

        def pubsub(self):
            # Simple pubsub: returns no messages, never fails.
            ev = asyncio.Event()  # never set
            return _FakePubsub(ev, msg_sessions=[])

        async def aclose(self):
            closed["called"] = True

    def fake_from_url(_url, decode_responses=True):
        return _Tracked()

    monkeypatch.setattr(redis_pubsub.redis_async, "from_url", fake_from_url)

    sub = Subscriber("redis://ignored", name="test.owned")

    async def h(payload, channel):
        return None

    sub.register("ch", h)
    await sub.start()  # NOTE: no redis_client= injection → builds its own
    await sub.stop()
    assert closed["called"] is True, "owned client must be closed on stop()"


# --------------------------------------------------------------------------- #
# 6. Backoff is bounded                                                        #
# --------------------------------------------------------------------------- #


def test_backoff_schedule_is_bounded_and_growing():
    """Defensive: any future edit to the constant must keep these invariants."""
    schedule = _BACKOFF_SCHEDULE_S
    assert len(schedule) > 0
    assert schedule[0] >= 0.5, "first backoff must be at least 0.5s"
    assert schedule[-1] <= 30.0, "last backoff must cap at 30s"
    # Monotonic non-decreasing.
    for a, b in zip(schedule, schedule[1:]):
        assert b >= a


# --------------------------------------------------------------------------- #
# 7. Two subscribers on the same channel both receive every message            #
# --------------------------------------------------------------------------- #


async def test_two_subscribers_receive_all_messages(redis_client):
    sub_a = Subscriber("redis://ignored", name="test.fanout.a")
    sub_b = Subscriber("redis://ignored", name="test.fanout.b")

    received_a: list = []
    received_b: list = []

    async def ha(payload, channel):
        received_a.append(payload)

    async def hb(payload, channel):
        received_b.append(payload)

    sub_a.register("fanout", ha)
    sub_b.register("fanout", hb)
    await sub_a.start(redis_client=redis_client)
    await sub_b.start(redis_client=redis_client)
    await _await_connected(sub_a)
    await _await_connected(sub_b)
    try:
        for i in range(5):
            await redis_client.publish("fanout", json.dumps({"i": i}))
        ok_a = await _wait_for(lambda: len(received_a) >= 5, timeout=2.0)
        ok_b = await _wait_for(lambda: len(received_b) >= 5, timeout=2.0)
        assert ok_a, f"sub_a got {len(received_a)} of 5"
        assert ok_b, f"sub_b got {len(received_b)} of 5"
        assert [m["i"] for m in received_a] == [0, 1, 2, 3, 4]
        assert [m["i"] for m in received_b] == [0, 1, 2, 3, 4]
    finally:
        await sub_a.stop()
        await sub_b.stop()


# --------------------------------------------------------------------------- #
# Bonus: health surface                                                        #
# --------------------------------------------------------------------------- #


async def test_health_counters_increment(redis_client):
    sub = Subscriber("redis://ignored", name="test.health")
    received: list = []

    async def h(payload, channel):
        received.append(payload)

    sub.register("ch", h)
    await sub.start(redis_client=redis_client)
    await _await_connected(sub)
    try:
        assert sub.is_connected is True
        assert sub.total_messages == 0
        await redis_client.publish("ch", json.dumps({"i": 0}))
        await redis_client.publish("ch", json.dumps({"i": 1}))
        ok = await _wait_for(lambda: sub.total_messages >= 2, timeout=2.0)
        assert ok
        assert sub.total_messages == 2
        assert sub.last_message_ts("ch") is not None
    finally:
        await sub.stop()


async def test_runtime_config_pubsub_invalidates_cache():
    """End-to-end wiring check: when the runtime_config:changed channel
    fires, RuntimeConfig._cache must drop to None so the next read
    refetches from Redis."""
    from src.control import runtime_config as rc_module
    from src.control.runtime_config import REDIS_PUBSUB_CHANNEL, RuntimeConfig

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        rc = RuntimeConfig(redis_client=client)
        # Prime the cache so we can observe invalidation.
        rc._cache = rc_module._CachedOverrides(values={"kelly_fraction": 0.5}, fetched_at=0.0)
        await rc.start_pubsub()
        # Wait for SUBSCRIBE to settle before publishing — fakeredis
        # has no pub/sub queue retention.
        assert rc._subscriber is not None
        await _await_connected(rc._subscriber)
        await client.publish(
            REDIS_PUBSUB_CHANNEL,
            json.dumps({"actor": "test", "edits": {"kelly_fraction": 0.6}, "ts": 0}),
        )
        ok = await _wait_for(lambda: rc._cache is None, timeout=2.0)
        assert ok, "RuntimeConfig._cache must be invalidated on pub/sub"
        await rc.stop_pubsub()
    finally:
        await client.aclose()
