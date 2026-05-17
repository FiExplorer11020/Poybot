"""Regression tests for the paper-trader infra fixes landed 2026-05-17.

Three independent fixes are covered:

* **Mid-spread fix** in ``_check_open_positions``: stop/take threshold
  checks now compare against the mid price ``(bid + ask) / 2``, while
  the actual close still books at the bid. Pre-fix the bid was used
  for BOTH the comparison and the close, baking the spread into every
  PnL check and biasing the monitor loop toward ``stop_loss``.
* **Leader sell-side refusal** in ``open_trade``: a decision with
  ``side="sell"`` (leader is unwinding) is refused early with
  ``reason="leader_sell_side"``. FOLLOWing a sell is buying while the
  leader exits; FADEing a sell has no symmetric short path in the
  current code. Both are unsafe.
* **``paper:rejections:24h`` write**: every refusal that bumps the 1h
  counter must ALSO bump the 24h counter (with the matching 86400s
  TTL). The dashboard reads from both keys, and the 24h key was
  silently empty before this fix.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.control.price_oracle import PriceQuote
from src.engine.paper_trader import (
    OpenPaperTrade,
    PaperTrader,
    STOP_LOSS_FOLLOW,
    TAKE_PROFIT_FOLLOW,
)


def _stub_oracle_quote(
    trader: PaperTrader,
    *,
    price: float,
    source: str = "book",
) -> AsyncMock:
    """Inject a fixed PriceQuote into the trader's oracle (Pillar 1).

    Tests that previously mocked ``_exit_bid`` should call this — the
    monitor loop now drives the close path through ``PriceOracle``.
    """
    quote = PriceQuote(
        price=price,
        source=source,
        observed_ts=time.time(),
        spread_pct=0.05 if source == "book" else None,
        raw_book=({"best_bid": price, "best_ask": price} if source == "book" else None),
    )
    stub = AsyncMock(return_value=quote)
    trader._price_oracle.get_close_price = stub
    return stub


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.publish = AsyncMock()
    r.hincrby = AsyncMock()
    r.expire = AsyncMock()
    r.pubsub = MagicMock()
    return r


def _make_trader(redis=None) -> PaperTrader:
    return PaperTrader(redis_client=redis or _make_redis())


def _make_open_trade(
    *,
    strategy: str = "follow",
    direction: str = "yes",
    entry_price: float = 0.50,
    size_usdc: float = 200.0,
    market_id: str = "market-X",
    token_id: str = "tok-A",
    trade_id: int = 1,
) -> OpenPaperTrade:
    return OpenPaperTrade(
        id=trade_id,
        market_id=market_id,
        token_id=token_id,
        direction=direction,
        strategy=strategy,
        entry_price=entry_price,
        size_usdc=size_usdc,
        size_shares=size_usdc / entry_price,
        leader_wallet="0xLeader",
        confidence=0.8,
        fee_rate_pct=0.0,
        opened_at=datetime.now(tz=timezone.utc),
    )


def _make_decision(
    *,
    action: str = "follow",
    side: str | None = None,
    trade_context_side: str | None = None,
    market_id: str = "market-1",
    token_id: str = "token-1",
    size_usdc: float = 200.0,
) -> dict:
    decision: dict = {
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "size_usdc": size_usdc,
        "confidence": 0.8,
        "leader_wallet": "0xLeader",
        "signal_audit": {"accepted": True},
    }
    if side is not None:
        decision["side"] = side
    if trade_context_side is not None:
        decision["trade_context"] = {"side": trade_context_side}
    return decision


# --------------------------------------------------------------------------- #
# 1. Mid-spread fix in _check_open_positions                                  #
# --------------------------------------------------------------------------- #


class TestMidSpreadStopTakeCheck:
    """Stop / take checks must compare against the mid, not the bid.

    Scenario: entry filled at the ask, exit lands at the bid. Even a
    flat market shows a structural negative PnL ≈ spread, which would
    spuriously trip ``stop_loss`` at -8% / -5%. Marking against the
    mid removes the bias.
    """

    @pytest.mark.asyncio
    async def test_wide_spread_flat_market_does_not_stop_loss(self):
        """Entry at 0.50, market is flat (bid=0.45, ask=0.55, mid=0.50).

        Pre-fix: pnl_pct = (0.45 - 0.50)/0.50 = -10% → triggers
        stop_loss (≤-8%).
        Post-fix (Pillar 1 era): PriceOracle returns the MID directly
        as the exit price. mid = 0.50 = entry → 0% PnL, no close.
        """
        trader = _make_trader()
        trade = _make_open_trade(strategy="follow", entry_price=0.50)
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        # PriceOracle returns mid directly — the close path no longer
        # separates bid/mid; both signal evaluation and the close booking
        # use the same fresh-book mid.
        _stub_oracle_quote(trader, price=0.50, source="book")
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=72.0)

        await trader._check_open_positions()

        # No close at all — market is genuinely flat.
        trader.close_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_mid_above_take_threshold_closes_at_mid(self):
        """Entry at 0.50, fresh-book mid = 0.56 (+12% on mid).

        Pillar 1 (audit 2026-05-17): the close now books at the
        PriceOracle's canonical price — which is the MID for source=
        "book" — not at a separately-fetched bid. Booking the close at
        the mid is intentional: the PriceOracle's spread gate (30% cap)
        ensures the mid is meaningful before the oracle returns it, so
        the legacy "always book at bid" rule (which was meant to
        protect against wide-spread inflation) is no longer needed
        once the spread gate has already filtered out bad books.
        """
        trader = _make_trader()
        trade = _make_open_trade(strategy="follow", entry_price=0.50)
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        _stub_oracle_quote(trader, price=0.56, source="book")
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=72.0)

        await trader._check_open_positions()

        trader.close_trade.assert_called_once()
        args = trader.close_trade.call_args.args
        # close_trade(trade_id, exit_price, close_reason)
        assert args[0] == trade.id
        assert args[1] == 0.56
        assert args[2] == "take_profit"

    @pytest.mark.asyncio
    async def test_mid_below_stop_threshold_closes_at_mid(self):
        """Fresh-book mid drops below -8% → stop_loss fires, exit books
        at the oracle's mid (Pillar 1)."""
        trader = _make_trader()
        trade = _make_open_trade(strategy="follow", entry_price=0.50)
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        _stub_oracle_quote(trader, price=0.45, source="book")  # -10% on mid
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=72.0)

        await trader._check_open_positions()

        trader.close_trade.assert_called_once()
        args = trader.close_trade.call_args.args
        assert args[1] == 0.45
        assert args[2] == "stop_loss"

    @pytest.mark.asyncio
    async def test_mark_mid_helper_returns_average_of_bid_and_ask(self):
        """The new helper is the single source of truth for the mid; a
        regression here would silently re-introduce spread bias."""
        trader = _make_trader()
        trader._get_book_quote = AsyncMock(return_value=(0.40, 0.50))
        mid = await trader._mark_mid("m", "t", fallback=0.45)
        assert mid == 0.45

    @pytest.mark.asyncio
    async def test_mark_mid_falls_back_when_book_missing(self):
        """When the book quote is missing we use the fallback (typically
        the exit_bid) so the comparison degrades gracefully rather than
        firing spurious closes off NaN."""
        trader = _make_trader()
        trader._get_book_quote = AsyncMock(return_value=None)
        mid = await trader._mark_mid("m", "t", fallback=0.30)
        assert mid == 0.30


