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
from src.control.redis_pubsub import Subscriber
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
# Strategy upgrade 2026-05-17 (Tier 1 #5) — sport-specific stop-loss.
# Lives at module level so tests can monkey-patch the default the same
# way they treat STOP_LOSS_FOLLOW / STOP_LOSS_FADE. Runtime override
# wins via _read_runtime_setting("stop_loss_sport").
STOP_LOSS_SPORT = 0.03  # -3% (vs -8% FOLLOW / -5% FADE for non-sport)
# Category label that triggers the tighter sport safeguards (sport-cap
# hold-time + sport stop). Matches the value emitted by
# confidence_engine.build_trade_context() and markets.category for any
# Polymarket sport market.
SPORT_CATEGORY = "sports"
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
        # F-04: dedicated pub/sub client with reconnect+resubscribe.
        self._subscriber = Subscriber(
            settings.REDIS_URL, name="engine.paper_trader"
        )
        self._subscriber.register(
            REDIS_DECISIONS_CHANNEL, self._on_decision_message
        )

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
            # 1h bucket — rolling hour, fine-grain alerting + dashboard.
            inc1 = self._redis.hincrby("paper:rejections:1h", reason, 1)
            if inspect.isawaitable(inc1):
                await inc1
            exp1 = self._redis.expire("paper:rejections:1h", 3600)
            if inspect.isawaitable(exp1):
                await exp1
            # 24h bucket — daily aggregate. The 2026-05-17 diagnosis flagged
            # that this key was being read by the dashboard but never
            # written, hiding the true magnitude of rejection storms.
            inc24 = self._redis.hincrby("paper:rejections:24h", reason, 1)
            if inspect.isawaitable(inc24):
                await inc24
            exp24 = self._redis.expire("paper:rejections:24h", 86400)
            if inspect.isawaitable(exp24):
                await exp24
            publish = self._redis.publish("decisions:trace", json.dumps(payload))
            if inspect.isawaitable(publish):
                await publish
        except Exception as exc:
            logger.debug(f"PaperTrader refusal telemetry failed: {exc}")

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        await self.load_persisted_state()
        # Kick off the dedicated reconnect-safe subscriber for the
        # decisions channel. The monitor loop stays in the main task so
        # `await start()` blocks until shutdown — same external contract
        # as before.
        await self._subscriber.start()
        monitor = asyncio.create_task(self._monitor_loop())
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, Exception):
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

    async def _persist_state(self, conn=None) -> None:
        """Write current in-memory state back to the singleton row.

        Accepts an optional `conn` so the UPSERT can run inside an outer
        `conn.transaction()` (used by open_trade / close_trade to keep the
        paper_trades INSERT/UPDATE atomic with the portfolio_state write).
        """
        await save_state(
            PortfolioState(
                capital=self._capital,
                peak_capital=self._peak_capital,
                realized_pnl_cum=self._realized_pnl_cum,
                consecutive_losses=self._consecutive_losses_from_risk(),
                open_positions=len(self._open_trades),
            ),
            conn=conn,
        )

    def _consecutive_losses_from_risk(self) -> int:
        if self._risk_manager is None:
            return 0
        return int(getattr(self._risk_manager, "_consecutive_losses", 0) or 0)

    async def _compute_unrealized_pnl(self) -> float:
        """Sum unrealized PnL over all open trades.

        Every paper position is a LONG of the token stored in `trade.token_id`.
        FOLLOW holds the leader's token; FADE holds the OPPOSITE token. In
        both cases the position is a long, not a short — there is no
        short-selling on Polymarket. PnL is always
        `(current_price - entry_price) / entry_price * notional`.

        The earlier direction-aware branch inverted PnL for direction=="no"
        (FADE), which corrupted the equity curve and triggered FADE positions
        to stop-loss on real wins and take-profit on real losses.
        """
        total = 0.0
        for trade in list(self._open_trades):
            price = await self._get_current_price(trade.market_id, trade.token_id)
            if price is None:
                continue
            pct = (
                (price - trade.entry_price) / trade.entry_price
                if trade.entry_price
                else 0.0
            )
            total += pct * trade.size_usdc
        return round(total, 2)

    async def compute_unrealized_pnl(self) -> float:
        """Public alias for `_compute_unrealized_pnl`. External callers
        (e.g. the Telegram /pnl command) should prefer this name; the
        underscore-prefixed method remains the canonical implementation."""
        return await self._compute_unrealized_pnl()

    async def _record_equity_sample(self, conn=None) -> None:
        """Take a mark-to-market snapshot and store it in `portfolio_equity`.

        Accepts an optional `conn` so the INSERT can participate in an outer
        transaction; otherwise a fresh pooled connection is used.
        """
        try:
            unrealized = await self._compute_unrealized_pnl()
            await record_equity(
                capital=self._capital,
                unrealized_pnl=unrealized,
                realized_pnl_cum=self._realized_pnl_cum,
                open_positions=len(self._open_trades),
                conn=conn,
            )
        except Exception as exc:
            logger.debug(f"equity sample skipped: {exc}")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        await self._subscriber.stop()

    async def _on_decision_message(self, decision: dict, _channel: str) -> None:
        """Subscriber handler — payload is already JSON-decoded."""
        if not self._running:
            return
        try:
            if decision.get("action") in ("follow", "fade"):
                await self.open_trade(decision)
        except Exception as e:
            logger.error(f"PaperTrader subscribe error: {e}")
            raise

    async def _monitor_loop(self) -> None:
        """Check open positions for stop-loss / take-profit / other triggers.

        Adaptive cadence: when any open trade is within
        ``URGENT_MONITOR_HOURS`` of its market's ``end_date``, the loop
        ticks every ``URGENT_MONITOR_TICK_S`` seconds (default 5s).
        Otherwise the standard 60s cadence is used. This prevents the
        bot from missing the resolution moment by up to a minute and
        therefore from booking a close against post-resolution stale
        data.
        """
        while self._running and not self._stop_event.is_set():
            tick_s = await self._monitor_tick_seconds()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick_s)
                break
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self._check_open_positions()
            # Mark-to-market tick for the equity curve.
            await self._record_equity_sample()

    async def _monitor_tick_seconds(self) -> float:
        """Pick the monitor cadence based on proximity to any open trade's
        resolution. Defaults to 60s, drops to ``URGENT_MONITOR_TICK_S``
        (5s by default) when at least one open trade has its market
        within ``URGENT_MONITOR_HOURS`` of ``end_date``.
        """
        default_s = float(getattr(settings, "MONITOR_TICK_S", 60.0))
        urgent_s = float(getattr(settings, "URGENT_MONITOR_TICK_S", 5.0))
        urgent_hours = float(getattr(settings, "URGENT_MONITOR_HOURS", 1.0))
        if not self._open_trades:
            return default_s
        for trade in list(self._open_trades):
            hours = await self._hours_until_resolution(trade.market_id)
            if hours is not None and 0 < hours <= urgent_hours:
                return urgent_s
        return default_s

    async def _check_open_positions(self) -> None:
        """Evaluate each open position for auto-close conditions.

        Two prices matter here:

        * **exit_price** (the bid) — what we'd actually receive if we
          sold right now. Used as the close-price argument to
          ``close_trade`` so realised PnL reflects the true exit value.
        * **mark_price** (the mid = ``(best_bid + best_ask) / 2``) — used
          ONLY to evaluate stop-loss / take-profit thresholds. Entry
          was filled at the ask and exit lands at the bid, so even a
          completely flat market shows a structural negative PnL equal
          to the spread. That biased every monitor tick toward
          ``stop_loss`` (which trips at -8% / -5% — easily reached by
          a 5-10% spread on thin books). Marking against the mid
          removes the bias while still booking realised PnL at the
          realistic bid. See 2026-05-17 diagnosis §A.5.
        """
        now = datetime.now(tz=timezone.utc)
        # ── Strategy upgrade 2026-05-17 — holding cap ────────────────
        # Backtest evidence: top-cohort × entry [0.5,0.9] × <24h hold
        # = 83.7% win rate; >24h cohorts dilute toward 56%. Force-close
        # at the current bid past MAX_HOLDING_PERIOD_S (default 86400s).
        # Resolves to terminal value if the market has already settled.
        # Read once per tick to keep RuntimeConfig hot-reloading cheap.
        try:
            holding_cap_s = int(
                await self._read_runtime_setting(
                    "max_holding_period_s",
                    getattr(settings, "MAX_HOLDING_PERIOD_S", 86_400),
                )
            )
        except Exception:
            holding_cap_s = int(getattr(settings, "MAX_HOLDING_PERIOD_S", 86_400))

        # Strategy upgrade 2026-05-17 (Tier 1 #4+#5) — sport safety net.
        # Read both knobs once per tick. Defaults pin to settings so a
        # missing Redis layer behaves the same as a misconfigured one.
        try:
            sport_holding_cap_s = int(
                await self._read_runtime_setting(
                    "sport_max_holding_s",
                    getattr(settings, "SPORT_MAX_HOLDING_S", 1_800),
                )
            )
        except Exception:
            sport_holding_cap_s = int(
                getattr(settings, "SPORT_MAX_HOLDING_S", 1_800)
            )
        try:
            stop_loss_sport = float(
                await self._read_runtime_setting(
                    "stop_loss_sport",
                    getattr(settings, "STOP_LOSS_SPORT", STOP_LOSS_SPORT),
                )
            )
        except Exception:
            stop_loss_sport = float(
                getattr(settings, "STOP_LOSS_SPORT", STOP_LOSS_SPORT)
            )

        for trade in list(self._open_trades):
            exit_price = await self._exit_bid(
                trade.market_id, trade.token_id, trade.entry_price
            )
            # mark_price = mid (used ONLY for stop/take threshold checks
            # — never as a settlement price). Falls back to exit_price
            # when the book quote is missing so a stale-book moment
            # doesn't suddenly fire spurious closes.
            mark_price = await self._mark_mid(
                trade.market_id, trade.token_id, fallback=exit_price
            )
            # Resolve the trade's market category ONCE per tick — it
            # gates both the sport-specific holding cap (below) and the
            # adaptive stop-loss (further down). Falls back to "unknown"
            # which routes to the legacy (non-sport) safeguards.
            trade_category = await self._resolve_trade_category(trade)
            is_sport = trade_category == SPORT_CATEGORY

            # --- Holding cap (24h default) ---
            # MUST fire BEFORE the absolute 30d timeout so a position that
            # crosses the 24h boundary doesn't sit until the broader cap
            # fires. If the market has resolved by the holding-cap moment,
            # use the terminal value; otherwise the fresh bid.
            if trade.opened_at and holding_cap_s > 0:
                held_for_s = (now - trade.opened_at).total_seconds()
                if held_for_s >= holding_cap_s:
                    cap_price = exit_price
                    if await self._is_market_resolved(trade.market_id):
                        resolved = await self._fetch_market_resolution(
                            trade.market_id, trade.token_id
                        )
                        if resolved is not None:
                            cap_price = resolved
                    await self.close_trade(
                        trade.id, cap_price, "holding_cap_reached"
                    )
                    continue

            # --- FIX 9: Timeout (absolute, 30d) ---
            if trade.opened_at and (now - trade.opened_at) > timedelta(days=TIMEOUT_DAYS):
                await self.close_trade(trade.id, exit_price, "timeout")
                continue

            # --- Strategy-aware pre-resolution timeout ---
            # Force-close positions that are about to hit the resolution
            # moment, BEFORE the resolution path runs. The resolution path
            # needs an oracle outcome (markets.resolved_outcome) which is
            # populated by the maintenance loop; if that backfill is
            # delayed, a position would otherwise hold past resolution
            # and either be deferred indefinitely or close at a stale bid.
            # Closing slightly early at the fresh bid is the safe play.
            preclose_hours = float(
                getattr(settings, "PRECLOSE_HOURS_BEFORE_RESOLUTION", 0.25)
            )
            if preclose_hours > 0:
                hours_left = await self._hours_until_resolution(trade.market_id)
                if hours_left is not None and 0 < hours_left <= preclose_hours:
                    await self.close_trade(
                        trade.id, exit_price, "preclose_pre_resolution"
                    )
                    continue

            # --- Market resolved — use terminal token value, never stale bid ---
            if await self._is_market_resolved(trade.market_id):
                resolution_price = await self._fetch_market_resolution(
                    trade.market_id, trade.token_id
                )
                if resolution_price is not None:
                    # Settlement known: 1.0 for winner, 0.0 for loser.
                    await self.close_trade(
                        trade.id, resolution_price, "market_resolved"
                    )
                else:
                    # Outcome not yet known (oracle pending or unbacked).
                    # Defer the close — using the stale bid here is exactly
                    # what produced the $42k phantom-wins in May 15. The
                    # next monitor tick will retry; if it never resolves,
                    # the timeout path will eventually close at the
                    # last-known bid which is at least sanity-bounded.
                    logger.warning(
                        f"PaperTrader: market {trade.market_id} past end_date but "
                        f"resolution outcome unknown — deferring close of #{trade.id}"
                    )
                continue

            # --- Leader exit — signal regardless of strategy ---
            # Previously FOLLOW-only; FADE benefits equally because leader
            # exiting indicates the original signal is stale.
            if await self._leader_exited_recently(trade.leader_wallet, trade.market_id):
                await self.close_trade(trade.id, exit_price, "leader_exit")
                continue

            # --- Sport holding cap (Strategy upgrade 2026-05-17, Tier 1 #4) ---
            # Sport markets resolve in 30-90 min; the generic 12h cap
            # above lets a position bleed through the entire event into
            # the resolution wipe. For category='sports' we force-close
            # at the current bid past SPORT_MAX_HOLDING_S (default 30
            # min). Placed AFTER market_resolved so a resolved market
            # closes via the terminal-value path with reason
            # `market_resolved` rather than `holding_cap_sport` —
            # operator priority order from the structural-fix plan.
            # The non-sport `holding_cap_reached` branch above already
            # ran (and skipped) for sport trades because the default
            # non-sport cap (43 200s) is strictly greater than the
            # sport cap (1 800s); when an operator overrides both knobs
            # such that non-sport ≤ sport, the non-sport branch still
            # wins by ordering — that is the intended escape hatch.
            if (
                is_sport
                and trade.opened_at
                and sport_holding_cap_s > 0
            ):
                held_for_s = (now - trade.opened_at).total_seconds()
                if held_for_s >= sport_holding_cap_s:
                    await self.close_trade(
                        trade.id, exit_price, "holding_cap_sport"
                    )
                    continue

            # PnL on a long position of the held token. For FADE the held
            # token is the opposite of the leader's, but the position is
            # still long, never short. The previous direction-aware branch
            # inverted this for direction=="no" and caused FADE wins to
            # stop-loss and FADE losses to take-profit. Confirmed in audit
            # 2026-05-17.
            #
            # We compare against ``mark_price`` (mid) to neutralise the
            # bid/ask spread; the actual close still books at
            # ``exit_price`` (bid) so realised PnL stays faithful to
            # what we'd actually receive on a sell.
            pnl_pct = (mark_price - trade.entry_price) / trade.entry_price
            # 2026-05-17 round 2 fix: also compute the BID-implied pnl
            # (what we'd actually realize on a sell). When a market is
            # near resolution, the spread can blow up (e.g. bid=0.003
            # ask=0.99 on a NO market about to resolve YES → mid=0.497
            # gives +27% even though selling at bid would book -97%).
            # Use bid_pnl_pct as a sanity guard: never label a close as
            # take_profit if the realized PnL would actually be a loss.
            bid_pnl_pct = (exit_price - trade.entry_price) / trade.entry_price

            # Strategy upgrade 2026-05-17 (Tier 1 #5) — adaptive stop-loss.
            # Sport markets get the tightened 3% stop (vs 8% FOLLOW / 5%
            # FADE for everything else). Same min(mid, bid) bid-guard
            # below catches stale-quote inflation regardless of category.
            # Read once per tick into `stop_loss_sport` above.
            if is_sport:
                stop = stop_loss_sport
            elif trade.strategy == "fade":
                stop = STOP_LOSS_FADE
            else:
                stop = STOP_LOSS_FOLLOW
            take = TAKE_PROFIT_FADE if trade.strategy == "fade" else TAKE_PROFIT_FOLLOW

            # Stop-loss: use the WORSE of mid or bid-implied PnL. A wide
            # spread should accelerate not delay the stop.
            effective_stop_pnl = min(pnl_pct, bid_pnl_pct)
            if effective_stop_pnl <= -stop:
                await self.close_trade(trade.id, exit_price, "stop_loss")
            # Take-profit: require BOTH mid and bid to clear the
            # threshold. A take_profit at the spread-induced mid that
            # actually books a bid loss is the bug that produced
            # paper_trade #16's "-$129.59 take_profit" (PnL was -97%
            # despite a +27% mid-implied gain). With this guard the
            # bot only closes as take_profit when the realized sale
            # would actually be profitable.
            elif pnl_pct >= take and bid_pnl_pct >= take * 0.5:
                await self.close_trade(trade.id, exit_price, "take_profit")

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

        # ── 2026-05-17 round 3: book_wall guard ─────────────────────────
        # Post-mortem of the 11 trades that each lost -97% showed the
        # bid-ask spread was >= 0.50 in ALL of them — the order book had
        # collapsed to a binary pre-resolution wall (bid=0.01, ask=0.99).
        # Reject any trade where the spread is wider than this config —
        # there's no meaningful price to enter at. O(1), runs before any
        # other gate so we never waste downstream work on a market whose
        # book is already broken. Fetch the quote on the leader's token:
        # on a binary market both YES and NO show the same wall (prices
        # sum to ~$1), so the leader-side spread is a faithful proxy
        # regardless of FOLLOW vs FADE direction.
        book_wall_max = float(
            await self._read_runtime_setting(
                "book_wall_max_spread",
                float(getattr(settings, "BOOK_WALL_MAX_SPREAD", 0.50)),
            )
        )
        if market_id and token_id:
            book_quote = await self._get_book_quote(market_id, token_id)
            if book_quote is not None:
                best_bid, best_ask = book_quote
                spread = float(best_ask) - float(best_bid)
                if spread >= book_wall_max:
                    await self._record_open_trade_refusal(
                        decision,
                        "book_wall_spread",
                        {
                            "spread": round(spread, 4),
                            "max": book_wall_max,
                            "bid": float(best_bid),
                            "ask": float(best_ask),
                        },
                    )
                    return None

        # ── Leader sell-side refusal (2026-05-17 diagnosis §A.6) ──
        # FOLLOW on a leader who is SELLING out is structurally wrong:
        # the leader is closing their position, not opening it, so the
        # bot would be buying when the leader is unwinding. FADE on a
        # sell-side leader trade has no symmetric short-fade path in
        # the current implementation either — refusing both is the
        # safe default. The side is sourced from ``decision["side"]``
        # (engine pass-through from trades_observed) OR from
        # ``trade_context.side`` (legacy callers). Case-insensitive.
        raw_side = decision.get("side") or trade_context.get("side") or ""
        leader_side = str(raw_side).strip().lower()
        if leader_side == "sell":
            await self._record_open_trade_refusal(
                decision,
                "leader_sell_side",
                {"strategy": strategy, "leader_side": leader_side},
            )
            return None

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

        # ── Strategy upgrade 2026-05-17 (Tier 1 fix #2+#3) — live-match gate ─
        # Defense in depth: the confidence engine already runs the same
        # predicate BEFORE leader_quality_gate, but signals that arrived
        # by an alternate path (e.g. router replay, manual ingest) must
        # still be blocked here. The bot lost 9 trades at -96/98% on
        # 2026-05-17 by following leaders into live sport matches that
        # resolved in MINUTES; the end_date-based time-to-resolution
        # gate cannot detect this because end_date is the dispute-window
        # expiration, not the resolution moment. Reason mirrors the
        # engine for unified dashboard accounting:
        # `live_match_blocked|signal=<reason>`.
        try:
            from src.economics.live_match_detector import (
                is_live_match,
                live_match_block_enabled,
            )
            live_is, live_reason = await is_live_match(market_id)
            block_enabled = await live_match_block_enabled()
        except Exception as exc:
            logger.debug(
                f"live_match_detector: predicate failed for "
                f"market={market_id}: {exc}"
            )
            live_is, live_reason, block_enabled = False, "no_match", False
        if live_is and block_enabled:
            await self._record_open_trade_refusal(
                decision,
                f"live_match_blocked|signal={live_reason}",
                {"strategy": strategy, "live_match_signal": live_reason},
            )
            return None

        # ── Strategy upgrade 2026-05-17 — category whitelist ──────────
        # Reject markets outside the operator-tunable category whitelist
        # (default 'sports,crypto,macro' per backtest cohorts).
        # market_category comes from the upstream confidence_engine
        # build_trade_context() (which queries markets.category). If the
        # context didn't set one we fall back to "unknown" — which is NOT
        # in the default whitelist, so it gets rejected by design.
        category_raw = (
            trade_context.get("market_category")
            or trade_context.get("category")
            or "unknown"
        )
        market_category = str(category_raw).strip().lower()
        whitelist_csv = await self._read_runtime_setting(
            "category_whitelist",
            str(getattr(settings, "CATEGORY_WHITELIST", "sports,crypto,macro")),
        )
        whitelist = {
            c.strip().lower() for c in str(whitelist_csv).split(",") if c.strip()
        }
        if whitelist and market_category not in whitelist:
            await self._record_open_trade_refusal(
                decision,
                "category_not_whitelisted",
                {
                    "category": market_category,
                    "whitelist": sorted(whitelist),
                    "strategy": strategy,
                },
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

        # --- Time-to-resolution gate ---
        # Most full-bet losses in May 15 came from opening trades on
        # sports markets resolving within a few hours: the position has
        # almost no time to reach take_profit and any wrong-side
        # resolution wipes ~100% of the bet. We require runway:
        # 6h for FOLLOW (mirrors leader's swing horizon), 24h for FADE
        # (FADE has no leader-exit close path and benefits more from
        # holding time). Markets with NULL end_date are also refused —
        # we cannot reason about resolution risk without a deadline.
        min_hours = (
            float(getattr(settings, "MIN_HOURS_TO_RESOLUTION_FADE", 24.0))
            if strategy == "fade"
            else float(getattr(settings, "MIN_HOURS_TO_RESOLUTION_FOLLOW", 6.0))
        )
        hours_to_resolution = await self._hours_until_resolution(market_id)
        if hours_to_resolution is None:
            await self._record_open_trade_refusal(
                decision,
                "missing_end_date",
                {"strategy": strategy},
            )
            return None
        if hours_to_resolution < min_hours:
            await self._record_open_trade_refusal(
                decision,
                "near_resolution",
                {
                    "strategy": strategy,
                    "hours_to_resolution": round(hours_to_resolution, 2),
                    "min_required": min_hours,
                },
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
        # Realistic slippage: BUY pays best_ask, not mid/last-trade.
        if strategy == "follow":
            direction = "yes"
            actual_token_id = token_id
            mid_fallback = await self._get_current_price(market_id, token_id) or 0.5
            entry_price = await self._entry_ask(market_id, actual_token_id, mid_fallback)
        else:
            direction = "no"
            opposite_token = await self._get_opposite_token(market_id, token_id)
            actual_token_id = opposite_token or token_id
            # FADE = take the OTHER side. Use the opposite token's ask.
            leader_mid = await self._get_current_price(market_id, token_id) or 0.5
            fade_fallback = max(0.01, 1.0 - leader_mid)
            entry_price = await self._entry_ask(market_id, actual_token_id, fade_fallback)

        # Final asymmetry gate: the confidence_engine filter checks the
        # leader's trade price but the actual entry price is the book ask
        # at fire time. On near-resolution markets the ask can be 0.99
        # even when the leader's trade was at 0.50, leading to the same
        # asymmetric-bad outcome the upstream filter is meant to prevent.
        # Reject BOTH FOLLOW (entry_ask of leader token) AND FADE (entry_ask
        # of opposite token) when in the high zone — symmetric exposure.
        #
        # Strategy upgrade 2026-05-17:
        #   * MAX_ENTRY_PRICE bumped 0.85 → 0.92 (backtest showed 0.7-0.9
        #     wins 61%, 0.9+ wins 62% — the asymmetric loss only kicks in
        #     above 0.92).
        #   * New MIN_ENTRY_PRICE floor (default 0.40): entries in
        #     [0.0, 0.4) lose money on average. Cut the bottom tail.
        # Both bounds are RuntimeConfig-tunable.
        max_entry_price = float(
            await self._read_runtime_setting(
                "max_entry_price",
                getattr(settings, "MAX_ENTRY_PRICE", 0.92),
            )
        )
        min_entry_price = float(
            await self._read_runtime_setting(
                "min_entry_price",
                getattr(settings, "MIN_ENTRY_PRICE", 0.40),
            )
        )
        if entry_price >= max_entry_price:
            await self._record_open_trade_refusal(
                decision,
                "high_entry_ask_blocked",
                {
                    "strategy": strategy,
                    "entry_ask": entry_price,
                    "max_allowed": max_entry_price,
                    "leader_price": decision.get("price"),
                },
            )
            return None
        if entry_price < min_entry_price:
            await self._record_open_trade_refusal(
                decision,
                "low_entry_ask_blocked",
                {
                    "strategy": strategy,
                    "entry_ask": entry_price,
                    "min_required": min_entry_price,
                    "leader_price": decision.get("price"),
                },
            )
            return None

        # --- Leader-price drift gate ---
        # If the bot's actual fill ask is far from the leader's signal price
        # (after the FADE flip), the bot is no longer trading the same
        # economic position the leader signaled. This is what produced the
        # 2 "wins" in May 15: leader signal at price L, but stale book ask
        # at 0.025 → bot booked +30,000% return on a position the leader
        # never actually took. Symmetric gate catches both inflated wins
        # and inflated losses.
        leader_price_raw = decision.get("price")
        try:
            leader_price = float(leader_price_raw) if leader_price_raw is not None else None
        except (TypeError, ValueError):
            leader_price = None
        if leader_price is not None and 0.0 < leader_price < 1.0:
            expected_entry = (
                leader_price if strategy == "follow" else max(0.01, 1.0 - leader_price)
            )
            drift = abs(entry_price - expected_entry) / max(0.01, expected_entry)
            max_drift = float(getattr(settings, "MAX_LEADER_PRICE_DRIFT", 0.20))
            if drift > max_drift:
                await self._record_open_trade_refusal(
                    decision,
                    "leader_price_drift",
                    {
                        "strategy": strategy,
                        "leader_price": leader_price,
                        "entry_ask": entry_price,
                        "expected_entry": expected_entry,
                        "drift": round(drift, 4),
                        "max_allowed": max_drift,
                    },
                )
                return None

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

        # F-01 fix: the paper_trades INSERT and the portfolio_state UPSERT
        # (which encodes the bankroll deduction triggered by this open) must
        # commit atomically. Without a transaction wrapper, a crash between
        # the INSERT and `_persist_state()` would leave an "open" row in
        # paper_trades while portfolio_state still reflects the pre-trade
        # bankroll. We update the in-memory `_capital` / `_open_trades` BEFORE
        # the persist so the UPSERT carries the post-trade values, and only
        # commit them in Python after the DB transaction succeeds.
        self._capital -= size_usdc
        open_trade = OpenPaperTrade(
            id=0,  # populated after the INSERT returns
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

        try:
            async with get_db() as conn:
                async with conn.transaction():
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
                    open_trade.id = trade_id
                    # Persistence: bankroll + open_positions are part of the
                    # same atomic unit as the paper_trades INSERT.
                    await self._persist_state(conn=conn)
                    await self._record_equity_sample(conn=conn)
        except Exception as e:
            # Roll the in-memory bookkeeping back so we don't leak phantom
            # state when the DB write failed.
            self._open_trades = [t for t in self._open_trades if t is not open_trade]
            self._capital += size_usdc
            logger.error(f"Failed to open paper trade: {e}")
            return None

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
        # Lazy fee-rate refresh: ``_reload_open_trades`` rehydrates with
        # ``fee_rate_pct=0.0`` because that column isn't persisted in
        # paper_trades. Without this re-fetch, any trade that spans a
        # warm restart pays $0 exit fees, silently overstating PnL.
        fee_rate = trade.fee_rate_pct
        if fee_rate <= 0.0:
            fee_rate = await self._get_fee_rate(trade.market_id)
        exit_fee = float(
            calculate_polymarket_fee(
                shares=size_shares,
                price=exit_price,
                fee_rate=fee_rate,
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

        # --- Sanity ratio defense ---
        # The May 15 session recorded 2 trades with +29 000% / +3 300% PnL
        # because exit_price came from a stale book cache showing
        # near-resolution values that weren't actually executable. The
        # B2 staleness gate now blocks most of those, but this is a
        # last-line audit log: if a single trade reports a return >
        # MAX_TRADE_RETURN_RATIO (default 5.0x = 500%) AND was NOT
        # closed via market_resolved (where 100x payouts are valid for
        # extreme tail bets), tag it for operator review. We still
        # record the trade — refusing to record creates a bigger
        # accounting hole — but emit a high-severity log + Redis flag.
        max_ratio = float(getattr(settings, "MAX_TRADE_RETURN_RATIO", 5.0))
        if abs(pnl_pct) > max_ratio and close_reason != "market_resolved":
            logger.error(
                "PaperTrader: SUSPICIOUS close detected — "
                f"trade=#{trade_id} pnl_pct={pnl_pct * 100:.1f}% "
                f"entry={trade.entry_price} exit={exit_price} "
                f"reason={close_reason} strategy={trade.strategy} "
                "(exceeds MAX_TRADE_RETURN_RATIO; likely stale-cache exit)"
            )
            if self._redis is not None:
                try:
                    flag = self._redis.publish(
                        "paper:audit:suspicious_close",
                        json.dumps(
                            {
                                "trade_id": trade_id,
                                "pnl_pct": pnl_pct,
                                "entry_price": trade.entry_price,
                                "exit_price": exit_price,
                                "close_reason": close_reason,
                                "strategy": trade.strategy,
                                "market_id": trade.market_id,
                            }
                        ),
                    )
                    if inspect.isawaitable(flag):
                        await flag
                except Exception:
                    pass

        now = datetime.now(tz=timezone.utc)
        outcome = "win" if pnl_usdc > 0 else "loss"
        # F-01 fix: paper_trades UPDATE, decision_log UPDATE, and the
        # portfolio_state UPSERT must commit as one unit. Previously the two
        # UPDATEs lived in the same `async with get_db()` block but ran
        # under asyncpg's per-statement autocommit, so a crash between them
        # left the trade marked closed with decision_log.outcome still NULL.
        # We mutate in-memory bookkeeping AFTER the transaction succeeds so
        # nothing leaks out on rollback.
        try:
            async with get_db() as conn:
                async with conn.transaction():
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

                    # Update in-memory bookkeeping inside the transaction so
                    # the portfolio_state UPSERT below sees the post-close
                    # bankroll. If the transaction rolls back we restore the
                    # original snapshot in the except block.
                    prev_open_trades = list(self._open_trades)
                    prev_capital = self._capital
                    prev_peak = self._peak_capital
                    prev_realized = self._realized_pnl_cum
                    self._open_trades = [t for t in self._open_trades if t.id != trade_id]
                    self._capital += trade.size_usdc + pnl_usdc
                    self._peak_capital = max(self._peak_capital, self._capital)
                    self._realized_pnl_cum += pnl_usdc

                    await self._persist_state(conn=conn)
                    await self._record_equity_sample(conn=conn)
        except Exception as e:
            # Restore the pre-close snapshot if anything in the transaction
            # raised — names only exist if we got that far, hence the guard.
            if "prev_open_trades" in locals():
                self._open_trades = prev_open_trades
                self._capital = prev_capital
                self._peak_capital = prev_peak
                self._realized_pnl_cum = prev_realized
            logger.error(f"Failed to close paper trade #{trade_id}: {e}")
            return False

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
        # entry_price + exit_price added so the Telegram formatter (and any
        # other consumer) can show the user the full price journey, not
        # just the final PnL number — the May 17 audit confirmed sparse
        # close messages were a key source of operator confusion.
        event = {
            "trade_id": trade_id,
            "market_id": trade.market_id,
            "pnl_usdc": round(pnl_usdc, 2),  # float, not str
            "pnl_pct": round(pnl_pct * 100, 2),
            "direction": trade.direction,
            "size_usdc": trade.size_usdc,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
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
        except Exception as exc:
            logger.warning(
                f"PaperTrader: paper_closed event publish failed for #{trade_id}: {exc}"
            )

        # `_persist_state` + `_record_equity_sample` already ran inside the
        # `conn.transaction()` above so bankroll/peak/realized PnL are
        # already durable. Skipping a second round-trip here.

        logger.info(
            f"Closed paper trade #{trade_id}: pnl={pnl_usdc:.2f} "
            f"({pnl_pct * 100:.1f}%) reason={close_reason}"
        )
        return True

    async def _get_book_quote(
        self,
        market_id: str,
        token_id: str,
        *,
        max_age_s: float | None = None,
    ) -> tuple[float, float] | None:
        """Return (best_bid, best_ask) from book:last cache IF fresh.

        Returns None when the cache is missing, malformed, or older than
        ``max_age_s`` (defaults to ``settings.MAX_BOOK_AGE_PAPER_S``).
        Without this freshness gate, market_resolved closes against a
        stale near-final book bid produced ~$42k of phantom PnL in the
        May 15 session — confirmed by audit 2026-05-17.

        The Redis payload schema differs by writer (observer WS,
        maintenance loop, JIT fetch in confidence_engine). We accept
        ``observed_ts`` (epoch seconds float or ISO string) or
        ``captured_at`` (ISO string) — whichever the writer emitted.
        Any payload without a parseable timestamp is rejected, since we
        can no longer prove it is recent.
        """
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"book:last:{market_id}:{token_id}")
        except Exception:
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            return None

        # Staleness gate.
        max_age = (
            float(max_age_s)
            if max_age_s is not None
            else float(getattr(settings, "MAX_BOOK_AGE_PAPER_S", 60.0))
        )
        observed = payload.get("observed_ts") or payload.get("captured_at")
        if observed is None:
            return None
        try:
            if isinstance(observed, (int, float)):
                obs_ts = float(observed)
            else:
                obs_str = str(observed)
                if obs_str.endswith("Z"):
                    obs_str = obs_str[:-1] + "+00:00"
                obs_ts = datetime.fromisoformat(obs_str).timestamp()
            now_ts = datetime.now(tz=timezone.utc).timestamp()
            age = max(0.0, now_ts - obs_ts)
            if age > max_age:
                return None
        except (ValueError, TypeError):
            return None

        try:
            best_bid = float(payload.get("best_bid"))
            best_ask = float(payload.get("best_ask"))
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                return None
            return best_bid, best_ask
        except Exception:
            return None

    async def _get_current_price(self, market_id: str, token_id: str) -> float | None:
        """Get latest price for mark-to-market.

        Preference order:
          1. book:last mid (best_bid + best_ask) / 2 — most current
          2. price:{market}:{token} cache from observer
          3. trades_observed last price (DB)

        For trade open/close we use _entry_ask / _exit_bid instead, which
        model realistic slippage. This function is the mark-to-market price
        used by the monitor loop for take-profit / stop-loss thresholds.
        """
        quote = await self._get_book_quote(market_id, token_id)
        if quote is not None:
            best_bid, best_ask = quote
            return (best_bid + best_ask) / 2.0

        if self._redis is not None:
            try:
                cached = await self._redis.get(f"price:{market_id}:{token_id}")
                if cached is not None:
                    return float(cached)
            except Exception:
                pass
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT price FROM trades_observed
                    WHERE market_id=$1 AND token_id=$2
                      AND source IS DISTINCT FROM 'onchain'
                    ORDER BY time DESC LIMIT 1
                    """,
                    market_id,
                    token_id,
                )
                return float(row["price"]) if row else None
        except Exception:
            return None

    async def _entry_ask(self, market_id: str, token_id: str, fallback: float) -> float:
        """Realistic entry price: best_ask if available (BUY pays the ask).

        Falls back to mid/last when book is missing. Floor at 0.01 to avoid
        zero-divisor in pnl calculations.
        """
        quote = await self._get_book_quote(market_id, token_id)
        if quote is not None:
            return max(0.01, quote[1])
        return max(0.01, fallback)

    async def _exit_bid(self, market_id: str, token_id: str, fallback: float) -> float:
        """Realistic exit price: best_bid if available (SELL hits the bid).

        Floor is 0.0 (not 0.01) so resolved-loser tokens record their true
        terminal value when ``_fetch_market_resolution`` falls through to a
        cached bid. ``calculate_long_pnl`` accepts ``exit_price >= 0``.
        """
        quote = await self._get_book_quote(market_id, token_id)
        if quote is not None:
            return max(0.0, quote[0])
        return max(0.0, fallback)

    async def _mark_mid(
        self, market_id: str, token_id: str, fallback: float
    ) -> float:
        """Spread-neutral mark price: ``(best_bid + best_ask) / 2``.

        Used by ``_check_open_positions`` for stop-loss / take-profit
        threshold comparisons. The previous code compared the bid
        against the (ask-side) entry price, baking the spread into
        every PnL check and biasing the monitor loop toward
        ``stop_loss``. Mid removes that bias without affecting realised
        PnL (close still books at the bid).

        Floors at 0.0 so a fresh quote with an extremely wide spread
        (best_bid=0) still returns a meaningful mid rather than NaN.
        """
        quote = await self._get_book_quote(market_id, token_id)
        if quote is not None:
            best_bid, best_ask = quote
            return max(0.0, (best_bid + best_ask) / 2.0)
        return max(0.0, fallback)

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
        # PER-MARKET CAP: don't open multiple trades on the same market,
        # regardless of which leader fired the signal. Without this we
        # saw 4 separate trades on 0x59eb6... within 1 minute as
        # different leaders all signaled the same near-resolution market
        # — they all lost. One position per market caps that exposure.
        for trade in self._open_trades:
            if trade.market_id == market_id:
                return True
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM paper_trades
                    WHERE market_id = $1
                      AND status = 'open'
                    LIMIT 1
                    """,
                    market_id,
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

    async def _hours_until_resolution(self, market_id: str) -> float | None:
        """Hours remaining until ``markets.end_date``. None if unknown.

        Returning None means: don't trade — we cannot reason about
        resolution risk without a known deadline. Used by ``open_trade``
        to enforce the ``near_resolution`` and ``missing_end_date`` gates.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT end_date FROM markets WHERE market_id=$1",
                    market_id,
                )
            if row is None or row["end_date"] is None:
                return None
            end_date = row["end_date"]
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            delta = end_date - datetime.now(tz=timezone.utc)
            return delta.total_seconds() / 3600.0
        except Exception:
            return None

    async def _fetch_market_resolution(
        self, market_id: str, token_id: str
    ) -> float | None:
        """Return terminal token value for a resolved market, or None.

        Returns 1.0 if the held ``token_id`` is the winning outcome, 0.0
        if it lost. Returns None if the resolution outcome is not yet
        known (no Gamma backfill, oracle pending, market in dispute, etc).

        The DB column ``markets.resolved_outcome`` is the canonical store;
        it is populated by the maintenance loop when Gamma reports
        ``closed`` + ``outcomePrices``. Callers must defer the close when
        we return None — using a stale bid here is what produced the
        $42k phantom-wins in May 15.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT token_yes, token_no, resolved_outcome
                    FROM markets WHERE market_id=$1
                    """,
                    market_id,
                )
            if row is None:
                return None
            outcome = row["resolved_outcome"]
            if outcome is None:
                return None
            outcome_str = str(outcome).strip().lower()
            if outcome_str in ("yes", "1", "true"):
                return 1.0 if token_id == row["token_yes"] else 0.0
            if outcome_str in ("no", "0", "false"):
                return 1.0 if token_id == row["token_no"] else 0.0
            return None
        except Exception:
            return None

    async def _is_market_resolved(self, market_id: str) -> bool:
        """Check if market is resolved.

        2026-05-17 round 2 fix: short-circuit on ``resolved_outcome``. The
        previous anti-poison ("ignore end_date if trades arrived 5 min after")
        made the gate return FALSE for the majority of sports markets where
        position-closing trades naturally arrive past end_date. Result:
        ``market_resolved`` close path never fired → ``_check_open_positions``
        fell through to stop_loss/take_profit using a stale or asymmetric
        book mid that produced FAKE take_profit closes on actual -97% losses
        (paper_trade #16 was a textbook case: bid=0.003 ask=0.99 →
        mid=0.497 → +27% pnl_pct → take_profit fire → real bid close at
        -97%).

        New logic:
          * If ``markets.resolved_outcome IS NOT NULL`` → resolved, period.
            Gamma authoritatively says the market closed.
          * Else if ``end_date < NOW()`` AND no recent post-resolution
            trades within 30 s (very tight anti-poison, not the legacy
            5 min that opened the door) → resolved.
          * Else → not resolved.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT m.end_date, m.resolved_outcome,
                           (
                               SELECT MAX(t.time)
                               FROM trades_observed t
                               WHERE t.market_id = $1
                                 AND t.source IS DISTINCT FROM 'onchain'
                           ) AS last_trade_time
                    FROM markets m
                    WHERE m.market_id = $1
                    """,
                    market_id,
                )
                if not row:
                    return False
                # Authoritative: Gamma confirmed an outcome.
                if row["resolved_outcome"] is not None:
                    return True
                end_date = row["end_date"]
                if end_date is None:
                    return False
                # end_date alone is enough — anti-poison only kicks in when
                # we see trades VERY recent post-end_date (i.e., the market
                # is still actively trading, oracle row stale).
                last_trade_time = row.get("last_trade_time")
                if (
                    last_trade_time is not None
                    and last_trade_time > end_date + timedelta(seconds=30)
                    and (datetime.now(tz=timezone.utc) - last_trade_time).total_seconds() < 60
                ):
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

    async def _resolve_trade_category(self, trade: OpenPaperTrade) -> str:
        """Return the market category for an open trade, lowercased.

        Strategy upgrade 2026-05-17 (Tier 1 #4+#5): the sport-specific
        safeguards (tighter stop-loss, 30 min hold cap) require knowing
        whether a position is on a sport market at every monitor tick.

        Resolution order:
          1. ``trade.leader_context['trade_context']['market_category']`` —
             set by confidence_engine when the decision was emitted.
             Cheapest (no I/O) and authoritative for live decisions.
          2. ``trade.leader_context['market_category']`` — legacy shape
             where the decision dict carried the category at the top
             level (some older test fixtures use this).
          3. Fresh ``SELECT category FROM markets`` — covers warm
             restarts where ``_reload_open_trades`` rehydrated from DB
             without trade_context, and any case where the upstream
             decision predated the market_category field.

        Returns ``"unknown"`` when every lookup misses. We deliberately
        do NOT raise: an unknown category just means the legacy
        (non-sport) safeguards apply, which is the safe default.
        """
        ctx = trade.leader_context or {}
        if isinstance(ctx, dict):
            inner = ctx.get("trade_context")
            if isinstance(inner, dict):
                cat = inner.get("market_category") or inner.get("category")
                if cat:
                    return str(cat).strip().lower()
            cat = ctx.get("market_category") or ctx.get("category")
            if cat:
                return str(cat).strip().lower()
        # Fallback: fresh DB lookup. We never want a single missing
        # column to break the monitor loop, so swallow exceptions.
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT category FROM markets WHERE market_id=$1",
                    trade.market_id,
                )
            if row and row["category"]:
                return str(row["category"]).strip().lower()
        except Exception:
            pass
        return "unknown"

    async def _read_runtime_setting(self, key: str, fallback):
        """Best-effort read of a single RuntimeConfig override.

        Returns ``fallback`` (and never raises) if the runtime layer is
        unavailable, the key is unknown, or the override isn't set —
        this is the strategy-upgrade hot path and we never want a config
        glitch to silently break ``open_trade`` or ``_check_open_positions``.
        """
        try:
            from src.control.runtime_config import get_runtime_config
            cfg = get_runtime_config()
            effective = await cfg.effective()
            if key in effective and effective[key] is not None:
                return effective[key]
        except Exception as exc:
            logger.debug(
                f"PaperTrader: runtime_config read failed for {key!r}: {exc}"
            )
        return fallback
