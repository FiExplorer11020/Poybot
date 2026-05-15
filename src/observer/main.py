"""Entry point for the Observer module (WebSocket + TradeObserver + PositionTracker)."""

import asyncio
import json
import signal
from typing import Any

import aiohttp
import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging
from src.observer.position_tracker import PositionTracker
from src.observer.trade_observer import TradeObserver
from src.registry.falcon_client import FalconClient

MAX_OBSERVER_WS_TOKENS = 100


def _extract_gamma_market_tokens(markets: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for market in markets:
        raw_tokens = market.get("clobTokenIds")
        if isinstance(raw_tokens, str):
            try:
                raw_tokens = json.loads(raw_tokens)
            except Exception:
                raw_tokens = []
        if not isinstance(raw_tokens, list):
            raw_tokens = []
        tokens.update(str(token) for token in raw_tokens if token)
        single = str(market.get("clobTokenId") or "").strip()
        if single:
            tokens.add(single)
    return tokens


async def _fetch_active_market_tokens(
    session: aiohttp.ClientSession,
    *,
    limit: int = 50,
) -> set[str]:
    url = (
        "https://gamma-api.polymarket.com/markets"
        f"?active=true&closed=false&limit={limit}&order=volume24hr&ascending=false"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                logger.warning(f"Gamma active market bootstrap failed with status {response.status}")
                return set()
            return _extract_gamma_market_tokens(await response.json())
    except Exception as exc:
        logger.warning(f"Gamma active market bootstrap failed: {exc}")
        return set()


async def _load_db_subscriptions(conn, *, wallet_limit: int = 50, token_limit: int = 250):
    # IMPORTANT: each query is wrapped in its own try/except so a slow / failing
    # query (e.g. GROUP BY full-scan on trades_observed) doesn't void the whole
    # bootstrap — the silent root cause of the "0 leader wallets" bug.
    wallets: set[str] = set()
    tokens: set[str] = set()

    try:
        # Mix: top by falcon_score (curated quality) + top by confirmed-
        # follower count (where the real leader signal lives).
        # The leaderboard ranks wallets by PnL — but the bot's edge is
        # the FOLLOWER POOL the leader excites, not the leader's own
        # accuracy. So we must include both populations: high-pnl
        # wallets that have a small but reliable follower cluster
        # already, AND high-influence wallets we may have missed in
        # the leaderboard.
        wallet_rows = await conn.fetch(
            """
            (
                SELECT wallet_address
                FROM leaders
                WHERE excluded = FALSE
                ORDER BY falcon_score DESC NULLS LAST
                LIMIT $1
            )
            UNION
            (
                SELECT fe.leader_wallet AS wallet_address
                FROM follower_edges fe
                JOIN leaders l ON l.wallet_address = fe.leader_wallet
                                AND l.excluded = FALSE
                WHERE fe.co_occurrences >= 5
                  AND fe.same_direction_rate >= 0.7
                GROUP BY fe.leader_wallet
                HAVING COUNT(*) >= 5
                ORDER BY COUNT(*) DESC
                LIMIT $1
            )
            """,
            wallet_limit,
        )
        wallets = {str(row["wallet_address"]) for row in wallet_rows if row["wallet_address"]}
    except Exception as exc:
        logger.warning(f"Observer bootstrap: leaders query failed: {exc!r}")

    # Use a time-bounded query that hits the (time DESC) index instead of a
    # full GROUP BY scan. Cheap path: most recent active tokens last 24h.
    try:
        trade_token_rows = await conn.fetch(
            """
            SELECT DISTINCT token_id
            FROM trades_observed
            WHERE time >= NOW() - INTERVAL '24 hours'
              AND NULLIF(token_id, '') IS NOT NULL
            LIMIT $1
            """,
            token_limit,
        )
        tokens.update(str(r["token_id"]) for r in trade_token_rows if r["token_id"])
    except Exception as exc:
        logger.warning(f"Observer bootstrap: recent token query failed: {exc!r}")

    try:
        market_token_rows = await conn.fetch(
            """
            SELECT token_yes, token_no, volume_24h
            FROM markets
            WHERE active = TRUE
              AND end_date > NOW()
              AND (NULLIF(token_yes, '') IS NOT NULL OR NULLIF(token_no, '') IS NOT NULL)
            ORDER BY volume_24h DESC NULLS LAST
            LIMIT $1
            """,
            token_limit,
        )
        for row in market_token_rows:
            if row["token_yes"]:
                tokens.add(str(row["token_yes"]))
            if row["token_no"]:
                tokens.add(str(row["token_no"]))
    except Exception as exc:
        logger.warning(f"Observer bootstrap: markets query failed: {exc!r}")

    return wallets, tokens


def _prioritize_subscription_tokens(
    *,
    active_tokens: set[str],
    db_tokens: set[str],
    limit: int = MAX_OBSERVER_WS_TOKENS,
) -> set[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for token in sorted(active_tokens):
        if token not in seen:
            ordered.append(token)
            seen.add(token)
    for token in sorted(db_tokens):
        if token not in seen:
            ordered.append(token)
            seen.add(token)
    return set(ordered[: max(1, int(limit))])


async def _bootstrap_subscriptions() -> tuple[set[str], set[str]]:
    from src.database.connection import get_db

    wallets: set[str] = set()
    tokens: set[str] = set()
    try:
        async with get_db() as conn:
            wallets, tokens = await _load_db_subscriptions(conn)
    except Exception as exc:
        logger.warning(f"Observer DB subscription bootstrap failed: {exc!r}")

    async with aiohttp.ClientSession() as session:
        active_tokens = await _fetch_active_market_tokens(session, limit=50)
        tokens = _prioritize_subscription_tokens(active_tokens=active_tokens, db_tokens=tokens)
        logger.info(f"Observer bootstrap: {len(active_tokens)} active market tokens from Gamma")

    logger.info(
        f"Observer bootstrap: {len(wallets)} leader wallets, {len(tokens)} market tokens"
    )
    return wallets, tokens


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting Observer (log_level={level})")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    falcon = FalconClient(redis_client=redis_client)
    leader_wallets, leader_markets = await _bootstrap_subscriptions()
    observer = TradeObserver(
        falcon_client=falcon,
        redis_client=redis_client,
        leader_wallets=leader_wallets,
        leader_markets=leader_markets,
    )
    tracker = PositionTracker(redis_client=redis_client)
    # Phase 2 Task C: hydrate _open_positions from position_tracker_state
    # BEFORE tracker.start() subscribes to trades. Without this, a SELL
    # that lands seconds after restart can't be matched to the OPEN it
    # closes and is silently dropped (the very Red Flag #4 we're closing).
    try:
        await tracker.warm_start()
    except Exception as exc:
        logger.warning(f"PositionTracker warm_start failed: {exc}")

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down Observer")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    try:
        await asyncio.gather(
            observer.start(),
            tracker.start(),
            stop_event.wait(),
        )
    finally:
        await observer.stop()
        await tracker.stop()
        await close_pool()
        await redis_client.aclose()
        logger.info("Observer stopped")


if __name__ == "__main__":
    asyncio.run(main())
