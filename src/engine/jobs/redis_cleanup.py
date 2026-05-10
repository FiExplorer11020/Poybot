"""
Redis cleanup job (S3.10).

Daily second-pass over Redis after the nightly batch. Targets:

    * Stale `heartbeat:*` keys whose TTL is gone (TTL is 4× the
      component's interval, but if a component was unregistered we want
      the key purged).
    * Stale `subscriptions:active_markets` membership for tokens whose
      market has resolved — checked against the `markets` table.
    * Position-tracker debris: keys for markets the observer hasn't
      touched in RETENTION_TRADES_DAYS.

This is a defensive job — most caches are bounded by S1.2 — but it's
the right place to put any cleanup that doesn't fit a hot-path TTL.

Set REDIS_CLEANUP_HOUR_UTC=0 to disable.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from src.engine.watchdog import REDIS_HEARTBEAT_PREFIX


def make_redis_cleanup_job(redis_client) -> Callable[[], Awaitable[None]]:
    """Return a coroutine factory that runs cleanup. Idempotent."""

    async def _job() -> None:
        deleted_total = 0
        deleted_total += await _purge_orphan_heartbeats(redis_client)
        # Future passes can be appended here (position-tracker scan,
        # resolved-market token purge, ...).
        logger.info(
            f"redis_cleanup: completed (deleted {deleted_total} orphan keys)"
        )

    return _job


async def _purge_orphan_heartbeats(redis_client) -> int:
    """Heartbeat keys are TTL'd, but if Redis was restarted with `appendonly
    no` or someone wrote a bad TTL, we may end up with persistent zombies.
    Scan and delete any heartbeat key whose TTL is -1 (no TTL set)."""
    deleted = 0
    try:
        cursor = 0
        match = f"{REDIS_HEARTBEAT_PREFIX}*"
        while True:
            cursor, keys = await redis_client.scan(
                cursor=cursor, match=match, count=200
            )
            for key in keys:
                try:
                    ttl = await redis_client.ttl(key)
                except Exception:
                    continue
                # ttl == -1: no expiry set; -2: key gone already.
                if ttl == -1:
                    try:
                        await redis_client.delete(key)
                        deleted += 1
                    except Exception:
                        pass
            if cursor == 0:
                break
    except Exception:
        logger.exception("redis_cleanup: heartbeat scan failed")
    if deleted:
        logger.info(f"redis_cleanup: purged {deleted} orphan heartbeat keys")
    return deleted
