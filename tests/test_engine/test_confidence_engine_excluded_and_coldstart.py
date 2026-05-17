"""
Tests for the 2026-05-17 round 3 quick-win patches in
``confidence_engine.evaluate``:

1. **Excluded-leader guard** — `leaders.excluded=TRUE` is the most
   authoritative deny signal in the system. The guard runs BEFORE any
   downstream gate (including stale_trade / liquidity) so an excluded
   wallet costs us only a single PK lookup.

2. **Cold-start floor** — if
   `internal_resolved + external_resolved < MIN_LEADER_TOTAL_RESOLVED`
   (default 5), skip with `cold_start_zero_resolved`. Catches zero-
   history leaders that slip past the tier-specific gates.

The tests stub out `_log_decision` and downstream dependencies so we
exercise only the new gates. The DB roundtrip used by
`_get_leader_gate_state` is patched per-test via `engine._get_leader_
gate_state = AsyncMock(...)` so we don't need a live Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine.confidence_engine import ConfidenceEngine


@asynccontextmanager
async def _liquid_db():
    """`get_db` patch that returns a healthy market_volume so the
    `low_market_liquidity` gate (lines ~265-340 in confidence_engine)
    does NOT short-circuit before our excluded / cold-start gates run.
    """
    conn = AsyncMock()

    async def fetchrow(sql, *args):
        if "SELECT volume_24h FROM markets" in sql:
            return {"volume_24h": 50_000.0}
        if "SUM(size_usdc)" in sql:
            return {"vol": 50_000.0}
        if "FROM leaders" in sql and "excluded" in sql:
            # Default: not excluded. Tests override via
            # engine._get_leader_gate_state directly so this is only
            # used if a test forgets to stub.
            return {"excluded": False, "exclude_reason": None}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=42)
    yield conn


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    return ConfidenceEngine(redis_client=redis)


def _trade(**overrides) -> dict:
    base = {
        "wallet_address": "0xLEADER",
        "market_id": "mkt-test",
        "token_id": "tok-test",
        "is_leader": True,
        "price": 0.50,
    }
    base.update(overrides)
    return base


def _readiness(**overrides) -> dict:
    """Reasonable default readiness dict — passes downstream gates so
    only the new excluded/cold-start gates can short-circuit.
    """
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


def _wire_downstream_stubs(engine: ConfidenceEngine) -> None:
    """Stub everything past the new gates so they fail open (i.e. allow
    `evaluate` to reach the next decision step). Each individual test
    can override what it needs.
    """
    engine._get_readiness = AsyncMock(return_value=_readiness())
    engine._get_profile_snapshot = AsyncMock(
        return_value={"accuracy": {"overall": 0.70, "resolved_count": 50}}
    )
    engine._build_trade_context = AsyncMock(
        return_value={"process_score": 0.9, "category": "crypto"}
    )
    engine._build_signal_audit = AsyncMock(
        return_value={"accepted": True, "reject_reason": None}
    )
    engine._log_decision = AsyncMock()
    engine._emit = AsyncMock()
    # Keep market-liquidity gate happy without a DB.
    engine._fetch_market_volume_24h = AsyncMock(return_value=10_000.0)


# --------------------------------------------------------------------------- #
# Excluded-leader guard                                                       #
# --------------------------------------------------------------------------- #


class TestExcludedLeaderGuard:
    """`leaders.excluded=TRUE` is an authoritative deny — must short-
    circuit BEFORE any downstream check, with a SKIP reason starting
    with `leader_excluded`."""

    @pytest.mark.asyncio
    async def test_excluded_true_skips_with_leader_excluded_reason(self):
        """Vanilla excluded leader → SKIP with reason `leader_excluded|
        reason=unspecified` (no exclude_reason set).
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": True, "exclude_reason": None}
        )

        result = await engine.evaluate(_trade(wallet_address="0xBANNED"))

        assert result is None
        engine._log_decision.assert_awaited_once()
        call_args = engine._log_decision.await_args[0]
        # Positional shape: (wallet, market_id, action, t_follow, t_fade,
        # kelly, confidence, reason)
        assert call_args[2] == "skip"
        assert call_args[7].startswith("leader_excluded"), (
            f"expected reason to start with 'leader_excluded', got: "
            f"{call_args[7]!r}"
        )
        assert "unspecified" in call_args[7], (
            "missing exclude_reason should surface as 'unspecified', got: "
            f"{call_args[7]!r}"
        )
        # Downstream gates must NOT run — excluded is BEFORE _get_readiness.
        engine._get_readiness.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_excluded_with_structural_bot_reason_propagates_to_audit(self):
        """When `exclude_reason='structural_bot'` the reason must appear
        verbatim in the audit log so the dashboard can categorize the
        rejection cohort.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={
                "excluded": True,
                "exclude_reason": "structural_bot",
            }
        )

        result = await engine.evaluate(_trade(wallet_address="0xBOT"))

        assert result is None
        engine._log_decision.assert_awaited_once()
        reason = engine._log_decision.await_args[0][7]
        assert reason == "leader_excluded|reason=structural_bot", (
            f"reason should embed the exclude_reason verbatim, got: "
            f"{reason!r}"
        )

    @pytest.mark.asyncio
    async def test_excluded_false_proceeds_past_guard(self):
        """`excluded=FALSE` (the default for production wallets) must
        NOT trigger the leader_excluded path — downstream gates run.

        We can't easily assert "decision is non-None" without wiring
        the full Thompson/Kelly path, but we CAN assert that the
        readiness lookup got called (proof the excluded guard let
        evaluate continue past line 200). The DB is stubbed to keep
        the liquidity gate happy so we reach `_get_readiness`.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": False, "exclude_reason": None}
        )

        # Patch get_db so the low_market_liquidity gate sees positive
        # volume and passes through to _get_readiness.
        with patch(
            "src.engine.confidence_engine.get_db",
            side_effect=lambda *a, **kw: _liquid_db(),
        ):
            await engine.evaluate(_trade(wallet_address="0xCLEAN"))

        engine._get_readiness.assert_awaited(), (
            "_get_readiness was NOT called → the excluded guard "
            "short-circuited despite excluded=False"
        )
        # And no _log_decision call should have leader_excluded as reason.
        for call in engine._log_decision.await_args_list:
            reason = call[0][7] if len(call[0]) >= 8 else ""
            assert "leader_excluded" not in reason, (
                f"leader_excluded reason fired for excluded=False wallet: "
                f"{reason!r}"
            )


