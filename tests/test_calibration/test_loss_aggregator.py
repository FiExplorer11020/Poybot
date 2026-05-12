"""Tests for :mod:`src.calibration.loss_aggregator`.

The pure math helpers are the load-bearing surface — calibration loss
numbers feed straight into the drift detector + auto-disable pipeline.
We pin each function's math against numerically-verifiable cases.
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.loss_aggregator import (
    LossRecord,
    ModelLossAggregator,
    compute_brier,
    compute_causal_residual,
    compute_ci_coverage,
    compute_log_loss,
    compute_mape,
)


# --------------------------------------------------------------------------- #
# 1. Brier score                                                              #
# --------------------------------------------------------------------------- #


def test_brier_perfect_predictions():
    """Brier = 0 iff every prediction matches its outcome."""
    assert compute_brier([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_worst_predictions():
    """Brier = 1 when every prediction is the opposite of the outcome."""
    assert compute_brier([0.0, 1.0, 0.0], [1, 0, 1]) == 1.0


def test_brier_known_mixed():
    """Spec sanity: mean((0.8-1)² + (0.3-0)² + (0.9-1)²)
    = mean(0.04, 0.09, 0.01) = 0.04666..."""
    actual = compute_brier([0.8, 0.3, 0.9], [1, 0, 1])
    assert actual == pytest.approx((0.04 + 0.09 + 0.01) / 3, rel=1e-9)


def test_brier_drops_none_values():
    """None pairs are filtered out before computing the mean."""
    assert compute_brier(
        [0.5, None, 0.5, 0.5], [1, 0, None, 1]
    ) == pytest.approx(0.25, rel=1e-9)


def test_brier_returns_none_on_empty():
    assert compute_brier([], []) is None
    assert compute_brier([None, None], [None, None]) is None


# --------------------------------------------------------------------------- #
# 2. MAPE                                                                     #
# --------------------------------------------------------------------------- #


def test_mape_perfect_forecasts_is_zero():
    assert compute_mape([100.0, 200.0], [100.0, 200.0]) == 0.0


def test_mape_50_percent_error():
    """Forecast=150 vs actual=100 → 50% error, twice → 50%."""
    assert compute_mape([150.0, 150.0], [100.0, 100.0]) == pytest.approx(0.5)


def test_mape_handles_zero_actual_via_eps_floor():
    """Actual = 0 doesn't crash — epsilon floor protects /0."""
    out = compute_mape([10.0], [0.0])
    assert out is not None
    assert math.isfinite(out)
    # 10 / eps is huge but finite
    assert out > 1e3


def test_mape_returns_none_on_empty():
    assert compute_mape([], []) is None


# --------------------------------------------------------------------------- #
# 3. CI coverage                                                              #
# --------------------------------------------------------------------------- #


def test_ci_coverage_all_inside():
    assert compute_ci_coverage([5.0, 7.0], [0.0, 0.0], [10.0, 10.0]) == 1.0


def test_ci_coverage_all_outside():
    assert compute_ci_coverage([20.0, 30.0], [0.0, 0.0], [10.0, 10.0]) == 0.0


def test_ci_coverage_half_inside():
    actual = compute_ci_coverage(
        [5.0, 20.0, 5.0, 20.0],
        [0.0, 0.0, 0.0, 0.0],
        [10.0, 10.0, 10.0, 10.0],
    )
    assert actual == 0.5


def test_ci_coverage_returns_none_on_empty():
    assert compute_ci_coverage([], [], []) is None


# --------------------------------------------------------------------------- #
# 4. Log loss                                                                 #
# --------------------------------------------------------------------------- #


def test_log_loss_perfect_class():
    """log_loss = 0 when the true class has probability 1.0."""
    # Two-class case for clarity
    probs = [[1.0, 0.0], [0.0, 1.0]]
    ys = [0, 1]
    # We use clip so log(1.0) → log(1 - eps); near-zero but not exactly 0.
    out = compute_log_loss(probs, ys)
    assert out is not None
    assert out < 1e-9  # essentially zero


def test_log_loss_worst_case_clipped():
    """log_loss is finite even when the true class has prob ~0."""
    probs = [[0.0, 1.0]]
    ys = [0]
    out = compute_log_loss(probs, ys)
    assert out is not None
    assert math.isfinite(out)
    assert out > 10  # very large but not inf


def test_log_loss_uniform_2class_is_ln2():
    """Uniform [0.5, 0.5] → log_loss = -log(0.5) = ln(2) ≈ 0.693."""
    probs = [[0.5, 0.5]] * 10
    ys = [0, 0, 0, 1, 1, 1, 0, 1, 0, 1]
    out = compute_log_loss(probs, ys)
    assert out == pytest.approx(math.log(2), rel=1e-6)


