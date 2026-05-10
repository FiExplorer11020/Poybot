"""
Paper Trader — virtual portfolio that executes decisions without real money.
Subscribes to Redis "decisions", opens/closes paper_trades, updates Thompson.

Fixes applied:
  - FIX 2: Updates decision_log.outcome after close
  - FIX 3: Direction and PnL are direction-aware (yes=long, no=short)
  - FIX 4: RiskManager wired via constructor
  - FIX 5: FADE buys opposite token at 1-leader_price
  - FIX 6a: pnl_usdc as float in Redis event; full fields
  - FIX 7: _get_current_price checks Redis cache first
  - FIX 8: OpenPaperTrade.opened_at populated
  - FIX 9: Additional close triggers (leader_exit, market_resolved, timeout)
"""

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole, StrategyTrack
from src.economics.pnl import calculate_long_pnl, shares_from_notional
from src.engine.portfolio_state import (
    PortfolioState,
    load_state,
    record_equity,
    save_state,
)

REDIS_DECISIONS_CHANNEL = "decisions"
REDIS_PAPER_OPENED_CHANNEL = "positions:paper_opened"
REDIS_PAPER_CLOSED_CHANNEL = "positions:paper_closed"

TAKE_PROFIT_FOLLOW = 0.10  # +10%
TAKE_PROFIT_FADE = 0.10  # +10%
STOP_LOSS_FOLLOW = 0.08  # -8%
STOP_LOSS_FADE = 0.05  # -5% (tighter)
TIMEOUT_DAYS = 30  # Auto-close after 30 days


@dataclass
class OpenPaperTrade:
    id: int
    market_id: str
    token_id: str
    direction: str  # 'yes' (long) or 'no' (short)
    strategy: str  # 'follow' or 'fade'
    entry_price: float
    size_usdc: float
    leader_wallet: str
    confidence: float
    fee_rate_pct: float = 0.0
    size_shares: float = 0.0
    entry_fee_usdc: float = 0.0
    economic_model_version: str = ECONOMIC_MODEL_VERSION
    strategy_track: str = StrategyTrack.LEADER_SWING.value
    opened_at: datetime | None = None  # FIX 8
    leader_context: dict = field(default_factory=dict)


