"""
Nightly batch scheduler.

Wakes up once per day at BATCH_HOUR_UTC (default 03:00 UTC) and runs the
cold-path jobs from scripts/batch_runner.py — Hawkes refit, error-model
phase progression, Redis cache warmup, retention cleanup.  Without this loop
those jobs only run if someone invokes batch_runner.py manually, so the
follower graph and error models stagnate.

Deliberately dependency-free (no APScheduler).  Runs inside the same event
loop as the rest of run_all.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.config import settings


def _next_run(now: datetime, hour_utc: int) -> datetime:
    """Return the next datetime at `hour_utc:00:00` strictly after `now`."""
    candidate = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


class NightlyBatchScheduler:
    """Single-shot-per-day loop that invokes `scripts.batch_runner.run_batch`.

    Usage:
        scheduler = NightlyBatchScheduler()
        task = asyncio.create_task(scheduler.run())
        ...
        await scheduler.stop()
    """

    def __init__(self, hour_utc: int | None = None, run_on_start: bool = False):
        self._hour = int(hour_utc if hour_utc is not None else settings.BATCH_HOUR_UTC)
        self._run_on_start = run_on_start
        self._stop_event = asyncio.Event()
        self._last_run_at: datetime | None = None

    @property
    def last_run_at(self) -> datetime | None:
        return self._last_run_at

    async def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        logger.info(
            f"NightlyBatchScheduler: active, firing daily at {self._hour:02d}:00 UTC"
        )
        if self._run_on_start:
            await self._fire_once()

        while not self._stop_event.is_set():
            now = datetime.now(tz=timezone.utc)
            target = _next_run(now, self._hour)
            wait_s = max(1.0, (target - now).total_seconds())
            logger.debug(
                f"NightlyBatchScheduler: next run at {target.isoformat()} "
                f"(sleep {wait_s:.0f}s)"
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_s)
                # Stopped.
                return
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            await self._fire_once()

    async def _fire_once(self) -> None:
        """Invoke the batch.  Exceptions are logged, never raised — a failing
        batch should not kill the whole bot."""
        # Import lazily so importing the scheduler does not pull asyncpg pools.
        from scripts.batch_runner import run_batch  # type: ignore

        started = datetime.now(tz=timezone.utc)
        logger.info(f"NightlyBatchScheduler: run_batch() starting at {started.isoformat()}")
        try:
            # Do NOT let run_batch touch the shared asyncpg pool owned by
            # run_all.py — just reuse it.
            await run_batch(manage_infrastructure=False)
            self._last_run_at = datetime.now(tz=timezone.utc)
            logger.info(
                f"NightlyBatchScheduler: run_batch() completed at "
                f"{self._last_run_at.isoformat()}"
            )
        except Exception as exc:
            logger.exception(f"NightlyBatchScheduler: run_batch() failed: {exc}")
