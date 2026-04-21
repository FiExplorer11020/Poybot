"""
Position Tracker — reconstructs OPEN→CLOSE position cycles from trades_observed.
Subscribes to Redis trades:observed, maintains in-memory state, writes to positions_reconstructed.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from src.database.connection import get_db
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole
from src.economics.pnl import calculate_long_pnl

REDIS_TRADES_CHANNEL = "trades:observed"
REDIS_POSITIONS_CHANNEL = "positions:closed"
MERGE_WINDOW_S = 600  # 10 minutes


@dataclass
class OpenPosition:
    wallet_address: str
    market_id: str
    token_id: str
    direction: str  # 'yes' or 'no'
    open_time: datetime
    entry_price: Decimal
    size_usdc: Decimal
    size_shares: Decimal
    shares_remaining: Decimal
    fee_rate_pct: Decimal = field(default_factory=lambda: Decimal("0"))


class PositionTracker:
    def __init__(self, redis_client):
        self._redis = redis_client
        # Key: (wallet, market_id, token_id) → list of OpenPosition (FIFO queue)
        self._open_positions: dict[tuple, list[OpenPosition]] = {}
        self._running = False
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
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
                    logger.error(f"PositionTracker error processing message: {e}")
        finally:
            await pubsub.unsubscribe(REDIS_TRADES_CHANNEL)

    async def on_trade(self, trade: dict) -> None:
        """Process a single trade dict. Called by _subscribe_loop or directly in tests."""
        wallet = trade.get("wallet_address", "")
        market_id = trade.get("market_id", "")
        token_id = trade.get("token_id", "")
        side = (trade.get("side") or "").upper()

        if not wallet or not market_id or not token_id:
            return

        try:
            price = Decimal(str(trade.get("price", 0)))
            size_usdc = Decimal(str(trade.get("size_usdc", 0)))
            size_shares_raw = trade.get("size_shares")
            if size_shares_raw is None:
                size_shares = size_usdc / price if price > Decimal("0") else Decimal("0")
            else:
                size_shares = Decimal(str(size_shares_raw))
            ts = trade.get("time")
            if isinstance(ts, str):
                trade_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                trade_time = datetime.now(tz=timezone.utc)
        except (ValueError, TypeError) as e:
            logger.debug(f"Bad trade fields: {e}")
            return

        direction = _token_direction(token_id, market_id)

        if side == "BUY":
            await self._handle_buy(
                wallet,
                market_id,
                token_id,
                direction,
                trade_time,
                price,
                size_usdc,
                size_shares,
            )
        elif side == "SELL":
            await self._handle_sell(
                wallet,
                market_id,
                token_id,
                trade_time,
                price,
                size_usdc,
                size_shares,
            )

    async def _handle_buy(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        direction: str,
        trade_time: datetime,
        price: Decimal,
        size_usdc: Decimal,
        size_shares: Decimal,
    ) -> None:
        """Open a new position or detect merge exit."""
        # Check if this BUY of opposite direction closes an existing position (merge).
        # _opposite_key returns None in the simplified stub, so merge detection is
        # skipped until the markets table lookup is wired in.
        opposite_token_key = _opposite_key(wallet, market_id, token_id)
        if opposite_token_key and opposite_token_key in self._open_positions:
            opposite_positions = self._open_positions[opposite_token_key]
            for pos in list(opposite_positions):
                delta_s = abs((trade_time - pos.open_time).total_seconds())
                if delta_s <= MERGE_WINDOW_S:
                    await self._close_position(
                        pos, trade_time, price, pos.shares_remaining, "merge"
                    )
                    opposite_positions.remove(pos)
                    if not opposite_positions:
                        del self._open_positions[opposite_token_key]
                    return

        # Open new position
        fee_rate = await self._get_fee_rate(market_id)
        pos = OpenPosition(
            wallet_address=wallet,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
            open_time=trade_time,
            entry_price=price,
            size_usdc=size_usdc,
            size_shares=size_shares,
            shares_remaining=size_shares,
            fee_rate_pct=fee_rate,
        )
        key = (wallet, market_id, token_id)
        self._open_positions.setdefault(key, []).append(pos)

    async def _handle_sell(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        trade_time: datetime,
        price: Decimal,
        size_usdc: Decimal,
        size_shares: Decimal,
    ) -> None:
        """Partially or fully close open positions FIFO."""
        key = (wallet, market_id, token_id)
        if key not in self._open_positions:
            return

        remaining_sell = size_shares
        positions = self._open_positions[key]

        while remaining_sell > 0 and positions:
            pos = positions[0]
            if remaining_sell >= pos.shares_remaining:
                # Full close of this slot
                remaining_sell -= pos.shares_remaining
                sell_size = pos.shares_remaining
                await self._close_position(pos, trade_time, price, sell_size, "sell")
                positions.pop(0)
            else:
                # Partial close — reduce remaining on the open position
                closed_size = remaining_sell
                pos.shares_remaining -= closed_size
                # Create a synthetic closed slice
                entry_notional = pos.entry_price * closed_size
                closed_pos = OpenPosition(
                    wallet_address=pos.wallet_address,
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    direction=pos.direction,
                    open_time=pos.open_time,
                    entry_price=pos.entry_price,
                    size_usdc=entry_notional,
                    size_shares=closed_size,
                    shares_remaining=closed_size,
                    fee_rate_pct=pos.fee_rate_pct,
                )
                await self._close_position(closed_pos, trade_time, price, closed_size, "sell")
                remaining_sell = Decimal("0")

        if not positions:
            del self._open_positions[key]

    async def _close_position(
        self,
        pos: OpenPosition,
        close_time: datetime,
        exit_price: Decimal,
        close_shares: Decimal,
        close_method: str,
    ) -> None:
        """Calculate PnL, write to DB, publish to Redis."""
        entry_cost = pos.entry_price * close_shares
        entry_fee = calculate_polymarket_fee(
            shares=close_shares,
            price=pos.entry_price,
            fee_rate=pos.fee_rate_pct,
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=True,
        )
        exit_fee = calculate_polymarket_fee(
            shares=close_shares,
            price=exit_price,
            fee_rate=pos.fee_rate_pct,
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=True,
        )
        pnl = calculate_long_pnl(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_shares=close_shares,
            entry_fee_usdc=entry_fee,
            exit_fee_usdc=exit_fee,
        )
        gross_pnl = pnl.gross_pnl_usdc
        pnl_usdc = pnl.net_pnl_usdc
        pnl_pct = pnl.pnl_pct
        holding_s = int((close_time - pos.open_time).total_seconds())
        category = "unknown"
        is_contrarian = False

        try:
            async with get_db() as conn:
                market_row = await conn.fetchrow(
                    "SELECT category FROM markets WHERE market_id = $1",
                    pos.market_id,
                )
                if market_row and market_row["category"]:
                    category = market_row["category"]

                trend_row = await conn.fetchrow(
                    """
                    SELECT AVG(price) AS avg_price FROM (
                        SELECT price
                        FROM trades_observed
                        WHERE market_id = $1
                          AND token_id = $2
                          AND time < $3
                        ORDER BY time DESC
                        LIMIT 10
                    ) recent
                    """,
                    pos.market_id,
                    pos.token_id,
                    pos.open_time,
                )
                if trend_row and trend_row["avg_price"] is not None:
                    avg_price = Decimal(str(trend_row["avg_price"]))
                    if pos.direction == "yes":
                        is_contrarian = pos.entry_price < avg_price
                    else:
                        is_contrarian = pos.entry_price > avg_price

                await conn.execute(
                    """
                    INSERT INTO positions_reconstructed
                        (wallet_address, market_id, token_id, direction,
                         open_time, close_time, entry_price, exit_price,
                         size_usdc, pnl_usdc, pnl_pct, holding_period_s, close_method,
                         size_shares, entry_fee_usdc, exit_fee_usdc, gross_pnl_usdc,
                         net_pnl_usdc, economic_model_version)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                    """,
                    pos.wallet_address,
                    pos.market_id,
                    pos.token_id,
                    pos.direction,
                    pos.open_time,
                    close_time,
                    pos.entry_price,
                    exit_price,
                    entry_cost,
                    round(pnl_usdc, 2),
                    round(pnl_pct, 4),
                    holding_s,
                    close_method,
                    close_shares,
                    entry_fee,
                    exit_fee,
                    gross_pnl,
                    pnl_usdc,
                    ECONOMIC_MODEL_VERSION,
                )
        except Exception as e:
            logger.error(f"Failed to insert closed position: {e}")
            return

        event = {
            "wallet_address": pos.wallet_address,
            "market_id": pos.market_id,
            "token_id": pos.token_id,
            "direction": pos.direction,
            "open_time": pos.open_time.isoformat(),
            "close_time": close_time.isoformat(),
            "pnl_usdc": str(round(pnl_usdc, 2)),
            "gross_pnl_usdc": str(round(gross_pnl, 2)),
            "category": category,
            "size_usdc": str(entry_cost),
            "size_shares": str(close_shares),
            "entry_price": str(pos.entry_price),
            "exit_price": str(exit_price),
            "economic_model_version": ECONOMIC_MODEL_VERSION,
            "holding_period_s": holding_s,
            "is_contrarian": is_contrarian,
            "close_method": close_method,
        }
        try:
            await self._redis.publish(REDIS_POSITIONS_CHANNEL, json.dumps(event))
        except Exception as e:
            logger.warning(f"Failed to publish position close: {e}")

    async def close_market_positions(self, market_id: str, resolution_price: Decimal) -> None:
        """Close all open positions for a resolved market at the resolution price."""
        keys_to_close = [(w, m, t) for (w, m, t) in list(self._open_positions) if m == market_id]
        now = datetime.now(tz=timezone.utc)
        for key in keys_to_close:
            positions = list(self._open_positions.get(key, []))
            for pos in positions:
                await self._close_position(
                    pos, now, resolution_price, pos.shares_remaining, "resolution"
                )
            if key in self._open_positions:
                del self._open_positions[key]

    async def _get_fee_rate(self, market_id: str) -> Decimal:
        """Look up fee rate for a market from the markets table. Returns 0 if not found."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT fee_rate_pct FROM markets WHERE market_id = $1",
                    market_id,
                )
                if row and row["fee_rate_pct"] is not None:
                    return Decimal(str(row["fee_rate_pct"]))
        except Exception as e:
            logger.debug(f"Fee lookup failed for {market_id}: {e}")
        return Decimal("0")


def _token_direction(token_id: str, market_id: str) -> str:
    """
    Infer direction from token_id. In Polymarket each market has a token_yes and
    token_no. Without the markets table we cannot resolve this here, so we default
    to 'yes'. The caller can pass explicit direction if available.
    """
    return "yes"  # Simplified — real implementation would check markets table


def _opposite_key(wallet: str, market_id: str, token_id: str) -> tuple | None:
    """
    Return the dict key that would represent the opposite token in the same market.
    Merge detection requires market context (token_yes / token_no mapping) which is
    not available in this module. Returns None to skip merge detection until wired in.
    """
    return None  # Simplified — merge detection requires market context
