"""
Tests for the APScheduler-backed Scheduler wrapper (S3.10).

We don't actually wait for the cron to fire — that would be flaky and
slow. Instead we assert (a) jobs register correctly, (b) `_safe_run`
swallows exceptions, (c) interval=0 disables a job.
"""

from __future__ import annotations

import asyncio

import pytest

from src.engine.scheduler import Scheduler


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #


async def test_scheduler_start_stop_idempotent():
    sched = Scheduler()

    async def noop():
        pass

    sched.add_interval("noop", noop, seconds=60)
    await sched.start()
    # second start is a no-op
    await sched.start()
    assert "noop" in sched.job_names
    await sched.stop()
    # second stop is a no-op
    await sched.stop()


async def test_add_interval_zero_disables():
    sched = Scheduler()

    async def noop():
        pass

    sched.add_interval("disabled", noop, seconds=0)
    assert "disabled" not in sched.job_names


async def test_add_cron_registers_at_hour():
    sched = Scheduler()

    async def noop():
        pass

    sched.add_cron("nightly", noop, hour=3)
    assert "nightly" in sched.job_names


async def test_add_replaces_existing():
    sched = Scheduler()
    calls = []

    async def first():
        calls.append("first")

    async def second():
        calls.append("second")

    sched.add_interval("dup", first, seconds=60)
    sched.add_interval("dup", second, seconds=60)
    # Only one job named 'dup'; the second replaced the first.
    assert sched.job_names.count("dup") == 1


async def test_remove_job():
    sched = Scheduler()

    async def noop():
        pass

    sched.add_interval("temp", noop, seconds=60)
    assert "temp" in sched.job_names
    sched.remove("temp")
    assert "temp" not in sched.job_names


# --------------------------------------------------------------------------- #
# _safe_run                                                                    #
# --------------------------------------------------------------------------- #


async def test_safe_run_swallows_exceptions():
    """A buggy job must not propagate — we want APScheduler to fire the
    next tick."""
    async def kaboom():
        raise RuntimeError("oh no")

    # Must not raise.
    await Scheduler._safe_run("kaboom", kaboom)


async def test_safe_run_invokes_async_function():
    seen = []

    async def fn():
        seen.append("ran")

    await Scheduler._safe_run("ok", fn)
    assert seen == ["ran"]


async def test_safe_run_handles_sync_function_returning_awaitable():
    """add_cron passes the registered fn through `_safe_run`. If a job
    factory returned a coroutine (sync caller), we still want it
    awaited."""
    seen = []

    def sync_fn():
        async def inner():
            seen.append("ran")
        return inner()

    await Scheduler._safe_run("sync_returns_coro", sync_fn)
    assert seen == ["ran"]


# --------------------------------------------------------------------------- #
# Interval job actually fires                                                  #
# --------------------------------------------------------------------------- #


async def test_interval_job_fires():
    """End-to-end: register an interval=1 job, start the scheduler, wait
    a moment, assert it ran. This is the only test that exercises
    APScheduler's actual triggering."""
    counter = {"n": 0}

    async def tick():
        counter["n"] += 1

    sched = Scheduler()
    sched.add_interval("tick", tick, seconds=1)
    await sched.start()
    try:
        # Wait up to 3 seconds for at least 1 tick.
        for _ in range(60):
            if counter["n"] >= 1:
                break
            await asyncio.sleep(0.05)
        assert counter["n"] >= 1, "interval job didn't fire within 3s"
    finally:
        await sched.stop()
