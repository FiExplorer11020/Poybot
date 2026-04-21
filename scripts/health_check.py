"""
Health check script — prints status of all system components.

Checks:
  1. DB connectivity
  2. Redis connectivity
  3. Falcon API reachability
  4. Age of the most recent stored trade (freshness)
  5. Leader registry stats (active / total)
  6. Paper trading P&L summary

Usage:
    python scripts/health_check.py
"""

import asyncio
import os
import sys

import aiohttp
import redis.asyncio as redis_async

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.monitoring.metrics import (
    check_db_connectivity,
    check_redis_connectivity,
    get_latest_trade_age,
    get_leader_registry_stats,
    get_paper_trading_summary,
)


async def main() -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    results = {}

    # DB
    results["db"] = "OK" if await check_db_connectivity() else "FAIL"

    # Redis
    results["redis"] = "OK" if await check_redis_connectivity(redis_client) else "FAIL"

    # Falcon API reachability
    if not settings.FALCON_API_KEY:
        results["falcon"] = "MISSING KEY (FALCON_API_KEY not set)"
    else:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    settings.FALCON_API_URL,
                    json={
                        "agent_id": 581,
                        "params": {"proxy_wallet": "0xabc", "window_days": "7"},
                        "pagination": {"limit": 5, "offset": 0},
                        "formatter_config": {"format_type": "raw"},
                    },
                    headers={"Authorization": f"Bearer {settings.FALCON_API_KEY}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        results["falcon"] = f"HTTP {resp.status} ({body[:80]})"
                    else:
                        body = await resp.json()
                        if str(body.get("status", "")).lower() == "error":
                            results["falcon"] = (
                                f"ERROR ({body.get('error', {}).get('message', 'unknown')[:80]})"
                            )
                        else:
                            results["falcon"] = "OK"
        except Exception as e:
            results["falcon"] = f"FAIL ({e})"

    # Latest trade freshness (threshold: 5 minutes)
    fresh, age = await get_latest_trade_age(max_age_s=300)
    if age == -1:
        results["trades_fresh"] = "NO DATA"
    elif fresh:
        results["trades_fresh"] = f"OK ({age}s ago)"
    else:
        results["trades_fresh"] = f"STALE ({age}s ago)"

    # Leader registry stats
    reg_stats = await get_leader_registry_stats()
    active = reg_stats.get("active", 0)
    total = reg_stats.get("total", 0)
    results["leaders"] = f"{active} active / {total} total"

    # Paper trading P&L summary
    pt = await get_paper_trading_summary()
    pnl = float(pt.get("total_pnl", 0) or 0)
    open_count = int(pt.get("open_count", 0) or 0)
    closed_count = int(pt.get("closed_count", 0) or 0)
    wins = int(pt.get("wins", 0) or 0)
    sign = "+" if pnl >= 0 else ""
    results["paper_pnl"] = (
        f"{sign}${pnl:.2f} | {open_count} open / {closed_count} closed | {wins} wins"
    )

    # Print summary
    print("\n=== Polymarket Bot Health Check ===")
    for key, value in results.items():
        ok = any(v in str(value) for v in ("OK", "active", "+$", "$0"))
        status = "[OK]" if ok else "[!!]"
        print(f"  {status} {key:<16}: {value}")
    print("===================================\n")

    await close_pool()
    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
