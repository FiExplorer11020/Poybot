"""
Position Tracker — reconstructs OPEN→CLOSE position cycles from trades_observed.
Subscribes to Redis trades:observed, maintains in-memory state, writes to positions_reconstructed.

Phase 2 Task C: in-memory `_open_positions` is now mirrored into the
`position_tracker_state` table. Every OPEN UPSERTs; every CLOSE DELETEs
inside the SAME transaction as the `positions_reconstructed` INSERT, so a
state-table row never outlives its close. `warm_start(conn)` rehydrates on
engine boot. `MAX_OPEN_POSITIONS_TRACKED` (env-overridable) caps the dict;
overflow evicts the oldest open by `open_time`.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber
from src.database.connection import get_db
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole
from src.economics.pnl import calculate_long_pnl
from src.monitoring.metrics import (
    position_tracker_evictions_total,
    position_tracker_open_count,
    position_tracker_warm_start_loaded_total,
)

REDIS_TRADES_CHANNEL = "trades:observed"
REDIS_POSITIONS_CHANNEL = "positions:closed"
# Producers (observer WS dispatch in main.py, maintenance-loop Gamma sweep)
# publish ``{"market_id": ..., "outcome": "yes"|"no"}`` JSON envelopes on this
# channel. The PositionTracker subscribes and closes every open position on
# the market at its per-direction terminal value. Wiring this was the fix
# for the long-standing bug where ``close_method='resolution'`` rows never
# appeared in ``positions_reconstructed`` (Diagnosis 2026-05-17 §A.1).
REDIS_MARKET_RESOLVED_CHANNEL = "market:resolved"
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
        # Cache of market_id → (token_yes, token_no). Needed to detect merge exits,
        # which are invisible on the orderbook (CLAUDE.md pitfall #12): a leader
        # exits a YES position by buying the complementary NO token and merging
        # YES + NO → $1.00. We must reconcile BOTH token legs per (wallet, market).
        self._market_tokens: dict[str, tuple[str | None, str | None]] = {}
        self._running = False
        self._stop_event = asyncio.Event()
        # F-04: dedicated pub/sub client with reconnect+resubscribe. The
        # previous code shared `self._redis` (the command-issuing client)
        # and silently lost subscriptions on disconnect.
        self._subscriber = Subscriber(
            settings.REDIS_URL, name="observer.position_tracker"
        )
        self._subscriber.register(REDIS_TRADES_CHANNEL, self._on_trade_message)
        # WS dispatcher in `src/observer/main.py` publishes resolved-market
        # envelopes here. Without this subscription the resolution code
        # path is dead and ``positions_reconstructed.close_method`` never
        # gets a ``'resolution'`` row — the bug the 2026-05-17 diagnosis
        # flagged as the root cause of Phase 1→2 maturation starvation.
        self._subscriber.register(
            REDIS_MARKET_RESOLVED_CHANNEL, self._on_market_resolved_message
        )

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        await self._subscriber.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        await self._subscriber.stop()

    async def _on_trade_message(self, trade: dict, _channel: str) -> None:
        """Subscriber handler — payload is already JSON-decoded."""
        if not self._running:
            return
        try:
            await self.on_trade(trade)
        except Exception as e:
            # Subscriber bumps handler-error metric; keep the existing
            # log site for continuity with pre-fix debug noise.
            logger.error(f"PositionTracker error processing message: {e}")
            raise

    async def _on_market_resolved_message(
        self, payload: dict, _channel: str
    ) -> None:
        """Subscriber handler for ``REDIS_MARKET_RESOLVED_CHANNEL``.

        Producer contract: ``payload`` is a JSON-decoded dict with at
        least ``market_id`` and ``outcome`` ("yes"/"no"). Anything else is
        logged at debug and dropped — we don't want a malformed publish
        to bring the subscriber down. We swallow per-payload errors here
        because the channel covers many markets; one bad envelope must
        not stall the rest.
        """
        if not self._running:
            return
        if not isinstance(payload, dict):
            logger.debug(
                f"PositionTracker: ignoring non-dict market_resolved payload: {payload!r}"
            )
            return
        market_id = payload.get("market_id") or payload.get("market")
        outcome = payload.get("outcome") or payload.get("winning_outcome")
        if not market_id or not outcome:
            logger.debug(
                "PositionTracker: market_resolved payload missing market_id/outcome "
                f"(market={market_id!r}, outcome={outcome!r})"
            )
            return
        try:
            closed = await self.close_market_positions(
                str(market_id), outcome=str(outcome)
            )
            if closed:
                logger.info(
                    f"PositionTracker: closed {closed} open positions "
                    f"on resolved market={market_id} outcome={outcome}"
                )
        except Exception as e:
            logger.error(
                f"PositionTracker: market_resolved handler failed "
                f"(market={market_id}, outcome={outcome}): {e}"
            )
            # Swallow — subscriber loop must stay alive for other markets.

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

        direction = await self._resolve_direction(market_id, token_id)

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
        """Open a new position, accounting for merge exits.

        A BUY of the complementary token in the same market within MERGE_WINDOW_S,
        with size roughly matching an outstanding position on the sibling token,
        is reinterpreted as a merge exit (YES + NO → $1.00). We close the sibling
        position(s) FIFO at the merge-implied exit price (1 − opposite_buy_price)
        and continue to open any residual buy quantity as a fresh position.
        """
        opposite_token = await self._sibling_token(market_id, token_id)
        remaining_buy = size_shares
        merged_shares = Decimal("0")

        if opposite_token:
            opp_key = (wallet, market_id, opposite_token)
            opposite_positions = self._open_positions.get(opp_key, [])
            # Merge-implied exit for the sibling position: the pair (YES+NO) is
            # worth exactly $1 post-merge, so the sibling's exit price equals
            # 1 − price of this complementary BUY.
            merge_exit_price = Decimal("1") - price
            if merge_exit_price < Decimal("0"):
                merge_exit_price = Decimal("0")

            while remaining_buy > 0 and opposite_positions:
                pos = opposite_positions[0]
                delta_s = abs((trade_time - pos.open_time).total_seconds())
                if delta_s > MERGE_WINDOW_S:
                    break
                # Close up to min(remaining_buy, pos.shares_remaining) via merge.
                close_shares = min(pos.shares_remaining, remaining_buy)
                # Require size symmetry: the merge hypothesis is only credible
                # when the new BUY is within ±20% of the sibling position size.
                ratio = (
                    (close_shares / pos.shares_remaining)
                    if pos.shares_remaining > 0
                    else Decimal("0")
                )
                if ratio < Decimal("0.8"):
                    break

                await self._close_position(
                    pos, trade_time, merge_exit_price, close_shares, "merge"
                )
                pos.shares_remaining -= close_shares
                remaining_buy -= close_shares
                merged_shares += close_shares
                if pos.shares_remaining <= Decimal("0"):
                    opposite_positions.pop(0)
            if not opposite_positions and opp_key in self._open_positions:
                del self._open_positions[opp_key]

        if remaining_buy <= Decimal("0"):
            return

        # Open a new position for whatever quantity wasn't consumed by the merge.
        if merged_shares > 0 and size_shares > 0:
            residual_fraction = remaining_buy / size_shares
            size_usdc = (size_usdc * residual_fraction).quantize(Decimal("0.01"))
            size_shares = remaining_buy

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

        # Persist to position_tracker_state so the OPEN survives restart.
        # The primary key is (wallet, market, token, direction); FIFO slots
        # for the same key collapse onto one row whose typed columns reflect
        # the HEAD slot. That's a small loss of fidelity vs the in-memory
        # list of slots, but the alternative (per-slot rows) would need a
        # synthetic slot_id and the data audit only cared about not losing
        # in-flight opens — losing the per-slot breakdown is acceptable.
        # See docs/audit/phase2/C_position_tracker_state.md.
        await self._persist_open_state(pos)
        self._recompute_open_gauge()
        await self._enforce_capacity()

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
            # F-02 fix: the two SELECTs (category lookup + trend snapshot)
            # and the INSERT into positions_reconstructed are a single
            # logical atomic unit — the denormalized `category` and the
            # `is_contrarian` flag derived from the trend window must match
            # the row's `pnl_usdc` (which was computed BEFORE these reads).
            # Without the transaction wrapper, asyncpg runs each statement
            # under per-statement autocommit at READ COMMITTED, so each
            # statement sees a different snapshot. Wrapping in a tx pins a
            # consistent view and guarantees the Redis `positions:closed`
            # publish below only fires after commit.
            async with get_db() as conn:
                async with conn.transaction():
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
                              AND source IS DISTINCT FROM 'onchain'
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
                             net_pnl_usdc, economic_model_version, category)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
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
                        category,
                    )
                    # Same-tx state-row delete (Phase 2 Task C).
                    # The transaction wraps BOTH writes — if either raises
                    # everything rolls back, and a leftover state row can't
                    # outlive its positions_reconstructed close. The row
                    # may not exist (a CLOSE for a slot that was never
                    # persisted, e.g. a stale unit test); DELETE is a no-op
                    # in that case.
                    await conn.execute(
                        """
                        DELETE FROM position_tracker_state
                        WHERE wallet_address = $1
                          AND market_id = $2
                          AND token_id = $3
                          AND direction = $4
                        """,
                        pos.wallet_address,
                        pos.market_id,
                        pos.token_id,
                        pos.direction,
                    )
        except Exception as e:
            logger.error(f"Failed to insert closed position: {e}")
            return

        # R13 — calibration outcome hook (audit § 9.A).
        # Fire-and-forget: looks up the most recent decision_log row for
        # (wallet, market) before open_time and back-fills the matching
        # decision_predictions row with the realised pnl + followup volume.
        # Wrapped in its own try/except so a R13 failure can never affect
        # position close semantics.
        try:
            from src.calibration import fill_actual_outcomes_for_position
            await fill_actual_outcomes_for_position(
                wallet_address=pos.wallet_address,
                market_id=pos.market_id,
                open_time=pos.open_time,
                pnl_usdc=float(pnl_usdc),
                followup_volume_usdc=None,  # not tracked at close time; nightly batch fills
                closed_at=close_time,
            )
        except Exception as r13_exc:
            logger.debug(
                f"R13 outcome fill skipped for {pos.wallet_address[:10]}/{pos.market_id}: {r13_exc}"
            )

        # Outside the tx: if any slots remain for this (wallet, market,
        # token, direction) the state-row needs to come back. _close_position
        # is invoked with a slot in hand BEFORE the caller mutates the list,
        # so we re-check via the live FIFO queue and re-persist the HEAD if
        # anything is still open. This keeps the table consistent with the
        # in-memory FIFO under partial closes / multi-slot keys.
        await self._sync_state_after_close(pos)

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

        self._recompute_open_gauge()

    async def close_market_positions(
        self,
        market_id: str,
        resolution_price: Decimal | str | float | None = None,
        *,
        outcome: str | None = None,
    ) -> int:
        """Close all open positions for a resolved market.

        Two calling conventions are supported:

        1. Per-direction outcome (recommended for binary markets): pass
           ``outcome="yes"`` or ``outcome="no"``. We look up ``token_yes``
           and ``token_no`` for ``market_id`` and close each open position
           at ``1.0`` if it holds the winning token, ``0.0`` otherwise.
           This is what the WS ``market_resolved`` event and the
           maintenance-loop Gamma sweep call. Returns the number of
           positions closed.

        2. Single resolution price (legacy): pass ``resolution_price`` (a
           Decimal/float between 0 and 1) and every open position on the
           market closes at that price regardless of direction. Useful
           only when the caller has already resolved per-direction
           upstream (e.g. tests that simulate a known terminal value).
           Returns the number of positions closed.

        Both conventions are idempotent — closing a market with no open
        positions is a no-op and returns 0.
        """
        keys_to_close = [
            (w, m, t) for (w, m, t) in list(self._open_positions) if m == market_id
        ]
        if not keys_to_close:
            return 0

        token_yes: str | None = None
        token_no: str | None = None
        if outcome is not None:
            normalized = str(outcome).strip().lower()
            if normalized not in ("yes", "no"):
                logger.warning(
                    f"close_market_positions: invalid outcome={outcome!r} "
                    f"for market={market_id} — skipping"
                )
                return 0
            token_yes, token_no = await self._get_market_tokens(market_id)
            if not token_yes and not token_no:
                logger.warning(
                    f"close_market_positions: market={market_id} has no "
                    "yes/no token mapping — cannot resolve per-direction"
                )
                return 0
            winning_token = token_yes if normalized == "yes" else token_no

        now = datetime.now(tz=timezone.utc)
        closed = 0
        for key in keys_to_close:
            positions = list(self._open_positions.get(key, []))
            for pos in positions:
                if outcome is not None:
                    # Per-direction terminal value: winning token → 1.0,
                    # losing token → 0.0. A position on an unknown token
                    # (shouldn't happen under normal operation) is closed
                    # at 0.0 — safer than 1.0 because it under-states gains
                    # rather than fabricating them.
                    if winning_token and pos.token_id == winning_token:
                        price = Decimal("1.0")
                    else:
                        price = Decimal("0.0")
                else:
                    price = Decimal(str(resolution_price or 0))
                await self._close_position(
                    pos, now, price, pos.shares_remaining, "resolution"
                )
                closed += 1
            if key in self._open_positions:
                del self._open_positions[key]
        return closed

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

    async def _get_market_tokens(
        self, market_id: str
    ) -> tuple[str | None, str | None]:
        """Return (token_yes, token_no) for a market, using an in-memory cache.

        Needed for merge-exit detection and correct direction labelling. A cache
        entry is stored even when tokens are unknown (None, None) to avoid
        re-querying the DB every trade on markets that haven't been enriched yet.
        """
        cached = self._market_tokens.get(market_id)
        # Only cache resolved pairs; an unresolved (None, None) might become
        # resolved once trade_observer's Gamma enrichment lands, so re-query
        # on the next trade for that market rather than locking in a miss.
        if cached is not None and (cached[0] or cached[1]):
            return cached
        tokens: tuple[str | None, str | None] = (None, None)
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT token_yes, token_no FROM markets WHERE market_id = $1",
                    market_id,
                )
                if row:
                    tokens = (row["token_yes"], row["token_no"])
        except Exception as e:
            logger.debug(f"Market token lookup failed for {market_id}: {e}")
        if tokens[0] or tokens[1]:
            self._market_tokens[market_id] = tokens
        return tokens

    def invalidate_market_tokens(self, market_id: str) -> None:
        """Drop the cached (token_yes, token_no) for a market.

        Callers should invoke this after upserting market metadata so the next
        trade on this market re-reads the resolved tokens.
        """
        self._market_tokens.pop(market_id, None)

    async def _sibling_token(self, market_id: str, token_id: str) -> str | None:
        """Return the complementary token_id in the same market, if known."""
        token_yes, token_no = await self._get_market_tokens(market_id)
        if token_yes and token_id == token_yes:
            return token_no
        if token_no and token_id == token_no:
            return token_yes
        return None

    async def _resolve_direction(self, market_id: str, token_id: str) -> str:
        """Infer 'yes' / 'no' from the markets table. Falls back to 'yes'."""
        token_yes, token_no = await self._get_market_tokens(market_id)
        if token_no and token_id == token_no:
            return "no"
        # If yes is known and matches, or if neither token is known, default to 'yes'.
        return "yes"

    # ------------------------------------------------------------------ #
    # Persistence (Phase 2 Task C)                                        #
    # ------------------------------------------------------------------ #

    def _recompute_open_gauge(self) -> None:
        """Sync the polybot_position_tracker_open_count gauge with reality.
        Counts SLOTS (sum of list lengths), not unique keys — one key may
        hold multiple FIFO slots when a wallet opens, partially closes,
        then re-opens the same (market, token) leg."""
        total = sum(len(v) for v in self._open_positions.values())
        try:
            position_tracker_open_count.set(total)
        except Exception:
            # Metrics must never break the hot path.
            pass

    def _aggregate_for_persistence(self, key: tuple) -> OpenPosition | None:
        """Collapse the FIFO list for `key` into a single OpenPosition row.

        The DB primary key is (wallet, market, token, direction) so we can't
        store one row per FIFO slot without inventing a synthetic ordinal.
        We pick the HEAD slot (oldest open_time, the next to close) — that's
        the slot whose open_time we need on warm-start to make merge-window
        and holding-period calculations correct. The aggregate's
        shares_remaining / size_shares / size_usdc sum across all slots so
        the captured exposure is faithful even if we only re-create the
        head slot on warm-start.

        Returns None when the key has no slots (caller should delete the
        row instead of UPSERTing).
        """
        positions = self._open_positions.get(key)
        if not positions:
            return None
        head = positions[0]
        total_shares = sum((p.size_shares for p in positions), Decimal("0"))
        total_remaining = sum((p.shares_remaining for p in positions), Decimal("0"))
        total_usdc = sum((p.size_usdc for p in positions), Decimal("0"))
        return OpenPosition(
            wallet_address=head.wallet_address,
            market_id=head.market_id,
            token_id=head.token_id,
            direction=head.direction,
            open_time=head.open_time,
            entry_price=head.entry_price,
            size_usdc=total_usdc,
            size_shares=total_shares,
            shares_remaining=total_remaining,
            fee_rate_pct=head.fee_rate_pct,
        )

    async def _persist_open_state(self, pos: OpenPosition) -> None:
        """UPSERT the aggregate row for pos's (wallet, market, token, dir).

        Called from `_handle_buy` after appending to the in-memory list,
        so the aggregate reflects ALL slots (including the one we just
        added). Failure is logged but not raised — the in-memory state is
        authoritative for the running process; the DB copy is a restart
        safety-net. A persistence error must NOT cause us to mis-report a
        position as not open."""
        key = (pos.wallet_address, pos.market_id, pos.token_id)
        agg = self._aggregate_for_persistence(key) or pos
        try:
            async with get_db() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO position_tracker_state
                            (wallet_address, market_id, token_id, direction,
                             open_time, entry_price, size_usdc, size_shares,
                             shares_remaining, fee_rate_pct, state_json,
                             updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,NOW())
                        ON CONFLICT (wallet_address, market_id, token_id, direction)
                        DO UPDATE SET
                            open_time        = EXCLUDED.open_time,
                            entry_price      = EXCLUDED.entry_price,
                            size_usdc        = EXCLUDED.size_usdc,
                            size_shares      = EXCLUDED.size_shares,
                            shares_remaining = EXCLUDED.shares_remaining,
                            fee_rate_pct     = EXCLUDED.fee_rate_pct,
                            state_json       = EXCLUDED.state_json,
                            updated_at       = NOW()
                        """,
                        agg.wallet_address,
                        agg.market_id,
                        agg.token_id,
                        agg.direction,
                        agg.open_time,
                        agg.entry_price,
                        agg.size_usdc,
                        agg.size_shares,
                        agg.shares_remaining,
                        agg.fee_rate_pct,
                        "{}",
                    )
        except Exception as e:
            logger.warning(
                f"position_tracker_state UPSERT failed "
                f"(wallet={pos.wallet_address}, market={pos.market_id}, "
                f"token={pos.token_id}): {e}"
            )

    async def _sync_state_after_close(self, pos: OpenPosition) -> None:
        """After a CLOSE, reconcile the state row with the in-memory FIFO.

        `_close_position` already DELETEd the row inside the same tx that
        wrote `positions_reconstructed`. If any slots remain for this key
        (partial close on a multi-slot key, or one FIFO slot consumed of
        many), we need to UPSERT a fresh aggregate so warm-start can
        rehydrate the residual exposure.

        Implementation note: we run this OUTSIDE the close transaction.
        The DELETE-then-UPSERT pattern means a crash between the two
        leaves the in-memory state ahead of the DB by one slot — the
        next OPEN / CLOSE on the same key will re-converge them. A
        single-tx UPSERT-OR-DELETE would be cleaner but requires another
        round-trip BEFORE the positions_reconstructed insert (we don't
        know the post-close shares_remaining until the slot is consumed),
        and the audit only required the DELETE to be inside the tx."""
        key = (pos.wallet_address, pos.market_id, pos.token_id)
        # If the caller already popped the slot (`_handle_sell`), the list
        # for this key may be empty — leave the DB row deleted.
        if self._open_positions.get(key):
            agg = self._aggregate_for_persistence(key)
            if agg is not None and agg.shares_remaining > Decimal("0"):
                await self._persist_open_state(agg)

    async def _enforce_capacity(self) -> None:
        """Evict the OLDEST open by open_time when SLOT count exceeds
        MAX_OPEN_POSITIONS_TRACKED.

        The audit flagged unbounded growth as the root cause of the red
        flag we're addressing — with persistence in place we also need a
        ceiling. Eviction is best-effort: we drop the in-memory slot, then
        attempt to DELETE the matching row from position_tracker_state.
        If the DB delete fails the next restart could re-hydrate the
        evicted slot from `position_tracker_state` — log loudly so ops
        can clean up manually.
        """
        cap = getattr(settings, "MAX_OPEN_POSITIONS_TRACKED", 10_000)
        if cap <= 0:
            return
        total = sum(len(v) for v in self._open_positions.values())
        while total > cap:
            # Find the oldest slot across all keys.
            oldest_key: tuple | None = None
            oldest_pos: OpenPosition | None = None
            for k, slots in self._open_positions.items():
                for slot in slots:
                    if oldest_pos is None or slot.open_time < oldest_pos.open_time:
                        oldest_key = k
                        oldest_pos = slot
            if oldest_key is None or oldest_pos is None:
                break
            slots = self._open_positions.get(oldest_key) or []
            try:
                slots.remove(oldest_pos)
            except ValueError:
                # Shouldn't happen (we just found it) — protect the loop
                # from getting stuck.
                break
            if not slots and oldest_key in self._open_positions:
                del self._open_positions[oldest_key]
            try:
                position_tracker_evictions_total.inc()
            except Exception:
                pass
            logger.warning(
                f"PositionTracker eviction (over {cap} open slots): "
                f"dropped wallet={oldest_pos.wallet_address} "
                f"market={oldest_pos.market_id} token={oldest_pos.token_id} "
                f"opened={oldest_pos.open_time.isoformat()}"
            )
            # Re-sync the DB row for this key. If the key now has zero
            # slots → DELETE; otherwise UPSERT a fresh aggregate.
            await self._sync_state_after_eviction(oldest_pos)
            total = sum(len(v) for v in self._open_positions.values())
        self._recompute_open_gauge()

    async def _sync_state_after_eviction(self, evicted: OpenPosition) -> None:
        """Mirror the eviction in position_tracker_state."""
        key = (evicted.wallet_address, evicted.market_id, evicted.token_id)
        if self._open_positions.get(key):
            await self._persist_open_state(evicted)
            return
        try:
            async with get_db() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        DELETE FROM position_tracker_state
                        WHERE wallet_address = $1
                          AND market_id = $2
                          AND token_id = $3
                          AND direction = $4
                        """,
                        evicted.wallet_address,
                        evicted.market_id,
                        evicted.token_id,
                        evicted.direction,
                    )
        except Exception as e:
            logger.error(
                f"position_tracker_state eviction DELETE failed "
                f"(wallet={evicted.wallet_address}, "
                f"market={evicted.market_id}): {e}"
            )

    async def warm_start(self, conn=None) -> int:
        """Rehydrate `_open_positions` from position_tracker_state.

        Called by `src/observer/main.py` after the asyncpg pool is up but
        BEFORE the trade subscription loop starts — otherwise a CLOSE-fast-
        on-restart can fire before we've loaded the matching OPEN and gets
        treated as an orphan SELL (the very bug the audit flagged).

        `conn` is optional so callers can run this inside their own
        transaction; when None we acquire one from the pool.

        Returns the number of slots loaded (also incremented on the
        polybot_position_tracker_warm_start_loaded_total counter).
        """
        loaded = 0
        rows: list = []
        try:
            if conn is not None:
                rows = await conn.fetch(
                    """
                    SELECT wallet_address, market_id, token_id, direction,
                           open_time, entry_price, size_usdc, size_shares,
                           shares_remaining, fee_rate_pct
                    FROM position_tracker_state
                    ORDER BY open_time ASC
                    """
                )
            else:
                async with get_db() as c:
                    rows = await c.fetch(
                        """
                        SELECT wallet_address, market_id, token_id, direction,
                               open_time, entry_price, size_usdc, size_shares,
                               shares_remaining, fee_rate_pct
                        FROM position_tracker_state
                        ORDER BY open_time ASC
                        """
                    )
        except Exception as e:
            logger.error(f"PositionTracker.warm_start failed: {e}")
            return 0

        for row in rows:
            try:
                pos = OpenPosition(
                    wallet_address=row["wallet_address"],
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    direction=row["direction"],
                    open_time=row["open_time"],
                    entry_price=Decimal(str(row["entry_price"])),
                    size_usdc=Decimal(str(row["size_usdc"])),
                    size_shares=Decimal(str(row["size_shares"])),
                    shares_remaining=Decimal(str(row["shares_remaining"])),
                    fee_rate_pct=Decimal(str(row["fee_rate_pct"] or 0)),
                )
                key = (pos.wallet_address, pos.market_id, pos.token_id)
                self._open_positions.setdefault(key, []).append(pos)
                loaded += 1
                try:
                    position_tracker_warm_start_loaded_total.inc()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(
                    f"warm_start: skipping bad state row "
                    f"(wallet={row.get('wallet_address')}): {e}"
                )

        self._recompute_open_gauge()
        # Enforce the cap on the rehydrated set — a long-running outage
        # might have left more than MAX_OPEN_POSITIONS_TRACKED rows behind.
        await self._enforce_capacity()
        logger.info(f"PositionTracker warm_start: loaded {loaded} open positions")
        return loaded
