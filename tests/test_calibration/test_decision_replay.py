"""Tests for :mod:`src.calibration.decision_replay`.

Verifies the atomic prediction-logging hook and the position-tracker
outcome backfill path. Both call into a connection that the caller
owns — we mock the connection and inspect the SQL + bind values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.decision_replay import (
    DecisionPrediction,
    DecisionPredictionLogger,
    fill_actual_outcomes,
    record_decision_predictions,
)


# --------------------------------------------------------------------------- #
# 1. DecisionPrediction.from_decision_context                                 #
# --------------------------------------------------------------------------- #


def test_from_decision_context_extracts_thompson_samples():
    decision = SimpleNamespace(
        thompson_follow=0.6,
        thompson_fade=0.2,
        trade_context={
            "wallet_strategy": "directional",
            "strategy_confidence": 0.85,
            "volume_forecast": {
                "total_volume_usdc": 12000.0,
                "ci_low": 8000.0,
                "ci_high": 16000.0,
            },
            "causal_gate": {
                "ate": 1.4,
                "ci_low": 1.1,
                "ci_high": 1.7,
                "hawkes_alpha_mu": 1.6,
            },
        },
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence == 0.6
    assert pred.fade_confidence == 0.2
    assert pred.strategy_class == "directional"
    assert pred.strategy_confidence == 0.85
    assert pred.volume_forecast_usdc == 12000.0
    assert pred.volume_forecast_ci_low == 8000.0
    assert pred.volume_forecast_ci_high == 16000.0
    assert pred.causal_ate == 1.4
    assert pred.hawkes_alpha_mu == 1.6
    assert pred.predicted_at is not None


def test_from_decision_context_missing_context_returns_all_none():
    decision = SimpleNamespace(
        thompson_follow=None,
        thompson_fade=None,
        trade_context=None,
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence is None
    assert pred.strategy_class is None
    assert pred.volume_forecast_usdc is None
    # predicted_at always populated — the wall clock is always known.
    assert pred.predicted_at is not None


def test_from_decision_context_nan_fades_to_none():
    decision = SimpleNamespace(
        thompson_follow=float("nan"),
        thompson_fade=None,
        trade_context={},
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence is None  # NaN filtered out


# --------------------------------------------------------------------------- #
# 2. record_decision_predictions                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_record_decision_predictions_issues_atomic_insert():
    """Single INSERT with ON CONFLICT DO NOTHING — the contract is
    atomic with the decision_log write owned by the caller."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    predictions = DecisionPrediction(
        follow_confidence=0.7,
        fade_confidence=0.2,
        strategy_class="momentum",
        strategy_confidence=0.6,
        predicted_at=datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc),
    )
    await record_decision_predictions(conn, 42, predictions)
    args = conn.execute.await_args
    assert args is not None
    sql = args.args[0]
    assert "INSERT INTO decision_predictions" in sql
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    # First positional argument is the decision_id.
    assert args.args[1] == 42


@pytest.mark.asyncio
async def test_record_skips_for_invalid_decision_id():
    """decision_id <= 0 → no SQL issued (defensive — saves a round-trip)."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    await record_decision_predictions(conn, 0, DecisionPrediction())
    conn.execute.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 3. fill_actual_outcomes                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fill_outcomes_uses_coalesce_pattern():
    """The UPDATE uses COALESCE so partial outcome data preserves
    pre-existing values (the position_tracker can call this twice —
    once with pnl, later with followup_volume — without clobbering)."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    await fill_actual_outcomes(
        conn,
        decision_id=42,
        pnl_usdc=12.5,
        followup_volume_usdc=None,
        closed_at=datetime(2026, 5, 11, 11, 0, 0, tzinfo=timezone.utc),
    )
    args = conn.execute.await_args
    sql = args.args[0]
    assert "UPDATE decision_predictions" in sql
    assert "COALESCE" in sql
    # pnl is the second positional arg
    assert args.args[2] == 12.5
    # followup_volume_usdc is None → preserves existing via COALESCE
    assert args.args[3] is None


@pytest.mark.asyncio
async def test_fill_outcomes_skips_for_invalid_decision_id():
    conn = MagicMock()
    conn.execute = AsyncMock()
    await fill_actual_outcomes(
        conn,
        decision_id=-1,
        pnl_usdc=1.0,
        followup_volume_usdc=2.0,
        closed_at=datetime.now(tz=timezone.utc),
    )
    conn.execute.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 4. Logger class is a thin namespace                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_logger_record_calls_through():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    predictions = DecisionPrediction(follow_confidence=0.5)
    await DecisionPredictionLogger.record(conn, 1, predictions)
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_logger_fill_outcomes_calls_through():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    await DecisionPredictionLogger.fill_outcomes(
        conn, 1, pnl_usdc=1.0, followup_volume_usdc=2.0, closed_at=None
    )
    conn.execute.assert_awaited_once()