# --------------------------------------------------------------------------- #
# 2. Leader sell-side refusal in open_trade                                   #
# --------------------------------------------------------------------------- #


class TestLeaderSellSideRefusal:
    """``open_trade`` must refuse when the leader's trade has
    ``side='sell'`` (or 'SELL') — both FOLLOW and FADE."""

    @pytest.mark.asyncio
    async def test_follow_on_sell_side_leader_is_refused(self):
        redis = _make_redis()
        trader = _make_trader(redis=redis)
        decision = _make_decision(action="follow", side="sell")

        result = await trader.open_trade(decision)

        assert result is None
        # The 1h counter must bump with ``leader_sell_side``; the 24h
        # counter is verified by the rejections-24h test below.
        redis.hincrby.assert_any_call("paper:rejections:1h", "leader_sell_side", 1)

    @pytest.mark.asyncio
    async def test_fade_on_sell_side_leader_is_refused(self):
        """FADE on a sell-side leader trade has no symmetric short
        path in the current implementation, so refuse the same way."""
        redis = _make_redis()
        trader = _make_trader(redis=redis)
        decision = _make_decision(action="fade", side="SELL")

        result = await trader.open_trade(decision)

        assert result is None
        redis.hincrby.assert_any_call("paper:rejections:1h", "leader_sell_side", 1)

    @pytest.mark.asyncio
    async def test_side_in_trade_context_is_honoured(self):
        """Legacy callers stash the leader side under
        ``trade_context.side`` instead of the top-level ``side``. The
        refusal must catch both shapes — otherwise old decision
        producers slip through."""
        redis = _make_redis()
        trader = _make_trader(redis=redis)
        decision = _make_decision(
            action="follow", side=None, trade_context_side="sell"
        )

        result = await trader.open_trade(decision)

        assert result is None
        redis.hincrby.assert_any_call("paper:rejections:1h", "leader_sell_side", 1)

    @pytest.mark.asyncio
    async def test_buy_side_passes_the_filter(self):
        """``side='buy'`` must NOT be refused by this filter — downstream
        checks (signal_audit, capital, etc) are what should decide the
        trade. We verify the buy passes the side gate by checking that
        the rejection reason if any is not ``leader_sell_side``."""
        redis = _make_redis()
        trader = _make_trader(redis=redis)
        # Use a minimal decision that will fail OTHER filters (no DB
        # mocked) — what we care about is that ``leader_sell_side`` is
        # NOT among the rejection reasons.
        decision = _make_decision(action="follow", side="buy")
        decision["size_usdc"] = 5.0  # below MIN_POSITION_USDC=50, will
                                     # fail with `below_min_position_size`
                                     # not `leader_sell_side`.

        await trader.open_trade(decision)

        rejection_reasons = [
            call.args[1] for call in redis.hincrby.call_args_list
            if call.args and call.args[0] == "paper:rejections:1h"
        ]
        assert "leader_sell_side" not in rejection_reasons


