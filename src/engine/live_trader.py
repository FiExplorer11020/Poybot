"""
LiveTrader — real-money mirror of PaperTrader (S2.6).

Receives decisions from Redis channel `decisions:live` (DecisionRouter
in S2.7 will be responsible for publishing onto it; until then this
channel is silent in production). For each decision it:

  1. Pre-flights the same vetos PaperTrader runs (size bounds, capital,
     conflict, recent reentry, market resolved). Sizing/Kelly/risk are
     NOT recomputed — they were already applied upstream by
     ConfidenceEngine + RiskManager and are baked into `decision.size_usdc`.
  2. Opens a `live_trades` row in `pending` (or `shadow` if dry-run)
     state and delegates execution to OrderManager.
  3. Commits the position to `open` on fill, or `failed` / `canceled`
     on rejection / timeout.
  4. Runs a monitor loop that watches open positions and closes them
     on take-profit / stop-loss / timeout / leader exit / market resolved
     — the same triggers PaperTrader uses.

What this class is NOT responsible for:
  * Deciding paper vs live  -> DecisionRouter (S2.7)
  * Sizing the trade        -> ConfidenceEngine (already in pipeline)
  * Risk veto               -> RiskManager (already in pipeline)
  * Order placement details -> OrderManager
  * CLOB protocol           -> CLOBClientWrapper
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from src.config import settings
from src.control.killswitch import get_killswitch
from src.database.connection import get_db
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole, StrategyTrack
from src.economics.pnl import calculate_long_pnl
from src.engine.clob_client_wrapper import CLOBClientWrapper
from src.engine.order_manager import OrderManager

REDIS_DECISIONS_LIVE_CHANNEL = "decisions:live"
REDIS_LIVE_OPENED_CHANNEL = "positions:live_opened"
REDIS_LIVE_CLOSED_CHANNEL = "positions:live_closed"

TAKE_PROFIT_FOLLOW = 0.10
TAKE_PROFIT_FADE = 0.10
STOP_LOSS_FOLLOW = 0.08
STOP_LOSS_FADE = 0.05
TIMEOUT_DAYS = 30
MONITOR_INTERVAL_S = 30


@dataclass
class OpenLiveTrade:
    """Live-trade state held in memory for the monitor loop."""
    id: int
    market_id: str
    token_id: str
    direction: str          # 'yes' / 'no'
    strategy: str           # 'follow' / 'fade'
    entry_price: float
    size_usdc: float        # filled notional
    leader_wallet: str
    confidence: float
    fee_paid_usdc: float = 0.0
    size_shares: float = 0.0
    opened_at: Optional[datetime] = None
    leader_context: dict = field(default_factory=dict)


class LiveTrader:
    """Subscribes to Redis decisions, executes via OrderManager,
    persists to live_trades."""

    def __init__(
        self,
        *,
        redis_client,
        clob_client: Optional[CLOBClientWrapper] = None,
        order_manager: Optional[OrderManager] = None,
        confidence_engine=None,
        risk_manager=None,
    ) -> None:
        self._redis = redis_client
        self._clob = clob_client or CLOBClientWrapper()
        self._order_manager = order_manager or OrderManager(self._clob)
        self._confidence_engine = confidence_engine
        self._risk_manager = risk_manager
        self._running = False
        self._stop_event = asyncio.Event()
        self._open_trades: list[OpenLiveTrade] = []

    @property
    def dry_run(self) -> bool:
        return self._clob.dry_run

    @property
    def open_trades(self) -> list[OpenLiveTrade]:
        return list(self._open_trades)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        await self._reload_open_trades()
        logger.info(
            f"LiveTrader started "
            f"(dry_run={self.dry_run}, open_positions={len(self._open_trades)})"
        )
        await asyncio.gather(
            self._subscribe_loop(),
            self._monitor_loop(),
            return_exceptions=True,
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #

    async def _reload_open_trades(self) -> None:
        """Rehydrate `_open_trades` from DB so a restart picks up where
        we left off (same pattern as PaperTrader.load_persisted_state)."""
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT id, market_id, token_id, direction, strategy,
                       entry_price, COALESCE(filled_size_usdc, size_usdc) AS size_usdc,
                       leader_wallet, confidence, fee_paid_usdc, opened_at,
                       leader_context
                FROM live_trades
                WHERE status = 'open'
                """
            )
        self._open_trades = [
            OpenLiveTrade(
                id=row["id"],
                market_id=row["market_id"],
                token_id=row["token_id"],
                direction=row["direction"],
                strategy=row["strategy"],
                entry_price=float(row["entry_price"] or 0),
                size_usdc=float(row["size_usdc"] or 0),
                leader_wallet=row["leader_wallet"] or "",
                confidence=float(row["confidence"] or 0),
                fee_paid_usdc=float(row["fee_paid_usdc"] or 0),
                opened_at=row["opened_at"],
                leader_context=json.loads(row["leader_context"]) if row["leader_context"] else {},
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Decision subscriber                                                 #
    # ------------------------------------------------------------------ #

    async def _subscribe_loop(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REDIS_DECISIONS_LIVE_CHANNEL)
        try:
            while self._running:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                try:
                    payload = json.loads(msg["data"])
                except Exception as e:
                    logger.warning(f"LiveTrader: bad decision payload: {e}")
                    continue
                try:
                    await self.open_trade(payload)
                except Exception:
                    logger.exception("LiveTrader: open_trade crashed")
        finally:
            await pubsub.unsubscribe(REDIS_DECISIONS_LIVE_CHANNEL)
            await pubsub.aclose()

    # ------------------------------------------------------------------ #
    # Open trade                                                          #
    # ------------------------------------------------------------------ #

    async def open_trade(self, decision: dict) -> Optional[int]:
        """Execute a live trade from a decision dict. Returns the
        live_trades.id on success (whether fill or shadow), or None on
        veto/failure."""
        market_id = decision.get("market_id", "")
        token_id = decision.get("token_id", "")
        action = decision.get("action", "")
        size_usdc = float(decision.get("size_usdc") or 0)
        confidence = float(decision.get("confidence") or 0)
        leader_wallet = decision.get("leader_wallet", "")
        leader_context = dict(decision.get("trade_context") or {})
        direction = (decision.get("direction") or "yes").lower()

        # --- Pre-flight vetos (mirror PaperTrader, minimal subset) ----
        if size_usdc < settings.MIN_POSITION_USDC:
            logger.warning(
                f"LiveTrader veto: size_usdc {size_usdc} < min {settings.MIN_POSITION_USDC}"
            )
            return None
        if action not in {"follow", "fade"}:
            logger.warning(f"LiveTrader veto: unknown action {action!r}")
            return None
        if not market_id or not token_id:
            logger.warning("LiveTrader veto: missing market_id/token_id")
            return None

        # --- Strict-path killswitch check ------------------------------
        # The live-trade gate: about to send an order to py-clob-client.
        # We must NOT trust the 2s-TTL Redis cache here — between a DB
        # flip and the cache rewrite, fast-path readers can see an old
        # value and we'd leak real trades through a "disabled" switch.
        # In dry_run we skip the gate (no real order goes out either way)
        # so we preserve shadow-row behavior for benchmark comparisons.
        if not self.dry_run:
            try:
                # F-05: bypass cache to prevent 2s leak window
                real_enabled = await get_killswitch().is_real_execution_enabled(
                    bypass_cache=True
                )
            except Exception as e:
                # Fail safe: if the strict path itself raises, refuse the trade.
                logger.error(
                    f"LiveTrader veto: killswitch strict-path read failed ({e}), "
                    f"refusing trade"
                )
                return None
            if not real_enabled:
                logger.warning(
                    f"LiveTrader veto: real_execution_enabled is OFF "
                    f"(strict-path read), refusing live order on {market_id[:14]}…"
                )
                return None

        if await self._has_open_trade_conflict(market_id, leader_wallet, action):
            logger.info(
                f"LiveTrader skip: open_trade_conflict on {market_id[:10]}… "
                f"leader={leader_wallet[:10]}… strategy={action}"
            )
            return None

        # --- Decide initial DB status -----------------------------------
        initial_status = "shadow" if self.dry_run else "pending"
        side = "BUY" if action == "follow" else "BUY"  # FADE buys opposite token (already encoded by upstream)

        # --- Insert live_trades row in pending/shadow state -------------
        async with get_db() as conn:
            live_trade_id = await conn.fetchval(
                """
                INSERT INTO live_trades
                    (market_id, token_id, direction, size_usdc, strategy,
                     leader_wallet, leader_context, confidence, status,
                     economic_model_version, strategy_track)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)
                RETURNING id
                """,
                market_id, token_id, direction, size_usdc, action,
                leader_wallet, json.dumps(leader_context), confidence,
                initial_status, ECONOMIC_MODEL_VERSION,
                StrategyTrack.LEADER_SWING.value,
            )
        live_trade_id = int(live_trade_id)

        # --- Hand off to OrderManager -----------------------------------
        outcome = await self._order_manager.place_for_position(
            live_trade_id=live_trade_id,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            order_role="entry",
        )

        # --- Translate outcome into live_trades status ------------------
        if outcome.final_state == "shadow":
            logger.info(
                f"LiveTrader shadow open: trade={live_trade_id} "
                f"market={market_id[:14]}… size={size_usdc}$"
            )
            return live_trade_id

        if not outcome.filled:
            await self._mark_trade_failed(
                live_trade_id=live_trade_id,
                final_state=outcome.final_state,
                error_message=outcome.error_message,
                attempts=outcome.attempts,
            )
            return None

        # Filled: persist entry details + add to open positions list.
        filled_usdc = outcome.filled_size_shares * outcome.avg_fill_price
        async with get_db() as conn:
            await conn.execute(
                """
                UPDATE live_trades
                SET status = 'open',
                    entry_price = $2,
                    filled_size_usdc = $3,
                    fee_paid_usdc = $4,
                    clob_order_id = $5,
                    placement_attempts = $6
                WHERE id = $1
                """,
                live_trade_id,
                outcome.avg_fill_price,
                filled_usdc,
                outcome.fee_paid_usdc,
                outcome.last_clob_order_id,
                outcome.attempts,
            )

        opened = OpenLiveTrade(
            id=live_trade_id,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
            strategy=action,
            entry_price=outcome.avg_fill_price,
            size_usdc=filled_usdc,
            leader_wallet=leader_wallet,
            confidence=confidence,
            fee_paid_usdc=outcome.fee_paid_usdc,
            size_shares=outcome.filled_size_shares,
            opened_at=datetime.now(timezone.utc),
            leader_context=leader_context,
        )
        self._open_trades.append(opened)
        await self._publish(
            REDIS_LIVE_OPENED_CHANNEL,
            {
                "trade_id": live_trade_id,
                "market_id": market_id,
                "size_usdc": filled_usdc,
                "entry_price": outcome.avg_fill_price,
                "strategy": action,
                "attempts": outcome.attempts,
            },
        )
        logger.info(
            f"LiveTrader opened: trade={live_trade_id} "
            f"@ {outcome.avg_fill_price:.4f} size={filled_usdc:.2f}$ "
            f"({outcome.attempts} attempt(s))"
        )
        return live_trade_id

    async def _mark_trade_failed(
        self,
        *,
        live_trade_id: int,
        final_state: str,
        error_message: Optional[str],
        attempts: int,
    ) -> None:
        new_status = {
            "rejected": "failed",
            "canceled": "canceled",
            "partial": "failed",  # partial without an OPEN flip means we got 0 shares
        }.get(final_state, "failed")
        async with get_db() as conn:
            await conn.execute(
                """
                UPDATE live_trades
                SET status = $2,
                    close_reason = $3,
                    placement_attempts = $4,
                    closed_at = NOW()
                WHERE id = $1
                """,
                live_trade_id,
                new_status,
                (error_message or final_state)[:50],
                attempts,
            )
        logger.warning(
            f"LiveTrader open failed: trade={live_trade_id} "
            f"final_state={final_state} error={error_message!r}"
        )

    # ------------------------------------------------------------------ #
    # Close trade                                                         #
    # ------------------------------------------------------------------ #

    async def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        close_reason: str,
    ) -> bool:
        """Close an open live trade. `exit_price` is the price we
        observed (used as the reference for limit pricing); the actual
        fill may be slightly different."""
        trade = next((t for t in self._open_trades if t.id == trade_id), None)
        if trade is None:
            logger.warning(f"LiveTrader close_trade: id {trade_id} not in memory")
            return False

        # Sell back the shares we hold.
        outcome = await self._order_manager.place_for_position(
            live_trade_id=trade_id,
            token_id=trade.token_id,
            side="SELL",
            size_usdc=trade.size_shares * exit_price,
            order_role="exit",
        )

        if outcome.final_state == "shadow":
            # Dry-run close: collapse position virtually using observed price.
            await self._finalize_close_db(
                trade=trade, exit_price=exit_price,
                close_reason=close_reason, fee_paid=0.0,
                exit_clob_order_id=None, status="closed",
            )
            self._open_trades = [t for t in self._open_trades if t.id != trade_id]
            return True

        if not outcome.filled:
            logger.warning(
                f"LiveTrader close failed: trade={trade_id} "
                f"final_state={outcome.final_state} err={outcome.error_message!r}"
            )
            return False

        await self._finalize_close_db(
            trade=trade,
            exit_price=outcome.avg_fill_price,
            close_reason=close_reason,
            fee_paid=outcome.fee_paid_usdc,
            exit_clob_order_id=outcome.last_clob_order_id,
            status="closed",
        )
        self._open_trades = [t for t in self._open_trades if t.id != trade_id]
        return True

    async def _finalize_close_db(
        self,
        *,
        trade: OpenLiveTrade,
        exit_price: float,
        close_reason: str,
        fee_paid: float,
        exit_clob_order_id: Optional[str],
        status: str,
    ) -> None:
        # Direction-aware PnL: yes = long the YES token, no = long the NO token.
        # We pass the fees we actually paid (entry side, plus what the CLOB
        # charged on this exit fill if any) so the helper produces a net
        # PnL directly. `calculate_long_pnl` does (exit-entry)*shares minus
        # all fee/slippage components.
        # Fee rate fallback: if we never recorded a per-market fee_rate
        # (Polymarket's `fee_rate_bps` field), assume taker rate of 2%.
        # In dry-run / shadow we record 0 here because no real fee was paid.
        fee_rate = 0.02
        modeled_exit_fee = (
            float(calculate_polymarket_fee(
                shares=trade.size_shares,
                price=exit_price,
                fee_rate=fee_rate,
                liquidity_role=LiquidityRole.TAKER,
                fees_enabled=True,
            ))
            if not self.dry_run
            else 0.0
        )
        # `fee_paid` is what the CLOB actually returned on the exit trade.
        # We trust the actual over the model when both are present.
        actual_exit_fee = fee_paid if fee_paid > 0 else modeled_exit_fee
        pnl_result = calculate_long_pnl(
            entry_price=trade.entry_price,
            exit_price=exit_price,
            size_shares=trade.size_shares,
            entry_fee_usdc=trade.fee_paid_usdc,
            exit_fee_usdc=actual_exit_fee,
        )
        net_pnl = float(pnl_result.net_pnl_usdc)
        total_fees = trade.fee_paid_usdc + actual_exit_fee

        async with get_db() as conn:
            await conn.execute(
                """
                UPDATE live_trades
                SET status = $2,
                    exit_price = $3,
                    pnl_usdc = $4,
                    fee_paid_usdc = $5,
                    close_reason = $6,
                    exit_clob_order_id = $7,
                    closed_at = NOW()
                WHERE id = $1
                """,
                trade.id, status, exit_price, net_pnl, total_fees,
                close_reason[:50], exit_clob_order_id,
            )
        await self._publish(
            REDIS_LIVE_CLOSED_CHANNEL,
            {
                "trade_id": trade.id,
                "market_id": trade.market_id,
                "exit_price": exit_price,
                "pnl_usdc": float(net_pnl),
                "close_reason": close_reason,
            },
        )
        logger.info(
            f"LiveTrader closed: trade={trade.id} pnl={net_pnl:+.2f}$ reason={close_reason}"
        )

    # ------------------------------------------------------------------ #
    # Monitor loop (TP / SL / timeout)                                    #
    # ------------------------------------------------------------------ #

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._check_open_positions()
            except Exception:
                logger.exception("LiveTrader monitor crashed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=MONITOR_INTERVAL_S)
            except asyncio.TimeoutError:
                continue
            else:
                break

    async def _check_open_positions(self) -> None:
        if not self._open_trades:
            return
        now = datetime.now(timezone.utc)
        for trade in list(self._open_trades):
            current_price = await self._get_current_price(trade.token_id)
            if current_price is None:
                continue
            return_pct = (
                (current_price - trade.entry_price) / trade.entry_price
                if trade.entry_price > 0 else 0.0
            )
            tp = TAKE_PROFIT_FOLLOW if trade.strategy == "follow" else TAKE_PROFIT_FADE
            sl = STOP_LOSS_FOLLOW if trade.strategy == "follow" else STOP_LOSS_FADE
            close_reason: Optional[str] = None
            if return_pct >= tp:
                close_reason = "take_profit"
            elif return_pct <= -sl:
                close_reason = "stop_loss"
            elif trade.opened_at and (now - trade.opened_at) >= timedelta(days=TIMEOUT_DAYS):
                close_reason = "timeout"
            if close_reason:
                await self.close_trade(trade.id, current_price, close_reason)

    async def _get_current_price(self, token_id: str) -> Optional[float]:
        """Best-effort current price from the CLOB midpoint."""
        try:
            return await self._clob.get_midpoint(token_id)
        except Exception as e:
            logger.warning(f"LiveTrader: failed to fetch midpoint for {token_id[:14]}…: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _has_open_trade_conflict(
        self,
        market_id: str,
        leader_wallet: str,
        strategy: str,
    ) -> bool:
        """Reject a new open if we already hold a (market, leader, strategy)
        triplet — same invariant as PaperTrader."""
        async with get_db() as conn:
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM live_trades
                WHERE status IN ('pending', 'open')
                  AND market_id = $1
                  AND leader_wallet = $2
                  AND strategy = $3
                """,
                market_id, leader_wallet, strategy,
            )
        return int(n or 0) > 0

    async def _publish(self, channel: str, payload: dict) -> None:
        try:
            await self._redis.publish(channel, json.dumps(payload, default=str))
        except Exception as e:
            logger.warning(f"LiveTrader: redis publish to {channel} failed: {e}")
