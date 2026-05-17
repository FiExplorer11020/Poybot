"""
Integration tests for the live-match detector wired into
`confidence_engine.evaluate()` (Tier 1 fix #2 + #3, 2026-05-17).

The detector is called BEFORE the leader_quality_gate, so an
`is_live_match=TRUE` market must produce a SKIP with reason
`live_match_blocked|signal=<reason>` regardless of the leader's
Falcon tier / posterior counts.

Style mirrors `tests/test_engine/test_confidence_engine_falcon_prior.py`
(the AsyncMock patcher + readiness/profile stubs).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine.confidence_engine import ConfidenceEngine


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return ConfidenceEngine(redis_client=redis)


def _patch_db_with_rows(rows_by_query: dict):
    """Patch `confidence_engine.get_db` so fetchrow returns the first
    row whose SQL substring matches the executed statement. Mirrors
    the helper in test_confidence_engine_falcon_prior.py."""

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

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx)


def _bypass_liquidity_gate() -> dict:
    """The engine queries `markets.volume_24h` for the liquidity gate;
    return $100k so it passes and we reach the live-match gate."""
    return {
        "SELECT volume_24h FROM markets WHERE market_id = $1": {
            "volume_24h": 100_000.0,
        },
    }


def _readiness(**overrides) -> dict:
    base = {
        "trades_observed": 100,
        "positions_resolved": 50,
        "confirmed_followers": 10,
        "external_resolved_count": 0,
        "external_wins": 0,
        "external_losses": 0,
        "falcon_score": 80.0,
    }
    base.update(overrides)
    return base


def _trade(**overrides) -> dict:
    base = {
        "wallet_address": "0xLEADER",
        "market_id": "mkt-test",
        "token_id": "tok-test",
        "is_leader": True,
        "price": 0.55,
    }
    base.update(overrides)
    return base


def _wire_engine_stubs(engine: ConfidenceEngine) -> None:
    """Stub the readiness / profile / context / signal-audit / log
    methods so the engine reaches the live-match gate cleanly."""
    engine._get_readiness = AsyncMock(return_value=_readiness())
    engine._get_profile_snapshot = AsyncMock(
        return_value={"accuracy": {"overall": 0.70, "resolved_count": 50}}
    )
    engine._build_trade_context = AsyncMock(
        return_value={"process_score": 0.9, "category": "sports"}
    )
    engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
    engine._log_decision = AsyncMock()
    engine._emit = AsyncMock()


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


class TestLiveMatchBlockedAtEngine:
    """The detector fires BEFORE the leader_quality_gate, so a passing
    leader on a live market still gets blocked with the live-match
    reason — never the leader-resolved reason."""

    @pytest.mark.asyncio
    async def test_gamma_flag_market_skipped(self):
        engine = _make_engine()
        _wire_engine_stubs(engine)

        with _patch_db_with_rows(_bypass_liquidity_gate()), patch(
            "src.economics.live_match_detector.is_live_match",
            AsyncMock(return_value=(True, "gamma_flag")),
        ), patch(
            "src.economics.live_match_detector.live_match_block_enabled",
            AsyncMock(return_value=True),
        ):
            result = await engine.evaluate(_trade())

        assert result is None
        # The live-match SKIP must be present and its reason carries the
        # `signal=<reason>` suffix the dashboard inspects.
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        assert any(
            r.startswith("live_match_blocked|signal=gamma_flag")
            for r in reasons
        ), f"Expected live_match_blocked SKIP, got: {reasons}"

    @pytest.mark.asyncio
    async def test_regex_signal_skipped(self):
        engine = _make_engine()
        _wire_engine_stubs(engine)

        with _patch_db_with_rows(_bypass_liquidity_gate()), patch(
            "src.economics.live_match_detector.is_live_match",
            AsyncMock(return_value=(True, "regex_map")),
        ), patch(
            "src.economics.live_match_detector.live_match_block_enabled",
            AsyncMock(return_value=True),
        ):
            result = await engine.evaluate(_trade())

        assert result is None
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        assert any(
            r == "live_match_blocked|signal=regex_map" for r in reasons
        ), f"Expected regex_map SKIP, got: {reasons}"
        # And critically — we did NOT fall through to the leader-gate.
        assert not any("leader_resolved_too_low" in r for r in reasons)
        assert not any("leader_winrate_too_low" in r for r in reasons)


class TestLiveMatchAllowedAtEngine:
    """The detector must not block long-dated futures / non-live
    markets. When `is_live_match` returns False, evaluate() proceeds
    to the leader_quality_gate as before."""

    @pytest.mark.asyncio
    async def test_no_match_proceeds_to_leader_gate(self):
        """`no_match` reason must allow evaluation to continue. The
        leader_quality_gate may still SKIP (or not), but the live-match
        SKIP must NOT fire."""
        engine = _make_engine()
        _wire_engine_stubs(engine)

        with _patch_db_with_rows(_bypass_liquidity_gate()), patch(
            "src.economics.live_match_detector.is_live_match",
            AsyncMock(return_value=(False, "no_match")),
        ), patch(
            "src.economics.live_match_detector.live_match_block_enabled",
            AsyncMock(return_value=True),
        ):
            await engine.evaluate(_trade())

        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        # The live-match SKIP must NOT appear.
        assert not any(
            r.startswith("live_match_blocked") for r in reasons
        ), (
            f"live_match_blocked SKIP fired on a no_match market: "
            f"{reasons}"
        )

    @pytest.mark.asyncio
    async def test_gate_disabled_lets_live_market_through(self):
        """Operator master gate. Even on a clearly-live market, when
        `live_match_block_enabled` returns False the detector still
        runs (for telemetry) but does NOT short-circuit the engine."""
        engine = _make_engine()
        _wire_engine_stubs(engine)

        with _patch_db_with_rows(_bypass_liquidity_gate()), patch(
            "src.economics.live_match_detector.is_live_match",
            AsyncMock(return_value=(True, "gamma_flag")),
        ), patch(
            "src.economics.live_match_detector.live_match_block_enabled",
            AsyncMock(return_value=False),
        ):
            await engine.evaluate(_trade())

        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        assert not any(
            r.startswith("live_match_blocked") for r in reasons
        ), (
            "live_match_blocked SKIP fired even though the master "
            f"gate is OFF: {reasons}"
        )

    @pytest.mark.asyncio
    async def test_predicate_exception_is_swallowed(self):
        """If the predicate raises (e.g. DB transient), the engine
        must NOT crash — fall back to permissive behavior so a
        runtime glitch doesn't silently block all signals. This
        matches the existing `try/except` pattern around
        `_read_runtime_setting` in paper_trader."""
        engine = _make_engine()
        _wire_engine_stubs(engine)

        with _patch_db_with_rows(_bypass_liquidity_gate()), patch(
            "src.economics.live_match_detector.is_live_match",
            AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            # The crucial assertion: this does NOT raise.
            await engine.evaluate(_trade())

        # And no live_match_blocked SKIP was logged.
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        reasons = [c[0][7] for c in skip_calls]
        assert not any(
            r.startswith("live_match_blocked") for r in reasons
        ), f"live_match_blocked SKIP fired despite predicate raising: {reasons}"
