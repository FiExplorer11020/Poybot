"""Tests for the Round 10 (The Truth Test) causal gate in ConfidenceEngine.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.5.

Coverage:
  1. Flag OFF (default) — gate is a no-op, behavior byte-identical
     to pre-R10. This is the regression-proof test.
  2. Flag ON + CI excludes 0 positively — gate ALLOWS, full confidence.
  3. Flag ON + CI excludes 0 negatively (ci_high < 0) — gate DOWNGRADES.
  4. Flag ON + CI brackets 0 — gate DOWNGRADES.
  5. Flag ON + no causal_estimates row — gate DOWNGRADES gracefully.
  6. Flag ON + DB read fails — gate falls back to no-op (defensive).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine.confidence_engine import ConfidenceEngine


def _make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    return ConfidenceEngine(redis_client=redis)


def _mock_get_db(row=None, raise_exc: bool = False):
    """Patcher for src.engine.confidence_engine.get_db.

    ``row`` is the dict returned from fetchrow; ``raise_exc=True`` makes
    the DB call raise.
    """
    conn = AsyncMock()
    if raise_exc:
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("db boom"))
    else:
        conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx)


def _mock_runtime_config(enabled: bool):
    """Patcher for the runtime_config import inside the gate."""
    cfg = MagicMock()
    cfg.effective = AsyncMock(
        return_value={"causal_gating_enabled": bool(enabled)}
    )

    def _get_runtime_config():
        return cfg

    return patch(
        "src.control.runtime_config.get_runtime_config",
        side_effect=_get_runtime_config,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCausalGateOff:
    @pytest.mark.asyncio
    async def test_flag_off_returns_none(self):
        """When causal_gating_enabled=False (default), gate returns None.

        This is the regression-proof contract: nothing about pre-R10
        behavior changes when the flag is off.
        """
        engine = _make_engine()
        # No DB patcher needed — the gate should NEVER read DB when
        # the flag is off.
        with _mock_runtime_config(enabled=False):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is None


class TestCausalGateOnEvidenceClean:
    @pytest.mark.asyncio
    async def test_ci_excludes_zero_positively_allowed(self):
        """ci_low > 0 + converged -> result='allowed', full multiplier."""
        engine = _make_engine()
        row = {
            "causal_ate": 0.5,
            "causal_ate_ci_low": 0.1,
            "causal_ate_ci_high": 0.9,
            "wu_hausman_p": 0.01,
            "first_stage_f": 50.0,
            "convergence": "converged",
        }
        with _mock_runtime_config(enabled=True), _mock_get_db(row=row):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is not None
        assert result["result"] == "allowed"
        assert result["follow_multiplier"] == 1.0
        assert result["ate"] == 0.5

    @pytest.mark.asyncio
    async def test_ci_excludes_zero_negatively_downgraded(self):
        """ci_high < 0 -> result='downgraded' (no positive evidence)."""
        engine = _make_engine()
        row = {
            "causal_ate": -0.5,
            "causal_ate_ci_low": -0.9,
            "causal_ate_ci_high": -0.1,
            "wu_hausman_p": 0.01,
            "first_stage_f": 50.0,
            "convergence": "converged",
        }
        with _mock_runtime_config(enabled=True), _mock_get_db(row=row):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is not None
        assert result["result"] == "downgraded"
        assert result["follow_multiplier"] == 0.5


class TestCausalGateOnEvidenceUnclear:
    @pytest.mark.asyncio
    async def test_ci_brackets_zero_downgraded(self):
        """CI includes 0 -> result='downgraded'."""
        engine = _make_engine()
        row = {
            "causal_ate": 0.05,
            "causal_ate_ci_low": -0.2,
            "causal_ate_ci_high": 0.3,
            "wu_hausman_p": 0.4,
            "first_stage_f": 50.0,
            "convergence": "converged",
        }
        with _mock_runtime_config(enabled=True), _mock_get_db(row=row):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is not None
        assert result["result"] == "downgraded"
        assert result["follow_multiplier"] == 0.5

    @pytest.mark.asyncio
    async def test_weak_instruments_downgraded(self):
        """convergence='weak_instruments' -> downgrade even if CI > 0.

        Per spec § 6 the instrument-validity gate is precisely the
        first-stage F + Wu-Hausman; a 'weak_instruments' fit fails the
        gate regardless of where its CI lands.
        """
        engine = _make_engine()
        row = {
            "causal_ate": 0.5,
            "causal_ate_ci_low": 0.1,
            "causal_ate_ci_high": 0.9,
            "wu_hausman_p": 0.01,
            "first_stage_f": 3.0,
            "convergence": "weak_instruments",
        }
        with _mock_runtime_config(enabled=True), _mock_get_db(row=row):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is not None
        assert result["result"] == "downgraded"


class TestCausalGateMissingData:
    @pytest.mark.asyncio
    async def test_no_row_downgrades(self):
        """No causal_estimates row -> downgrade (safer default)."""
        engine = _make_engine()
        with _mock_runtime_config(enabled=True), _mock_get_db(row=None):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is not None
        assert result["result"] == "downgraded"
        assert result["follow_multiplier"] == 0.5
        assert result["ate"] is None

    @pytest.mark.asyncio
    async def test_db_read_fails_returns_none(self):
        """When the DB read raises, we fall back to allowed (no-op).

        Rationale: a transient DB outage should NOT silently degrade
        every signal. The gate is opt-in; failing-open preserves the
        pre-R10 behavior under infra issues.
        """
        engine = _make_engine()
        with _mock_runtime_config(enabled=True), _mock_get_db(raise_exc=True):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {"wallet_strategy": "directional"}
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_default_pool_class_when_strategy_missing(self):
        """When wallet_strategy is absent, we look up 'all_followers'."""
        engine = _make_engine()
        row = {
            "causal_ate": 0.5,
            "causal_ate_ci_low": 0.1,
            "causal_ate_ci_high": 0.9,
            "wu_hausman_p": 0.01,
            "first_stage_f": 50.0,
            "convergence": "converged",
        }
        with _mock_runtime_config(enabled=True), _mock_get_db(row=row):
            result = await engine._maybe_apply_causal_gate(
                "0xLEADER", {}  # no wallet_strategy
            )
        assert result is not None
        assert result["pool_class"] == "all_followers"
