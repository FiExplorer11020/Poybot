"""Unit tests for the reconciliation_queries module (PLAN-UIA-001).

The async functions are pure SQL — we test the verdict thresholds and
the classification logic in isolation by mocking the asyncpg connection.
For end-to-end coverage with a live DB, run `scripts/smoke.sh`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import reconciliation_queries as rq


# --------------------------------------------------------------------------- #
# Pure logic — no DB needed                                                   #
# --------------------------------------------------------------------------- #


def test_verdict_unknown_when_no_trades_evaluated():
    assert rq._verdict_for_delta(0.0, trades_evaluated=0) == "unknown"
    assert rq._verdict_for_delta(1000.0, trades_evaluated=0) == "unknown"


def test_verdict_unknown_when_delta_is_none():
    assert rq._verdict_for_delta(None, trades_evaluated=100) == "unknown"


def test_verdict_ok_below_25_usdc():
    assert rq._verdict_for_delta(24.99, trades_evaluated=1) == "ok"
    assert rq._verdict_for_delta(-24.99, trades_evaluated=1) == "ok"
    assert rq._verdict_for_delta(0.0, trades_evaluated=1) == "ok"


def test_verdict_warn_between_25_and_250():
    assert rq._verdict_for_delta(25.0, trades_evaluated=1) == "warn"
    assert rq._verdict_for_delta(-100.0, trades_evaluated=1) == "warn"
    assert rq._verdict_for_delta(249.99, trades_evaluated=1) == "warn"


def test_verdict_critical_at_or_above_250():
    assert rq._verdict_for_delta(250.0, trades_evaluated=1) == "critical"
    assert rq._verdict_for_delta(-500.0, trades_evaluated=1) == "critical"
    # The +39 784 audit case:
    assert rq._verdict_for_delta(39_784.0, trades_evaluated=2) == "critical"


def test_classify_flag_phantom():
    assert rq._classify_flag("fake_win") == "phantom"
    assert rq._classify_flag("fake_loss") == "phantom"


def test_classify_flag_premature():
    assert rq._classify_flag("still_open_in_reality") == "premature"
    assert rq._classify_flag("premature_close") == "premature"


def test_classify_flag_drift_default():
    assert rq._classify_flag("some_other_flag") == "drift"
    assert rq._classify_flag(None) == "drift"


def test_safe_iso_handles_datetime():
    dt = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    result = rq._safe_iso(dt)
    assert result == "2026-05-18T12:00:00+00:00"


def test_safe_iso_handles_none():
    assert rq._safe_iso(None) is None


def test_safe_iso_handles_string():
    assert rq._safe_iso("already-a-string") == "already-a-string"


# --------------------------------------------------------------------------- #
# reconciliation_summary — mock asyncpg                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_empty_table_returns_unknown_verdict():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "trades_evaluated": 0,
        "trades_drift_count": 0,
        "pnl_displayed_sum": 0.0,
        "sum_delta_usdc": 0.0,
        "phantom_count": 0,
        "premature_count": 0,
        "latest_divergence_at": None,
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    result = await rq.reconciliation_summary(conn, window_days=30)
    assert result["verdict"] == "unknown"
    assert result["trades_evaluated"] == 0
    assert result["trades_drift_count"] == 0
    assert result["phantom_count"] == 0
    assert result["premature_count"] == 0
    assert result["last_5_runs"] == []


@pytest.mark.asyncio
async def test_summary_critical_when_delta_above_threshold():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "trades_evaluated": 10,
        "trades_drift_count": 2,
        "pnl_displayed_sum": 39_784.0,  # the audit memory case
        "sum_delta_usdc": 41_846.0,     # displayed - oracle = 41 846  → critical
        "phantom_count": 2,
        "premature_count": 0,
        "latest_divergence_at": datetime.now(timezone.utc),
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    result = await rq.reconciliation_summary(conn, window_days=30)
    assert result["verdict"] == "critical"
    assert result["pnl_displayed_sum"] == 39_784.0
    # oracle = displayed - sum_delta = 39 784 - 41 846 = -2 062 (the audit memory's truth!)
    assert result["pnl_oracle_sum"] == pytest.approx(-2062.0)
    assert result["pnl_delta_abs"] == pytest.approx(41846.0)
    assert result["phantom_count"] == 2


@pytest.mark.asyncio
async def test_summary_age_seconds_computed_correctly():
    five_min_ago = datetime.now(timezone.utc) - timedelta(seconds=300)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "trades_evaluated": 5,
        "trades_drift_count": 1,
        "pnl_displayed_sum": 100.0,
        "sum_delta_usdc": 5.0,
        "phantom_count": 0,
        "premature_count": 0,
        "latest_divergence_at": five_min_ago,
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    result = await rq.reconciliation_summary(conn, window_days=30)
    assert result["age_s"] is not None
    assert 295 <= result["age_s"] <= 310  # 5 min ago ± slack
    assert result["run_at_iso"] is not None


# --------------------------------------------------------------------------- #
# trigger_run — Redis SET                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trigger_run_sets_redis_key():
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    conn = MagicMock()

    result = await rq.reconciliation_trigger_run(conn, redis, window_days=14)
    assert result["scheduled"] is True
    assert result["key"] == "recon:trigger:queued"
    assert result["window_days"] == 14
    redis.set.assert_called_once()
    args, kwargs = redis.set.call_args
    assert args[0] == "recon:trigger:queued"
    assert kwargs["ex"] == 300


@pytest.mark.asyncio
async def test_trigger_run_with_no_redis_returns_scheduled_false():
    conn = MagicMock()
    result = await rq.reconciliation_trigger_run(conn, None, window_days=30)
    assert result["scheduled"] is False
    assert result["key"] == "recon:trigger:queued"


@pytest.mark.asyncio
async def test_trigger_run_handles_redis_failure_gracefully():
    redis = MagicMock()
    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    conn = MagicMock()
    result = await rq.reconciliation_trigger_run(conn, redis, window_days=30)
    assert result["scheduled"] is False
    assert "error" in result
