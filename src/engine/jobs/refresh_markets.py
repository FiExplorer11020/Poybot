"""
Refresh active markets job (S3.10).

Hourly fetch of the top active markets from Gamma API. The token IDs
are written to a Redis SET — the observer (running in its own
container) re-reads this set and pivots its WebSocket subscriptions to
match.

Why a Redis set instead of pushing into the observer directly:
    * The engine container does not own the observer. In dev (run_all.py)
      they share a process; in prod (S4.11) they're separate containers.
    * A set is idempotent: re-publishing the same N tokens is a no-op,
      so a job that runs every hour without changes costs basically
      nothing.

Schema:
    Redis key:  subscriptions:active_markets
    Type:       SET[token_id: str]
    Updated by: this job (engine container)
    Read by:    observer (subscription reconciliation loop)
"""

from __future__ import annotations

from typing import Awaitable, Callable

import aiohttp
from loguru import logger

REDIS_ACTIVE_MARKETS_KEY = "subscriptions:active_markets"


async def _fetch_active_market_tokens(
    session: aiohttp.ClientSession, limit: int
) -> set[str]:
    """Best-effort fetch of top active markets by 24h volume. Returns
    an empty set on any error — caller decides whether to overwrite or
    skip."""
    tokens: set[str] = set()
    try:
        url = (
            f"https://gamma-api.polymarket.com/markets"
            f"?active=true&closed=false&limit={limit}"
            f"&order=volume24hr&ascending=false"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                logger.warning(f"refresh_markets: gamma returned status {r.status}")
                return tokens
            markets = await r.json()
        import json as _json

        for m in markets:
            raw = m.get("clobTokenIds", "[]")
            try:
                ids = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(ids, list):
                    tokens.update(str(i) for i in ids)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"refresh_markets: gamma fetch failed: {e}")
    return tokens


def make_refresh_markets_job(
    redis_client,
    *,
    limit: int = 50,
) -> Callable[[], Awaitable[None]]:
    """Return a coroutine factory that pushes the latest active token
    set into Redis."""

    async def _job() -> None:
        async with aiohttp.ClientSession() as session:
            tokens = await _fetch_active_market_tokens(session, limit=limit)
        if not tokens:
            logger.info("refresh_markets: no tokens fetched, skipping update")
            return
        try:
            # Replace the set atomically: SADD new, then trim to the
            # exact membership. Pipeline keeps it to a single round-trip.
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.delete(REDIS_ACTIVE_MARKETS_KEY)
                pipe.sadd(REDIS_ACTIVE_MARKETS_KEY, *tokens)
                await pipe.execute()
            logger.info(
                f"refresh_markets: published {len(tokens)} token IDs "
                f"to {REDIS_ACTIVE_MARKETS_KEY!r}"
            )
        except Exception:
            logger.exception("refresh_markets: redis publish failed")

    return _job
