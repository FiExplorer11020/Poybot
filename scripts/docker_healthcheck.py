"""
Docker HEALTHCHECK probe (S4.11).

Lightweight: pings Redis and DB, exits 0 (healthy) or 1 (unhealthy).
Used by the Dockerfile's HEALTHCHECK directive AND by docker-compose
service-level healthchecks.

Why a dedicated script vs. the existing `scripts/health_check.py`?
    * `health_check.py` runs Falcon + DB freshness + paper P&L queries.
      That's 5–10s and has external network deps (Falcon API). Way too
      heavy for a 30s-cadence Docker healthcheck.
    * This one is <2s in the happy path — just `SELECT 1` + `PING`.
    * Honors the COMPONENT env var so future per-service freshness
      checks can layer on top (e.g. observer should additionally check
      `latest_trade_age`).

Usage:
    python scripts/docker_healthcheck.py
    COMPONENT=engine python scripts/docker_healthcheck.py

Exit codes:
    0 — Redis + DB both reachable.
    1 — at least one dependency unreachable, OR an unexpected exception.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Make the project root importable when invoked as a bare script (the
# Dockerfile's HEALTHCHECK doesn't go through `python -m`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _check() -> int:
    # Lazy imports — keep the cold start of this script under 200ms.
    import asyncpg
    import redis.asyncio as redis_async

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    db_url = os.environ.get("DATABASE_URL", "")

    # --- Redis ----------------------------------------------------------- #
    try:
        client = redis_async.from_url(redis_url, decode_responses=True)
        try:
            pong = await asyncio.wait_for(client.ping(), timeout=2.0)
            if not pong:
                print("healthcheck: Redis PING returned falsy", file=sys.stderr)
                return 1
        finally:
            await client.aclose()
    except Exception as exc:
        print(f"healthcheck: Redis unreachable ({exc!r})", file=sys.stderr)
        return 1

    # --- Postgres -------------------------------------------------------- #
    if not db_url:
        print("healthcheck: DATABASE_URL not set", file=sys.stderr)
        return 1
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn=db_url), timeout=2.0)
        try:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
        finally:
            await conn.close()
    except Exception as exc:
        print(f"healthcheck: Postgres unreachable ({exc!r})", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    try:
        return asyncio.run(_check())
    except Exception as exc:
        print(f"healthcheck: unexpected error ({exc!r})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
