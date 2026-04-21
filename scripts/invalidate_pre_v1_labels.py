"""
Invalidate all pre-V1 economic labels while preserving raw events.

This script marks legacy PnL/outcome/learning data as unusable for V1 reports.
It does not delete raw trades or market data.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool

REASON = "pre_v1_economic_reset"
NEW_VERSION = "v1.0.0"


async def invalidate() -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    try:
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO v1_label_invalidations
                        (
                            target_table,
                            target_id,
                            reason,
                            previous_economic_model_version,
                            new_economic_model_version
                        )
                    SELECT 'paper_trades', id::text, $1, economic_model_version, $2
                    FROM paper_trades
                    WHERE invalidated_at IS NULL
                      AND (economic_model_version IS NULL OR economic_model_version <> $2)
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET invalidated_at = NOW(),
                        invalidated_reason = $1
                    WHERE invalidated_at IS NULL
                      AND (economic_model_version IS NULL OR economic_model_version <> $2)
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    INSERT INTO v1_label_invalidations
                        (
                            target_table,
                            target_id,
                            reason,
                            previous_economic_model_version,
                            new_economic_model_version
                        )
                    SELECT 'decision_log', id::text, $1, economic_model_version, $2
                    FROM decision_log
                    WHERE invalidated_at IS NULL
                      AND outcome IS NOT NULL
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    UPDATE decision_log
                    SET invalidated_at = NOW(),
                        invalidated_reason = $1,
                        outcome = NULL
                    WHERE invalidated_at IS NULL
                      AND outcome IS NOT NULL
                    """,
                    REASON,
                )
                await conn.execute(
                    """
                    UPDATE leader_profiles
                    SET learning_invalidated_at = NOW(),
                        learning_invalidated_reason = $1,
                        economic_model_version = $2,
                        profile_json = profile_json - 'decision_learning',
                        error_model_blob = NULL
                    WHERE learning_invalidated_at IS NULL
                       OR profile_json ? 'decision_learning'
                       OR error_model_blob IS NOT NULL
                    """,
                    REASON,
                    NEW_VERSION,
                )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(invalidate())
