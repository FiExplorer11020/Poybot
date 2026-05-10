"""
Nightly batch job (S3.10).

Wraps `scripts.batch_runner.run_batch` so APScheduler can schedule it as
a cron job. The shared asyncpg pool / Redis client are owned by the
engine container, so we pass `manage_infrastructure=False` to keep the
batch runner from trying to recreate them.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger


def make_nightly_batch_job() -> Callable[[], Awaitable[None]]:
    """Return a coroutine factory the Scheduler can call."""

    async def _job() -> None:
        # Lazy import — the batch_runner pulls in lots of optional deps
        # (lightgbm, jax, ...) and we don't want them imported just
        # because the engine container is starting up.
        try:
            from scripts.batch_runner import run_batch  # type: ignore
        except Exception:
            logger.exception("nightly_batch: failed to import scripts.batch_runner")
            return
        await run_batch(manage_infrastructure=False)

    return _job
