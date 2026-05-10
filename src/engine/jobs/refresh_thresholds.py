"""
Adaptive threshold refresh job (S5.x).

Every N seconds (typically 5 min), reads the current system maturity
from the DB (profiles_with_data, total resolved positions, confirmed
follower edges) and updates the EFFECTIVE_THRESHOLDS dict that the
confidence_engine, error_model, behavior_profiler, and graph_engine
read from at decision time.

This is what makes the cold-start floors actually relax over time as
the bot accumulates data. Without this job firing, the static cold
values are used forever.

Why a periodic job rather than an on-the-fly compute per decision:
the maturity is a slow-moving aggregate (changes with hours of new
data, not milliseconds). Caching it for 5 min trades a few minutes of
staleness for 100% reduction in DB load on the hot path.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from src.config import refresh_effective_thresholds
from src.database.connection import get_db


def make_refresh_thresholds_job() -> Callable[[], Awaitable[None]]:
    """Returns a coroutine factory that refreshes the EFFECTIVE_THRESHOLDS
    cache. Designed to be registered with the engine APScheduler at
    interval ~ 300s.
    """

    async def _job() -> None:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM leader_profiles
                         WHERE positions_resolved > 0) AS profiles_with_data,
                        (SELECT COUNT(*) FROM positions_reconstructed
                         WHERE close_time IS NOT NULL) AS resolved_total,
                        (SELECT COUNT(*) FROM follower_edges
                         WHERE co_occurrences >= 5
                           AND same_direction_rate >= 0.7) AS confirmed_edges
                    """
                )
            new_thresholds = refresh_effective_thresholds(
                profiles_with_data=int(row["profiles_with_data"] or 0),
                resolved_total=int(row["resolved_total"] or 0),
                confirmed_edges=int(row["confirmed_edges"] or 0),
            )
            logger.info(
                "Adaptive thresholds refreshed | maturity={:.3f} "
                "FOLLOW_MIN_TRADES={:.1f} MIN_CO={:.1f} P2={:.0f} P3={:.0f}",
                new_thresholds["_maturity"],
                new_thresholds["FOLLOW_MIN_TRADES"],
                new_thresholds["MIN_CO_OCCURRENCES"],
                new_thresholds["MIN_RESOLVED_FOR_ERROR_P2"],
                new_thresholds["MIN_RESOLVED_FOR_ERROR_P3"],
            )
        except Exception as exc:
            logger.warning(f"refresh_thresholds job failed: {exc}")

    return _job
