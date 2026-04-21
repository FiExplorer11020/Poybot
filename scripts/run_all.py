"""
Local dev entry point — starts all modules in a single process.

Usage:
    python scripts/run_all.py
"""

import asyncio
import os
import signal
import sys

import aiohttp
import redis.asyncio as redis_async
from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.engine.confidence_engine import ConfidenceEngine
from src.engine.paper_trader import PaperTrader
from src.engine.risk_manager import RiskManager
from src.engine.scheduler import NightlyBatchScheduler
from src.graph.graph_engine import GraphEngine
from src.observer.position_tracker import PositionTracker
from src.observer.trade_observer import TradeObserver
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry


async def _fetch_active_market_tokens(session: aiohttp.ClientSession, limit: int = 50) -> set[str]:
    """Fetch token IDs from top active markets on Gamma API (by 24h volume)."""
    tokens: set[str] = set()
    try:
        url = (
            f"https://gamma-api.polymarket.com/markets"
            f"?active=true&closed=false&limit={limit}&order=volume24hr&ascending=false"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                markets = await r.json()
                for m in markets:
                    raw = m.get("clobTokenIds", "[]")
                    try:
                        import json as _json

                        ids = _json.loads(raw) if isinstance(raw, str) else raw
                        tokens.update(ids)
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"Failed to fetch active market tokens: {e}")
    return tokens


async def _bootstrap_subscriptions() -> tuple[set[str], set[str]]:
    """Fetch leader wallets from DB and subscribe to top active + leader-touched markets."""
    from src.database.connection import get_db

    wallets: set[str] = set()
    tokens: set[str] = set()
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT wallet_address
                FROM leaders
                WHERE excluded=FALSE
                ORDER BY falcon_score DESC
                LIMIT 50
                """
            )
            wallets = {r["wallet_address"] for r in rows}

        async with aiohttp.ClientSession() as session:
            # Always subscribe to top 50 active markets by 24h volume
            active_tokens = await _fetch_active_market_tokens(session, limit=50)
            tokens.update(active_tokens)
            logger.info(f"Bootstrap: {len(active_tokens)} active market tokens from Gamma API")

            # Also add tokens from leaders' recent trades
            for wallet in list(wallets)[:20]:
                url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=50"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            for t in await r.json():
                                if t.get("asset"):
                                    tokens.add(t["asset"])
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Bootstrap subscriptions failed: {e}")
    return wallets, tokens


async def main() -> None:
    logger.info("Starting Polymarket Intelligence Engine (all modules)")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    falcon = FalconClient(redis_client=redis_client)
    registry = LeaderRegistry(falcon_client=falcon)

    # Bootstrap WebSocket subscriptions from leaders' recent activity
    leader_wallets, leader_markets = await _bootstrap_subscriptions()
    logger.info(
        f"Bootstrap: {len(leader_wallets)} leader wallets, {len(leader_markets)} market tokens"
    )

    observer = TradeObserver(
        falcon_client=falcon,
        redis_client=redis_client,
        leader_wallets=leader_wallets,
        leader_markets=leader_markets,
    )
    tracker = PositionTracker(redis_client=redis_client)
    graph = GraphEngine(redis_client=redis_client)
    error_model = ErrorModel()
    profiler = BehaviorProfiler(redis_client=redis_client, error_model=error_model)
    confidence = ConfidenceEngine(
        redis_client=redis_client,
        behavior_profiler=profiler,
        error_model=error_model,
    )
    risk_manager = RiskManager()
    paper_trader = PaperTrader(
        redis_client=redis_client,
        confidence_engine=confidence,
        risk_manager=risk_manager,
    )
    batch_scheduler = NightlyBatchScheduler(hour_utc=settings.BATCH_HOUR_UTC)

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutdown signal received — stopping all modules")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await asyncio.gather(
            registry.run(),
            observer.start(),
            tracker.start(),
            graph.start(),
            profiler.start(),
            confidence.start(),
            paper_trader.start(),
            batch_scheduler.run(),
            stop_event.wait(),
            return_exceptions=True,
        )
    finally:
        for component in (
            observer,
            tracker,
            graph,
            profiler,
            confidence,
            paper_trader,
            registry,
            batch_scheduler,
        ):
            try:
                await component.stop()
            except Exception:
                pass
        await close_pool()
        await redis_client.aclose()
        await falcon.close()
        logger.info("All modules stopped")


if __name__ == "__main__":
    asyncio.run(main())
