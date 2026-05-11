"""
Unit tests for ``src.profiler.feature_store.get_orderbook_features_asof``.

Agent Z's piece of the feature store: per-token order-book features
(depth imbalance / spread / microprice deviation) read AS-OF a given
timestamp from the ``orderbook_features_minute`` rollup table.

The function takes an already-open asyncpg connection, so the tests pass
in an AsyncMock with a configured ``fetchrow`` response. No DB needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.profiler.feature_store import get_orderbook_features_asof


def _mock_row(
    *,
    bucket_ts: datetime,
    depth_imbalance_mean: float = 0.25,
    depth_imbalance_max: float = -0.40,
    spread_bps_mean: float = 30.0,
    spread_bps_max: float = 80.0,
    microprice_mean: float = 0.612,
    microprice_deviation_mean: float = 0.002,
    n_snapshots: int = 25,
) -> dict:
    """Build a fake asyncpg.Record-like dict for fetchrow."""
    return {
        "bucket_ts": bucket_ts,
        "depth_imbalance_mean": Decimal(str(depth_imbalance_mean)),
        "depth_imbalance_max": Decimal(str(depth_imbalance_max)),
        "spread_bps_mean": Decimal(str(spread_bps_mean)),
        "spread_bps_max": Decimal(str(spread_bps_max)),
        "microprice_mean": Decimal(str(microprice_mean)),
        "microprice_deviation_mean": Decimal(str(microprice_deviation_mean)),
        "n_snapshots": n_snapshots,
    }


@pytest.mark.asyncio
async def test_returns_most_recent_row_within_lookback():
    """The function should return the most-recent row when one exists
    within the lookback window."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    # Row was captured 2 minutes before asof — within the 300 s default lookback.
    bucket = asof - timedelta(seconds=120)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_mock_row(bucket_ts=bucket))

    out = await get_orderbook_features_asof(conn, token_id="tok-A", asof_ts=asof)

    assert out is not None
    assert out["bucket_ts"] == bucket
    assert out["depth_imbalance_mean"] == Decimal("0.25")
    assert out["n_snapshots"] == 25
    # feature_age_s is synthesised, should be 120s
    assert out["feature_age_s"] == pytest.approx(120.0, abs=1e-6)

    # Verify the SQL bounds: WHERE bucket_ts <= asof AND bucket_ts >= asof - 300s
    args = conn.fetchrow.call_args.args
    # args[0] is the SQL string; args[1:] are token_id, asof, floor
    assert args[1] == "tok-A"
    assert args[2] == asof
    assert args[3] == asof - timedelta(seconds=300)


@pytest.mark.asyncio
async def test_returns_none_when_all_rows_stale():
    """If the most-recent row is older than lookback_s, the WHERE clause
    excludes it and the function returns None."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    # SQL would have filtered the stale row out; the mock simulates that
    # by returning None from fetchrow.
    conn.fetchrow = AsyncMock(return_value=None)

    out = await get_orderbook_features_asof(
        conn, token_id="tok-A", asof_ts=asof, lookback_s=300
    )

    assert out is None


@pytest.mark.asyncio
async def test_returns_none_for_unknown_token():
    """Unknown token → no rows → None. Behaviourally identical to the
    all-stale case from the caller's perspective, but worth a separate
    test for documentation."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    out = await get_orderbook_features_asof(
        conn, token_id="does-not-exist", asof_ts=asof
    )
    assert out is None


@pytest.mark.asyncio
async def test_custom_lookback_is_honoured():
    """A caller can opt into a tighter or looser staleness budget."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    await get_orderbook_features_asof(
        conn, token_id="tok-A", asof_ts=asof, lookback_s=60
    )

    args = conn.fetchrow.call_args.args
    assert args[3] == asof - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_feature_age_clamped_non_negative():
    """A row whose bucket_ts is exactly at asof has feature_age_s = 0
    (not negative)."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_mock_row(bucket_ts=asof))

    out = await get_orderbook_features_asof(conn, token_id="tok-A", asof_ts=asof)

    assert out is not None
    assert out["feature_age_s"] == 0.0


@pytest.mark.asyncio
async def test_db_error_returns_none():
    """An asyncpg error in fetchrow should be swallowed and surface as
    None — feature lookup is best-effort and must not break the
    training/decision path."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("connection reset"))

    out = await get_orderbook_features_asof(conn, token_id="tok-A", asof_ts=asof)

    assert out is None


@pytest.mark.asyncio
async def test_lookback_zero_clamped_to_one():
    """A lookback_s of 0 (or negative) is clamped to 1 so the SQL
    bound is always strictly less than asof_ts."""
    asof = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    await get_orderbook_features_asof(
        conn, token_id="tok-A", asof_ts=asof, lookback_s=0
    )
    args = conn.fetchrow.call_args.args
    assert args[3] == asof - timedelta(seconds=1)
