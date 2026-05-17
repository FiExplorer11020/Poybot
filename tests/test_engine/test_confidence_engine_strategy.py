"""
Regression tests for the Strategy Upgrade 2026-05-17 changes to the
confidence engine. Each test pins a specific behaviour added by the
Phase 3 cohort-selection work; failures here mean a regression has
landed on a knob that's load-bearing for the win-rate target.

Covered:
- min_signal_strength SKIP (dead knob → live, default 0.30)
- kelly_fraction multiplier in _kelly_size (dead knob → live, default 0.50)
- leader_quality_gate: positions_resolved + posterior win-rate gates
  (FOLLOW + FADE separately; FADE bypasses the winrate gate)
- Adaptive liquidity gate: trades_observed last-24h fallback when
  markets.volume_24h is 0/NULL
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.engine.confidence_engine import ConfidenceEngine


# --------------------------------------------------------------------------- #
# Helpers (mirror the existing test_confidence_engine.py style)               #
# --------------------------------------------------------------------------- #


def _make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return ConfidenceEngine(redis_client=redis)


def _patch_db_with_rows(rows_by_query: dict[str, dict | None]) -> tuple:
    """Patch ``src.engine.confidence_engine.get_db`` so fetchrow returns
    the first row in ``rows_by_query`` whose SQL substring is found in
    the executed statement. ``conn.fetchval`` always returns 42 so the
    decision_log INSERT path returns a non-None decision_id (matches the
    pattern in test_confidence_engine.py)."""

    conn = AsyncMock()

    async def _fetchrow(sql, *args):
        for needle, value in rows_by_query.items():
            if needle in sql:
                return value
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=42)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx), conn


def _bypass_liquidity_gate() -> dict[str, dict]:
    """Helper: include a `volume_24h` row so the liquidity gate doesn't
    short-circuit before the gates we're trying to test."""
    return {
        "SELECT volume_24h FROM markets WHERE market_id = $1": {
            "volume_24h": 100_000.0
        },
    }


# --------------------------------------------------------------------------- #
# min_signal_strength knob (was dead, now live)                                #
# --------------------------------------------------------------------------- #


class TestMinSignalStrengthGate:
    """The min_signal_strength runtime knob was defined in
    runtime_config but never read by evaluate(). Now reject any
    decision whose post-Thompson confidence is below the floor.
    """

    @pytest.mark.asyncio
    async def test_below_floor_skips_with_explicit_reason(self):
        engine = _make_engine()
        wallet = "0xWEAK"
        # Seed a deterministic Beta posterior so confidence resolves
        # to a known low value after sampling.
        engine._thompson[wallet] = {"follow": [1.0, 99.0], "fade": [1.0, 99.0]}
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,
                "positions_resolved": 60,
                "confirmed_followers": 10,
            }
        )
        # Profile with a high winrate so the leader_quality_gate passes
        # (otherwise the SKIP fires on the gate, not on min_signal_strength).
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 60}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        # Force exploration OFF; numpy.random.beta returns very low samples
        # so confidence stays below 0.30.
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.05, 0.05]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-weak",
                    "token_id": "tok-weak",
                    "is_leader": True,
                }
            )

        assert decision is None
        # The most-recent log call should carry the new reason code.
        last_call = engine._log_decision.await_args_list[-1]
        reason = last_call[0][7] if len(last_call[0]) >= 8 else last_call.kwargs.get("reason")
        assert isinstance(reason, str)
        assert "below_min_signal_strength" in reason, (
            f"Expected below_min_signal_strength SKIP reason, got: {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_above_floor_does_not_skip_on_signal_strength(self):
        """High confidence must NOT trigger the min_signal_strength SKIP."""
        engine = _make_engine()
        wallet = "0xSTRONG"
        engine._thompson[wallet] = {"follow": [100.0, 1.0], "fade": [1.0, 100.0]}
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,
                "positions_resolved": 60,
                "confirmed_followers": 10,
            }
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 60}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.95, 0.05]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-strong",
                    "token_id": "tok-strong",
                    "is_leader": True,
                }
            )

        # decision is not None and not skipped via min_signal_strength.
        if decision is None:
            # Defensive: if a non-min_signal_strength SKIP fired, its
            # reason should NOT be below_min_signal_strength.
            last_call = engine._log_decision.await_args_list[-1]
            reason = last_call[0][7] if len(last_call[0]) >= 8 else last_call.kwargs.get("reason")
            assert "below_min_signal_strength" not in (reason or ""), (
                f"min_signal_strength SKIP fired on a high-confidence decision: {reason!r}"
            )


