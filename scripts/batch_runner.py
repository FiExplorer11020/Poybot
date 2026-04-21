"""
Batch runner — nightly cold path jobs (Hawkes refit, LogReg refit, LightGBM refit).
Runs at BATCH_HOUR_UTC (default 3 AM UTC). Each step logs timing.
Failed steps are logged but do not abort the batch.

Usage:
    python scripts/batch_runner.py
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis_async
from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.engine.confidence_engine import ConfidenceEngine
from src.graph.hawkes_fitter import HawkesFitter
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry


async def step_refresh_registry(falcon: FalconClient) -> None:
    """Step A: Refresh leader registry from Falcon."""
    async with get_db() as conn:
        registry = LeaderRegistry(falcon_client=falcon)
        await registry.refresh_leaderboard(conn)
        await registry.enrich_leaders(conn)


async def step_sync_markets(falcon: FalconClient) -> None:
    """Step A2: Upsert market metadata via Falcon agent 574."""
    registry = LeaderRegistry(falcon_client=falcon)
    async with get_db() as conn:
        count = await registry.sync_markets(conn)
    logger.info(f"sync_markets: {count} markets upserted")


async def step_backfill_trades(falcon: FalconClient) -> None:
    """Step B: Backfill missing trades via Falcon agent 556."""
    async with get_db() as conn:
        rows = await conn.fetch(
            "SELECT wallet_address FROM leaders "
            "WHERE on_watchlist = TRUE AND excluded = FALSE "
            "LIMIT 200"
        )
    for row in rows:
        try:
            result = await falcon.query(
                agent_id=556,
                params={"wallet_proxy": row["wallet_address"], "condition_id": "ALL"},
                limit=200,
            )
            trades = result if isinstance(result, list) else result.get("results", [])
            logger.debug(f"Backfill {row['wallet_address']}: {len(trades)} trades fetched")
        except Exception as e:
            logger.warning(f"Backfill failed for {row['wallet_address']}: {e}")


async def step_refit_hawkes() -> None:
    """Step C: Refit Hawkes process for confirmed edges."""
    fitter = HawkesFitter()
    updated = await fitter.run_batch()
    logger.info(f"Hawkes refit: {updated} edges updated")


async def step_refit_error_models() -> None:
    """Step D/E: Upgrade/refit error models for leaders with sufficient resolved positions."""
    async with get_db() as conn:
        rows = await conn.fetch(
            "SELECT wallet_address, positions_resolved FROM leader_profiles "
            "ORDER BY positions_resolved DESC LIMIT 200"
        )
    model = ErrorModel()
    for row in rows:
        wallet = row["wallet_address"]
        resolved = int(row["positions_resolved"] or 0)
        if resolved >= settings.MIN_RESOLVED_FOR_ERROR_P3:
            phase = 3
        elif resolved >= settings.MIN_RESOLVED_FOR_ERROR_P2:
            phase = 2
        else:
            phase = 1
        if phase > 1:
            try:
                _, profile, _ = await model._load_state(wallet)
                await model._upgrade_phase(wallet, phase, profile)
            except Exception as e:
                logger.warning(f"Error model refit failed for {wallet}: {e}")


async def step_precompute_confidence_cache(redis_client) -> None:
    """Precompute wallet-level confidence state for the hot path."""
    profiler = BehaviorProfiler(redis_client=None)
    error_model = ErrorModel()
    confidence = ConfidenceEngine(
        redis_client=redis_client,
        behavior_profiler=profiler,
        error_model=error_model,
    )
    cached = await confidence.precompute_redis_cache()
    logger.info(f"Confidence cache precomputed for {cached} leaders")


async def step_backfill_decision_learning() -> None:
    """Rebuild persisted follow/fade learning from historical paper trades."""
    profiler = BehaviorProfiler(redis_client=None)
    process_result = await profiler.rebuild_order_process()
    result = await profiler.rebuild_decision_learning()
    logger.info(
        f"Learning backfill: {process_result['wallets']} wallets / "
        f"{process_result['orders']} orders, "
        f"{result['wallets']} wallets / {result['trades']} trades replayed"
    )


async def step_cleanup_old_trades() -> None:
    """Step H: Delete trades older than RETENTION_TRADES_DAYS."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=settings.RETENTION_TRADES_DAYS)
    async with get_db() as conn:
        await conn.execute(
            "DELETE FROM trades_observed WHERE time < $1",
            cutoff,
        )
    logger.info(f"Cleanup: deleted old trades before {cutoff.date()}")


async def _run_steps(redis_client, falcon: FalconClient) -> None:
    """Execute the batch pipeline against already-initialised infrastructure.

    Safe to call from inside a long-running process (the nightly scheduler)
    because it does NOT open or close the asyncpg pool.
    """
    steps = [
        ("refresh_registry", step_refresh_registry, (falcon,)),
        ("sync_markets", step_sync_markets, (falcon,)),  # FIX 1
        ("backfill_trades", step_backfill_trades, (falcon,)),
        ("refit_hawkes", step_refit_hawkes, ()),
        ("backfill_decision_learning", step_backfill_decision_learning, ()),
        ("refit_error_models", step_refit_error_models, ()),
        ("precompute_confidence_cache", step_precompute_confidence_cache, (redis_client,)),
        ("cleanup_old_trades", step_cleanup_old_trades, ()),
    ]

    for name, fn, args in steps:
        t0 = time.time()
        try:
            await fn(*args)
            elapsed = time.time() - t0
            logger.info(f"Batch step '{name}' completed in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"Batch step '{name}' failed after {elapsed:.1f}s: {e}")


async def run_batch(*, manage_infrastructure: bool = True) -> None:
    """Standalone entry point.

    When invoked via `python scripts/batch_runner.py` (manage_infrastructure=True)
    we open/close the pool ourselves.  When the in-process scheduler calls us
    (manage_infrastructure=False) we reuse the already-open pool and only
    create short-lived redis + Falcon clients.
    """
    if manage_infrastructure:
        await initialize_pool(
            dsn=settings.DATABASE_URL,
            min_size=settings.DB_POOL_MIN,
            max_size=settings.DB_POOL_MAX,
        )

    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    falcon = FalconClient(redis_client=redis_client)
    try:
        await _run_steps(redis_client, falcon)
    finally:
        if manage_infrastructure:
            await close_pool()
        await redis_client.aclose()
        await falcon.close()
    logger.info("Batch run complete")


if __name__ == "__main__":
    asyncio.run(run_batch())
