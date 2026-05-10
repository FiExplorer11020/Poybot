"""
Killswitch sync job (S3.10).

The killswitch service caches the DB state in Redis with a 2s TTL — fast
enough for the trade hot path, but it means a manual `UPDATE
system_control SET execution_enabled=false` could go un-honoured for up
to 2 seconds (in practice irrelevant). What this job *does* protect
against is a stale Redis cache lingering after Redis was flushed or the
key TTL is botched: every 5 minutes (config) we force-read from
Postgres and overwrite the Redis cache.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from src.control.killswitch import KillswitchService


def make_killswitch_sync_job(
    killswitch: KillswitchService,
) -> Callable[[], Awaitable[None]]:
    """Return a coroutine factory that force-refreshes the killswitch
    cache from the DB."""

    async def _job() -> None:
        try:
            # `force_refresh=True` bypasses the Redis cache and writes a
            # fresh value back. KillswitchService doesn't expose a public
            # method named that, so we lean on the "invalidate then read"
            # pattern.
            await killswitch._invalidate_cache()  # type: ignore[attr-defined]
            state = await killswitch.get_state()
            logger.debug(
                "killswitch_sync: refreshed "
                f"exec={state.execution_enabled} real={state.real_execution_enabled}"
            )
        except Exception:
            logger.exception("killswitch_sync: refresh failed")

    return _job
