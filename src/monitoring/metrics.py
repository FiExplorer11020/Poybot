"""
Monitoring — structured health checks and logging helpers.
"""

from datetime import datetime, timezone

from loguru import logger

from src.database.connection import get_db


async def check_db_connectivity() -> bool:
    try:
        async with get_db() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"DB connectivity failed: {e}")
        return False


async def check_redis_connectivity(redis_client) -> bool:
    try:
        await redis_client.ping()
        return True
    except Exception as e:
        logger.error(f"Redis connectivity failed: {e}")
        return False


async def get_latest_trade_age(max_age_s: int = 300) -> tuple[bool, int]:
    """Returns (is_fresh, age_seconds). Fresh = within max_age_s."""
    try:
        async with get_db() as conn:
            row = await conn.fetchrow("SELECT MAX(time) AS latest FROM trades_observed")
            if not row or not row["latest"]:
                return False, -1
            age = int((datetime.now(tz=timezone.utc) - row["latest"]).total_seconds())
            return age < max_age_s, age
    except Exception:
        return False, -1


async def get_leader_registry_stats() -> dict:
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE on_watchlist AND NOT excluded) AS active,
                       COUNT(*) FILTER (WHERE on_watchlist OR excluded) AS total,
                       MIN(last_refresh) AS oldest_refresh
                FROM leaders
                """
            )
            return dict(row) if row else {}
    except Exception:
        return {}


async def get_paper_trading_summary() -> dict:
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE status='open') AS open_count,
                       COUNT(*) FILTER (WHERE status='closed') AS closed_count,
                       COALESCE(SUM(pnl_usdc) FILTER (WHERE status='closed'), 0) AS total_pnl,
                       COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END)
                           FILTER (WHERE status='closed'), 0) AS wins
                FROM paper_trades
                """
            )
            return dict(row) if row else {}
    except Exception:
        return {}