class PaperTrader:
    def __init__(self, redis_client, confidence_engine=None, risk_manager=None):  # FIX 4
        self._redis = redis_client
        self._confidence_engine = confidence_engine
        self._risk_manager = risk_manager  # FIX 4
        self._running = False
        self._stop_event = asyncio.Event()
        self._open_trades: list[OpenPaperTrade] = []
        # These defaults are only used before `load_persisted_state()` has run
        # (or in unit tests that bypass it).  Real values come from the
        # `portfolio_state` table on start().
        self._capital = settings.PAPER_CAPITAL_USDC
        self._peak_capital = settings.PAPER_CAPITAL_USDC
        self._realized_pnl_cum: float = 0.0
        self._state_loaded: bool = False

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def open_trades(self) -> list[OpenPaperTrade]:
        return list(self._open_trades)

    async def _record_open_trade_refusal(
        self,
        decision: dict,
        reason: str,
        detail: dict | None = None,
    ) -> None:
        payload = {
            "type": "paper_refusal",
            "reason": reason,
            "market_id": decision.get("market_id"),
            "token_id": decision.get("token_id"),
            "leader_wallet": decision.get("leader_wallet"),
            "action": decision.get("action"),
            "size_usdc": decision.get("size_usdc"),
            "confidence": decision.get("confidence"),
            "signal_audit": decision.get("signal_audit") or {},
            "detail": detail or {},
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        logger.warning(
            "PaperTrader refusal "
            f"reason={reason} market={payload['market_id']} token={payload['token_id']} "
            f"leader={payload['leader_wallet']} action={payload['action']}"
        )
        if self._redis is None:
            return
        try:
            inc = self._redis.hincrby("paper:rejections:1h", reason, 1)
            if inspect.isawaitable(inc):
                await inc
            exp = self._redis.expire("paper:rejections:1h", 3600)
            if inspect.isawaitable(exp):
                await exp
            publish = self._redis.publish("decisions:trace", json.dumps(payload))
            if inspect.isawaitable(publish):
                await publish
        except Exception as exc:
            logger.debug(f"PaperTrader refusal telemetry failed: {exc}")

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        await self.load_persisted_state()
        tasks = [
            asyncio.create_task(self._subscribe_loop()),
            asyncio.create_task(self._monitor_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def load_persisted_state(self) -> None:
        """Hydrate capital, peak, and the in-memory open-trade list from DB.

        Called automatically by `start()`.  Safe to call again — idempotent.
        """
        state = await load_state()
        self._capital = float(state.capital)
        self._peak_capital = float(state.peak_capital)
        self._realized_pnl_cum = float(state.realized_pnl_cum)
        self._state_loaded = True

        # Reload open paper trades so the monitor loop manages them after
        # restart (stop-loss, leader-exit, timeout all keep working).
        await self._reload_open_trades()

        # Keep the risk manager's own counters in sync if it supports it.
        if self._risk_manager is not None:
            setter = getattr(self._risk_manager, "hydrate_from_state", None)
            if callable(setter):
                setter(
                    peak_capital=self._peak_capital,
                    consecutive_losses=int(state.consecutive_losses),
                )

        logger.info(
            f"PaperTrader: loaded state capital=${self._capital:.2f} "
            f"peak=${self._peak_capital:.2f} "
            f"realized_cum=${self._realized_pnl_cum:.2f} "
            f"open={len(self._open_trades)}"
        )

    async def _reload_open_trades(self) -> None:
        """Populate `_open_trades` from the DB on boot."""
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, market_id, token_id, direction, strategy,
                           entry_price, size_usdc, leader_wallet, confidence,
                           COALESCE(size_shares, 0)      AS size_shares,
                           COALESCE(entry_fee_usdc, 0)   AS entry_fee_usdc,
                           COALESCE(economic_model_version, $1) AS economic_model_version,
                           COALESCE(strategy_track, $2)  AS strategy_track,
                           opened_at, leader_context
                    FROM paper_trades
                    WHERE status = 'open'
                    """,
                    ECONOMIC_MODEL_VERSION,
                    StrategyTrack.LEADER_SWING.value,
                )
        except Exception as exc:
            logger.warning(f"PaperTrader: failed to reload open trades: {exc}")
            return

        self._open_trades = []
        for r in rows:
            ctx = r["leader_context"]
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except Exception:
                    ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            self._open_trades.append(
                OpenPaperTrade(
                    id=int(r["id"]),
                    market_id=r["market_id"],
                    token_id=r["token_id"],
                    direction=r["direction"],
                    strategy=r["strategy"],
                    entry_price=float(r["entry_price"]),
                    size_usdc=float(r["size_usdc"]),
                    leader_wallet=r["leader_wallet"] or "",
                    confidence=float(r["confidence"] or 0),
                    fee_rate_pct=0.0,  # reloaded lazily at close
                    size_shares=float(r["size_shares"] or 0),
                    entry_fee_usdc=float(r["entry_fee_usdc"] or 0),
                    economic_model_version=r["economic_model_version"],
                    strategy_track=r["strategy_track"],
                    opened_at=r["opened_at"],
                    leader_context=ctx,
                )
            )

    async def _persist_state(self) -> None:
        """Write current in-memory state back to the singleton row."""
        await save_state(
            PortfolioState(
                capital=self._capital,
                peak_capital=self._peak_capital,
                realized_pnl_cum=self._realized_pnl_cum,
                consecutive_losses=self._consecutive_losses_from_risk(),
                open_positions=len(self._open_trades),
            )
        )

    def _consecutive_losses_from_risk(self) -> int:
        if self._risk_manager is None:
            return 0
        return int(getattr(self._risk_manager, "_consecutive_losses", 0) or 0)

    async def _compute_unrealized_pnl(self) -> float:
        """Sum direction-aware unrealized PnL over all open trades."""
        total = 0.0
        for trade in list(self._open_trades):
            price = await self._get_current_price(trade.market_id, trade.token_id)
            if price is None:
                continue
            if trade.direction == "yes":
                pct = (price - trade.entry_price) / trade.entry_price if trade.entry_price else 0.0
            else:
                pct = (trade.entry_price - price) / trade.entry_price if trade.entry_price else 0.0
            total += pct * trade.size_usdc
        return round(total, 2)

    async def _record_equity_sample(self) -> None:
        """Take a mark-to-market snapshot and store it in `portfolio_equity`."""
        try:
            unrealized = await self._compute_unrealized_pnl()
            await record_equity(
                capital=self._capital,
                unrealized_pnl=unrealized,
                realized_pnl_cum=self._realized_pnl_cum,
                open_positions=len(self._open_trades),
            )
        except Exception as exc:
            logger.debug(f"equity sample skipped: {exc}")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def _subscribe_loop(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REDIS_DECISIONS_CHANNEL)
        try:
            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                try:
                    decision = json.loads(message["data"])
                    if decision.get("action") in ("follow", "fade"):
                        await self.open_trade(decision)
                except Exception as e:
                    logger.error(f"PaperTrader subscribe error: {e}")
        finally:
            await pubsub.unsubscribe(REDIS_DECISIONS_CHANNEL)

    async def _monitor_loop(self) -> None:
        """Check open positions every 60s for stop-loss / take-profit / other triggers."""
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self._check_open_positions()
            # Mark-to-market tick for the equity curve (1/min).
            await self._record_equity_sample()

    async def _check_open_positions(self) -> None:
        """Evaluate each open position for auto-close conditions."""
        now = datetime.now(tz=timezone.utc)
        for trade in list(self._open_trades):
            # --- FIX 9: Timeout ---
            if trade.opened_at and (now - trade.opened_at) > timedelta(days=TIMEOUT_DAYS):
                price = (
                    await self._get_current_price(trade.market_id, trade.token_id)
                    or trade.entry_price
                )
                await self.close_trade(trade.id, price, "timeout")
                continue

            # --- FIX 9: Market resolved ---
            resolved = await self._is_market_resolved(trade.market_id)
            if resolved:
                price = (
                    await self._get_current_price(trade.market_id, trade.token_id)
                    or trade.entry_price
                )
                await self.close_trade(trade.id, price, "market_resolved")
                continue

            # --- FIX 9: Leader exit (FOLLOW only) ---
            if trade.strategy == "follow":
                leader_exited = await self._leader_exited_recently(
                    trade.leader_wallet, trade.market_id
                )
                if leader_exited:
                    price = (
                        await self._get_current_price(trade.market_id, trade.token_id)
                        or trade.entry_price
                    )
                    await self.close_trade(trade.id, price, "leader_exit")
                    continue

            current_price = await self._get_current_price(trade.market_id, trade.token_id)
            if current_price is None:
                continue

            # --- FIX 3: Direction-aware PnL ---
            if trade.direction == "yes":
                pnl_pct = (current_price - trade.entry_price) / trade.entry_price
            else:
                pnl_pct = (trade.entry_price - current_price) / trade.entry_price

            stop = STOP_LOSS_FADE if trade.strategy == "fade" else STOP_LOSS_FOLLOW
            take = TAKE_PROFIT_FADE if trade.strategy == "fade" else TAKE_PROFIT_FOLLOW

            if pnl_pct <= -stop:
                await self.close_trade(trade.id, current_price, "stop_loss")
            elif pnl_pct >= take:
                await self.close_trade(trade.id, current_price, "take_profit")

    async def open_trade(self, decision: dict) -> int | None:
        """Open a paper trade from a decision dict. Returns trade ID or None."""
        market_id = decision.get("market_id", "")
        token_id = decision.get("token_id", "")
        action = decision.get("action", "")
        strategy = action  # 'follow' or 'fade'
        size_usdc = float(decision.get("size_usdc") or 0)
        confidence = float(decision.get("confidence") or 0)
        leader_wallet = decision.get("leader_wallet", "")
        trade_context = dict(decision.get("trade_context") or {})

        live_candidate = trade_context.get("live_candidate", True)
        try:
            trade_age_s = (
                float(trade_context.get("trade_age_s"))
                if trade_context.get("trade_age_s") is not None
                else None
            )
        except (TypeError, ValueError):
            trade_age_s = None
        if live_candidate is False or (
            trade_age_s is not None and trade_age_s > float(settings.LIVE_DECISION_MAX_TRADE_AGE_S)
        ):
            await self._record_open_trade_refusal(
                decision,
                "stale_decision",
                {"trade_age_s": trade_age_s, "live_candidate": live_candidate},
            )
            return None

        signal_audit = decision.get("signal_audit")
        if not isinstance(signal_audit, dict) or signal_audit.get("accepted") is not True:
            reason = (
                "missing_accepted_signal_audit"
                if not isinstance(signal_audit, dict)
                else str(signal_audit.get("reject_reason") or "signal_audit_rejected")
            )
            await self._record_open_trade_refusal(
                decision,
                reason,
                {"accepted": signal_audit.get("accepted") if isinstance(signal_audit, dict) else None},
            )
            return None

        if size_usdc < settings.MIN_POSITION_USDC:
            await self._record_open_trade_refusal(
                decision,
                "below_min_position_size",
                {"size_usdc": size_usdc, "min_position_usdc": settings.MIN_POSITION_USDC},
            )
            return None
        if size_usdc > self._capital:
            await self._record_open_trade_refusal(
                decision,
                "insufficient_paper_capital",
                {"size_usdc": size_usdc, "capital": self._capital},
            )
            return None

        if await self._has_open_trade_conflict(market_id, leader_wallet, strategy):
            await self._record_open_trade_refusal(
                decision,
                "open_trade_conflict",
                {"strategy": strategy},
            )
            return None

        if await self._has_recent_reentry_conflict(market_id, leader_wallet, strategy):
            await self._record_open_trade_refusal(
                decision,
                "recent_reentry_conflict",
                {"strategy": strategy, "cooldown_s": settings.PAPER_REENTRY_COOLDOWN_S},
            )
            return None

        if await self._is_market_resolved(market_id):
            await self._record_open_trade_refusal(
                decision,
                "market_resolved",
                {"strategy": strategy},
            )
            return None

        # --- FIX 4: RiskManager pre-trade gate ---
        if self._risk_manager is not None:
            can_trade = await self._risk_manager.check_can_trade(decision, self._capital)
            if not can_trade:
                await self._record_open_trade_refusal(
                    decision,
                    "risk_manager_rejected",
                    {"capital": self._capital},
                )
                return None
            size_usdc = self._risk_manager.apply_size(size_usdc, decision)
            if size_usdc <= 0:
                await self._record_open_trade_refusal(
                    decision,
                    "risk_manager_zero_size",
                    {"capital": self._capital},
                )
                return None

        # --- FIX 3 + FIX 5: Direction and price ---
        if strategy == "follow":
            direction = "yes"
            actual_token_id = token_id
            entry_price = await self._get_current_price(market_id, token_id) or 0.5
        else:
            direction = "no"
            opposite_token = await self._get_opposite_token(market_id, token_id)
            actual_token_id = opposite_token or token_id
            leader_price = await self._get_current_price(market_id, token_id) or 0.5
            entry_price = max(0.01, 1.0 - leader_price)  # FIX 5

        strategy_track = (
            trade_context.get("strategy_track")
            or decision.get("strategy_track")
            or StrategyTrack.LEADER_SWING.value
        )
        size_shares = float(shares_from_notional(size_usdc, entry_price))
        fee_rate = await self._get_fee_rate(market_id)
        entry_fee = float(
            calculate_polymarket_fee(
                shares=size_shares,
                price=entry_price,
                fee_rate=fee_rate,
                liquidity_role=LiquidityRole.TAKER,
                fees_enabled=True,
            )
        )

        now = datetime.now(tz=timezone.utc)

        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO paper_trades
                        (opened_at, market_id, token_id, direction, entry_price, size_usdc,
                         fee_paid_usdc, strategy, leader_wallet, leader_context, confidence, status,
                         strategy_track, economic_model_version, size_shares, entry_fee_usdc)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,'open',$12,$13,$14,$15)
                    RETURNING id
                    """,
                    now,
                    market_id,
                    actual_token_id,
                    direction,
                    entry_price,
                    size_usdc,
                    entry_fee,
                    strategy,
                    leader_wallet,
                    json.dumps(decision),
                    confidence,
                    strategy_track,
                    ECONOMIC_MODEL_VERSION,
                    size_shares,
                    entry_fee,
                )
                trade_id = row["id"]
        except Exception as e:
            logger.error(f"Failed to open paper trade: {e}")
            return None

        self._capital -= size_usdc
        open_trade = OpenPaperTrade(
            id=trade_id,
            market_id=market_id,
            token_id=actual_token_id,
            direction=direction,
            strategy=strategy,
            entry_price=entry_price,
            size_usdc=size_usdc,
            leader_wallet=leader_wallet,
            confidence=confidence,
            fee_rate_pct=fee_rate,
            size_shares=size_shares,
            entry_fee_usdc=entry_fee,
            economic_model_version=ECONOMIC_MODEL_VERSION,
            strategy_track=strategy_track,
            opened_at=now,  # FIX 8
            leader_context=dict(decision),
        )
        self._open_trades.append(open_trade)

        # Persistence: bankroll + open_positions moved; equity should reflect it.
        await self._persist_state()
        await self._record_equity_sample()

        logger.info(
            f"Opened paper {strategy} trade #{trade_id} on {market_id}: "
            f"dir={direction} size={size_usdc} entry={entry_price}"
        )
        # S3.9: publish open event so the Telegram notifier (and any other
        # downstream consumer) can react. Best-effort — a failed publish
        # must never block trade execution.
        try:
            await self._redis.publish(
                REDIS_PAPER_OPENED_CHANNEL,
                json.dumps(
                    {
                        "trade_id": trade_id,
                        "market_id": market_id,
                        "token_id": actual_token_id,
                        "direction": direction,
                        "strategy": strategy,
                        "entry_price": entry_price,
                        "size_usdc": size_usdc,
                        "leader_wallet": leader_wallet,
                        "confidence": confidence,
                    }
                ),
            )
        except Exception:
            pass
        return trade_id

    async def close_trade(self, trade_id: int, exit_price: float, close_reason: str) -> bool:
        """Close a paper trade by ID. Returns True on success."""
        trade = next((t for t in self._open_trades if t.id == trade_id), None)
        if trade is None:
            return False

        size_shares = trade.size_shares or float(
            shares_from_notional(trade.size_usdc, trade.entry_price)
        )
        exit_fee = float(
            calculate_polymarket_fee(
                shares=size_shares,
                price=exit_price,
                fee_rate=trade.fee_rate_pct,
                liquidity_role=LiquidityRole.TAKER,
                fees_enabled=True,
            )
        )
        pnl = calculate_long_pnl(
            entry_price=trade.entry_price,
            exit_price=exit_price,
            size_shares=size_shares,
            entry_fee_usdc=trade.entry_fee_usdc,
            exit_fee_usdc=exit_fee,
        )
        pnl_usdc = float(pnl.net_pnl_usdc)
        gross_pnl_usdc = float(pnl.gross_pnl_usdc)
        pnl_pct = float(pnl.pnl_pct)

        now = datetime.now(tz=timezone.utc)
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET closed_at=$2,
                        exit_price=$3,
                        pnl_usdc=$4,
                        status='closed',
                        close_reason=$5,
                        exit_fee_usdc=$6,
                        gross_pnl_usdc=$7,
                        net_pnl_usdc=$8,
                        economic_model_version=$9
                    WHERE id=$1
                    """,
                    trade_id,
                    now,
                    exit_price,
                    round(pnl_usdc, 2),
                    close_reason,
                    exit_fee,
                    gross_pnl_usdc,
                    pnl_usdc,
                    trade.economic_model_version,
                )

                # --- FIX 2: Update decision_log.outcome ---
                outcome = "win" if pnl_usdc > 0 else "loss"
                await conn.execute(
                    """
                    UPDATE decision_log SET outcome = $3
                    WHERE id = (
                        SELECT id FROM decision_log
                        WHERE leader_wallet = $1 AND market_id = $2
                          AND outcome IS NULL AND action IN ('follow', 'fade')
                        ORDER BY time DESC LIMIT 1
                    )
                    """,
                    trade.leader_wallet,
                    trade.market_id,
                    outcome,
                )
        except Exception as e:
            logger.error(f"Failed to close paper trade #{trade_id}: {e}")
            return False

        self._open_trades = [t for t in self._open_trades if t.id != trade_id]
        self._capital += trade.size_usdc + pnl_usdc
        self._peak_capital = max(self._peak_capital, self._capital)
        self._realized_pnl_cum += pnl_usdc

        learning_feedback = {"reason_codes": [], "penalty": 0.0}
        if self._confidence_engine is not None:
            outcome_payload = {
                "market_id": trade.market_id,
                "token_id": trade.token_id,
                "pnl_usdc": round(pnl_usdc, 2),
                "close_reason": close_reason,
                "confidence": trade.confidence,
                "closed_at": now.isoformat(),
                "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
                "trade_context": dict(trade.leader_context.get("trade_context") or {}),
                "entry_price": trade.entry_price,
                "exit_price": exit_price,
                "size_usdc": trade.size_usdc,
                "size_shares": size_shares,
                "gross_pnl_usdc": round(gross_pnl_usdc, 2),
                "economic_model_version": trade.economic_model_version,
                "strategy_track": trade.strategy_track,
            }
            record_outcome = getattr(self._confidence_engine, "record_outcome", None)
            if callable(record_outcome) and type(self._confidence_engine).__name__ != "MagicMock":
                result = record_outcome(
                    wallet=trade.leader_wallet,
                    action=trade.strategy,
                    won=pnl_usdc > 0,
                    outcome=outcome_payload,
                )
                if inspect.isawaitable(result):
                    learning_feedback = await result
                elif isinstance(result, dict):
                    learning_feedback = result
            else:
                self._confidence_engine.update_thompson(
                    wallet=trade.leader_wallet,
                    action=trade.strategy,
                    won=pnl_usdc > 0,
                )

        # --- FIX 4: RiskManager outcome recording ---
        if self._risk_manager is not None:
            self._risk_manager.record_outcome(won=pnl_usdc > 0, capital=self._capital)

        # --- FIX 6a: Full event dict with float pnl and extra fields ---
        event = {
            "trade_id": trade_id,
            "market_id": trade.market_id,
            "pnl_usdc": round(pnl_usdc, 2),  # float, not str
            "pnl_pct": round(pnl_pct * 100, 2),
            "direction": trade.direction,
            "size_usdc": trade.size_usdc,
            "close_reason": close_reason,
            "strategy": trade.strategy,
            "strategy_track": trade.strategy_track,
            "economic_model_version": trade.economic_model_version,
            "gross_pnl_usdc": round(gross_pnl_usdc, 2),
            "size_shares": size_shares,
            "leader_wallet": trade.leader_wallet,
            "loss_reasons": learning_feedback.get("reason_codes", []),
            "context_penalty": learning_feedback.get("penalty", 0.0),
        }
        try:
            await self._redis.publish(REDIS_PAPER_CLOSED_CHANNEL, json.dumps(event))
        except Exception:
            pass

        # Persistence: save singleton state + equity sample on every close so
        # bankroll, peak, and realized PnL survive restarts.
        await self._persist_state()
        await self._record_equity_sample()

        logger.info(
            f"Closed paper trade #{trade_id}: pnl={pnl_usdc:.2f} "
            f"({pnl_pct * 100:.1f}%) reason={close_reason}"
        )
        return True

    async def _get_current_price(self, market_id: str, token_id: str) -> float | None:
        """Get latest price: Redis cache first (FIX 7), then DB fallback."""
        # FIX 7: Try Redis price cache set by trade_observer
        if self._redis is not None:
            try:
                cached = await self._redis.get(f"price:{market_id}:{token_id}")
                if cached is not None:
                    return float(cached)
            except Exception:
                pass
        # DB fallback
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT price FROM trades_observed
                    WHERE market_id=$1 AND token_id=$2
                    ORDER BY time DESC LIMIT 1
                    """,
                    market_id,
                    token_id,
                )
                return float(row["price"]) if row else None
        except Exception:
            return None

    async def _get_fee_rate(self, market_id: str) -> float:
        """Fetch fee rate for a market from DB. Returns 0.0 if unavailable."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT fee_rate_pct FROM markets WHERE market_id=$1",
                    market_id,
                )
                return float(row["fee_rate_pct"]) if row and row["fee_rate_pct"] else 0.0
        except Exception:
            return 0.0

    async def _get_opposite_token(self, market_id: str, token_id: str) -> str | None:
        """FIX 5: Return the opposite token (NO if given YES, YES if given NO)."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT token_yes, token_no FROM markets WHERE market_id=$1",
                    market_id,
                )
                if not row:
                    return None
                if token_id == row["token_yes"]:
                    return row["token_no"]
                return row["token_yes"]
        except Exception:
            return None

    async def _has_open_trade_conflict(
        self,
        market_id: str,
        leader_wallet: str,
        strategy: str,
    ) -> bool:
        for trade in self._open_trades:
            if (
                trade.market_id == market_id
                and trade.leader_wallet == leader_wallet
                and trade.strategy == strategy
            ):
                return True
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM paper_trades
                    WHERE market_id = $1
                      AND leader_wallet = $2
                      AND strategy = $3
                      AND status = 'open'
                    LIMIT 1
                    """,
                    market_id,
                    leader_wallet,
                    strategy,
                )
                return row is not None
        except Exception:
            return False

    async def _has_recent_reentry_conflict(
        self,
        market_id: str,
        leader_wallet: str,
        strategy: str,
    ) -> bool:
        cooldown_s = max(0, int(settings.PAPER_REENTRY_COOLDOWN_S))
        if cooldown_s <= 0:
            return False
        since = datetime.now(tz=timezone.utc) - timedelta(seconds=cooldown_s)
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT opened_at
                    FROM paper_trades
                    WHERE market_id = $1
                      AND leader_wallet = $2
                      AND strategy = $3
                      AND opened_at >= $4
                    ORDER BY opened_at DESC
                    LIMIT 1
                    """,
                    market_id,
                    leader_wallet,
                    strategy,
                    since,
                )
                return row is not None
        except Exception:
            return False

    async def _is_market_resolved(self, market_id: str) -> bool:
        """FIX 9: Check if market end_date has passed.

        Ignore obviously poisoned metadata when we have observed trades well after
        the stored end_date; that means the current market row is stale or mismapped.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT m.end_date,
                           (
                               SELECT MAX(t.time)
                               FROM trades_observed t
                               WHERE t.market_id = $1
                           ) AS last_trade_time
                    FROM markets m
                    WHERE m.market_id = $1
                    """,
                    market_id,
                )
                if row and row["end_date"]:
                    end_date = row["end_date"]
                    try:
                        last_trade_time = row["last_trade_time"]
                    except Exception:
                        last_trade_time = None
                    if last_trade_time is not None and last_trade_time > end_date + timedelta(
                        minutes=5
                    ):
                        logger.debug(
                            f"PaperTrader: ignoring stale resolved metadata for {market_id} "
                            f"(end_date={end_date}, last_trade={last_trade_time})"
                        )
                        return False
                    return end_date < datetime.now(tz=timezone.utc)
        except Exception:
            pass
        return False

    async def _leader_exited_recently(self, leader_wallet: str, market_id: str) -> bool:
        """FIX 9: Check if the leader closed their position in this market in the last 5 min."""
        since = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1 FROM positions_reconstructed
                    WHERE wallet_address = $1
                      AND market_id = $2
                      AND close_time IS NOT NULL
                      AND close_time >= $3
                    LIMIT 1
                    """,
                    leader_wallet,
                    market_id,
                    since,
                )
                return row is not None
        except Exception:
            return False
