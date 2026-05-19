"""Shared uptime helper (B3v2 fix, 2026-05-19).

The bot uptime displayed on the dashboard should reflect the ENGINE's
boot time, not the API container's. The engine writes its boot timestamp
to Redis (`bot:engine:started_at`, set by `src/engine/main.py`) precisely
so the value survives API + maintenance container restarts.

Both `src/api/main.py` (live live-summary handler) and
`src/api/snapshot_builder.py` (maintenance background builder) need to
expose this same uptime in the snapshot payload. Without a shared helper
the maintenance builder was zeroing `runtime["uptime_seconds"]` while
the API handler was correctly reading Redis, producing inconsistent
"Uptime: 0s" flashes on the dashboard whenever the cached snapshot from
maintenance was served.

This module is the single source of truth. Both callers route through
`get_bot_uptime_seconds()` and the dashboard gets the same answer no
matter who built the snapshot.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger


async def get_bot_uptime_seconds(
    redis_client: Any | None,
    fallback_started_at: datetime | None = None,
) -> int:
    """Return the bot's effective uptime in seconds.

    Source of truth precedence:
      1. Redis key `bot:engine:started_at` (epoch seconds, written by
         the engine container on boot — survives API/maintenance
         restarts).
      2. `fallback_started_at` (typically the API container's module
         load time — useful before the engine has booted).
      3. 0 (safe default — dashboard renders "0s" rather than crashing).

    Parameters
    ----------
    redis_client : redis.asyncio.Redis | None
        Async Redis client. May be ``None`` (tests / cold start) in
        which case we skip straight to fallback.
    fallback_started_at : datetime | None
        Optional timezone-aware datetime used when Redis is unavailable
        or the engine hasn't written its boot timestamp yet.

    Returns
    -------
    int
        Non-negative integer seconds since the chosen reference point.
        Never raises — every failure path returns 0.
    """
    # Try Redis first — the engine timestamp is the canonical source.
    try:
        if redis_client is not None:
            engine_ts_raw = await redis_client.get("bot:engine:started_at")
            if engine_ts_raw is not None:
                try:
                    engine_ts = float(engine_ts_raw)
                except (TypeError, ValueError):
                    logger.warning(
                        f"uptime: bot:engine:started_at is not a float: {engine_ts_raw!r}"
                    )
                else:
                    return max(0, int(time.time() - engine_ts))
    except Exception as exc:  # noqa: BLE001 — uptime must never crash
        logger.warning(f"uptime: failed to read bot:engine:started_at: {exc}")

    # Fallback: caller-provided wallclock anchor.
    if fallback_started_at is not None:
        try:
            return max(
                0,
                int((datetime.now(timezone.utc) - fallback_started_at).total_seconds()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"uptime: fallback datetime arithmetic failed: {exc}")

    return 0
