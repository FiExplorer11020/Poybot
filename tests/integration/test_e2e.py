"""
End-to-end smoke tests.

These tests verify the system is wired up correctly against the local Docker
PostgreSQL and Redis instances. They do NOT require live Polymarket data.

Run with:
    pytest tests/integration/ -m integration
"""

import os

import pytest

# ---------------------------------------------------------------------------
# Skip guard: only run when DATABASE_URL points at local Docker
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")
_SKIP_REASON = (
    "DATABASE_URL not set or does not point at localhost — "
    "integration tests require the local Docker stack"
)
_IS_LOCAL = "localhost" in _DB_URL or "127.0.0.1" in _DB_URL

pytestmark = pytest.mark.integration

skip_unless_local = pytest.mark.skipif(
    not _IS_LOCAL,
    reason=_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    "leaders",
    "trades_observed",
    "positions_reconstructed",
    "follower_edges",
    "leader_profiles",
    "markets",
    "paper_trades",
    "decision_log",
    "schema_migrations",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_unless_local
@pytest.mark.asyncio
async def test_db_connectivity() -> None:
    """Connect to DB and verify all tables from 001_schema.sql exist."""
    import asyncpg

    conn = await asyncpg.connect(_DB_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            """
        )
        existing = {r["table_name"] for r in rows}
        missing = _EXPECTED_TABLES - existing
        assert not missing, f"Missing tables: {missing}"
    finally:
        await conn.close()


@skip_unless_local
@pytest.mark.asyncio
async def test_redis_connectivity() -> None:
    """Connect to Redis and verify it responds to PING."""
    import redis.asyncio as aioredis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        result = await client.ping()
        assert result is True
    finally:
        await client.aclose()


@skip_unless_local
@pytest.mark.asyncio
async def test_migrations_applied() -> None:
    """Verify that schema_migrations has at least version 1 applied."""
    import asyncpg

    conn = await asyncpg.connect(_DB_URL)
    try:
        version = await conn.fetchval("SELECT version FROM schema_migrations WHERE version = 1")
        assert version == 1, (
            "Migration 001_schema.sql not applied. Run scripts/setup_db.py to apply migrations."
        )
    finally:
        await conn.close()


def test_config_loads() -> None:
    """Settings can be loaded and critical values are non-empty."""
    from src.config import settings

    assert settings.DATABASE_URL, "DATABASE_URL must not be empty"
    assert settings.REDIS_URL, "REDIS_URL must not be empty"
    assert settings.PAPER_TRADING is True, "PAPER_TRADING must be True during development"
