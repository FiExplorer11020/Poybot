"""
Unit tests for `src.api.snapshot_builder`.

The builder runs in the maintenance container — it MUST:

1. Compose a dict with every dashboard section key present.
2. Tolerate per-section failures (return defaults, keep building).
3. Write three things to Redis: snapshot JSON, built-at epoch, and a
   pubsub event on the dedicated channel.
4. Apply the 120s TTL on both keys.
5. Serialise concurrent builds via the in-process lock.
6. Bound DB concurrency to `_MAX_PARALLEL` via the semaphore.
7. Survive total query failure (every section raises) without crashing.

All external dependencies are mocked — the tests stub out `queries.*`,
the asyncpg `Pool`, and the redis client. The real production wiring is
exercised in the maintenance container integration test (Agent B's
territory, not here).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import snapshot_builder
from src.api.snapshot_builder import (
    SNAPSHOT_BUILT_AT_KEY,
    SNAPSHOT_PUBSUB_CHANNEL,
    SNAPSHOT_REDIS_KEY,
    SNAPSHOT_TTL_S,
    _MAX_PARALLEL,
    _SECTION_NAMES,
    build_terminal_snapshot,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_conn():
    """An asyncpg-like connection — methods aren't actually called because
    every `queries.*` function is patched at module level."""
    return MagicMock()


@pytest.fixture
def fake_pool(fake_conn):
    """Pool whose `.acquire()` returns an async context manager yielding
    a stub connection.  We track in-flight acquisitions to verify the
    semaphore bounds concurrency."""

    pool = MagicMock()
    pool.in_flight = 0
    pool.max_in_flight = 0

    @asynccontextmanager
    async def _acquire():
        pool.in_flight += 1
        pool.max_in_flight = max(pool.max_in_flight, pool.in_flight)
        try:
            yield fake_conn
        finally:
            pool.in_flight -= 1

    pool.acquire = _acquire
    return pool


@pytest.fixture
def fake_redis():
    """Redis stub — `set`, `publish`, `ping`, `get`, `hgetall` all
    answer as AsyncMocks so the builder's awaits resolve immediately."""
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.publish = AsyncMock(return_value=1)
    redis.ping = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={})
    return redis


@pytest.fixture
def patch_queries():
    """Patch every `queries.*` function the builder calls.  Each returns
    a section-appropriate stub payload, enough that
    `build_terminal_snapshot` (the composer) doesn't choke on missing
    keys.  Individual tests override patches via `monkeypatch` when
    they want a section to fail."""
    targets = {
        "overview": AsyncMock(return_value={"total_pnl": 12.5, "win_rate": 0.42}),
        "ml_summary": AsyncMock(return_value={"phase": "phase_1"}),
        "system_status": AsyncMock(return_value={"leaders": {"active": 7}}),
        "open_positions_with_prices": AsyncMock(return_value=[]),
        "positions": AsyncMock(
            return_value={"open": [], "closed": [], "stats": {}}
        ),
        "decisions": AsyncMock(return_value=[]),
        "decisions_stats": AsyncMock(return_value={"totals": {"total": 0}}),
        "risk": AsyncMock(return_value={"paper_capital": 10_000}),
        "activation_queue": AsyncMock(return_value=[]),
        "data_quality": AsyncMock(return_value={"issues": []}),
        "market_scanner_rows": AsyncMock(return_value=[]),
        "recent_observed_trades": AsyncMock(return_value=[]),
        "alpha_extras": AsyncMock(
            return_value={"timeline": [], "follow_ready": [], "totals": {}}
        ),
        "wallet_graph": AsyncMock(
            return_value={"nodes": [], "edges": [], "stats": {}}
        ),
        "decision_rejections_breakdown": AsyncMock(
            return_value={"total": 0, "breakdown": []}
        ),
        "equity_curve": AsyncMock(
            return_value={"series": [], "by_leader": [], "by_strategy": []}
        ),
    }
    patchers = [
        patch.object(snapshot_builder.queries, name, mock)
        for name, mock in targets.items()
    ]
    for p in patchers:
        p.start()
    yield targets
    for p in patchers:
        p.stop()