# --------------------------------------------------------------------------- #
# Cold-start floor                                                            #
# --------------------------------------------------------------------------- #


class TestColdStartFloor:
    """`internal + external < MIN_LEADER_TOTAL_RESOLVED` → SKIP with
    `cold_start_zero_resolved|internal=X|external=Y|min=Z`."""

    @pytest.mark.asyncio
    async def test_cold_start_blocks_when_sum_below_floor(self):
        """internal=0 external=2 sum=2 < 5 → SKIP cold_start_zero_resolved.

        The 11 losses post-mortem identified several leaders with
        internal=0 (no reconstructed positions) and external=2-3
        (Falcon-reported only). They all lost.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": False, "exclude_reason": None}
        )
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                positions_resolved=0, external_resolved_count=2
            )
        )

        with patch(
            "src.engine.confidence_engine.get_db",
            side_effect=lambda *a, **kw: _liquid_db(),
        ):
            result = await engine.evaluate(_trade(wallet_address="0xCOLD"))

        assert result is None
        # Find the cold_start log call (other gates may also have logged).
        cold_start_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 8 and "cold_start_zero_resolved" in c[0][7]
        ]
        assert len(cold_start_calls) == 1, (
            "expected exactly one cold_start_zero_resolved log call, got: "
            f"{[c[0][7] for c in engine._log_decision.await_args_list]}"
        )
        reason = cold_start_calls[0][0][7]
        assert "internal=0" in reason
        assert "external=2" in reason
        assert "min=5" in reason

    @pytest.mark.asyncio
    async def test_internal_above_floor_passes_cold_start(self):
        """internal=10 external=0 sum=10 > 5 → cold-start gate is silent.

        We only assert the gate did NOT fire — downstream gates may
        still skip for other (correct) reasons. The test is positive
        about ONE thing: the cold_start_zero_resolved reason MUST NOT
        appear in any logged decision.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": False, "exclude_reason": None}
        )
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                positions_resolved=10, external_resolved_count=0
            )
        )

        await engine.evaluate(_trade(wallet_address="0xWARM"))

        for call in engine._log_decision.await_args_list:
            if len(call[0]) >= 8:
                assert "cold_start_zero_resolved" not in call[0][7], (
                    f"cold_start_zero_resolved fired with internal=10: "
                    f"{call[0][7]!r}"
                )

    @pytest.mark.asyncio
    async def test_external_alone_can_clear_floor(self):
        """internal=3 external=10 sum=13 > 5 → cold-start gate silent.

        Pins the SUM semantics: a leader with little reconstructed
        history but rich Falcon-reported history clears the floor.
        Without this property a brand-new system with mostly external
        priors would skip every leader.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": False, "exclude_reason": None}
        )
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                positions_resolved=3, external_resolved_count=10
            )
        )

        await engine.evaluate(_trade(wallet_address="0xFALCON"))

        for call in engine._log_decision.await_args_list:
            if len(call[0]) >= 8:
                assert "cold_start_zero_resolved" not in call[0][7], (
                    f"cold_start_zero_resolved fired with sum=13: "
                    f"{call[0][7]!r}"
                )

    @pytest.mark.asyncio
    async def test_cold_start_reason_format_pins_log_shape(self):
        """The reason string format is read by the dashboard cohort
        bucketer — pin it explicitly so a refactor can't silently break
        the dashboard.
        """
        engine = _make_engine()
        _wire_downstream_stubs(engine)
        engine._get_leader_gate_state = AsyncMock(
            return_value={"excluded": False, "exclude_reason": None}
        )
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                positions_resolved=1, external_resolved_count=1
            )
        )

        with patch(
            "src.engine.confidence_engine.get_db",
            side_effect=lambda *a, **kw: _liquid_db(),
        ):
            await engine.evaluate(_trade(wallet_address="0xPIN"))

        cold_start_reasons = [
            c[0][7] for c in engine._log_decision.await_args_list
            if len(c[0]) >= 8 and "cold_start_zero_resolved" in c[0][7]
        ]
        assert len(cold_start_reasons) == 1
        reason = cold_start_reasons[0]
        # Format: cold_start_zero_resolved|internal=X|external=Y|min=Z
        assert reason == (
            "cold_start_zero_resolved|internal=1|external=1|min=5"
        ), f"unexpected reason shape: {reason!r}"
