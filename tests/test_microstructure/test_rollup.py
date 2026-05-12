"""Unit tests for :mod:`src.microstructure.rollup` — Round 11 § 3.2.

Cover:
  * Per-minute aggregation correctness — given a snapshot with iceberg
    + spoof + OFI counters, the writer produces one row per
    (market_id, token_id) with the right values.
  * Configurable bucket size doesn't change the row-shape contract.
  * Idempotency — re-flushing the same bucket is an upsert, not an
    insert (relies on ON CONFLICT DO UPDATE — we just assert the SQL
    payload, not the DB).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.microstructure.derivers import IcebergBucket, OFIBucket, SpoofBucket
from src.microstructure.rollup import MicrostructureRollup


@pytest.fixture
def bucket_ts():
    return datetime(2026, 5, 12, 10, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    """Mock asyncpg connection. ``executemany`` captures the rows for
    inspection."""
    c = AsyncMock()
    c.executemany = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_flush_writes_one_row_per_key(bucket_ts, conn):
    """Two (market, token) keys produced by the deriver → two rows
    written by the rollup."""
    rollup = MicrostructureRollup(bucket_s=60)
    iceberg_b = IcebergBucket(count=2, total_size=100.0)
    spoof_b = SpoofBucket(count=1, total_size=5000.0)
    ofi_b = OFIBucket(samples=[10.0, 20.0, 30.0])
    snapshot = {
        "iceberg": {("m1", "t1"): iceberg_b},
        "spoof": {("m2", "t2"): spoof_b},
        "ofi": {("m1", "t1"): ofi_b, ("m2", "t2"): ofi_b},
    }
    n = await rollup.flush(bucket_ts, snapshot, conn=conn)
    assert n == 2
    conn.executemany.assert_awaited_once()
    sql, rows = conn.executemany.await_args.args
    assert "INSERT INTO microstructure_features" in sql
    assert "ON CONFLICT" in sql
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_empty_snapshot_writes_nothing(bucket_ts, conn):
    rollup = MicrostructureRollup(bucket_s=60)
    n = await rollup.flush(
        bucket_ts, {"iceberg": {}, "spoof": {}, "ofi": {}}, conn=conn
    )
    assert n == 0
    conn.executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_iceberg_only_emits_row(bucket_ts, conn):
    """Iceberg fired but no OFI / spoof — row still written, OFI fields
    are NULL."""
    rollup = MicrostructureRollup(bucket_s=60)
    snapshot = {
        "iceberg": {("m1", "t1"): IcebergBucket(count=3, total_size=250.0)},
        "spoof": {},
        "ofi": {},
    }
    n = await rollup.flush(bucket_ts, snapshot, conn=conn)
    assert n == 1
    _sql, rows = conn.executemany.await_args.args
    row = rows[0]
    # Column order: market_id, token_id, bucket_ts, iceberg_count,
    # iceberg_size, spoof_count, spoof_size, ofi_mean, ofi_max, ofi_min, ofi_std
    assert row[0] == "m1"
    assert row[1] == "t1"
    assert row[2] == bucket_ts
    assert row[3] == 3
    assert row[4] == 250.0
    assert row[5] is None
    assert row[6] is None
    assert row[7] is None  # ofi_mean
    assert row[8] is None  # ofi_max
    assert row[9] is None  # ofi_min
    assert row[10] is None  # ofi_std


@pytest.mark.asyncio
async def test_configurable_bucket_size(bucket_ts, conn):
    """A 5 s bucket and a 60 s bucket both write to the same table; the
    writer doesn't change shape."""
    rollup_5 = MicrostructureRollup(bucket_s=5)
    rollup_60 = MicrostructureRollup(bucket_s=60)
    snapshot = {
        "iceberg": {("m1", "t1"): IcebergBucket(count=1, total_size=10.0)},
        "spoof": {},
        "ofi": {},
    }
    n5 = await rollup_5.flush(bucket_ts, snapshot, conn=conn)
    assert n5 == 1
    n60 = await rollup_60.flush(bucket_ts, snapshot, conn=conn)
    assert n60 == 1
    assert conn.executemany.await_count == 2
    # Both calls executed the same SQL.
    sqls = [c.args[0] for c in conn.executemany.await_args_list]
    assert sqls[0] == sqls[1]


@pytest.mark.asyncio
async def test_ofi_summary_in_row(bucket_ts, conn):
    rollup = MicrostructureRollup(bucket_s=60)
    samples = [10.0, 20.0, 30.0, 40.0]
    snapshot = {
        "iceberg": {},
        "spoof": {},
        "ofi": {("m1", "t1"): OFIBucket(samples=samples)},
    }
    await rollup.flush(bucket_ts, snapshot, conn=conn)
    _sql, rows = conn.executemany.await_args.args
    row = rows[0]
    # mean = 25.0, max = 40.0, min = 10.0, std = sqrt(125) ≈ 11.18
    assert row[7] == pytest.approx(25.0)  # ofi_mean
    assert row[8] == pytest.approx(40.0)  # ofi_max
    assert row[9] == pytest.approx(10.0)  # ofi_min
    assert row[10] == pytest.approx(11.18, rel=1e-2)  # ofi_std