@pytest.fixture(autouse=True)
def _reset_lock():
    """Replace the module's lock with a fresh one between tests so a
    test that doesn't release it doesn't poison the rest."""
    snapshot_builder._BUILD_LOCK = asyncio.Lock()
    yield


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_build_returns_dict_with_all_expected_keys(
    fake_pool, fake_redis, patch_queries
):
    """The composed snapshot must expose every top-level key the
    dashboard reads.  This is the byte-compatibility contract with the
    legacy in-process builder."""
    snap = await build_terminal_snapshot(fake_pool, fake_redis)

    expected_top_level = {
        "clock",
        "meta",
        "bot",
        "stats",
        "analytics",
        "positions",
        "recent_trades",
        "decision_engine",
        "risk_config",
        "ingestion",
        "alpha_extras",
        "wallet_graph",
        "rejections",
        "equity_curve",
        "data_quality_full",
        "adaptive_thresholds",
        "logs",
    }
    missing = expected_top_level - set(snap.keys())
    assert not missing, f"Snapshot missing top-level keys: {missing}"


@pytest.mark.asyncio
async def test_partial_failure_uses_default(fake_pool, fake_redis, patch_queries):
    """If `queries.ml_summary` raises, the builder should swap in the
    default `{}` and the other 16 sections must still populate."""
    patch_queries["ml_summary"].side_effect = RuntimeError("ml exploded")

    snap = await build_terminal_snapshot(fake_pool, fake_redis)

    # ml_summary defaulted to {} → readiness still computed → snapshot
    # composed normally.  We can't directly assert "ml is empty" because
    # `build_terminal_snapshot` (composer) flattens ml into nested
    # fields, but we CAN assert that other sections still rendered.
    assert snap["wallet_graph"] == {"nodes": [], "edges": [], "stats": {}}
    assert snap["alpha_extras"]["timeline"] == []
    # And the snapshot was still written to Redis — partial failure
    # never blocks publication.
    assert fake_redis.set.call_count >= 2  # snapshot + built_at


@pytest.mark.asyncio
async def test_redis_set_called_with_correct_keys(
    fake_pool, fake_redis, patch_queries
):
    """Three Redis writes must land on each successful build:
    SET snapshot:live_summary, SET snapshot:live_summary:built_at,
    PUBLISH snapshot:live_summary:updated."""
    await build_terminal_snapshot(fake_pool, fake_redis)

    set_keys = [call.args[0] for call in fake_redis.set.call_args_list]
    assert SNAPSHOT_REDIS_KEY in set_keys
    assert SNAPSHOT_BUILT_AT_KEY in set_keys
    fake_redis.publish.assert_called_once()
    pub_channel = fake_redis.publish.call_args.args[0]
    assert pub_channel == SNAPSHOT_PUBSUB_CHANNEL


@pytest.mark.asyncio
async def test_redis_ttl_applied(fake_pool, fake_redis, patch_queries):
    """Both Redis SET calls must use ex=120 (SNAPSHOT_TTL_S) so the
    payload self-expires if the maintenance container dies."""
    await build_terminal_snapshot(fake_pool, fake_redis)

    for call in fake_redis.set.call_args_list:
        # asyncpg-style kwargs check
        assert call.kwargs.get("ex") == SNAPSHOT_TTL_S, (
            f"Expected ex={SNAPSHOT_TTL_S} on SET, got {call.kwargs}"
        )


@pytest.mark.asyncio
async def test_concurrent_builds_are_serialized(
    fake_pool, fake_redis, patch_queries
):
    """The module-level lock must prevent two concurrent builds from
    overlapping inside the same process. We observe overlap by making
    `queries.data_quality` take a measurable amount of time and counting
    how many builds are inside its body at once — with the lock, at most
    one build should be active at any moment."""
    in_flight = {"current": 0, "peak": 0}

    async def slow_data_quality(*args, **kwargs):
        in_flight["current"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["current"])
        # Yield enough turns that any non-serialised second build
        # would catch up and overlap.
        for _ in range(5):
            await asyncio.sleep(0)
        in_flight["current"] -= 1
        return {"issues": []}

    patch_queries["data_quality"].side_effect = slow_data_quality

    await asyncio.gather(
        build_terminal_snapshot(fake_pool, fake_redis),
        build_terminal_snapshot(fake_pool, fake_redis),
    )

    # With the lock in place, never more than one build active at once.
    assert in_flight["peak"] == 1, (
        f"Builds overlapped: peak {in_flight['peak']} concurrent — lock did not serialise"
    )
    # And both completed — two SET + two SET + two publishes.
    assert fake_redis.publish.call_count == 2