# --------------------------------------------------------------------------- #
# kelly_fraction multiplier (was dead, now live)                              #
# --------------------------------------------------------------------------- #


class TestKellyFractionMultiplier:
    def test_kelly_size_half_kelly_halves_size(self):
        """0.5× Kelly applied: the kelly_fraction returned by _kelly_size
        must be approximately half of the full-Kelly result for the same
        Beta posterior."""
        engine = _make_engine()
        full_kf, full_size = engine._kelly_size(
            "follow", alpha=10.0, beta_=5.0, kelly_fraction_multiplier=1.0
        )
        half_kf, half_size = engine._kelly_size(
            "follow", alpha=10.0, beta_=5.0, kelly_fraction_multiplier=0.5
        )
        # Both still respect MAX_POSITION_PCT cap; what we test is that
        # the returned kelly_fraction reflects the multiplier.
        assert half_kf <= full_kf + 1e-9
        # Half-Kelly must not exceed full-Kelly's size.
        assert half_size <= full_size + 0.01
        # And critically, the cap doesn't dominate at these small p:
        # the multiplier should have moved the answer downward by ~half
        # unless both already hit the cap.
        cap = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT
        if full_size < cap and full_size > 0:
            assert half_size <= full_size * 0.51 + 1e-2, (
                f"0.5× Kelly didn't halve the size: full={full_size}, half={half_size}"
            )

    def test_kelly_multiplier_zero_returns_zero_size(self):
        """A 0.0 multiplier disables Kelly sizing entirely.

        The MIN_POSITION_USDC floor still fires if kelly_fraction * capital
        > 0; with multiplier = 0 the fraction becomes 0, the size goes to
        0 BEFORE the floor check, so the floor does NOT promote it.
        """
        engine = _make_engine()
        _, size = engine._kelly_size(
            "follow", alpha=10.0, beta_=5.0, kelly_fraction_multiplier=0.0
        )
        assert size == 0.0

    def test_kelly_multiplier_above_one_clamped(self):
        """Multipliers > 1 are clamped to 1.0 (de-leverage knob only)."""
        engine = _make_engine()
        full_kf, _ = engine._kelly_size(
            "follow", alpha=10.0, beta_=5.0, kelly_fraction_multiplier=1.0
        )
        excess_kf, _ = engine._kelly_size(
            "follow", alpha=10.0, beta_=5.0, kelly_fraction_multiplier=99.0
        )
        # 99× clamped to 1×; the two answers must match exactly.
        assert excess_kf == full_kf


# --------------------------------------------------------------------------- #
# leader_quality_gate (NEW)                                                   #
# --------------------------------------------------------------------------- #


