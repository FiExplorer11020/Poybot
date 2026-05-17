"""
Regression tests for the sport-position safety mechanisms added in the
Strategy Upgrade 2026-05-17 (Tier 1 fix #4 + #5).

Context: the bot was losing -97% on sport positions because (a) the 8%
stop-loss is far too loose for live sport price dynamics and (b) there
was no time-based forced exit, so positions died in the resolution wipe.
Agent C blocks new sport-on-live-match opens at the entry-time gate; this
module is the exit-time safety net for any sport position that slipped
past it (or was opened before the Agent C filter shipped).

Each test pins one branch of the two new safeguards in
``PaperTrader._check_open_positions``:

- Sport position closed at -3% (``stop_loss``) — sport stop is tighter
  than the legacy 8% FOLLOW / 5% FADE thresholds.
- Non-sport position still closed at -8% (no regression).
- Sport position force-closed at T+31 min with reason
  ``holding_cap_sport``.
- Sport position NOT force-closed at T+29 min.
- Non-sport position is unaffected by ``SPORT_MAX_HOLDING_S`` (still
  governed by the 12h ``MAX_HOLDING_PERIOD_S``).
- Edge: ``market_resolved`` fires BEFORE ``holding_cap_sport`` when both
  conditions are simultaneously true (priority order).
- Sanity: the two new config knobs and runtime keys exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.control.runtime_config import (
    ALLOWED_KEYS,
    BOUNDS,
    INTEGER_KEYS,
    _defaults_from_settings,
)
from src.engine.paper_trader import (
    STOP_LOSS_FOLLOW,
    STOP_LOSS_SPORT,
    OpenPaperTrade,
    PaperTrader,
)


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


def _make_trader() -> PaperTrader:
    return PaperTrader(redis_client=_make_redis())


def _make_open_trade(
    *,
    trade_id: int = 1,
    market_id: str = "market-sport-1",
    token_id: str = "tok-A",
    strategy: str = "follow",
    direction: str = "yes",
    entry_price: float = 0.52,
    size_usdc: float = 200.0,
    opened_at: datetime | None = None,
    category: str | None = "sports",
) -> OpenPaperTrade:
    """Build an open trade with the category embedded in leader_context
    where the production code expects it (``trade_context.market_category``).
    Pass ``category=None`` to simulate a legacy trade with no category
    hint — the production code will fall through to the DB lookup which
    the test harness mocks out via ``_resolve_trade_category``.
    """
    leader_context: dict = {}
    if category is not None:
        leader_context["trade_context"] = {"market_category": category}
    return OpenPaperTrade(
        id=trade_id,
        market_id=market_id,
        token_id=token_id,
        direction=direction,
        strategy=strategy,
        entry_price=entry_price,
        size_usdc=size_usdc,
        leader_wallet="0xLeader",
        confidence=0.8,
        opened_at=opened_at or datetime.now(tz=timezone.utc),
        leader_context=leader_context,
    )


def _stub_trader_for_monitor(
    trader: PaperTrader,
    *,
    exit_price: float = 0.50,
    mark_price: float | None = None,
    market_resolved: bool = False,
    resolution_price: float | None = None,
    leader_exited: bool = False,
    hours_to_resolution: float = 48.0,
) -> None:
    """Wire the bare-minimum mocks ``_check_open_positions`` needs so a
    test can focus on the branch under inspection."""
    trader._exit_bid = AsyncMock(return_value=exit_price)
    trader._mark_mid = AsyncMock(
        return_value=mark_price if mark_price is not None else exit_price
    )
    trader._is_market_resolved = AsyncMock(return_value=market_resolved)
    trader._fetch_market_resolution = AsyncMock(return_value=resolution_price)
    trader._leader_exited_recently = AsyncMock(return_value=leader_exited)
    trader._hours_until_resolution = AsyncMock(return_value=hours_to_resolution)
    trader.close_trade = AsyncMock(return_value=True)
    trader._record_equity_sample = AsyncMock()


# --------------------------------------------------------------------------- #
# Adaptive stop-loss — Tier 1 fix #5                                          #
# --------------------------------------------------------------------------- #


class TestAdaptiveStopLoss:
    """Sport positions must use STOP_LOSS_SPORT (3%) instead of the
    legacy STOP_LOSS_FOLLOW (8%) / STOP_LOSS_FADE (5%) thresholds."""

    @pytest.mark.asyncio
    async def test_sport_position_closes_at_minus_3pct(self):
        """A sport FOLLOW down 5% (entry 0.52 → 0.494) is comfortably
        past the 3% sport stop but well inside the legacy 8% threshold.
        It MUST close as ``stop_loss``."""
        trader = _make_trader()
        # Entry 0.52, drift to 0.494 → -5.0% drawdown.
        trade = _make_open_trade(
            entry_price=0.52,
            opened_at=datetime.now(tz=timezone.utc) - timedelta(seconds=30),
            category="sports",
        )
        trader._open_trades = [trade]
        # mark_price == exit_price so min(mid, bid) PnL is the same drift.
        _stub_trader_for_monitor(trader, exit_price=0.494, mark_price=0.494)

        await trader._check_open_positions()

        assert trader.close_trade.called, (
            "Sport position at -5% should have triggered stop_loss "
            "under the new 3% threshold."
        )
        trade_id, exit_price, reason = trader.close_trade.call_args[0]
        assert trade_id == trade.id
        assert reason == "stop_loss", (
            f"Expected stop_loss (sport 3% threshold), got {reason!r}."
        )

    @pytest.mark.asyncio
    async def test_non_sport_position_still_uses_8pct(self):
        """A crypto FOLLOW down 5% (entry 0.52 → 0.494) is INSIDE the
        legacy 8% threshold — it must NOT close on stop_loss."""
        trader = _make_trader()
        trade = _make_open_trade(
            entry_price=0.52,
            opened_at=datetime.now(tz=timezone.utc) - timedelta(seconds=30),
            category="crypto",
        )
        trader._open_trades = [trade]
        _stub_trader_for_monitor(trader, exit_price=0.494, mark_price=0.494)

        await trader._check_open_positions()

        # No close fired — the -5% drawdown is inside the 8% non-sport stop.
        if trader.close_trade.called:
            _, _, reason = trader.close_trade.call_args[0]
            assert reason != "stop_loss", (
                f"Non-sport position closed at -5% via {reason!r}; the "
                "8% threshold should have held the position open."
            )
        # But a -9% drawdown on the SAME non-sport trade should fire
        # stop_loss — confirms the legacy threshold is still in effect.
        trader.close_trade.reset_mock()
        trader._exit_bid = AsyncMock(return_value=0.52 * (1 - 0.09))
        trader._mark_mid = AsyncMock(return_value=0.52 * (1 - 0.09))
        await trader._check_open_positions()
        assert trader.close_trade.called
        _, _, reason = trader.close_trade.call_args[0]
        assert reason == "stop_loss"

    @pytest.mark.asyncio
    async def test_sport_position_inside_3pct_holds(self):
        """A sport position at exactly -2% must NOT close on stop_loss
        — the gate is `<= -stop`, so -2% sits inside the 3% threshold."""
        trader = _make_trader()
        trade = _make_open_trade(
            entry_price=0.52,
            opened_at=datetime.now(tz=timezone.utc) - timedelta(seconds=30),
            category="sports",
        )
        trader._open_trades = [trade]
        # 0.52 * 0.98 = 0.5096 → -2.0% drift
        _stub_trader_for_monitor(trader, exit_price=0.5096, mark_price=0.5096)

        await trader._check_open_positions()

        if trader.close_trade.called:
            _, _, reason = trader.close_trade.call_args[0]
            assert reason != "stop_loss", (
                f"Sport position closed prematurely at -2% via {reason!r}."
            )


# --------------------------------------------------------------------------- #
# Sport holding cap — Tier 1 fix #4                                           #
# --------------------------------------------------------------------------- #


class TestSportHoldingCap:
    """Sport positions must force-close past SPORT_MAX_HOLDING_S (30 min)
    with reason ``holding_cap_sport``. Non-sport positions keep the
    legacy MAX_HOLDING_PERIOD_S (12h) and are unaffected."""

    @pytest.mark.asyncio
    async def test_sport_force_close_at_31_min(self):
        """A sport position opened 31 minutes ago must force-close at
        the current bid with reason ``holding_cap_sport``."""
        trader = _make_trader()
        opened_at = datetime.now(tz=timezone.utc) - timedelta(minutes=31)
        trade = _make_open_trade(
            entry_price=0.52, opened_at=opened_at, category="sports"
        )
        trader._open_trades = [trade]
        # Hold the bid steady — no other close branch should fire.
        _stub_trader_for_monitor(trader, exit_price=0.51, mark_price=0.51)

        await trader._check_open_positions()

        assert trader.close_trade.called, (
            "Sport holding cap did not trigger at T+31 min."
        )
        trade_id, exit_price, reason = trader.close_trade.call_args[0]
        assert trade_id == trade.id
        assert reason == "holding_cap_sport", (
            f"Expected holding_cap_sport, got {reason!r}. The sport "
            "safety net is mis-wired."
        )
        # Must close at the current bid (not at a stale mid / entry).
        assert exit_price == pytest.approx(0.51)

    @pytest.mark.asyncio
    async def test_sport_not_forced_at_29_min(self):
        """A sport position 29 minutes old is INSIDE the 30 min cap
        — must not close on the sport-cap branch."""
        trader = _make_trader()
        opened_at = datetime.now(tz=timezone.utc) - timedelta(minutes=29)
        trade = _make_open_trade(
            entry_price=0.52, opened_at=opened_at, category="sports"
        )
        trader._open_trades = [trade]
        # Hold the bid at entry so the stop / take branches stay quiet.
        _stub_trader_for_monitor(trader, exit_price=0.52, mark_price=0.52)

        await trader._check_open_positions()

        if trader.close_trade.called:
            _, _, reason = trader.close_trade.call_args[0]
            assert reason != "holding_cap_sport", (
                f"Sport holding cap fired prematurely at T+29 min ({reason!r})."
            )

    @pytest.mark.asyncio
    async def test_non_sport_unaffected_by_sport_cap(self):
        """A crypto FOLLOW open for 2 hours must NOT trip
        ``holding_cap_sport`` — non-sport positions keep the legacy 12h
        ``MAX_HOLDING_PERIOD_S`` envelope."""
        trader = _make_trader()
        opened_at = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        trade = _make_open_trade(
            entry_price=0.52, opened_at=opened_at, category="crypto"
        )
        trader._open_trades = [trade]
        _stub_trader_for_monitor(trader, exit_price=0.52, mark_price=0.52)

        await trader._check_open_positions()

        if trader.close_trade.called:
            _, _, reason = trader.close_trade.call_args[0]
            assert reason != "holding_cap_sport", (
                f"Non-sport position got holding_cap_sport ({reason!r}); "
                "the sport-only branch leaked across categories."
            )


# --------------------------------------------------------------------------- #
# Priority order — market_resolved beats holding_cap_sport                    #
# --------------------------------------------------------------------------- #


class TestPriorityOrder:
    """When a sport position is both past the 30 min cap AND the market
    has resolved, ``market_resolved`` MUST take priority (operator order
    from the structural-fix plan). The terminal token value (0.0 or 1.0)
    is the only authoritative exit; the sport cap is a fallback."""

    @pytest.mark.asyncio
    async def test_market_resolved_fires_before_sport_cap(self):
        """Position opened 45 min ago (past sport cap), market has
        resolved to NO (loser token = 0.0). Reason must be
        ``market_resolved``, not ``holding_cap_sport``."""
        trader = _make_trader()
        opened_at = datetime.now(tz=timezone.utc) - timedelta(minutes=45)
        trade = _make_open_trade(
            entry_price=0.52, opened_at=opened_at, category="sports"
        )
        trader._open_trades = [trade]
        # Resolved market, this token lost.
        _stub_trader_for_monitor(
            trader,
            exit_price=0.01,
            mark_price=0.01,
            market_resolved=True,
            resolution_price=0.0,
        )

        await trader._check_open_positions()

        assert trader.close_trade.called
        trade_id, exit_price, reason = trader.close_trade.call_args[0]
        assert trade_id == trade.id
        assert reason == "market_resolved", (
            f"market_resolved must beat holding_cap_sport; got {reason!r}. "
            "Without this priority a sport cap close would book against "
            "the bid instead of the authoritative 0.0/1.0 terminal value."
        )
        # Must use the terminal value, not the stale bid.
        assert exit_price == 0.0


# --------------------------------------------------------------------------- #
# Sanity: config + runtime knobs                                              #
# --------------------------------------------------------------------------- #


def test_sport_settings_present():
    """Defensive: a settings rename here silently breaks the safety net."""
    assert getattr(settings, "STOP_LOSS_SPORT", None) is not None, (
        "settings.STOP_LOSS_SPORT missing — paper_trader stop-loss "
        "selector will fall back to the legacy 8% threshold."
    )
    assert getattr(settings, "SPORT_MAX_HOLDING_S", None) is not None, (
        "settings.SPORT_MAX_HOLDING_S missing — the sport hold cap "
        "branch will use the 12h non-sport cap instead."
    )
    # Module-level constant must match the settings default (operators
    # tune via env / runtime_config, NOT by editing the constant).
    assert STOP_LOSS_SPORT == pytest.approx(float(settings.STOP_LOSS_SPORT))
    # Sane absolute bounds — catches a typo like 30 (3000%) or 0.0003.
    assert 0.005 <= float(settings.STOP_LOSS_SPORT) <= 0.30
    # Sport cap must be strictly tighter than the non-sport cap.
    assert int(settings.SPORT_MAX_HOLDING_S) < int(settings.MAX_HOLDING_PERIOD_S)


def test_sport_runtime_knobs_registered():
    """Both safeguards must be flippable via ``runtime_config`` so the
    operator can tune from the dashboard without redeploying."""
    assert "sport_max_holding_s" in ALLOWED_KEYS
    assert "stop_loss_sport" in ALLOWED_KEYS
    # Bounds must exist so set_overrides won't crash on a write.
    assert "sport_max_holding_s" in BOUNDS
    assert "stop_loss_sport" in BOUNDS
    # Hold-cap is seconds → integer-coerced. Stop-loss is a float.
    assert "sport_max_holding_s" in INTEGER_KEYS
    assert "stop_loss_sport" not in INTEGER_KEYS
    # Defaults wired from settings.
    defaults = _defaults_from_settings()
    assert defaults["sport_max_holding_s"] == int(settings.SPORT_MAX_HOLDING_S)
    assert defaults["stop_loss_sport"] == pytest.approx(
        float(settings.STOP_LOSS_SPORT)
    )
    # Defaults must respect the bounds (catches accidental drift).
    lo, hi = BOUNDS["sport_max_holding_s"]
    assert lo <= defaults["sport_max_holding_s"] <= hi
    lo, hi = BOUNDS["stop_loss_sport"]
    assert lo <= defaults["stop_loss_sport"] <= hi
    # The legacy FOLLOW stop must still be looser than the sport stop —
    # if a future refactor accidentally equalises them, the test catches
    # the lost edge.
    assert float(settings.STOP_LOSS_SPORT) < STOP_LOSS_FOLLOW
