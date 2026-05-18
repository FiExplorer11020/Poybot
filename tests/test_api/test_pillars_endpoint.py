"""Unit tests for the pillars_queries module (PLAN-UIA-001).

Mocks asyncpg connection — we test each pillar's pass/fail logic in
isolation. End-to-end coverage via scripts/smoke.sh against a live DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import pillars_queries as pq


# --------------------------------------------------------------------------- #
# Helper functions                                                            #
# --------------------------------------------------------------------------- #


def test_fmt_age_buckets():
    assert pq._fmt_age(None) == "never"
    assert pq._fmt_age(30) == "30s ago"
    assert pq._fmt_age(120) == "2m ago"
    assert pq._fmt_age(3600 * 4) == "4h ago"
    assert pq._fmt_age(86400 * 3) == "3d ago"


def test_age_seconds_handles_none():
    assert pq._age_seconds(None) is None
    assert pq._age_seconds("not-a-datetime") is None


def test_age_seconds_naive_aware_datetime():
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    result = pq._age_seconds(past)
    assert 115 <= result <= 125


# --------------------------------------------------------------------------- #
# Each pillar in isolation                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_oracle_ok_with_recent_quotes():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "quotes_24h": 42,
        "last_quote_at": datetime.now(timezone.utc) - timedelta(seconds=60),
    })
    result = await pq._check_oracle(conn)
    assert result["ok"] is True
    assert result["quotes_24h"] == 42
    assert "42 quotes/24h" in result["detail"]


@pytest.mark.asyncio
async def test_oracle_not_ok_when_no_quotes_24h():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"quotes_24h": 0, "last_quote_at": None})
    result = await pq._check_oracle(conn)
    assert result["ok"] is False
    assert result["quotes_24h"] == 0


@pytest.mark.asyncio
async def test_oracle_handles_missing_table_gracefully():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=Exception("relation does not exist"))
    result = await pq._check_oracle(conn)
    assert result["ok"] is False
    assert "table missing" in result["detail"]


@pytest.mark.asyncio
async def test_reconciliation_ok_when_recent_run_exists():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[
        datetime.now(timezone.utc) - timedelta(seconds=300),  # last_run_at
        3,   # divergences_24h
        12,  # closed_paper_24h
    ])
    result = await pq._check_reconciliation(conn)
    assert result["ok"] is True
    assert result["divergences_24h"] == 3
    assert result["last_run_age_s"] is not None


@pytest.mark.asyncio
async def test_reconciliation_not_ok_when_stale_run():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[
        datetime.now(timezone.utc) - timedelta(days=2),  # 2-day-old run
        0, 5,
    ])
    result = await pq._check_reconciliation(conn)
    assert result["ok"] is False
    assert "stale" in result["detail"]


@pytest.mark.asyncio
async def test_reconciliation_ok_when_never_run_but_no_closes():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[None, 0, 0])
    result = await pq._check_reconciliation(conn)
    assert result["ok"] is True
    assert "no closes" in result["detail"]


@pytest.mark.asyncio
async def test_backfill_ok_when_more_resolved_than_pending():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"resolved": 100, "pending": 5})
    result = await pq._check_backfill(conn)
    assert result["ok"] is True
    assert result["markets_resolved"] == 100
    assert result["markets_pending"] == 5


@pytest.mark.asyncio
async def test_backfill_not_ok_when_pending_dominates():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"resolved": 5, "pending": 50})
    result = await pq._check_backfill(conn)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_spread_gates_ok_when_low_reject_rate():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"rejects_24h": 2, "total_24h": 50})
    result = await pq._check_spread_gates(conn)
    assert result["ok"] is True
    assert result["rejects_24h"] == 2


@pytest.mark.asyncio
async def test_spread_gates_not_ok_when_reject_rate_above_50pct():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"rejects_24h": 30, "total_24h": 50})
    result = await pq._check_spread_gates(conn)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_spread_gates_ok_when_no_activity():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"rejects_24h": 0, "total_24h": 0})
    result = await pq._check_spread_gates(conn)
    assert result["ok"] is True
    assert "no activity" in result["detail"]


@pytest.mark.asyncio
async def test_audit_log_ok_when_rows_match_closes():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "rows_24h": 12,
        "fallback_24h": 0,
        "fail_24h": 1,
    })
    conn.fetchval = AsyncMock(return_value=10)  # closed_paper_24h
    result = await pq._check_audit_log(conn)
    assert result["ok"] is True
    assert result["rows_24h"] == 12


@pytest.mark.asyncio
async def test_audit_log_not_ok_when_fewer_rows_than_closes():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"rows_24h": 3, "fallback_24h": 0, "fail_24h": 0})
    conn.fetchval = AsyncMock(return_value=10)
    result = await pq._check_audit_log(conn)
    assert result["ok"] is False
    assert "audit gap" in result["detail"]


# --------------------------------------------------------------------------- #
# pillars_status aggregator                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pillars_status_all_pillars_present():
    conn = MagicMock()
    # Provide every shape that any pillar needs; the simpler check
    # just verifies all 5 keys appear in the output even on a healthy DB.
    conn.fetchrow = AsyncMock(side_effect=[
        # oracle
        {"quotes_24h": 10, "last_quote_at": datetime.now(timezone.utc) - timedelta(seconds=30)},
        # backfill
        {"resolved": 100, "pending": 2},
        # spread_gates
        {"rejects_24h": 0, "total_24h": 10},
        # audit_log
        {"rows_24h": 10, "fallback_24h": 0, "fail_24h": 0},
    ])
    conn.fetchval = AsyncMock(side_effect=[
        datetime.now(timezone.utc) - timedelta(seconds=300),  # last_run_at
        2,   # divergences_24h
        8,   # closed_paper_24h (for recon)
        8,   # closed_paper_24h (for audit_log)
    ])

    result = await pq.pillars_status(conn)
    assert set(result["pillars"].keys()) == {
        "oracle", "reconciliation", "backfill", "spread_gates", "audit_log",
    }
    assert result["overall_ok"] is True
    assert "computed_at_iso" in result


@pytest.mark.asyncio
async def test_pillars_status_overall_ok_is_and_of_individual():
    """Toggling any single pillar to fail flips overall_ok to False."""
    conn = MagicMock()
    # oracle FAILS (no quotes)
    conn.fetchrow = AsyncMock(side_effect=[
        {"quotes_24h": 0, "last_quote_at": None},     # oracle FAIL
        {"resolved": 100, "pending": 2},               # backfill OK
        {"rejects_24h": 0, "total_24h": 10},           # spread_gates OK
        {"rows_24h": 10, "fallback_24h": 0, "fail_24h": 0},  # audit OK
    ])
    conn.fetchval = AsyncMock(side_effect=[
        datetime.now(timezone.utc) - timedelta(seconds=300),
        2, 8, 8,
    ])
    result = await pq.pillars_status(conn)
    assert result["pillars"]["oracle"]["ok"] is False
    assert result["overall_ok"] is False
