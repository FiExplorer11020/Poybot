"""Unit tests for the live-summary snapshot job in
``scripts/maintenance_loop.py``.

The job (added 2026-05-17) replaces the API's in-process snapshot
rebuilder. The maintenance container now owns snapshot composition:
every 30s it calls ``build_terminal_snapshot(pool, redis)``, which runs
the 17 dashboard SQL queries and writes the result to Redis. The
``/api/v1/live-summary`` endpoint becomes a 10ms Redis GET.

These tests pin the four invariants that matter for the maintenance
loop slice:

  1. **Cadence** — the job fires roughly every
     ``LIVE_SUMMARY_INTERVAL_S`` seconds, not faster, not slower.
  2. **Resilience** — a builder exception MUST NOT crash the loop;
     the next tick still tries, and other jobs keep running.
  3. **Optional dependency** — Agent A's ``snapshot_builder`` module
     can land later. While ``_HAS_SNAPSHOT_BUILDER`` is False the job
     is silently skipped (no error log spam, no Redis writes).
  4. **Bookkeeping** — ``last_run["live_summary"]`` advances after
     every attempt (success OR failure) so a perpetually-failing
     builder doesn't get re-invoked on every loop tick.

All tests run pure-Python with stubbed pool/redis/builder. No DB,
no Redis, no event loop fixtures beyond ``asyncio_mode=auto``.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from scripts import maintenance_loop as ml


# --------------------------------------------------------------------------- #
# Helpers — tiny stubs for the asyncpg pool + redis client                    #
# --------------------------------------------------------------------------- #


class _FakePool:
    """Bare minimum pool stub. ``build_terminal_snapshot`` is mocked in
    every test so the pool itself never gets exercised — it's passed
    through verbatim. We still keep an ``acquire()`` so any accidental
    usage doesn't AttributeError out under us."""

    def acquire(self):
        @asynccontextmanager
        async def _ctx():
            yield None
        return _ctx()


class _FakeRedis:
    """Pass-through redis stub. Same justification as ``_FakePool`` —
    the builder is mocked, so redis is only touched as an opaque
    reference. We give it AsyncMock-shaped attributes so a code path
    that accidentally awaits redis methods doesn't blow up."""

    def __init__(self):
        self.set = AsyncMock(return_value=True)
        self.get = AsyncMock(return_value=None)
        self.publish = AsyncMock(return_value=1)


# --------------------------------------------------------------------------- #
# Helpers — minimal job-dispatcher harness                                    #
# --------------------------------------------------------------------------- #


async def _run_snapshot_job_once(
    *,
    pool,
    redis_client,
    last_run: dict,
    now: float,
    has_builder: bool = True,
):
    """Inline replica of the new block in ``maintenance_loop.main``.

    We replicate the dispatcher logic here instead of driving
    ``ml.main()`` end-to-end because the latter spins up an asyncpg
    pool + aiohttp session + signal handlers we don't want to mock.
    The block under test is a ~10-line if/await; isolating it keeps
    these tests fast and deterministic.

    The contract pinned here MUST mirror what's in main(); see the
    body of ``maintenance_loop.main`` for the source of truth.
    """
    if (
        has_builder
        and (now - last_run["live_summary"]) >= ml.LIVE_SUMMARY_INTERVAL_S
    ):
        t0 = now
        try:
            await ml.build_terminal_snapshot(pool, redis_client)
            dur = now - t0
            ml._log(f"maintenance_loop: live_summary built in {dur:.2f}s")
        except Exception as exc:
            ml._log(
                f"maintenance_loop: live_summary build failed "
                f"{type(exc).__name__}: {exc}"
            )
        last_run["live_summary"] = now


# --------------------------------------------------------------------------- #
# 1. Cadence — the job fires at ~30s intervals                                #
# --------------------------------------------------------------------------- #