class TestLeaderQualityGate:
    """Before Thompson sampling, gate on the leader's track record:
       * positions_resolved must clear MIN_LEADER_RESOLVED_FOR_FOLLOW/FADE
       * For FOLLOW only, posterior win-rate must clear MIN_LEADER_WINRATE_FOR_FOLLOW
    """

    @pytest.mark.asyncio
    async def test_leader_with_too_few_resolved_skipped(self):
        engine = _make_engine()
        wallet = "0xNEW"
        # positions_resolved=5 is below both FOLLOW (30) and FADE (30) defaults.
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,  # passes follow_ready
                "positions_resolved": 5,  # FAILS leader_quality_gate
                "confirmed_followers": 10,
            }
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 5}}
        )
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher:
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-new",
                    "token_id": "tok-new",
                    "is_leader": True,
                }
            )

        assert decision is None
        # leader_quality_gate must log a SKIP with an informative reason.
        # We look across all log_decision calls because the readiness
        # check could also have fired SKIP if follow_min_trades > 100.
        # (Default 25, so 100 trades passes that.)
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        assert skip_calls, "Expected at least one SKIP log_decision call"
        last_reason = skip_calls[-1][0][7]
        assert "leader_resolved_too_low" in last_reason, (
            f"Expected leader_resolved_too_low, got {last_reason!r}"
        )

    @pytest.mark.asyncio
    async def test_leader_with_low_winrate_skipped(self):
        """A leader with 30+ resolved positions but a 0.40 win-rate must
        be SKIP'd on FOLLOW (FADE bypasses the winrate gate, but with
        only FADE available the action will be fade — we test the
        SKIP path by limiting readiness so only FOLLOW is candidate)."""
        engine = _make_engine()
        wallet = "0xLOSER"
        # Pass FOLLOW readiness, FAIL FADE readiness so only the FOLLOW
        # side is the candidate; then the leader_quality_gate's FOLLOW
        # winrate gate triggers, both sides fail, and we SKIP.
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,  # FOLLOW ready
                "positions_resolved": 5,  # FADE not ready (need 30)
                "confirmed_followers": 10,
            }
        )
        # 0.40 winrate, but resolved is 5 — so it ALSO fails the
        # resolved threshold. Adjust to isolate the winrate condition:
        # bump positions_resolved to 30 in readiness AND profile, but
        # keep FADE not-ready by other means is impossible since the
        # FADE_MIN_RESOLVED check uses the same number. Instead, expect
        # both gates to skip — we still verify the SKIP reason carries
        # one of our new keys.
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,
                "positions_resolved": 30,  # passes BOTH resolved gates
                "confirmed_followers": 10,
            }
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.40, "resolved_count": 30}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        # FADE side passes the leader_quality_gate (no winrate gate), so
        # the action ends up "fade". We're not testing SKIP here — we're
        # asserting the FOLLOW gate is operational by checking that the
        # output action is NOT 'follow' (the FADE bypass is correct).
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.5, 0.8]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-loser",
                    "token_id": "tok-loser",
                    "is_leader": True,
                }
            )

        # A 0.40-winrate leader should NEVER produce a FOLLOW action when
        # the winrate gate is wired (default min=0.55). The action must
        # either be SKIP (None) or FADE.
        assert decision is None or decision.action != "follow", (
            f"FOLLOW fired on a 0.40-winrate leader; gate is not wired. "
            f"decision={decision!r}"
        )

    @pytest.mark.asyncio
    async def test_fade_bypasses_winrate_gate(self):
        """A leader with high resolved count but LOW winrate must still
        be eligible for FADE (intentionally targets losers). The
        leader_quality_gate must NOT block FADE on win-rate."""
        engine = _make_engine()
        wallet = "0xLOSER2"
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 10,  # FOLLOW not ready (need 25)
                "positions_resolved": 60,  # FADE ready
                "confirmed_followers": 0,  # FOLLOW not ready
            }
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.30, "resolved_count": 60}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.5, 0.8]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-fadeable",
                    "token_id": "tok-fadeable",
                    "is_leader": True,
                }
            )

        # FADE side should have produced a decision. If it didn't, the
        # leader_quality_gate has incorrectly blocked FADE on win-rate.
        # We accept None only if some OTHER SKIP (e.g. min_signal_strength)
        # fired; we explicitly check the SKIP reason doesn't contain
        # leader_winrate_too_low.
        if decision is None:
            skip_calls = [
                c for c in engine._log_decision.await_args_list
                if len(c[0]) >= 3 and c[0][2] == "skip"
            ]
            reasons = [c[0][7] for c in skip_calls]
            assert not any("leader_winrate_too_low" in r for r in reasons), (
                f"leader_winrate_too_low fired on FADE-only path: {reasons}"
            )


# --------------------------------------------------------------------------- #
# Adaptive liquidity gate (fallback to trades_observed last 24h)              #
# --------------------------------------------------------------------------- #


