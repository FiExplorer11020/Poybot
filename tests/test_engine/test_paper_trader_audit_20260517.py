"""
Regression tests for the bugs identified in the May 17 2026 paper-trader audit.

Each test pins a specific failure mode that was producing incorrect PnL or
mis-timed closes prior to the audit fix.  Failures here mean a regression
has been introduced — read the docstring of the failing test for the
full bug context, then fix forward.

Covered bugs:
- B1/B10  FADE direction PnL inversion in `_check_open_positions` and
          `_compute_unrealized_pnl`. FADE positions are LONG of the
          opposite token; the formula was treating them as SHORT.
- B2      `_get_book_quote` reading a stale `book:last:*` cache and
          producing inflated TP/exit prices.
- B5      `high_entry_ask_blocked` applying to BOTH FOLLOW and FADE.
- B7      `_exit_bid` floor lowered from 0.01 to 0.0 so resolved-loser
          positions can record their true terminal value.
- B11     Telegram close payload now includes entry_price + exit_price.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.control.price_oracle import PriceQuote
from src.engine.paper_trader import (
    OpenPaperTrade,
    PaperTrader,
    STOP_LOSS_FADE,
    STOP_LOSS_FOLLOW,
    TAKE_PROFIT_FADE,
    TAKE_PROFIT_FOLLOW,
)
from src.telegram_bot.formatters import format_position_closed


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


def _book_payload(*, best_bid: float, best_ask: float, age_s: float) -> str:
    """Serialize a `book:last:*` payload with a `captured_at` ISO timestamp.

    `age_s` is how many seconds in the past `captured_at` should be set, so
    we can simulate a fresh (`age_s=5`) or stale (`age_s=300`) cache entry.
    """
    captured_at = datetime.now(tz=timezone.utc) - timedelta(seconds=age_s)
    return json.dumps(
        {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "captured_at": captured_at.isoformat(),
        }
    )


def _make_open_trade(
    *,
    strategy: str,
    direction: str,
    entry_price: float = 0.40,
    size_usdc: float = 200.0,
    token_id: str = "tok-A",
) -> OpenPaperTrade:
    return OpenPaperTrade(
        id=1,
        market_id="market-X",
        token_id=token_id,
        direction=direction,
        strategy=strategy,
        entry_price=entry_price,
        size_usdc=size_usdc,
        leader_wallet="0xLeader",
        confidence=0.8,
        opened_at=datetime.now(tz=timezone.utc),
    )


def _stub_oracle(
    trader: PaperTrader,
    *,
    price: float | None,
    source: str = "book",
    spread_pct: float | None = 0.04,
) -> AsyncMock:
    """Pillar 1 helper — replace the trader's PriceOracle with a stub
    that returns a fixed PriceQuote.

    Tests that previously mocked ``_exit_bid`` should call this instead,
    since the monitor loop now drives the close path through the oracle.
    """
    quote = PriceQuote(
        price=price,
        source=source,
        observed_ts=time.time(),
        spread_pct=spread_pct if source == "book" else None,
        raw_book=(
            {"best_bid": price, "best_ask": price, "spread_pct": spread_pct}
            if source == "book" and price is not None
            else None
        ),
    )
    stub = AsyncMock(return_value=quote)
    trader._price_oracle.get_close_price = stub
    return stub


# --------------------------------------------------------------------------- #
# Pillar 1 — PriceOracle integration into the close path                       #
# --------------------------------------------------------------------------- #


class TestPriceOracleIntegration:
    """The monitor loop now drives the close path through
    ``PriceOracle.get_close_price`` instead of ``_exit_bid`` (which had
    a fallback to ``entry_price`` — the May 15 phantom-win pattern).

    These tests pin the new contract:
      * When the oracle returns price=None → DEFER (do not close).
      * When source="resolved" + held wins → close at 1.0.
      * When source="resolved" + held loses → close at 0.0.
      * Trade NEVER closes at entry_price as a fallback.
    """

    @pytest.mark.asyncio
    async def test_defer_close_when_oracle_returns_fail(self):
        """No fresh book + no Gamma + no resolved_outcome → oracle
        returns source='fail'. Close MUST be deferred (no close_trade
        call), never papered over at entry_price."""
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="follow", direction="yes", entry_price=0.50
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        # Oracle says "no price available".
        trader._price_oracle.get_close_price = AsyncMock(
            return_value=PriceQuote(
                price=None, source="fail", observed_ts=time.time()
            )
        )
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=24.0)

        await trader._check_open_positions()

        # No close — we defer until the oracle has fresh data.
        trader.close_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolved_yes_held_yes_closes_at_one(self):
        """resolved_outcome=YES + direction=yes → exit at 1.0."""
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="follow", direction="yes", entry_price=0.40
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        resolved_quote = PriceQuote(
            price=1.0, source="resolved", observed_ts=time.time(),
            raw_resolution={"resolved_outcome": "yes", "held_token": "tok-A"},
        )
        trader._price_oracle.get_close_price = AsyncMock(return_value=resolved_quote)
        trader._is_market_resolved = AsyncMock(return_value=True)
        trader._leader_exited_recently = AsyncMock(return_value=False)

        await trader._check_open_positions()

        trader.close_trade.assert_called_once()
        args, kwargs = trader.close_trade.call_args
        trade_id, exit_price, reason = args
        assert exit_price == 1.0
        assert reason == "market_resolved"

    @pytest.mark.asyncio
    async def test_resolved_yes_held_no_closes_at_zero(self):
        """resolved_outcome=YES + direction=no (FADE) → exit at 0.0."""
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="fade", direction="no", entry_price=0.40
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        resolved_quote = PriceQuote(
            price=0.0, source="resolved", observed_ts=time.time(),
            raw_resolution={"resolved_outcome": "yes", "held_token": "tok-A"},
        )
        trader._price_oracle.get_close_price = AsyncMock(return_value=resolved_quote)
        trader._is_market_resolved = AsyncMock(return_value=True)
        trader._leader_exited_recently = AsyncMock(return_value=False)

        await trader._check_open_positions()

        trader.close_trade.assert_called_once()
        args, kwargs = trader.close_trade.call_args
        _, exit_price, reason = args
        assert exit_price == 0.0
        assert reason == "market_resolved"

    @pytest.mark.asyncio
    async def test_does_not_close_at_entry_price_on_stale_book(self):
        """Critical Pillar 1 invariant: if ALL three oracle steps fail
        (stale book + Gamma down + resolved_outcome NULL), the trade
        STAYS OPEN. Pre-fix the close would book at trade.entry_price,
        silently locking PnL to $0 and hiding real losses (the May 15
        phantom-win pattern)."""
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="follow", direction="yes", entry_price=0.40
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        trader._price_oracle.get_close_price = AsyncMock(
            return_value=PriceQuote(
                price=None, source="fail", observed_ts=time.time()
            )
        )
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=24.0)

        await trader._check_open_positions()

        # The trade is STILL OPEN. No silent close at entry_price.
        trader.close_trade.assert_not_called()
        assert trade in trader._open_trades


# --------------------------------------------------------------------------- #
# B1 / B10  —  FADE direction PnL inversion                                   #
# --------------------------------------------------------------------------- #


class TestFadeDirectionNotInverted:
    """The previous code computed `(entry - exit) / entry` for direction='no',
    treating FADE positions as shorts. They are LONG of the opposite token,
    so the formula must be `(exit - entry) / entry` regardless of direction.
    """

    @pytest.mark.asyncio
    async def test_fade_winning_position_does_not_stop_loss(self):
        """FADE bought NO at 0.40; bid rises to 0.50 → +25% real gain.

        Pre-fix: formula computed (0.40-0.50)/0.40 = -25% → stop_loss fired
        spuriously on a winning position. Post-fix: pnl_pct = +25%, neither
        threshold triggers (STOP_LOSS_FADE=0.05 → require ≤-5%,
        TAKE_PROFIT_FADE=0.10 → require ≥+10%; +25% triggers take_profit).
        The key regression is that we do NOT close via stop_loss.

        Updated 2026-05-17 (Pillar 1): the monitor loop now drives the
        close path through ``PriceOracle.get_close_price`` instead of
        ``_exit_bid``. We stub the oracle to return 0.50 as a fresh-book
        quote; the FAde direction-sign logic under test is unchanged.
        """
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="fade", direction="no", entry_price=0.40
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        # Book bid sits at 0.50 (our LONG of opposite token is winning).
        _stub_oracle(trader, price=0.50, source="book")
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)

        await trader._check_open_positions()

        # Must close at take_profit (real gain +25% ≥ +10%), NEVER stop_loss.
        assert trader.close_trade.called
        args, kwargs = trader.close_trade.call_args
        reason = args[2]
        assert reason == "take_profit", (
            f"FADE winning position closed as {reason!r}; expected take_profit. "
            "Direction-inversion regression."
        )

    @pytest.mark.asyncio
    async def test_fade_losing_position_does_not_take_profit(self):
        """FADE bought NO at 0.40; bid drops to 0.35 → -12.5% real loss.

        Pre-fix: formula computed (0.40-0.35)/0.40 = +12.5% → take_profit fired
        on a LOSS. Post-fix: pnl_pct = -12.5% → stop_loss fires (≤-5%), which is
        the correct sign.
        """
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="fade", direction="no", entry_price=0.40
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        _stub_oracle(trader, price=0.35, source="book")
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)

        await trader._check_open_positions()

        assert trader.close_trade.called
        args, kwargs = trader.close_trade.call_args
        _, exit_price, reason = args
        assert reason == "stop_loss", (
            f"FADE losing position closed as {reason!r}; expected stop_loss. "
            "Direction-inversion regression."
        )
        assert exit_price == 0.35

    @pytest.mark.asyncio
    async def test_follow_position_unchanged(self):
        """FOLLOW path must keep the same correct PnL behaviour."""
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="follow", direction="yes", entry_price=0.50
        )
        trader._open_trades = [trade]
        trader.close_trade = AsyncMock(return_value=True)
        # +20% gain
        _stub_oracle(trader, price=0.60, source="book")
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)

        await trader._check_open_positions()

        args, kwargs = trader.close_trade.call_args
        reason = args[2]
        assert reason == "take_profit"

    @pytest.mark.asyncio
    async def test_unrealized_pnl_for_fade_is_long_pnl(self):
        """`_compute_unrealized_pnl` must use long PnL for FADE too.

        Pre-fix this inverted, polluting the equity curve.
        """
        trader = PaperTrader(redis_client=_make_redis())
        # FADE position: bought opposite at 0.40, $200 size.
        trade = _make_open_trade(
            strategy="fade", direction="no", entry_price=0.40, size_usdc=200.0
        )
        trader._open_trades = [trade]
        # Current mid = 0.50 (we're up 25%).
        trader._get_current_price = AsyncMock(return_value=0.50)

        total = await trader._compute_unrealized_pnl()
        # Expected: 0.25 × 200 = +$50, NOT -$50.
        assert total == pytest.approx(50.0, abs=0.5), (
            f"Unrealized PnL for FADE = ${total}; expected +$50 "
            f"(long of opposite token, price up 25%)."
        )


# --------------------------------------------------------------------------- #
# B2  —  Staleness check on book:last                                          #
# --------------------------------------------------------------------------- #


class TestBookCacheStaleness:
    @pytest.mark.asyncio
    async def test_fresh_book_is_accepted(self):
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.45, best_ask=0.47, age_s=5)
        )
        trader = PaperTrader(redis_client=redis)
        quote = await trader._get_book_quote("market-X", "tok-A")
        assert quote == (0.45, 0.47)

    @pytest.mark.asyncio
    async def test_stale_book_is_rejected(self):
        redis = _make_redis()
        # 300s old, far past MAX_BOOK_AGE_PAPER_S=60s default.
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.99, best_ask=1.00, age_s=300)
        )
        trader = PaperTrader(redis_client=redis)
        quote = await trader._get_book_quote("market-X", "tok-A")
        assert quote is None, (
            "Stale book quote was accepted — the May 15 stale-cache "
            "phantom-win path is open again."
        )

    @pytest.mark.asyncio
    async def test_payload_without_timestamp_is_rejected(self):
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=json.dumps({"best_bid": 0.50, "best_ask": 0.52})
        )
        trader = PaperTrader(redis_client=redis)
        quote = await trader._get_book_quote("market-X", "tok-A")
        assert quote is None

    @pytest.mark.asyncio
    async def test_caller_can_relax_max_age(self):
        """Some callers (e.g. live trader, replay) may relax the age cap."""
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.30, best_ask=0.33, age_s=120)
        )
        trader = PaperTrader(redis_client=redis)
        assert await trader._get_book_quote("m", "t") is None  # default 60s
        # With max_age_s=180, the 120s-old cache passes.
        quote = await trader._get_book_quote("m", "t", max_age_s=180)
        assert quote == (0.30, 0.33)


# --------------------------------------------------------------------------- #
# QW4 (audit 2026-05-17) — book spread sanity gate                             #
# --------------------------------------------------------------------------- #


class TestBookSpreadGate:
    """``_get_book_quote`` must reject books whose spread exceeds
    ``MAX_BOOK_SPREAD_PCT`` (default 0.30 = 30%). Pre-fix the gate only
    checked staleness + ``bid < ask`` invariant, so the typical pre-UMA
    binary-wall book (bid≈0.001, ask≈0.999) was accepted and its
    meaningless mid biased the monitor loop's TP/SL triggers — the
    May 15 pattern-A phantom-win closes.
    """

    @pytest.mark.asyncio
    async def test_narrow_spread_accepted(self):
        """Spread = (0.47 - 0.45) / 0.46 ≈ 4.3% — well under the cap."""
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.45, best_ask=0.47, age_s=5)
        )
        trader = PaperTrader(redis_client=redis)
        quote = await trader._get_book_quote("m", "t")
        assert quote == (0.45, 0.47)

    @pytest.mark.asyncio
    async def test_borderline_spread_accepted(self):
        """Spread = (0.58 - 0.42) / 0.50 = 32% > 30% — must be rejected.

        Spread of exactly 30% sits on the boundary; we test the 32% case
        to verify the cap fires unambiguously (no off-by-one).
        """
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.42, best_ask=0.58, age_s=5)
        )
        trader = PaperTrader(redis_client=redis)
        quote = await trader._get_book_quote("m", "t")
        assert quote is None, (
            "QW4 regression: 32% spread accepted — pre-UMA binary-wall "
            "phantom-win path is open."
        )

    @pytest.mark.asyncio
    async def test_half_spread_rejected(self):
        """Spread = (0.75 - 0.25) / 0.50 = 100% > 30% — clearly rejected."""
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(best_bid=0.25, best_ask=0.75, age_s=5)
        )
        trader = PaperTrader(redis_client=redis)
        assert await trader._get_book_quote("m", "t") is None

    @pytest.mark.asyncio
    async def test_pre_uma_binary_wall_rejected(self):
        """The actual production failure mode: bid=0.001, ask=0.999.

        Spread ≈ 0.998 / 0.5 ≈ 200% — must be rejected. This is the
        canonical "useless mid" case the gate exists to catch.
        """
        redis = _make_redis()
        redis.get = AsyncMock(
            return_value=_book_payload(
                best_bid=0.001, best_ask=0.999, age_s=5
            )
        )
        trader = PaperTrader(redis_client=redis)
        assert await trader._get_book_quote("m", "t") is None


# --------------------------------------------------------------------------- #
# B7  —  Exit bid floor lowered to 0.0                                         #
# --------------------------------------------------------------------------- #


class TestExitBidFloor:
    @pytest.mark.asyncio
    async def test_exit_bid_returns_zero_when_fallback_is_zero(self):
        """A resolved-loser token's terminal value is 0. _exit_bid must
        be able to return 0 so the loss is recorded faithfully.
        """
        redis = _make_redis()
        trader = PaperTrader(redis_client=redis)
        # Cache miss → fallback path.
        price = await trader._exit_bid("m", "t", fallback=0.0)
        assert price == 0.0


# --------------------------------------------------------------------------- #
# B11  —  Telegram close message richer                                        #
# --------------------------------------------------------------------------- #


class TestTelegramCloseFormat:
    def test_close_message_includes_strategy_size_pct(self):
        text = format_position_closed(
            venue="paper",
            payload={
                "trade_id": 17,
                "market_id": "0x1234567890abcdef1234567890abcdef",
                "strategy": "fade",
                "direction": "no",
                "size_usdc": 200.0,
                "entry_price": 0.40,
                "exit_price": 0.36,
                "pnl_usdc": -20.0,
                "pnl_pct": -10.0,
                "close_reason": "stop_loss",
            },
        )
        assert "FADE" in text, "strategy must appear in CLOSE message"
        assert "size: 200" in text or "200.00$" in text, "size must appear"
        assert "0.40" in text and "0.36" in text, "entry/exit must appear"
        assert "-10.0%" in text, "pnl_pct must appear"
        assert "stop_loss" in text


# --------------------------------------------------------------------------- #
# Sanity check on the thresholds — make sure constants are sane after edits   #
# --------------------------------------------------------------------------- #


def test_threshold_constants_sane():
    assert 0 < STOP_LOSS_FADE < 1
    assert 0 < STOP_LOSS_FOLLOW < 1
    assert 0 < TAKE_PROFIT_FADE < 1
    assert 0 < TAKE_PROFIT_FOLLOW < 1
    assert getattr(settings, "MAX_BOOK_AGE_PAPER_S", None) is not None
    assert getattr(settings, "MAX_ENTRY_PRICE", None) is not None
    assert getattr(settings, "MAX_LEADER_PRICE_DRIFT", None) is not None
    assert getattr(settings, "MIN_HOURS_TO_RESOLUTION_FOLLOW", None) is not None
    assert getattr(settings, "MIN_HOURS_TO_RESOLUTION_FADE", None) is not None
    # Session 2 additions:
    assert getattr(settings, "MONITOR_TICK_S", None) is not None
    assert getattr(settings, "URGENT_MONITOR_TICK_S", None) is not None
    assert getattr(settings, "URGENT_MONITOR_HOURS", None) is not None
    assert getattr(settings, "PRECLOSE_HOURS_BEFORE_RESOLUTION", None) is not None
    assert getattr(settings, "MAX_TRADE_RETURN_RATIO", None) is not None


# --------------------------------------------------------------------------- #
# Session 2 — Adaptive monitor cadence                                         #
# --------------------------------------------------------------------------- #


class TestAdaptiveMonitorCadence:
    """Without urgent ticking, the bot can miss resolution by up to 60s and
    close against post-resolution stale data. With adaptive cadence, the
    loop drops to 5s as soon as any open trade's market is within 1h of
    its end_date.
    """

    @pytest.mark.asyncio
    async def test_default_cadence_when_no_open_trades(self):
        trader = PaperTrader(redis_client=_make_redis())
        trader._open_trades = []
        tick = await trader._monitor_tick_seconds()
        assert tick == settings.MONITOR_TICK_S

    @pytest.mark.asyncio
    async def test_default_cadence_when_far_from_resolution(self):
        trader = PaperTrader(redis_client=_make_redis())
        trader._open_trades = [_make_open_trade(strategy="follow", direction="yes")]
        # Plenty of runway
        trader._hours_until_resolution = AsyncMock(return_value=48.0)
        tick = await trader._monitor_tick_seconds()
        assert tick == settings.MONITOR_TICK_S

    @pytest.mark.asyncio
    async def test_urgent_cadence_within_one_hour(self):
        trader = PaperTrader(redis_client=_make_redis())
        trader._open_trades = [_make_open_trade(strategy="follow", direction="yes")]
        trader._hours_until_resolution = AsyncMock(return_value=0.5)
        tick = await trader._monitor_tick_seconds()
        assert tick == settings.URGENT_MONITOR_TICK_S

    @pytest.mark.asyncio
    async def test_urgent_cadence_picks_minimum_across_trades(self):
        """One urgent trade is enough to tick the whole loop at urgent rate."""
        trader = PaperTrader(redis_client=_make_redis())
        far = _make_open_trade(strategy="follow", direction="yes", token_id="A")
        far.id = 1
        near = _make_open_trade(strategy="fade", direction="no", token_id="B")
        near.id = 2
        trader._open_trades = [far, near]
        # 24h, 0.4h — return per trade.id
        async def _h(market_id):
            # Both trades use the same market_id in our helper; differentiate
            # by id via call sequence.
            _h.calls += 1
            return 24.0 if _h.calls == 1 else 0.4
        _h.calls = 0
        trader._hours_until_resolution = _h
        tick = await trader._monitor_tick_seconds()
        assert tick == settings.URGENT_MONITOR_TICK_S


# --------------------------------------------------------------------------- #
# Session 2 — Preclose before resolution                                       #
# --------------------------------------------------------------------------- #


class TestPrecloseBeforeResolution:
    """The preclose path force-closes a trade ~15 min before resolution to
    avoid the indeterminate-outcome deferral path.
    """

    @pytest.mark.asyncio
    async def test_preclose_fires_when_minutes_left(self):
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(strategy="follow", direction="yes")
        trader._open_trades = [trade]
        _stub_oracle(trader, price=0.50, source="book")
        trader._hours_until_resolution = AsyncMock(return_value=0.1)  # 6 min
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader.close_trade = AsyncMock(return_value=True)

        await trader._check_open_positions()

        assert trader.close_trade.called
        args, kwargs = trader.close_trade.call_args
        reason = args[2]
        assert reason == "preclose_pre_resolution"

    @pytest.mark.asyncio
    async def test_preclose_does_not_fire_with_runway(self):
        trader = PaperTrader(redis_client=_make_redis())
        trade = _make_open_trade(
            strategy="follow", direction="yes", entry_price=0.50
        )
        trader._open_trades = [trade]
        # +20% gain → would trigger take_profit
        _stub_oracle(trader, price=0.60, source="book")
        trader._hours_until_resolution = AsyncMock(return_value=12.0)
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader.close_trade = AsyncMock(return_value=True)

        await trader._check_open_positions()
        assert trader.close_trade.called
        args, kwargs = trader.close_trade.call_args
        reason = args[2]
        assert reason == "take_profit"


# --------------------------------------------------------------------------- #
# Session 2 — Sanity ratio audit log                                           #
# --------------------------------------------------------------------------- #


class TestSanityRatioAuditLog:
    """Defense-in-depth log: any non-resolution close with > 500% return
    should publish to ``paper:audit:suspicious_close``.
    """

    @pytest.mark.asyncio
    async def test_suspicious_close_publishes_audit_event(self, caplog):
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        redis = _make_redis()
        redis.publish = AsyncMock()
        trader = PaperTrader(redis_client=redis)

        trade = _make_open_trade(
            strategy="follow",
            direction="yes",
            entry_price=0.01,  # tiny entry
            size_usdc=100.0,
        )
        trader._open_trades = [trade]
        trader._get_fee_rate = AsyncMock(return_value=0.0)

        @asynccontextmanager
        async def _db():
            conn = AsyncMock()
            conn.execute = AsyncMock()
            from contextlib import asynccontextmanager as _acm

            @_acm
            async def _tx():
                yield None
            conn.transaction = MagicMock(side_effect=lambda: _tx())
            yield conn

        with patch("src.engine.paper_trader.get_db", _db):
            # Exit at 0.95: gross PnL = (0.95-0.01)*10000 = +$9400 on $100
            # → 94x return; reason = "take_profit" → triggers audit.
            await trader.close_trade(trade.id, 0.95, "take_profit")

        # The audit event must have been published.
        topics = [
            call.args[0]
            for call in redis.publish.call_args_list
            if call and call.args
        ]
        assert "paper:audit:suspicious_close" in topics, (
            f"Suspicious-close audit event not published. Topics: {topics}"
        )

    @pytest.mark.asyncio
    async def test_market_resolved_extreme_payout_is_exempt(self):
        """Tail-bet payouts via market_resolved are legitimate even at 100x."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        redis = _make_redis()
        redis.publish = AsyncMock()
        trader = PaperTrader(redis_client=redis)
        trade = _make_open_trade(
            strategy="follow",
            direction="yes",
            entry_price=0.01,
            size_usdc=100.0,
        )
        trader._open_trades = [trade]
        trader._get_fee_rate = AsyncMock(return_value=0.0)

        @asynccontextmanager
        async def _db():
            conn = AsyncMock()
            conn.execute = AsyncMock()
            from contextlib import asynccontextmanager as _acm

            @_acm
            async def _tx():
                yield None
            conn.transaction = MagicMock(side_effect=lambda: _tx())
            yield conn

        with patch("src.engine.paper_trader.get_db", _db):
            await trader.close_trade(trade.id, 1.0, "market_resolved")

        # Should NOT publish audit event for market_resolved closes.
        suspicious_calls = [
            call
            for call in redis.publish.call_args_list
            if call and call.args and call.args[0] == "paper:audit:suspicious_close"
        ]
        assert not suspicious_calls, (
            "Audit event published for legitimate market_resolved close — "
            "the 100x payoff on a tail-bet resolution is real, not suspicious."
        )
