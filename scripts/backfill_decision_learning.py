"""
Replay historical closed paper trades into leader_profiles.decision_learning.

Usage:
    python scripts/backfill_decision_learning.py
    python scripts/backfill_decision_learning.py --wallet 0xabc...
"""

import argparse
import asyncio
import os
import sys

from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.profiler.behavior_profiler import BehaviorProfiler


async def main(wallet: str | None = None) -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    profiler = BehaviorProfiler(redis_client=None)
    try:
        process_result = await profiler.rebuild_order_process(wallet=wallet)
        result = await profiler.rebuild_decision_learning(wallet=wallet)
        logger.info(
            f"Learning backfill complete: "
            f"{process_result['wallets']} wallets / {process_result['orders']} orders, "
            f"{result['wallets']} wallets / {result['trades']} trades"
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill decision learning from closed paper trades."
    )
    parser.add_argument("--wallet", help="Optional single wallet to rebuild.")
    args = parser.parse_args()
    asyncio.run(main(wallet=args.wallet))