@pytest.mark.asyncio
async def test_pubsub_channel_published(fake_pool, fake_redis, patch_queries):
    """A successful build MUST publish on the dedicated channel so the
    WS bridge can fan the event out to dashboard clients."""
    await build_terminal_snapshot(fake_pool, fake_redis)

    fake_redis.publish.assert_called_once_with(SNAPSHOT_PUBSUB_CHANNEL, "updated")


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency(fake_pool, fake_redis, patch_queries):
    """No more than `_MAX_PARALLEL` queries may be in flight at the same
    time.  We slow each patched query slightly so the gather actually
    has overlap to observe; without the sleep, queries would resolve
    synchronously and never queue."""

    async def slow(*args, **kwargs):
        # Yield to the loop a few times so all 17 coroutines get a
        # chance to start; the semaphore is what stops them.
        for _ in range(3):
            await asyncio.sleep(0)
        return {}

    # Wrap every query to introduce realistic concurrent scheduling
    # while still using the existing pool fixture to count acquisitions.
    for name in patch_queries:
        patch_queries[name].side_effect = slow

    await build_terminal_snapshot(fake_pool, fake_redis)

    # The fake_pool fixture records peak in-flight acquisitions; with
    # the semaphore in place, that peak should be <= _MAX_PARALLEL.
    assert fake_pool.max_in_flight <= _MAX_PARALLEL, (
        f"Pool saw {fake_pool.max_in_flight} concurrent acquires, "
        f"expected ≤ {_MAX_PARALLEL}"
    )


@pytest.mark.asyncio
async def test_full_failure_does_not_crash(fake_pool, fake_redis, patch_queries):
    """Even if every single query raises, the builder must still
    compose a valid (mostly-empty) dict and attempt to write to Redis.
    Partial dashboards are better than a crashed maintenance loop."""
    for mock in patch_queries.values():
        mock.side_effect = RuntimeError("simulated failure")

    snap = await build_terminal_snapshot(fake_pool, fake_redis)

    # The composer still produced a dict with the expected top-level keys.
    assert isinstance(snap, dict)
    assert "stats" in snap
    assert "positions" in snap
    # And the (mostly-empty) payload was still serialised + written.
    assert fake_redis.set.call_count >= 2
    fake_redis.publish.assert_called_once()


@pytest.mark.asyncio
async def test_section_names_match_defaults(fake_pool, fake_redis, patch_queries):
    """Guard against drift between the section list and defaults — if a
    new section is added, both must be updated together."""
    # 17 sections by design (matches the legacy gather() in main.py).
    assert len(_SECTION_NAMES) == 17
    assert set(_SECTION_NAMES) == set(snapshot_builder._DEFAULTS.keys())


@pytest.mark.asyncio
async def test_redis_write_failure_does_not_crash(
    fake_pool, fake_redis, patch_queries
):
    """If Redis SET raises (e.g. Redis is down), the build must still
    return the dict so the caller can inspect it / retry."""
    fake_redis.set.side_effect = ConnectionError("redis is down")

    snap = await build_terminal_snapshot(fake_pool, fake_redis)

    assert isinstance(snap, dict)
    assert "stats" in snap


@pytest.mark.asyncio
async def test_serialised_payload_is_valid_json(
    fake_pool, fake_redis, patch_queries
):
    """The bytes written to Redis must be valid JSON — the API endpoint
    will return them straight to the client."""
    await build_terminal_snapshot(fake_pool, fake_redis)

    set_calls = {call.args[0]: call.args[1] for call in fake_redis.set.call_args_list}
    raw = set_calls.get(SNAPSHOT_REDIS_KEY)
    assert raw is not None, "Snapshot payload not written to Redis"
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "stats" in parsed