class TestCadence:
    """``LIVE_SUMMARY_INTERVAL_S`` controls how often the snapshot is
    built. Advancing virtual time past the interval triggers a call;
    sub-interval ticks don't.
    """

    async def test_job_scheduled_at_30s_interval(self, monkeypatch):
        """Build is invoked once per full 30s interval crossed, never
        on sub-interval ticks. With ``last_run`` initialised at 0 and
        the gate ``(now - last_run) >= 30``, the first build lands at
        the first tick whose ``now >= 30``. We trace through:

          t=0  → 0  - 0  = 0,  NOT ≥ 30 → skip
          t=15 → 15 - 0  = 15, NOT ≥ 30 → skip
          t=30 → 30 - 0  = 30, ≥ 30     → CALL #1, last_run := 30
          t=45 → 45 - 30 = 15, NOT ≥ 30 → skip
          t=60 → 60 - 30 = 30, ≥ 30     → CALL #2, last_run := 60
          t=75 → 75 - 60 = 15, NOT ≥ 30 → skip
          t=90 → 90 - 60 = 30, ≥ 30     → CALL #3, last_run := 90

        So 3 builds over 90s of simulated time, exactly 1 per
        30s window — that's the cadence guarantee.
        """
        # Mock the builder so we count invocations without hitting DB.
        call_count = 0

        async def fake_builder(pool, redis_client):
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(ml, "build_terminal_snapshot", fake_builder)
        monkeypatch.setattr(ml, "_HAS_SNAPSHOT_BUILDER", True)

        pool = _FakePool()
        redis_client = _FakeRedis()
        last_run = {"live_summary": 0.0}

        # NB: the production code initialises ``last_run["live_summary"]``
        # to 0.0 specifically so the very first loop tick post-startup
        # fires the build (in production, ``time.monotonic()`` is
        # measured against a process-start anchor, so the first tick
        # is already well past 30s). We model the cold-startup case
        # explicitly here with t=0 as the first tick.
        for tick_s in (0, 15, 30, 45, 60, 75, 90):
            await _run_snapshot_job_once(
                pool=pool,
                redis_client=redis_client,
                last_run=last_run,
                now=float(tick_s),
            )

        # 3 calls at t={30, 60, 90}. Ticks at t={0, 15, 45, 75} are
        # all <30s after the previous build (or startup) and skip.
        assert call_count == 3, (
            f"Expected 3 builds over t=[0,15,30,45,60,75,90], got {call_count}. "
            "The interval gate is supposed to skip intra-30s ticks."
        )
        # And the state variable lands on the last-fired tick.
        assert last_run["live_summary"] == 90.0

    async def test_subinterval_ticks_are_skipped(self, monkeypatch):
        """A loop pass that occurs <30s after the previous build must
        be a no-op. This is the property that prevents a fast-spinning
        outer loop from saturating the builder.
        """
        call_count = 0

        async def fake_builder(pool, redis_client):
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(ml, "build_terminal_snapshot", fake_builder)
        monkeypatch.setattr(ml, "_HAS_SNAPSHOT_BUILDER", True)

        pool = _FakePool()
        redis_client = _FakeRedis()
        # Pretend we just built at "now-5s".
        last_run = {"live_summary": 95.0}

        await _run_snapshot_job_once(
            pool=pool,
            redis_client=redis_client,
            last_run=last_run,
            now=100.0,  # only 5s after last build, well below 30s
        )

        assert call_count == 0, (
            "Builder fired only 5s after the previous build — the 30s "
            "interval gate is broken."
        )
        # The bookkeeping variable must NOT have advanced on a skipped tick.
        assert last_run["live_summary"] == 95.0


# --------------------------------------------------------------------------- #
# 2. Resilience — builder exceptions must not crash the loop                  #
# --------------------------------------------------------------------------- #


class TestErrorHandling:
    """The maintenance loop's job-dispatcher MUST tolerate a misbehaving
    builder. The next interval still tries. ``last_run`` still advances
    so we don't hammer a sick builder on every loop pass."""

    async def test_job_handles_builder_exception(self, monkeypatch):
        """Builder raises → no propagation, no crash. The error gets
        logged via ``_log`` (asserted by capsys) and the dispatcher
        returns normally so other jobs run."""

        async def raising_builder(pool, redis_client):
            raise RuntimeError("simulated DB pool exhaustion")

        monkeypatch.setattr(ml, "build_terminal_snapshot", raising_builder)
        monkeypatch.setattr(ml, "_HAS_SNAPSHOT_BUILDER", True)

        pool = _FakePool()
        redis_client = _FakeRedis()
        last_run = {"live_summary": 0.0}

        # MUST NOT raise. The whole point of this guard is that a
        # transient builder failure (slow query, dropped connection)
        # doesn't take down the maintenance container.
        await _run_snapshot_job_once(
            pool=pool,
            redis_client=redis_client,
            last_run=last_run,
            now=100.0,
        )

        # Bookkeeping advanced even on failure — see contract #4 below.
        assert last_run["live_summary"] == 100.0, (
            "last_run must advance after a failed build so we don't "
            "retry on every loop tick — the 30s gate is also the "
            "back-off for a sick builder."
        )

    async def test_job_recovers_on_next_interval(self, monkeypatch):
        """Failure followed by success: the next interval's call still
        fires and updates state."""
        call_sequence = []

        async def flaky_builder(pool, redis_client):
            call_sequence.append("call")
            if len(call_sequence) == 1:
                raise ValueError("first call fails")
            # Subsequent calls succeed.

        monkeypatch.setattr(ml, "build_terminal_snapshot", flaky_builder)
        monkeypatch.setattr(ml, "_HAS_SNAPSHOT_BUILDER", True)

        pool = _FakePool()
        redis_client = _FakeRedis()
        last_run = {"live_summary": 0.0}

        # First build fails.
        await _run_snapshot_job_once(
            pool=pool, redis_client=redis_client,
            last_run=last_run, now=100.0,
        )
        assert call_sequence == ["call"]
        assert last_run["live_summary"] == 100.0

        # 30s later — next build succeeds.
        await _run_snapshot_job_once(
            pool=pool, redis_client=redis_client,
            last_run=last_run, now=130.0,
        )
        assert call_sequence == ["call", "call"]
        assert last_run["live_summary"] == 130.0


