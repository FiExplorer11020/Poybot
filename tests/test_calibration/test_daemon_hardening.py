"""Hardening tests for :mod:`src.calibration.daemon`.

Wave-3 reviewer additions:

* ``_initial_backfill_if_needed`` triggers the 90-day backfill when
  ``calibration_loss_history`` is empty, and skips when populated.
* Failure paths (DB unreachable) degrade gracefully — the daemon's
  hot path doesn't crash on a missing cold-start ping.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.daemon import CalibrationDaemon


def _make_db_fake(count_returned: int | None):
    """Return a ``get_db()``-shaped context manager whose ``fetchrow``
    yields ``{'n': count_returned}`` (or None when the count is None,
    simulating a missing table)."""

    class _Conn:
        async def fetchrow(self, sql, *args):
            if count_returned is None:
                return None
            return {"n": count_returned}

    class _CM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return None

    def _get_db():
        return _CM()

    return _get_db


@pytest.mark.asyncio
async def test_initial_backfill_runs_when_history_empty(monkeypatch):
    """history count = 0 → daemon kicks the 90-day backfill via
    aggregator.backfill().
    """
    monkeypatch.setattr(
        "src.database.connection.get_db", _make_db_fake(0)
    )
    agg = MagicMock()
    agg.backfill = AsyncMock(return_value=42)
    agg.run_for_day = AsyncMock(return_value=[])
    monitor = MagicMock()
    monitor.evaluate_day = AsyncMock(return_value=[])
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(
        aggregator=agg,
        drift_monitor=monitor,
        backfill_window_days=90,
    )
    await daemon._initial_backfill_if_needed()  # noqa: SLF001
    agg.backfill.assert_awaited_once_with(window_days=90)


@pytest.mark.asyncio
async def test_initial_backfill_skips_when_history_populated(monkeypatch):
    """history count > 0 → backfill is NOT called."""
    monkeypatch.setattr(
        "src.database.connection.get_db", _make_db_fake(123)
    )
    agg = MagicMock()
    agg.backfill = AsyncMock(return_value=0)
    monitor = MagicMock()
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(aggregator=agg, drift_monitor=monitor)
    await daemon._initial_backfill_if_needed()  # noqa: SLF001
    agg.backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_backfill_db_unreachable_is_silent(monkeypatch):
    """A DB failure on the count query is logged but doesn't crash
    run_forever startup.
    """

    class _BadCM:
        async def __aenter__(self):
            raise RuntimeError("simulated DB outage")

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(
        "src.database.connection.get_db",
        lambda: _BadCM(),
    )
    agg = MagicMock()
    agg.backfill = AsyncMock(return_value=0)
    monitor = MagicMock()
    monitor._days_for_disable = 3
    daemon = CalibrationDaemon(aggregator=agg, drift_monitor=monitor)
    # Must not raise:
    await daemon._initial_backfill_if_needed()  # noqa: SLF001
    agg.backfill.assert_not_awaited()