def test_log_loss_returns_none_on_empty():
    assert compute_log_loss([], []) is None
    assert compute_log_loss([[0.5, 0.5]], []) is None


# --------------------------------------------------------------------------- #
# 5. Causal residual                                                          #
# --------------------------------------------------------------------------- #


def test_causal_residual_zero_when_estimates_agree():
    out = compute_causal_residual([0.5, 0.7], [0.5, 0.7])
    assert out == pytest.approx(0.0)


def test_causal_residual_ci_width_normalisation():
    """Wide CI absorbs absolute residual — normalised residual shrinks."""
    narrow = compute_causal_residual([0.5], [1.5], [0.1])
    wide = compute_causal_residual([0.5], [1.5], [10.0])
    assert narrow is not None and wide is not None
    assert narrow > wide  # wide CI ⇒ smaller normalised residual


# --------------------------------------------------------------------------- #
# 6. Aggregator orchestration                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aggregator_run_for_day_empty_short_circuits(monkeypatch):
    """No predictions → no records persisted, no crash."""
    agg = ModelLossAggregator()

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(agg, "_fetch_predictions_for_day", _empty)
    # Persist + metrics must not run for empty result (verify no crash).
    out = await agg.run_for_day(date(2026, 5, 11))
    assert out == []


@pytest.mark.asyncio
async def test_aggregator_computes_follow_brier(monkeypatch):
    """One-day batch produces a follow_confidence record with the
    expected Brier value."""
    agg = ModelLossAggregator()

    async def _rows(*args, **kwargs):
        return [
            {
                "decision_id": 1,
                "predicted_at": None,
                "follow_confidence": 0.8,
                "fade_confidence": None,
                "strategy_class": None,
                "strategy_confidence": None,
                "hawkes_alpha_mu": None,
                "volume_forecast_usdc": None,
                "volume_forecast_ci_low": None,
                "volume_forecast_ci_high": None,
                "causal_ate": None,
                "causal_ate_ci_low": None,
                "causal_ate_ci_high": None,
                "actual_pnl_usdc": 5.0,  # win
                "actual_followup_volume_usdc": None,
                "closed_at": None,
            },
            {
                "decision_id": 2,
                "predicted_at": None,
                "follow_confidence": 0.3,
                "fade_confidence": None,
                "strategy_class": None,
                "strategy_confidence": None,
                "hawkes_alpha_mu": None,
                "volume_forecast_usdc": None,
                "volume_forecast_ci_low": None,
                "volume_forecast_ci_high": None,
                "causal_ate": None,
                "causal_ate_ci_low": None,
                "causal_ate_ci_high": None,
                "actual_pnl_usdc": -1.0,  # loss
                "actual_followup_volume_usdc": None,
                "closed_at": None,
            },
        ]

    monkeypatch.setattr(agg, "_fetch_predictions_for_day", _rows)

    async def _noop_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(agg, "_persist", _noop_persist)

    records = await agg.run_for_day(date(2026, 5, 11))
    follow_records = [r for r in records if r.model == "follow_confidence"]
    assert follow_records, "expected a follow_confidence record"
    rec = follow_records[0]
    assert rec.n_decisions == 2
    # Brier: mean((0.8-1)² + (0.3-0)²) = mean(0.04 + 0.09) = 0.065
    assert rec.brier_score == pytest.approx(0.065, rel=1e-6)


@pytest.mark.asyncio
async def test_aggregator_persists_via_on_conflict(monkeypatch):
    """Persist path uses ON CONFLICT DO UPDATE so re-runs are idempotent."""
    agg = ModelLossAggregator()

    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock(return_value="INSERT 0 1")
    fake_tx = MagicMock()
    fake_tx.__aenter__ = AsyncMock(return_value=None)
    fake_tx.__aexit__ = AsyncMock(return_value=None)
    fake_conn.transaction = MagicMock(return_value=fake_tx)

    class _GetDB:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "src.calibration.loss_aggregator.get_db",
        lambda: _GetDB(),
    )

    records = [
        LossRecord(
            model="follow_confidence",
            strategy_class=None,
            measured_at=date(2026, 5, 11),
            n_decisions=10,
            brier_score=0.1,
        )
    ]
    await agg._persist(records)  # noqa: SLF001 — explicit test
    # Ensure the INSERT was issued and contains ON CONFLICT clause.
    args = fake_conn.execute.await_args
    assert args is not None
    sql = args.args[0]
    assert "INSERT INTO calibration_loss_history" in sql
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