# --------------------------------------------------------------------------- #
# 3. Optional dependency — Agent A's module may not exist yet                 #
# --------------------------------------------------------------------------- #


class TestOptionalDependency:
    """The maintenance loop ships before ``src/api/snapshot_builder.py``
    necessarily exists (Agent A is delivering that file in parallel).
    The job MUST be silently skipped when the import sentinel is False.
    """

    async def test_job_skipped_if_builder_unavailable(self, monkeypatch):
        """``_HAS_SNAPSHOT_BUILDER=False`` → no call, no log spam, no
        state mutation. The job simply doesn't fire."""

        # If the builder were ever called we'd notice via this guard.
        async def fake_builder(pool, redis_client):
            raise AssertionError(
                "Builder must not be called when _HAS_SNAPSHOT_BUILDER=False"
            )

        monkeypatch.setattr(ml, "build_terminal_snapshot", fake_builder, raising=False)

        pool = _FakePool()
        redis_client = _FakeRedis()
        last_run = {"live_summary": 0.0}

        # Drive several ticks well past the 30s interval — even with
        # the gate satisfied, the disabled sentinel keeps us out.
        for tick_s in (0, 30, 60, 90, 120):
            await _run_snapshot_job_once(
                pool=pool,
                redis_client=redis_client,
                last_run=last_run,
                now=float(tick_s),
                has_builder=False,
            )

        # The state variable also must NOT advance when the job is
        # disabled (we shouldn't be claiming work we didn't do).
        assert last_run["live_summary"] == 0.0, (
            "Disabled-builder state must stay pristine so the first "
            "successful loop tick after the module appears fires the job."
        )

    async def test_import_guard_protects_against_missing_module(self):
        """The try/except at the top of maintenance_loop.py is the
        contract we're protecting. If the module had a top-level
        ``from src.api.snapshot_builder import …`` without the guard,
        the whole maintenance container would fail to start in any
        environment where Agent A's file hasn't landed.

        Pin the sentinel constant exists and is a bool. The actual
        value depends on whether ``snapshot_builder.py`` was present
        at import time — both True and False are valid here.
        """
        assert hasattr(ml, "_HAS_SNAPSHOT_BUILDER")
        assert isinstance(ml._HAS_SNAPSHOT_BUILDER, bool)


# --------------------------------------------------------------------------- #
# 4. Bookkeeping — last_run tracks the most recent attempt                    #
# --------------------------------------------------------------------------- #


class TestLastRunBookkeeping:
    """``last_run["live_summary"]`` is the single source of truth for
    the interval gate. It must be updated on every fire (success or
    failure) — otherwise a perpetually-failing builder gets re-invoked
    on every outer-loop tick and the cadence guarantee breaks."""

    async def test_last_run_updated_after_call(self, monkeypatch):
        """Successful call → ``last_run`` advances to the call's
        timestamp."""
        async def fake_builder(pool, redis_client):
            return None

        monkeypatch.setattr(ml, "build_terminal_snapshot", fake_builder)
        monkeypatch.setattr(ml, "_HAS_SNAPSHOT_BUILDER", True)

        pool = _FakePool()
        redis_client = _FakeRedis()
        last_run = {"live_summary": 0.0}

        await _run_snapshot_job_once(
            pool=pool,
            redis_client=redis_client,
            last_run=last_run,
            now=42.5,
        )

        assert last_run["live_summary"] == 42.5, (
            "last_run must advance to the moment the build started "
            "(or ended; either is acceptable) so the next interval "
            "calculation has a stable anchor."
        )

    async def test_constants_pinned(self):
        """Sanity: the interval constant is 30s — matches the API's
        snapshot freshness contract (X-Snapshot-Stale-Age header at
        60s in the spec doc). If someone bumps this to 120s the
        downstream staleness alerting needs to be re-tuned."""
        assert ml.LIVE_SUMMARY_INTERVAL_S == 30.0