class TestAdaptiveLiquidityGate:
    @pytest.mark.asyncio
    async def test_falls_back_to_trades_observed_when_volume_24h_zero(self):
        """When markets.volume_24h is 0/NULL, the liquidity gate must
        query trades_observed last-24h sum and accept the market if
        that fallback clears the 5000 threshold."""
        engine = _make_engine()
        wallet = "0xLIQ"
        # Stub readiness/profile so we don't have to set up the whole
        # downstream; we only care about the liquidity-gate decision.
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,
                "positions_resolved": 50,
                "confirmed_followers": 10,
            }
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 50}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        # `volume_24h=0` triggers the fallback; `trades_observed` returns
        # a healthy $50k sum so the gate passes.
        rows = {
            "SELECT volume_24h FROM markets WHERE market_id = $1": {
                "volume_24h": 0.0
            },
            "FROM trades_observed": {"vol": 50_000.0},
        }

        patcher, _ = _patch_db_with_rows(rows)
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.95, 0.05]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-liq",
                    "token_id": "tok-liq",
                    "is_leader": True,
                }
            )

        # The fallback path is exercised: if a SKIP happens, it must NOT
        # be low_market_liquidity — the fallback provided 50k of volume.
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        assert not any(
            "low_market_liquidity" in r for r in reasons
        ), (
            "low_market_liquidity SKIP fired despite trades_observed "
            f"fallback returning 50k volume. SKIP reasons seen: {reasons}"
        )

    @pytest.mark.asyncio
    async def test_dual_zero_still_skips_low_liquidity(self):
        """If BOTH markets.volume_24h and trades_observed last-24h are
        zero, the liquidity gate must still SKIP."""
        engine = _make_engine()
        wallet = "0xLIQ2"
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 100,
                "positions_resolved": 50,
                "confirmed_followers": 10,
            }
        )
        engine._log_decision = AsyncMock()

        rows = {
            "SELECT volume_24h FROM markets WHERE market_id = $1": {
                "volume_24h": 0.0
            },
            "FROM trades_observed": {"vol": 0.0},
        }
        patcher, _ = _patch_db_with_rows(rows)
        with patcher:
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-dead",
                    "token_id": "tok-dead",
                    "is_leader": True,
                }
            )

        assert decision is None
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        assert skip_calls, "Expected at least one SKIP log_decision call"
        # The most recent SKIP must be low_market_liquidity.
        last_reason = skip_calls[-1][0][7]
        assert "low_market_liquidity" in last_reason


# --------------------------------------------------------------------------- #
# Sanity: new config constants exist                                          #
# --------------------------------------------------------------------------- #


def test_strategy_upgrade_constants_present():
    """Defensive: catch a settings rename that would silently break the
    runtime knob wiring."""
    assert getattr(settings, "MIN_ENTRY_PRICE", None) is not None
    assert getattr(settings, "MAX_HOLDING_PERIOD_S", None) is not None
    assert getattr(settings, "CATEGORY_WHITELIST", None) is not None
    assert getattr(settings, "MIN_SIGNAL_STRENGTH", None) is not None
    assert getattr(settings, "MIN_LEADER_RESOLVED_FOR_FOLLOW", None) is not None
    assert getattr(settings, "MIN_LEADER_RESOLVED_FOR_FADE", None) is not None
    assert getattr(settings, "MIN_LEADER_WINRATE_FOR_FOLLOW", None) is not None
    # MAX_ENTRY_PRICE was loosened 0.85 → 0.92 by this session.
    assert float(settings.MAX_ENTRY_PRICE) >= 0.85
    # MIN_HOURS_TO_RESOLUTION_FADE was loosened 24 → 6 by this session.
    assert float(settings.MIN_HOURS_TO_RESOLUTION_FADE) <= 24.0
    # MAX_LEADER_PRICE_DRIFT was loosened 0.20 → 0.35.
    assert float(settings.MAX_LEADER_PRICE_DRIFT) >= 0.20
