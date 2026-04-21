"""
Tests connectivity to DB and Redis.
Usage: python scripts/test_connectivity.py
Prints "DB: OK | Redis: OK" when both are reachable.
"""

import asyncio
import os
import sys

import asyncpg
import redis.asyncio as aioredis


async def check_db(db_url: str) -> bool:
    try:
        conn = await asyncpg.connect(db_url)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return True
    except Exception as e:
        print(f"DB ERROR: {e}", file=sys.stderr)
        return False


async def check_redis(redis_url: str) -> bool:
    try:
        client = aioredis.from_url(redis_url)
        await client.ping()
        await client.aclose()
        return True
    except Exception as e:
        print(f"Redis ERROR: {e}", file=sys.stderr)
        return False


async def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket",
    )
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    db_ok, redis_ok = await asyncio.gather(check_db(db_url), check_redis(redis_url))

    db_status = "OK" if db_ok else "FAIL"
    redis_status = "OK" if redis_ok else "FAIL"
    print(f"DB: {db_status} | Redis: {redis_status}")

    if not db_ok or not redis_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
