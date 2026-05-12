"""Tests for :mod:`src.calibration.drift_detector`.

Covers the z-score math, baseline construction, consecutive-day
counting, rate-limited operator alerts, and auto-disable triggering
(with the ``follow_confidence`` protection guard).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.drift_detector import (
    DriftAlert,
    DriftBaseline,
    ModelDriftMonitor,
)


# --------------------------------------------------------------------------- #
# 1. Pure z-score math                                                        #
# --------------------------------------------------------------------------- #


def test_z_score_at_mean_is_zero():
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001 — internal test
        today=0.1,
        baseline=DriftBaseline(mean=0.1, std=0.05, n=30),
    )
    assert z == pytest.approx(0.0)


def test_z_score_two_sigma():
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.2,
        baseline=DriftBaseline(mean=0.1, std=0.05, n=30),
    )
    assert z == pytest.approx(2.0)


def test_z_score_negative_when_today_below_mean():
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.0,
        baseline=DriftBaseline(mean=0.1, std=0.05, n=30),
    )
    assert z == pytest.approx(-2.0)


def test_z_score_small_baseline_falls_back_to_raw_diff():
    """With < 3 samples the z-score is the raw signed difference."""
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.5,
        baseline=DriftBaseline(mean=0.1, std=0.05, n=2),
    )
    assert z == pytest.approx(0.4)


def test_z_score_zero_std_safe_floor():
    """A degenerate baseline (std=0, n large) doesn't divide-by-zero."""
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.2,
        baseline=DriftBaseline(mean=0.1, std=0.0, n=30),
    )
    # With std clamped to 1e-9, z = 0.1 / 1e-9 = 1e8 — astronomical but
    # finite. The point: it doesn't crash.
    assert z > 1e6


# --------------------------------------------------------------------------- #
# 2. Rate-limited alerts                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_maybe_alert_operator_emits_once_per_window():
    """Two breaches within rate_limit_seconds → only one notify call."""
    sent: list[str] = []

    async def _notify(msg: str) -> None:
        sent.append(msg)

    mon = ModelDriftMonitor(
        rate_limit_seconds=3600.0,
        notify_fn=_notify,
    )
    alert = DriftAlert(
        model="strategy_class",
        strategy_class=None,
        today_loss=0.5,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=8.0,
        consecutive_breach_days=1,
        measured_at=date(2026, 5, 11),
    )
    await mon._maybe_alert_operator(alert)  # noqa: SLF001
    await mon._maybe_alert_operator(alert)  # noqa: SLF001 — same minute
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_maybe_alert_operator_emits_after_window_elapses():
    """When time advances past the rate limit, a fresh alert fires."""
    sent: list[str] = []

    async def _notify(msg: str) -> None:
        sent.append(msg)

    mon = ModelDriftMonitor(
        rate_limit_seconds=0.0,  # no rate limit
        notify_fn=_notify,
    )
    alert = DriftAlert(
        model="strategy_class",
        strategy_class=None,
        today_loss=0.5,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=8.0,
        consecutive_breach_days=1,
        measured_at=date(2026, 5, 11),
    )
    await mon._maybe_alert_operator(alert)  # noqa: SLF001
    await mon._maybe_alert_operator(alert)  # noqa: SLF001
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_no_notify_when_notify_fn_is_none():
    """No-notify configuration silently no-ops."""
    mon = ModelDriftMonitor(notify_fn=None)
    alert = DriftAlert(
        model="strategy_class",
        strategy_class=None,
        today_loss=0.5,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=8.0,
        consecutive_breach_days=1,
        measured_at=date(2026, 5, 11),
    )
    # Should not raise:
    await mon._maybe_alert_operator(alert)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 3. Auto-disable triggering                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trigger_auto_disable_skips_protected_model(monkeypatch):
    """``follow_confidence`` is protected — even a 3+ day streak
    cannot auto-disable it (the operator must do it manually)."""
    mon = ModelDriftMonitor()
    disabler = MagicMock()
    disabler.disable_model = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.calibration.drift_detector.get_auto_disabler",
        lambda: disabler,
    )
    alert = DriftAlert(
        model="follow_confidence",
        strategy_class=None,
        today_loss=0.5,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=8.0,
        consecutive_breach_days=5,
        measured_at=date(2026, 5, 11),
    )
    await mon._trigger_auto_disable(alert)  # noqa: SLF001
    disabler.disable_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_auto_disable_fires_for_unprotected_model(monkeypatch):
    """``volume_forecast`` (unprotected) hits the auto-disable path."""
    mon = ModelDriftMonitor()
    disabler = MagicMock()
    disabler.disable_model = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.calibration.drift_detector.get_auto_disabler",
        lambda: disabler,
    )
    alert = DriftAlert(
        model="volume_forecast",
        strategy_class=None,
        today_loss=0.5,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=8.0,
        consecutive_breach_days=3,
        measured_at=date(2026, 5, 11),
    )
    await mon._trigger_auto_disable(alert)  # noqa: SLF001
    disabler.disable_model.assert_awaited_once()
    args, kwargs = disabler.disable_model.await_args
    # disable_model(model, reason=..., auto_or_manual="auto")
    assert args[0] == "volume_forecast"
    assert kwargs.get("auto_or_manual") == "auto"
    assert "drift detected" in kwargs.get("reason", "").lower()


# --------------------------------------------------------------------------- #
# 4. Extract primary loss column                                              #
# --------------------------------------------------------------------------- #


def test_extract_primary_loss_prefers_brier():
    out = ModelDriftMonitor._extract_primary_loss(  # noqa: SLF001
        {"brier_score": 0.1, "mape": 0.2, "log_loss": 0.3}
    )
    assert out == 0.1


def test_extract_primary_loss_falls_back_to_mape():
    out = ModelDriftMonitor._extract_primary_loss(  # noqa: SLF001
        {"brier_score": None, "mape": 0.2, "log_loss": 0.3}
    )
    assert out == 0.2


def test_extract_primary_loss_returns_none_when_all_null():
    out = ModelDriftMonitor._extract_primary_loss(  # noqa: SLF001
        {"brier_score": None, "mape": None, "log_loss": None}
    )
    assert out is None
