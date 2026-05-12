"""Tests for :mod:`src.calibration.daemon`.

Covers the run_once orchestration shape and the graceful-cancel
behaviour of run_forever. The two collaborators (aggregator + drift
monitor) are constructor-injected so the tests can supply mocks.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.daemon import CalibrationDaemon, CalibrationRunSummary
from src.calibration.drift_detector import DriftAlert
from src.calibration.loss_aggregator import LossRecord


@pytest.mark.asyncio
async def test_run_once_returns_summary_with_both_collaborator_outputs():
    """run_once orchestrates aggregator → drift_monitor and returns
    a structured summary."""
    agg = MagicMock()
    agg.run_for_day = AsyncMock(
        return_value=[
            LossRecord(
                model="follow_confidence",
                strategy_class=None,
                measured_at=date(2026, 5, 11),
                n_decisions=10,
                brier_score=0.1,
            )
        ]
    )
    monitor = MagicMock()
    monitor.evaluate_day = AsyncMock(
        return_value=[
            DriftAlert(
                model="volume_forecast",
                strategy_class=None,
                today_loss=0.5,
                baseline_mean=0.1,
                baseline_std=0.05,
                z_score=8.0,
                consecutive_breach_days=3,
                measured_at=date(2026, 5, 11),
            )
        ]
    )
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(aggregator=agg, drift_monitor=monitor)
    summary = await daemon.run_once(date(2026, 5, 11))
    assert isinstance(summary, CalibrationRunSummary)
    assert summary.target_day == date(2026, 5, 11)
    assert summary.n_loss_records == 1
    assert summary.n_drift_alerts == 1
    assert summary.auto_disabled_models == ["volume_forecast"]
    agg.run_for_day.assert_awaited_once_with(date(2026, 5, 11))
    monitor.evaluate_day.assert_awaited_once_with(date(2026, 5, 11))


@pytest.mark.asyncio
async def test_run_once_streak_below_threshold_no_auto_disable():
    agg = MagicMock()
    agg.run_for_day = AsyncMock(return_value=[])
    monitor = MagicMock()
    monitor.evaluate_day = AsyncMock(
        return_value=[
            DriftAlert(
                model="strategy_class",
                strategy_class=None,
                today_loss=0.5,
                baseline_mean=0.1,
                baseline_std=0.05,
                z_score=8.0,
                consecutive_breach_days=1,  # below 3-day threshold
                measured_at=date(2026, 5, 11),
            )
        ]
    )
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(aggregator=agg, drift_monitor=monitor)
    summary = await daemon.run_once(date(2026, 5, 11))
    assert summary.auto_disabled_models == []


@pytest.mark.asyncio
async def test_run_forever_cancellable():
    """The main loop must exit cleanly on asyncio.CancelledError."""
    agg = MagicMock()
    agg.run_for_day = AsyncMock(return_value=[])
    monitor = MagicMock()
    monitor.evaluate_day = AsyncMock(return_value=[])
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(
        aggregator=agg,
        drift_monitor=monitor,
        poll_interval_s=10.0,
    )

    # Patch _initial_backfill_if_needed to skip DB ping
    daemon._initial_backfill_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]

    task = asyncio.create_task(daemon.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_signal_exits_loop_cleanly():
    """stop() should drive run_forever to exit without raising."""
    agg = MagicMock()
    agg.run_for_day = AsyncMock(return_value=[])
    monitor = MagicMock()
    monitor.evaluate_day = AsyncMock(return_value=[])
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(
        aggregator=agg,
        drift_monitor=monitor,
        poll_interval_s=0.05,
    )
    daemon._initial_backfill_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]

    task = asyncio.create_task(daemon.run_forever())
    await asyncio.sleep(0.1)  # let it iterate at least once
    await daemon.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
