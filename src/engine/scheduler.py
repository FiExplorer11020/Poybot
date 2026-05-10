"""
APScheduler wrapper (S3.10).

Replaces the hand-rolled NightlyBatchScheduler. AsyncIOScheduler drives
every periodic job in the engine container:

    * nightly batch     (cron, BATCH_HOUR_UTC)
    * Redis cleanup     (cron, REDIS_CLEANUP_HOUR_UTC)
    * killswitch sync   (interval, KILLSWITCH_SYNC_INTERVAL_S)
    * watchdog tick     (interval, WATCHDOG_HEARTBEAT_INTERVAL_S)
    * refresh markets   (interval, REFRESH_MARKETS_INTERVAL_S — only
                         in run_all.py where the observer lives)

Design rules:
    * Every job goes through `_safe_run`. A buggy job MUST NOT take the
      scheduler down; we log and move on.
    * Jobs are coroutines (async def). APScheduler 3.10.x runs them via
      AsyncIOScheduler so they share the engine's event loop.
    * Setting an interval to 0 in config disables the corresponding job
      — useful for tests, paranoid prod tuning, or when a feature isn't
      yet hooked up in a given entry point.

Usage:

    sched = Scheduler()
    sched.add_cron("nightly_batch", run_batch, hour=3)
    sched.add_interval("watchdog", watchdog.tick, seconds=30)
    await sched.start()
    ...
    await sched.stop()
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Optional

from loguru import logger


JobFn = Callable[[], Awaitable[None]]


class Scheduler:
    """Thin wrapper over APScheduler's AsyncIOScheduler with safe job
    execution + structured logging."""

    def __init__(self) -> None:
        # Lazy import so test envs without apscheduler can still import
        # this module to use the scheduler-less helpers.
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "APScheduler is required for the engine scheduler. "
                "pip install apscheduler==3.10.4"
            ) from e
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._registered: dict[str, JobFn] = {}
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._running:
            return
        self._scheduler.start()
        self._running = True
        names = sorted(self._registered.keys())
        logger.info(
            f"Scheduler started ({len(names)} jobs): {', '.join(names) or '(none)'}"
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Scheduler: shutdown error")
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------ #
    # Registration                                                        #
    # ------------------------------------------------------------------ #

    def add_cron(
        self,
        name: str,
        fn: JobFn,
        *,
        hour: int,
        minute: int = 0,
        misfire_grace_time: int = 600,
    ) -> None:
        """Register a daily cron job at HH:MM UTC. `misfire_grace_time`
        gives APScheduler a window to catch up if the loop was busy when
        the trigger fired (10 minutes by default — long enough to absorb
        a slow nightly batch overrun)."""
        if name in self._registered:
            logger.warning(f"Scheduler: job {name!r} already registered, replacing")
        self._registered[name] = fn
        self._scheduler.add_job(
            self._safe_run,
            trigger="cron",
            args=[name, fn],
            hour=hour,
            minute=minute,
            id=name,
            replace_existing=True,
            misfire_grace_time=misfire_grace_time,
            coalesce=True,
            max_instances=1,
        )
        logger.info(f"Scheduler: cron job {name!r} → daily at {hour:02d}:{minute:02d} UTC")

    def add_interval(
        self,
        name: str,
        fn: JobFn,
        *,
        seconds: int,
        misfire_grace_time: int = 30,
    ) -> None:
        """Register an interval job. seconds<=0 disables it (logs and
        returns)."""
        if seconds <= 0:
            logger.info(f"Scheduler: job {name!r} disabled (interval=0)")
            return
        if name in self._registered:
            logger.warning(f"Scheduler: job {name!r} already registered, replacing")
        self._registered[name] = fn
        self._scheduler.add_job(
            self._safe_run,
            trigger="interval",
            args=[name, fn],
            seconds=seconds,
            id=name,
            replace_existing=True,
            misfire_grace_time=misfire_grace_time,
            coalesce=True,
            max_instances=1,
        )
        logger.info(f"Scheduler: interval job {name!r} → every {seconds}s")

    def remove(self, name: str) -> None:
        try:
            self._scheduler.remove_job(name)
            self._registered.pop(name, None)
            logger.info(f"Scheduler: removed job {name!r}")
        except Exception:
            logger.warning(f"Scheduler: failed to remove job {name!r}")

    @property
    def job_names(self) -> list[str]:
        return sorted(self._registered.keys())

    # ------------------------------------------------------------------ #
    # Safe execution                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _safe_run(name: str, fn: JobFn) -> None:
        """Run a job, logging start/end/duration. Exceptions are
        logged but never re-raised — APScheduler would otherwise mark the
        job as misfired and we want each tick to be independent."""
        loop = asyncio.get_event_loop()
        started = loop.time()
        logger.debug(f"Scheduler: job {name!r} starting")
        try:
            result = fn()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(f"Scheduler: job {name!r} crashed (swallowed)")
            return
        elapsed = loop.time() - started
        logger.debug(f"Scheduler: job {name!r} done in {elapsed:.2f}s")


# --------------------------------------------------------------------------- #
# Backward-compat shim                                                         #
# --------------------------------------------------------------------------- #
#
# scripts/run_all.py used to instantiate NightlyBatchScheduler directly.
# Keep a thin wrapper to avoid touching every entry point at once — its
# `run()` method spins up an APScheduler under the hood and registers the
# nightly batch job. Scheduled for removal once main.py and run_all.py
# are both migrated to the new API.
# --------------------------------------------------------------------------- #


class NightlyBatchScheduler:
    """Backward-compatible wrapper. Prefer using `Scheduler` directly."""

    def __init__(self, hour_utc: Optional[int] = None, run_on_start: bool = False):
        from src.config import settings as _settings

        self._hour = int(hour_utc if hour_utc is not None else _settings.BATCH_HOUR_UTC)
        self._run_on_start = run_on_start
        self._scheduler = Scheduler()
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        self._stop_event.set()
        await self._scheduler.stop()

    async def run(self) -> None:
        from scripts.batch_runner import run_batch  # type: ignore

        async def _job() -> None:
            await run_batch(manage_infrastructure=False)

        if self._run_on_start:
            await Scheduler._safe_run("nightly_batch", _job)
        self._scheduler.add_cron("nightly_batch", _job, hour=self._hour)
        await self._scheduler.start()
        # Block until stopped; the actual work runs inside APScheduler.
        try:
            await self._stop_event.wait()
        finally:
            await self._scheduler.stop()