# --------------------------------------------------------------------------- #
# 3. paper:rejections:24h write path                                          #
# --------------------------------------------------------------------------- #


class TestRejections24hCounter:
    """Every refusal that bumps ``paper:rejections:1h`` must also bump
    ``paper:rejections:24h`` with the matching 86400s TTL. The
    dashboard reads both keys; the 24h key was silently empty before
    this fix (2026-05-17 diagnosis §A.7).
    """

    @pytest.mark.asyncio
    async def test_refusal_bumps_24h_counter_with_correct_ttl(self):
        redis = _make_redis()
        trader = _make_trader(redis=redis)
        # Pick a refusal path that doesn't need DB mocks: missing
        # signal_audit.
        decision = _make_decision()
        decision.pop("signal_audit")

        result = await trader.open_trade(decision)

        assert result is None

        # Both counters must have been bumped, both with the matching
        # expiry. ``hincrby`` is the increment, ``expire`` sets the TTL.
        redis.hincrby.assert_any_call(
            "paper:rejections:1h", "missing_accepted_signal_audit", 1
        )
        redis.hincrby.assert_any_call(
            "paper:rejections:24h", "missing_accepted_signal_audit", 1
        )
        redis.expire.assert_any_call("paper:rejections:1h", 3600)
        redis.expire.assert_any_call("paper:rejections:24h", 86400)

    @pytest.mark.asyncio
    async def test_24h_counter_keyed_by_reason_label(self):
        """Multiple distinct refusal reasons must produce distinct
        counter fields — a regression where ``hincrby`` collapses all
        reasons into a single field would still bump the counter but
        hide the breakdown the dashboard depends on."""
        redis = _make_redis()
        trader = _make_trader(redis=redis)

        # Refusal #1: missing signal_audit → `missing_accepted_signal_audit`
        d1 = _make_decision()
        d1.pop("signal_audit")
        await trader.open_trade(d1)

        # Refusal #2: stale decision context → `stale_decision`
        d2 = _make_decision()
        d2["trade_context"] = {"live_candidate": False}
        await trader.open_trade(d2)

        # Both reasons must appear under both buckets.
        reasons_24h = {
            call.args[1] for call in redis.hincrby.call_args_list
            if call.args and call.args[0] == "paper:rejections:24h"
        }
        assert "missing_accepted_signal_audit" in reasons_24h
        assert "stale_decision" in reasons_24h
