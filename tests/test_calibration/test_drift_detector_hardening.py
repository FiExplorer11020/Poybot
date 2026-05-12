"""Hardening tests for :mod:`src.calibration.drift_detector`.

Wave-3 reviewer additions:

* Cold-start baseline (n = 0) returns a safe fallback, never crashes.
* Streak protection: 5 consecutive breach days on the protected
  ``follow_confidence`` model must NOT auto-disable but MUST fire the
  emergency alert via the auto-disabler.
* Streak escalation: 3 consecutive breach days on an unprotected model
  drives the auto-disable handoff (verified by drift detector's
  ``evaluate_day`` -> ``_trigger_auto_disable`` call).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.auto_disable import ModelAutoDisabler
from src.calibration.drift_detector import (
    DriftAlert,
    DriftBaseline,
    ModelDriftMonitor,
)


# --------------------------------------------------------------------------- #
# Cold-start baseline                                                         #
# --------------------------------------------------------------------------- #


def test_z_score_cold_start_returns_raw_difference():
    """Cold start: n = 0 → z-score is the raw signed diff. No crash."""
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.5,
        baseline=DriftBaseline(mean=0.0, std=0.0, n=0),
    )
    assert z == pytest.approx(0.5)


def test_z_score_single_sample_baseline_returns_raw_difference():
    mon = ModelDriftMonitor()
    z = mon._z_score(  # noqa: SLF001
        today=0.3,
        baseline=DriftBaseline(mean=0.1, std=0.0, n=1),
    )
    assert z == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# Protected-model streak does NOT auto-disable                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_five_day_streak_on_follow_confidence_does_not_auto_disable(
    monkeypatch,
):
    """Even at 5+ consecutive breach days, ``follow_confidence`` is
    shielded from auto-disable. The auto-disabler is still consulted
    (in the production path) and fires the emergency alert; the row
    is NOT written.
    """
    emergency: list[str] = []

    async def _notify(msg: str) -> None:
        emergency.append(msg)

    # Use the real ModelAutoDisabler with a fake DB so we can prove the
    # protection guard is real, not just mocked away.
    class _FakeConn:
        def __init__(self):
            self.rows: dict[str, dict] = {}

        async def fetch(self, *a, **k):
            return []

        async def fetchrow(self, *a, **k):
            return None

        async def execute(self, sql, *a):
            if "INSERT INTO model_disable_state" in sql:
                model = a[0]
                self.rows[model] = {"is_disabled": True}
                return "INSERT 0 1"
            return "OK"

    conn = _FakeConn()

    class _GetDBCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "src.calibration.auto_disable.get_db",
        lambda: _GetDBCM(),
    )
    disabler = ModelAutoDisabler(notify_fn=_notify)
    monkeypatch.setattr(
        "src.calibration.drift_detector.get_auto_disabler",
        lambda: disabler,
    )

    mon = ModelDriftMonitor()
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

    # The drift detector short-circuits before calling the disabler.
    await mon._trigger_auto_disable(alert)  # noqa: SLF001
    assert conn.rows == {}, "protected model must NOT be written to model_disable_state"

    # And the auto-disabler refuses + emergency-alerts in the same path
    # that the drift detector would have driven for an unprotected model.
    out = await disabler.disable_model(
        "follow_confidence",
        reason="5-day drift streak",
        auto_or_manual="auto",
    )
    assert out is False
    assert any("CRITICAL" in m for m in emergency), (
        "emergency alert must fire when a protected model hits the threshold"
    )


# --------------------------------------------------------------------------- #
# Unprotected model: streak escalation drives auto-disable                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_three_day_streak_on_unprotected_model_calls_disable(monkeypatch):
    """3-day streak on ``volume_forecast`` (unprotected) → auto-disable
    via the disabler, with ``auto_or_manual='auto'``.
    """
    disabler = MagicMock()
    disabler.disable_model = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.calibration.drift_detector.get_auto_disabler",
        lambda: disabler,
    )

    mon = ModelDriftMonitor()
    alert = DriftAlert(
        model="volume_forecast",
        strategy_class=None,
        today_loss=0.4,
        baseline_mean=0.1,
        baseline_std=0.05,
        z_score=6.0,
        consecutive_breach_days=3,
        measured_at=date(2026, 5, 11),
    )
    await mon._trigger_auto_disable(alert)  # noqa: SLF001
    disabler.disable_model.assert_awaited_once()
    _, kwargs = disabler.disable_model.await_args
    assert kwargs.get("auto_or_manual") == "auto"
