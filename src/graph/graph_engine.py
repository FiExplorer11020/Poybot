"""
Graph Engine — builds and maintains the leader→follower social graph.
Subscribes to trades:observed, detects follower patterns, updates follower_edges.
"""

import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger

from src.config import settings
from src.database.connection import get_db

REDIS_TRADES_CHANNEL = "trades:observed"


class GraphEngine:
    def __init__(self, redis_client):
        self._redis = redis_client
        self._running = False
        self._stop_event = asyncio.Event()
        # Buffer: market_id → deque of recent trades (for window lookup)
        # Each entry: {"time": datetime, "wallet": str, "side": str, "is_leader": bool}
        self._market_trades: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        await self._hydrate_recent_trades()
        await self._subscribe_loop()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def _subscribe_loop(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REDIS_TRADES_CHANNEL)
        try:
            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                try:
                    trade = json.loads(message["data"])
                    await self.on_trade(trade)
                except Exception as e:
                    logger.error(f"GraphEngine error: {e}")
        finally:
            await pubsub.unsubscribe(REDIS_TRADES_CHANNEL)

    async def _hydrate_recent_trades(self) -> None:
        """Prime the in-memory buffer so follower detection survives process restarts."""
        warm_window_s = max(1800, settings.FOLLOWER_WINDOW_S * 4)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=warm_window_s)
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT time, market_id, wallet_address, side, is_leader
                    FROM trades_observed
                    WHERE time >= $1
                    ORDER BY time ASC
                    """,
                    cutoff,
                )
        except Exception as exc:
            logger.debug(f"GraphEngine warm-start failed: {exc}")
            return

        loaded = 0
        for row in rows:
            market_id = row["market_id"]
            if not market_id:
                continue
            self._market_trades[market_id].append(
                {
                    "time": row["time"],
                    "wallet": row["wallet_address"],
                    "side": (row["side"] or "").upper(),
                    "is_leader": bool(row["is_leader"]),
                }
            )
            loaded += 1
        for row in rows:
            if bool(row["is_leader"]):
                continue
            await self._detect_recent_leaders(
                follower_wallet=row["wallet_address"],
                market_id=row["market_id"],
                side=(row["side"] or "").upper(),
                follower_time=row["time"],
            )
        if loaded:
            logger.info(f"GraphEngine warm-started with {loaded} recent trades")

    async def on_trade(self, trade: dict) -> None:
        """Process incoming trade from either side of the leader→follower relation."""
        wallet = trade.get("wallet_address", "")
        market_id = trade.get("market_id", "")
        side = (trade.get("side") or "").upper()
        is_leader = bool(trade.get("is_leader", False))

        try:
            ts = trade.get("time")
            if isinstance(ts, str):
                trade_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                trade_time = datetime.now(tz=timezone.utc)
        except (ValueError, TypeError):
            return

        entry = {
            "time": trade_time,
            "wallet": wallet,
            "side": side,
            "is_leader": is_leader,
        }
        self._market_trades[market_id].append(entry)

        if is_leader and wallet:
            await self._detect_followers(
                leader_wallet=wallet,
                market_id=market_id,
                side=side,
                leader_time=trade_time,
            )
        elif wallet:
            await self._detect_recent_leaders(
                follower_wallet=wallet,
                market_id=market_id,
                side=side,
                follower_time=trade_time,
            )

    async def _detect_followers(
        self, leader_wallet: str, market_id: str, side: str, leader_time: datetime
    ) -> None:
        """Scan FOLLOWER_WINDOW_S window for non-leader reactions to a leader trade."""
        window_s = settings.FOLLOWER_WINDOW_S
        candidates: dict[str, dict] = {}

        for entry in self._market_trades[market_id]:
            if entry["is_leader"] or entry["wallet"] == leader_wallet:
                continue
            delta = (entry["time"] - leader_time).total_seconds()
            if 0 <= delta <= window_s:
                same_direction = entry["side"] == side
                w = entry["wallet"]
                current = candidates.get(w)
                if (
                    current is None
                    or (same_direction and not current["same_direction"])
                    or delta < current["delay"]
                ):
                    candidates[w] = {"delay": delta, "same_direction": same_direction}

        for follower_wallet, info in candidates.items():
            await self._update_edge(
                leader_wallet=leader_wallet,
                follower_wallet=follower_wallet,
                delay_s=info["delay"],
                same_direction=info["same_direction"],
            )

    async def _detect_recent_leaders(
        self,
        follower_wallet: str,
        market_id: str,
        side: str,
        follower_time: datetime,
    ) -> None:
        """When a follower trade arrives, look back for leader trades in the causal window."""
        window_s = settings.FOLLOWER_WINDOW_S
        candidates: dict[str, dict] = {}

        for entry in self._market_trades[market_id]:
            if not entry["is_leader"] or entry["wallet"] == follower_wallet:
                continue
            delta = (follower_time - entry["time"]).total_seconds()
            if 0 <= delta <= window_s:
                same_direction = entry["side"] == side
                leader_wallet = entry["wallet"]
                current = candidates.get(leader_wallet)
                if (
                    current is None
                    or (same_direction and not current["same_direction"])
                    or delta < current["delay"]
                ):
                    candidates[leader_wallet] = {
                        "delay": delta,
                        "same_direction": same_direction,
                    }

        for leader_wallet, info in candidates.items():
            await self._update_edge(
                leader_wallet=leader_wallet,
                follower_wallet=follower_wallet,
                delay_s=info["delay"],
                same_direction=info["same_direction"],
            )

    async def _update_edge(
        self,
        leader_wallet: str,
        follower_wallet: str,
        delay_s: float,
        same_direction: bool,
    ) -> None:
        """Upsert follower_edges row with Beta-Binomial and EWMA updates."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT co_occurrences, follow_beta_a, follow_beta_b,
                           avg_delay_s, same_direction_rate
                    FROM follower_edges
                    WHERE leader_wallet = $1 AND follower_wallet = $2
                    """,
                    leader_wallet,
                    follower_wallet,
                )

                if row is None:
                    # New edge — Beta prior: (2,1) for success, (1,2) for failure
                    new_count = 1
                    beta_a = Decimal("2.0") if same_direction else Decimal("1.0")
                    beta_b = Decimal("1.0") if same_direction else Decimal("2.0")
                    new_avg_delay = Decimal(str(delay_s))
                    new_sdr = beta_a / (beta_a + beta_b)
                else:
                    new_count = (row["co_occurrences"] or 0) + 1
                    beta_a = Decimal(str(row["follow_beta_a"] or "1.0"))
                    beta_b = Decimal(str(row["follow_beta_b"] or "1.0"))
                    if same_direction:
                        beta_a += Decimal("1.0")
                    else:
                        beta_b += Decimal("1.0")

                    lam = Decimal(str(settings.EWMA_LAMBDA))
                    prev_delay = Decimal(str(row["avg_delay_s"] or delay_s))
                    new_avg_delay = lam * prev_delay + (1 - lam) * Decimal(str(delay_s))

                    # Beta posterior mean = alpha / (alpha + beta)
                    new_sdr = beta_a / (beta_a + beta_b)

                follow_probability = beta_a / (beta_a + beta_b)

                await conn.execute(
                    """
                    INSERT INTO follower_edges
                        (leader_wallet, follower_wallet, co_occurrences,
                         follow_probability, follow_beta_a, follow_beta_b,
                         avg_delay_s, same_direction_rate, last_observed)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                    ON CONFLICT (leader_wallet, follower_wallet) DO UPDATE SET
                        co_occurrences      = EXCLUDED.co_occurrences,
                        follow_probability  = EXCLUDED.follow_probability,
                        follow_beta_a       = EXCLUDED.follow_beta_a,
                        follow_beta_b       = EXCLUDED.follow_beta_b,
                        avg_delay_s         = EXCLUDED.avg_delay_s,
                        same_direction_rate = EXCLUDED.same_direction_rate,
                        last_observed       = EXCLUDED.last_observed
                    """,
                    leader_wallet,
                    follower_wallet,
                    new_count,
                    round(follow_probability, 4),
                    round(beta_a, 4),
                    round(beta_b, 4),
                    round(new_avg_delay, 2),
                    round(new_sdr, 4),
                )
        except Exception as e:
            logger.error(f"Failed to update edge {leader_wallet}→{follower_wallet}: {e}")

    async def get_followers(self, leader_wallet: str) -> list[dict]:
        """Return all follower edges for a given leader, ordered by probability."""
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM follower_edges
                WHERE leader_wallet = $1
                ORDER BY follow_probability DESC
                """,
                leader_wallet,
            )
            return [dict(r) for r in rows]

    async def get_leaders(self, follower_wallet: str) -> list[dict]:
        """Return all leader edges for a given follower, ordered by probability."""
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM follower_edges
                WHERE follower_wallet = $1
                ORDER BY follow_probability DESC
                """,
                follower_wallet,
            )
            return [dict(r) for r in rows]

    async def get_confirmed_edges(self, min_confidence: float = 0.5) -> list[dict]:
        """Return edges meeting minimum quality thresholds."""
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM follower_edges
                WHERE co_occurrences >= $1
                  AND same_direction_rate >= $2
                  AND follow_probability >= $3
                ORDER BY follow_probability DESC
                """,
                settings.MIN_CO_OCCURRENCES,
                Decimal(str(settings.MIN_SAME_DIRECTION_RATE)),
                Decimal(str(min_confidence)),
            )
            return [dict(r) for r in rows]
