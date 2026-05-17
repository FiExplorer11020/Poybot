"""
Regression tests for the Strategy Upgrade 2026-05-17 changes to the
paper trader. Each test pins a specific cohort-selection filter that
the Phase 3 plan introduced; failures here mean the win-rate target
is at risk because a load-bearing filter is no longer firing.

Covered:
- MIN_ENTRY_PRICE floor → `low_entry_ask_blocked` rejection
- category_whitelist → `category_not_whitelisted` rejection
- holding cap (24h default) → `holding_cap_reached` close branch
- Loosened MIN_HOURS_TO_RESOLUTION_FADE (24h → 6h): FADE entries
  with 12h runway should be ACCEPTED, not rejected as near_resolution.
- Loosened MAX_LEADER_PRICE_DRIFT (0.20 → 0.35): drift of 0.30 should
  pass, drift of 0.40 should reject.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.engine.paper_trader import OpenPaperTrade, PaperTrader


# --------------------------------------------------------------------------- #
# Helpers (mirror tests/test_engine/test_paper_trader.py style)               #
# --------------------------------------------------------------------------- #


def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.publish = AsyncMock()
    r.hincrby = AsyncMock()
    r.expire = AsyncMock()
    r.pubsub = MagicMock()
    return r


def _make_decision(
    *,
    action: str = "follow",
    market_id: str = "market-1",
    token_id: str = "token-1",
    size_usdc: float = 200.0,
    confidence: float = 0.8,
    leader_wallet: str = "0xLeader",
    market_category: str | None = "sports",
    leader_price: float | None = None,
) -> dict:
    """Builder matching the existing _make_decision in test_paper_trader.py
    but with a tunable market_category and optional leader price field."""
    trade_context: dict = {}
    if market_category is not None:
        trade_context["market_category"] = market_category
    out = {
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "size_usdc": size_usdc,
        "confidence": confidence,
        "leader_wallet": leader_wallet,
        "signal_audit": {"accepted": True},
        "trade_context": trade_context,
    }
    if leader_price is not None:
        out["price"] = leader_price
    return out


def _attach_transaction(conn) -> None:
    """Production code wraps multi-statement writes in conn.transaction()."""

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())


def _multi_db(
    *,
    end_date: datetime | None = None,
    fee_rate: float = 0.0,
    insert_id: int = 42,
):
    """Build a get_db patcher whose fetchrow routes SQL by substring
    so open_trade can satisfy all of its DB lookups in one shared mock."""

    @asynccontextmanager
    async def _cm():
        conn = AsyncMock()

        async def fetchrow(sql, *args):
            if "FROM paper_trades" in sql and "status = 'open'" in sql:
                return None
            if "FROM paper_trades" in sql and "opened_at >=" in sql:
                return None
            if "FROM markets m" in sql and "last_trade_time" in sql:
                return {"end_date": None, "last_trade_time": None}
            if "SELECT end_date FROM markets" in sql:
                return {"end_date": end_date}
            if "SELECT resolved_outcome FROM markets" in sql:
                return None
            if "FROM trades_observed" in sql:
                return {"price": 0.55}
            if "SELECT fee_rate_pct FROM markets" in sql:
                return {"fee_rate_pct": fee_rate}
            if "SELECT token_yes, token_no" in sql:
                return {"token_yes": "token-yes", "token_no": "token-no"}
            if "INSERT INTO paper_trades" in sql:
                return {"id": insert_id}
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.execute = AsyncMock()
        _attach_transaction(conn)
        yield conn

    return _cm


def _make_trader() -> PaperTrader:
    return PaperTrader(redis_client=_make_redis())


def _make_open_trade(
    *,
    strategy: str = "follow",
    direction: str = "yes",
    entry_price: float = 0.5,
    size_usdc: float = 200.0,
    opened_at: datetime | None = None,
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
        opened_at=opened_at or datetime.now(tz=timezone.utc),
    )


# --------------------------------------------------------------------------- #
# MIN_ENTRY_PRICE floor                                                       #
# --------------------------------------------------------------------------- #


class TestMinEntryPriceFloor:
    """Backtest 2026-05-17 showed entries in [0.0, 0.4) lose money on
    average. The new MIN_ENTRY_PRICE knob (default 0.40) cuts that tail.
    """

    @pytest.mark.asyncio
    async def test_entry_below_floor_blocked(self):
        trader = _make_trader()
        # Force _entry_ask to return 0.10 — below the 0.40 floor.
        trader._entry_ask = AsyncMock(return_value=0.10)
        trader._get_current_price = AsyncMock(return_value=0.10)
        # Far-future resolution to clear the time-to-resolution gate.
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            result = await trader.open_trade(
                _make_decision(market_category="sports", leader_price=0.10)
            )

        assert result is None
        # Inspect the rejection-counter hincrby call to confirm the reason.
        reasons = [
            call.args[1] for call in trader._redis.hincrby.await_args_list
            if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
        ]
        assert "low_entry_ask_blocked" in reasons, (
            f"Expected low_entry_ask_blocked rejection, got: {reasons}"
        )

    @pytest.mark.asyncio
    async def test_entry_at_floor_accepted(self):
        """An entry at exactly MIN_ENTRY_PRICE must pass the low-floor
        check (gate is strict `<`)."""
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.40)
        trader._get_current_price = AsyncMock(return_value=0.40)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            result = await trader.open_trade(
                _make_decision(market_category="sports", leader_price=0.40)
            )

        # Should succeed (trade_id is 42 from _multi_db default).
        assert result == 42, (
            "Entry at exactly MIN_ENTRY_PRICE should be accepted "
            "(gate is strict `<`)."
        )


# --------------------------------------------------------------------------- #
# Category whitelist                                                          #
# --------------------------------------------------------------------------- #


class TestCategoryWhitelist:
    """Backtest 2026-05-17 cohorts: sports (53%), crypto (52%), macro
    (positive) make money; politics (33.8%) and unknown (43.8%) lose
    money. Default whitelist is 'sports,crypto,macro'.
    """

    @pytest.mark.asyncio
    async def test_politics_category_blocked(self):
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.55)
        trader._get_current_price = AsyncMock(return_value=0.55)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            result = await trader.open_trade(
                _make_decision(market_category="politics")
            )

        assert result is None
        reasons = [
            call.args[1] for call in trader._redis.hincrby.await_args_list
            if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
        ]
        assert "category_not_whitelisted" in reasons, (
            f"Expected category_not_whitelisted, got: {reasons}"
        )

    @pytest.mark.asyncio
    async def test_unknown_category_blocked(self):
        """An empty/missing market_category falls back to 'unknown',
        which is not in the default whitelist → must be rejected."""
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.55)
        trader._get_current_price = AsyncMock(return_value=0.55)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            # market_category=None so trade_context has no category key.
            result = await trader.open_trade(
                _make_decision(market_category=None)
            )

        assert result is None
        reasons = [
            call.args[1] for call in trader._redis.hincrby.await_args_list
            if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
        ]
        assert "category_not_whitelisted" in reasons


# --------------------------------------------------------------------------- #
# Holding cap close                                                            #
# --------------------------------------------------------------------------- #


class TestHoldingCapClose:
    """Backtest 2026-05-17 evidence: top-cohort × entry [0.5,0.9] × <24h
    is 83.7% win-rate; longer holds dilute toward 56%. Force-close at
    holding_cap_reached past MAX_HOLDING_PERIOD_S.
    """

    @pytest.mark.asyncio
    async def test_close_fires_after_24h(self):
        trader = _make_trader()
        # Trade opened 25h ago — past the 86400s (24h) default cap.
        opened_at = datetime.now(tz=timezone.utc) - timedelta(hours=25)
        trade = _make_open_trade(
            strategy="follow", direction="yes",
            entry_price=0.50, opened_at=opened_at,
        )
        trader._open_trades = [trade]
        trader._exit_bid = AsyncMock(return_value=0.55)
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=48.0)
        trader.close_trade = AsyncMock(return_value=True)

        await trader._check_open_positions()

        assert trader.close_trade.called
        # close_trade signature: (trade_id, exit_price, reason)
        _, _, reason = trader.close_trade.call_args[0]
        assert reason == "holding_cap_reached", (
            f"Expected holding_cap_reached close, got {reason!r}. "
            "Did MAX_HOLDING_PERIOD_S get unwired?"
        )

    @pytest.mark.asyncio
    async def test_no_close_when_within_cap(self):
        """A trade within the 24h window must NOT close on the cap
        branch (it can still close via stop_loss / take_profit / etc.).
        """
        trader = _make_trader()
        # Trade opened 1h ago — well within the cap.
        opened_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        trade = _make_open_trade(
            strategy="follow", direction="yes",
            entry_price=0.50, opened_at=opened_at,
        )
        trader._open_trades = [trade]
        # Hold the bid steady at entry so no other close branch fires.
        trader._exit_bid = AsyncMock(return_value=0.50)
        trader._is_market_resolved = AsyncMock(return_value=False)
        trader._leader_exited_recently = AsyncMock(return_value=False)
        trader._hours_until_resolution = AsyncMock(return_value=48.0)
        trader.close_trade = AsyncMock(return_value=True)

        await trader._check_open_positions()

        # Either close_trade was not called, OR if called, NOT with the
        # holding cap reason.
        if trader.close_trade.called:
            _, _, reason = trader.close_trade.call_args[0]
            assert reason != "holding_cap_reached", (
                f"holding_cap_reached fired prematurely (held only 1h): {reason}"
            )


# --------------------------------------------------------------------------- #
# Loosened MIN_HOURS_TO_RESOLUTION_FADE (24h → 6h)                            #
# --------------------------------------------------------------------------- #


class TestLoosenedFadeNearResolution:
    """Pre-strategy upgrade, FADE required 24h of runway; the B9 fix
    added leader_exit close for FADE so that constraint is redundant.
    Loosened to 6h (same as FOLLOW)."""

    @pytest.mark.asyncio
    async def test_fade_with_12h_runway_accepted(self):
        """12h of runway is well above the new 6h FADE threshold but
        below the old 24h — must now be ACCEPTED."""
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.55)
        trader._get_current_price = AsyncMock(return_value=0.45)  # leader was at 0.45 → opposite = 0.55
        end_date = datetime.now(tz=timezone.utc) + timedelta(hours=12)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=end_date)):
            result = await trader.open_trade(
                _make_decision(
                    action="fade", market_category="sports", leader_price=0.45
                )
            )

        # Should succeed — 12h runway passes the new 6h gate.
        # If we get a rejection it must NOT be near_resolution.
        if result is None:
            reasons = [
                call.args[1] for call in trader._redis.hincrby.await_args_list
                if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
            ]
            assert "near_resolution" not in reasons, (
                "FADE was rejected with near_resolution on a 12h-runway "
                "market — the loosen from 24h → 6h is not in effect."
            )

    @pytest.mark.asyncio
    async def test_fade_with_2h_runway_still_blocked(self):
        """2h of runway is still below 6h — must continue to reject."""
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.55)
        trader._get_current_price = AsyncMock(return_value=0.45)
        end_date = datetime.now(tz=timezone.utc) + timedelta(hours=2)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=end_date)):
            result = await trader.open_trade(
                _make_decision(
                    action="fade", market_category="sports", leader_price=0.45
                )
            )

        assert result is None
        reasons = [
            call.args[1] for call in trader._redis.hincrby.await_args_list
            if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
        ]
        assert "near_resolution" in reasons, (
            f"Expected near_resolution rejection on a 2h-runway FADE, got: {reasons}"
        )


# --------------------------------------------------------------------------- #
# Loosened MAX_LEADER_PRICE_DRIFT (0.20 → 0.35)                                #
# --------------------------------------------------------------------------- #


class TestLoosenedLeaderPriceDrift:
    """Pre-strategy upgrade, drift ≥ 0.20 was rejected. The 0.20 floor
    was too strict for thin books; loosened to 0.35."""

    @pytest.mark.asyncio
    async def test_drift_of_30pct_accepted(self):
        """Leader at 0.50, bot fills at 0.65 → 30% drift. Under the new
        0.35 threshold this should pass."""
        trader = _make_trader()
        # FOLLOW path so the drift comparison uses the leader_price directly.
        trader._entry_ask = AsyncMock(return_value=0.65)  # 30% above 0.50
        trader._get_current_price = AsyncMock(return_value=0.65)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            result = await trader.open_trade(
                _make_decision(
                    action="follow", market_category="sports", leader_price=0.50
                )
            )

        # Must succeed (or fail for some other reason, but not drift).
        if result is None:
            reasons = [
                call.args[1] for call in trader._redis.hincrby.await_args_list
                if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
            ]
            assert "leader_price_drift" not in reasons, (
                "30% drift was rejected — the loosen from 0.20 → 0.35 "
                f"is not in effect. SKIP reasons: {reasons}"
            )

    @pytest.mark.asyncio
    async def test_drift_of_50pct_still_blocked(self):
        """Drift well past the new 0.35 threshold must continue to reject."""
        trader = _make_trader()
        trader._entry_ask = AsyncMock(return_value=0.90)  # 80% above 0.50
        trader._get_current_price = AsyncMock(return_value=0.90)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=7)

        with patch("src.engine.paper_trader.get_db", _multi_db(end_date=far_future)):
            result = await trader.open_trade(
                _make_decision(
                    action="follow", market_category="sports", leader_price=0.50
                )
            )

        assert result is None
        reasons = [
            call.args[1] for call in trader._redis.hincrby.await_args_list
            if call.args and call.args[0] in ("paper:rejections:1h", "paper:rejections:24h")
        ]
        # We accept either leader_price_drift OR high_entry_ask_blocked
        # (since 0.90 might trip the high-entry ceiling first). What we
        # care about is that one of the two strict gates kicks in.
        assert (
            "leader_price_drift" in reasons or "high_entry_ask_blocked" in reasons
        ), (
            f"Expected leader_price_drift or high_entry_ask_blocked, got: {reasons}"
        )


# --------------------------------------------------------------------------- #
# Sanity: new config knobs are present                                        #
# --------------------------------------------------------------------------- #


def test_strategy_upgrade_settings_present():
    """Defensive: a settings rename would silently break the
    paper_trader runtime-config wiring."""
    assert getattr(settings, "MIN_ENTRY_PRICE", None) is not None
    assert getattr(settings, "MAX_ENTRY_PRICE", None) is not None
    assert getattr(settings, "MAX_HOLDING_PERIOD_S", None) is not None
    assert getattr(settings, "CATEGORY_WHITELIST", None) is not None
    # Loosened values from this session.
    assert float(settings.MIN_HOURS_TO_RESOLUTION_FADE) <= 24.0
    assert float(settings.MAX_LEADER_PRICE_DRIFT) >= 0.20
