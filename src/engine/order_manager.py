"""
OrderManager — the "place a limit order, watch for fills, cancel and reprice
if the book moves" loop used by LiveTrader (S2.6).

The LiveTrader calls `manager.place_for_position(...)` with a USDC notional,
a side, and a token id. The OrderManager:

  1. Reads the current orderbook midpoint from the CLOB.
  2. Computes a limit price = mid +/- LIVE_SLIPPAGE_BPS (BUY = mid + bps,
     SELL = mid - bps). The intent is to be slightly aggressive enough
     that we cross liquidity quickly, without paying the full ask/bid.
  3. Converts USDC notional to shares (size_shares = size_usdc / price).
  4. Places a GTC limit, then polls `get_order_status` every
     LIVE_FILL_POLL_INTERVAL_S seconds until either:
       * the order is fully filled (-> return OrderOutcome(filled=True))
       * LIVE_ORDER_TIMEOUT_S elapses (-> cancel + reprice if attempts <
         LIVE_ORDER_MAX_RETRIES, else return OrderOutcome(filled=False))
       * the CLOB rejects/expires the order (-> return failure)

Why a separate class (instead of inlining in LiveTrader)?
  * The cancel/reprice loop is genuinely complex and benefits from being
    tested in isolation against a mocked CLOBClientWrapper.
  * A future MarketOrderManager (cross-spread FOK) could be a sibling
    without LiveTrader knowing.
  * The DB writes for `live_orders` rows live here, so the trader doesn't
    have to know about every reprice attempt.

In dry_run mode (LIVE_TRADING_DRY_RUN=true OR no private key), the wrapper
short-circuits the place call to a shadow log line and the manager
records a single `live_orders` row with `order_state='shadow'`. The
position-level `live_trades` row is created by the LiveTrader, not here.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.engine.clob_client_wrapper import (
    CLOBClientWrapper,
    PlaceOrderResult,
)


# --------------------------------------------------------------------------- #
# Public dataclasses                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class OrderOutcome:
    """End-state of a placement attempt sequence (i.e. one
    "open the position" or "close the position" job).

    `filled` is true when at least one fill was recorded AND the order
    is no longer open (fully filled OR partially filled then cancelled
    after timeout). Callers use `filled_size_shares` and `avg_fill_price`
    to decide whether a partial fill is acceptable.
    """
    filled: bool
    filled_size_shares: float
    avg_fill_price: float
    fee_paid_usdc: float
    last_clob_order_id: Optional[str]
    attempts: int
    final_state: str  # 'filled' / 'partial' / 'canceled' / 'rejected' / 'shadow'
    error_message: Optional[str] = None


# --------------------------------------------------------------------------- #
# OrderManager                                                                #
# --------------------------------------------------------------------------- #


class OrderManager:
    """Single-order placement orchestrator. Stateless across calls — each
    `place_for_position` is independent."""

    def __init__(self, clob_client: CLOBClientWrapper) -> None:
        self._clob = clob_client

    # ------------------------------------------------------------------ #
    # Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def place_for_position(
        self,
        *,
        live_trade_id: int,
        token_id: str,
        side: str,
        size_usdc: float,
        order_role: str = "entry",
    ) -> OrderOutcome:
        """Attempt to fill `size_usdc` of the given token, side. Records
        every attempt to `live_orders`. Returns an OrderOutcome.

        order_role: 'entry' (opening the position) or 'exit' (closing it).
                    Only matters for the live_orders.order_role column.
        """
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            return OrderOutcome(
                filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                fee_paid_usdc=0.0, last_clob_order_id=None, attempts=0,
                final_state="rejected", error_message=f"invalid side {side!r}",
            )

        max_attempts = max(1, settings.LIVE_ORDER_MAX_RETRIES)
        timeout_s = max(1, settings.LIVE_ORDER_TIMEOUT_S)
        slippage_bps = settings.LIVE_SLIPPAGE_BPS

        attempt = 0
        last_outcome = OrderOutcome(
            filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
            fee_paid_usdc=0.0, last_clob_order_id=None, attempts=0,
            final_state="rejected", error_message="no attempts performed",
        )

        while attempt < max_attempts:
            attempt += 1

            # 1. Refresh mid each attempt — book has likely moved.
            try:
                mid = await self._clob.get_midpoint(token_id)
            except Exception as e:
                logger.error(f"OrderManager: failed to fetch mid for {token_id}: {e}")
                last_outcome = OrderOutcome(
                    filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                    fee_paid_usdc=0.0, last_clob_order_id=None, attempts=attempt,
                    final_state="rejected", error_message=f"midpoint_failed: {e}",
                )
                break
            if not (0.0 < mid < 1.0):
                last_outcome = OrderOutcome(
                    filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                    fee_paid_usdc=0.0, last_clob_order_id=None, attempts=attempt,
                    final_state="rejected", error_message=f"bad_midpoint: {mid}",
                )
                break

            # 2. Compute limit price with slippage budget.
            limit_price = self._compute_limit_price(side_upper, mid, slippage_bps)
            # 3. Convert notional -> shares (round to 4 decimals; CLOB tick).
            size_shares = round(size_usdc / limit_price, 4)
            if size_shares <= 0:
                last_outcome = OrderOutcome(
                    filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                    fee_paid_usdc=0.0, last_clob_order_id=None, attempts=attempt,
                    final_state="rejected", error_message="size_shares<=0",
                )
                break

            # 4. Place the order.
            place_result = await self._clob.place_limit_order(
                token_id=token_id,
                side=side_upper,
                price=limit_price,
                size=size_shares,
            )

            order_id = await self._record_order_placed(
                live_trade_id=live_trade_id,
                attempt_index=attempt - 1,
                order_role=order_role,
                side=side_upper,
                requested_price=limit_price,
                requested_size=size_shares,
                place_result=place_result,
            )

            if place_result.shadow:
                # Dry-run: nothing more to wait for.
                return OrderOutcome(
                    filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                    fee_paid_usdc=0.0, last_clob_order_id=None, attempts=attempt,
                    final_state="shadow",
                )

            if not place_result.success or not place_result.clob_order_id:
                last_outcome = OrderOutcome(
                    filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                    fee_paid_usdc=0.0, last_clob_order_id=place_result.clob_order_id,
                    attempts=attempt, final_state="rejected",
                    error_message=place_result.error_message or "place_failed",
                )
                # If the CLOB rejects the order outright, retrying with a
                # different price is unlikely to help — break.
                break

            # 5. Wait for fill or timeout.
            wait_result = await self._wait_for_fill(
                clob_order_id=place_result.clob_order_id,
                timeout_s=timeout_s,
            )
            await self._update_order_finalized(
                order_db_id=order_id,
                wait_result=wait_result,
            )

            if wait_result["state"] == "filled":
                return OrderOutcome(
                    filled=True,
                    filled_size_shares=wait_result["filled_size"],
                    avg_fill_price=wait_result["avg_price"] or limit_price,
                    fee_paid_usdc=wait_result["fee_paid"],
                    last_clob_order_id=place_result.clob_order_id,
                    attempts=attempt,
                    final_state="filled",
                )

            # Timeout hit — partial or no fill. Cancel and decide whether
            # to reprice. We accept partial fills: if anything was
            # filled, treat as "done", since the leader signal is stale
            # by the time we'd reprice.
            await self._clob.cancel_order(place_result.clob_order_id)
            if wait_result["filled_size"] > 0:
                final_state = "partial"
                return OrderOutcome(
                    filled=True,
                    filled_size_shares=wait_result["filled_size"],
                    avg_fill_price=wait_result["avg_price"] or limit_price,
                    fee_paid_usdc=wait_result["fee_paid"],
                    last_clob_order_id=place_result.clob_order_id,
                    attempts=attempt,
                    final_state=final_state,
                )

            last_outcome = OrderOutcome(
                filled=False, filled_size_shares=0.0, avg_fill_price=0.0,
                fee_paid_usdc=0.0, last_clob_order_id=place_result.clob_order_id,
                attempts=attempt, final_state="canceled",
                error_message=f"timeout_no_fill_in_{timeout_s}s",
            )
            # Loop back for next attempt with refreshed mid + slippage.

        return last_outcome

    # ------------------------------------------------------------------ #
    # Pricing                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_limit_price(side: str, mid: float, slippage_bps: int) -> float:
        """BUY at mid+(bps), SELL at mid-(bps). Clamped into (0,1)
        because Polymarket prices are probabilities."""
        adj = mid * (slippage_bps / 10_000.0)
        if side == "BUY":
            price = mid + adj
        else:
            price = mid - adj
        # Clamp so we never send a degenerate price.
        return max(0.001, min(0.999, round(price, 4)))

    # ------------------------------------------------------------------ #
    # Polling loop                                                        #
    # ------------------------------------------------------------------ #

    async def _wait_for_fill(
        self,
        *,
        clob_order_id: str,
        timeout_s: int,
    ) -> dict:
        """Poll CLOB every LIVE_FILL_POLL_INTERVAL_S until terminal state
        or timeout. Returns a dict carrying the final observation."""
        deadline = time.monotonic() + timeout_s
        poll_interval = max(0.1, settings.LIVE_FILL_POLL_INTERVAL_S)
        last_filled = 0.0
        last_avg_price: Optional[float] = None
        last_state = "placed"
        while True:
            status = await self._clob.get_order_status(clob_order_id)
            if status is not None:
                last_filled = status.filled_size
                last_avg_price = status.avg_fill_price
                last_state = status.state
                if status.state in {"filled", "canceled", "rejected", "expired"}:
                    fills = await self._clob.get_trades_for_order(clob_order_id)
                    fee_total = sum(f.fee_usdc for f in fills)
                    return {
                        "state": status.state,
                        "filled_size": last_filled,
                        "avg_price": last_avg_price,
                        "fee_paid": fee_total,
                    }
            if time.monotonic() >= deadline:
                fills = await self._clob.get_trades_for_order(clob_order_id)
                fee_total = sum(f.fee_usdc for f in fills)
                return {
                    "state": last_state,
                    "filled_size": last_filled,
                    "avg_price": last_avg_price,
                    "fee_paid": fee_total,
                }
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------ #
    # DB persistence                                                      #
    # ------------------------------------------------------------------ #

    async def _record_order_placed(
        self,
        *,
        live_trade_id: int,
        attempt_index: int,
        order_role: str,
        side: str,
        requested_price: float,
        requested_size: float,
        place_result: PlaceOrderResult,
    ) -> int:
        """INSERT a `live_orders` row for this placement attempt and
        return its DB id. We persist BEFORE waiting on fills so that a
        crash mid-poll still leaves an audit trail."""
        if place_result.shadow:
            order_state = "shadow"
        elif not place_result.success:
            order_state = "rejected"
        else:
            order_state = "placed"

        async with get_db() as conn:
            order_id = await conn.fetchval(
                """
                INSERT INTO live_orders
                    (live_trade_id, order_role, order_state, clob_order_id,
                     side, requested_price, requested_size, attempt_index,
                     error_message, raw_clob_response, finalized_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
                        CASE WHEN $3 IN ('shadow', 'rejected') THEN NOW() ELSE NULL END)
                RETURNING id
                """,
                live_trade_id,
                order_role,
                order_state,
                place_result.clob_order_id,
                side,
                requested_price,
                requested_size,
                attempt_index,
                place_result.error_message,
                json.dumps(place_result.raw_response or {}),
            )
        return int(order_id)

    async def _update_order_finalized(
        self,
        *,
        order_db_id: int,
        wait_result: dict,
    ) -> None:
        """Update the live_orders row once the wait loop terminates."""
        state = wait_result.get("state") or "placed"
        # Translate the placeholder "placed" (timeout, no terminal status
        # observed) into "canceled" — the caller will have cancelled the
        # order before we land here.
        if state in {"placed", "partial"}:
            db_state = "partial" if wait_result.get("filled_size", 0) > 0 else "canceled"
        else:
            db_state = state
        async with get_db() as conn:
            await conn.execute(
                """
                UPDATE live_orders
                SET order_state = $2,
                    filled_size = $3,
                    filled_avg_price = $4,
                    fee_paid_usdc = $5,
                    finalized_at = NOW()
                WHERE id = $1
                """,
                order_db_id,
                db_state,
                float(wait_result.get("filled_size") or 0),
                wait_result.get("avg_price"),
                float(wait_result.get("fee_paid") or 0),
            )
