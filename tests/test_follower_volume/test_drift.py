"""
Tests for HawkesCouplingDriftDetector — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.5.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.follower_volume.drift import (
    DriftReport,
    HawkesCouplingDriftDetector,
)


def _mock_get_db(rows):
    """Build a fake DB ctx manager that returns the given fetch rows."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


@pytest.mark.asyncio
async def test_no_fits_returns_no_drift():
    """Zero fits in the table → no drift fired (not enough history)."""
    factory, _ = _mock_get_db([])
    detector = HawkesCouplingDriftDetector()
    with patch("src.follower_volume.drift.get_db", side_effect=factory):
        rep = await detector.evaluate("0xLEADER")
    assert isinstance(rep, DriftReport)
    assert rep.drift_detected is False
    assert rep.n_fits_seen == 0


@pytest.mark.asyncio
async def test_one_fit_only_returns_no_drift():
    """Only one fit so far → no transition can be measured."""
    rows = [{"convergence": "converged", "fit_at": None}]
    factory, _ = _mock_get_db(rows)
    detector = HawkesCouplingDriftDetector()
    with patch("src.follower_volume.drift.get_db", side_effect=factory):
        rep = await detector.evaluate("0xLEADER")
    assert rep.drift_detected is False
    assert rep.n_fits_seen == 1


@pytest.mark.asyncio
async def test_converged_to_bic_rejected_fires_drift():
    """The headline transition: prev=converged → latest=bic_rejected."""
    rows = [
        {"convergence": "bic_rejected", "fit_at": None},  # latest
        {"convergence": "converged", "fit_at": None},  # previous
    ]
    factory, _ = _mock_get_db(rows)
    detector = HawkesCouplingDriftDetector()
    with patch("src.follower_volume.drift.get_db", side_effect=factory):
        rep = await detector.evaluate("0xLEADER")
    assert rep.drift_detected is True
    assert rep.previous_convergence == "converged"
    assert rep.latest_convergence == "bic_rejected"


@pytest.mark.asyncio
async def test_both_converged_does_not_fire_drift():
    """Steady-state → no drift."""
    rows = [
        {"convergence": "converged", "fit_at": None},
        {"convergence": "converged", "fit_at": None},
    ]
    factory, _ = _mock_get_db(rows)
    detector = HawkesCouplingDriftDetector()
    with patch("src.follower_volume.drift.get_db", side_effect=factory):
        rep = await detector.evaluate("0xLEADER")
    assert rep.drift_detected is False


@pytest.mark.asyncio
async def test_db_failure_does_not_crash():
    """A DB error returns a no-drift report instead of raising."""

    @asynccontextmanager
    async def _broken():
        raise RuntimeError("db_down")
        yield  # pragma: no cover

    detector = HawkesCouplingDriftDetector()
    with patch("src.follower_volume.drift.get_db", side_effect=_broken):
        rep = await detector.evaluate("0xLEADER")
    assert rep.drift_detected is False
