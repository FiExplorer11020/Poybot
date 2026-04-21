"""
asyncpg connection pool with get_db() context manager.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg
from loguru import logger

_pool: asyncpg.Pool | None = None


async def initialize_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> None:
    """Create the global connection pool. Retries up to 5 times with 2s backoff."""
    global _pool
    for attempt in range(1, 6):
        try:
            _pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=min_size,
                max_size=max_size,
                command_timeout=30,
                server_settings={"application_name": "polymarket_bot"},
            )
            logger.info("DB pool initialized", extra={"min_size": min_size, "max_size": max_size})
            return
        except Exception as e:
            logger.warning(f"DB pool init attempt {attempt}/5 failed: {e}")
            if attempt < 5:
                await asyncio.sleep(2)
    raise RuntimeError("Failed to initialize DB pool after 5 attempts")


async def close_pool() -> None:
    """Close the global connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


@asynccontextmanager
async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    """Async context manager that yields a connection from the pool."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call initialize_pool() first.")
    async with _pool.acquire() as conn:
        yield conn
