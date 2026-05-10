"""
Unit tests for `scripts/maintenance/create_trades_partitions.py`.

These tests verify the generated DDL is well-formed monthly-partition SQL
without touching a real database. asyncpg is patched out via a fake
connection (mirroring the pattern in tests/test_scripts/test_batch_retention.py)
so the test stays pure-Python.

The maintenance script's contract:
  1. Default `--months 3` from the current month UTC.
  2. Each generated DDL is `CREATE TABLE IF NOT EXISTS trades_observed_YYYYMM
     PARTITION OF trades_observed FOR VALUES FROM (a) TO (b)`.
  3. Bounds are month-aligned (first instant of a month, UTC).
  4. Bounds are contiguous (each upper bound == the next lower bound) and
     non-overlapping.
  5. Naming is the deterministic `trades_observed_YYYYMM` pattern.
  6. The connect/apply path verifies the parent is partitioned before
     issuing DDL (RuntimeError if not).
  7. Year boundary (December -> January) is handled correctly.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from scripts.maintenance import create_trades_partitions as ctp


# --------------------------------------------------------------------------- #
# 1. DDL shape                                                                 #
# --------------------------------------------------------------------------- #


def test_generate_ddl_default_three_months():
    """3 months ahead from May 2026 -> May, June, July partitions."""
    start = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    ddl = ctp.generate_partition_ddl(start, months_ahead=3)

    names = [name for name, _ in ddl]
    assert names == [
        "trades_observed_202605",
        "trades_observed_202606",
        "trades_observed_202607",
    ]


def test_generate_ddl_emits_idempotent_create():
    """Every emitted statement must include IF NOT EXISTS."""
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    ddl = ctp.generate_partition_ddl(start, months_ahead=2)
    for _name, sql in ddl:
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "PARTITION OF trades_observed" in sql
        assert "FOR VALUES FROM" in sql
        assert "TO (" in sql


def test_generate_ddl_bounds_are_contiguous_and_month_aligned():
    """Upper bound of partition N must equal lower bound of partition N+1,
    and both must be the first instant of their month."""
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    ddl = ctp.generate_partition_ddl(start, months_ahead=4)

    # Extract the FROM/TO timestamps from each statement.
    bound_re = re.compile(
        r"FROM \('([^']+)'\) TO \('([^']+)'\)"
    )
    parsed: list[tuple[datetime, datetime]] = []
    for _name, sql in ddl:
        m = bound_re.search(sql)
        assert m is not None, f"Could not parse bounds from: {sql}"
        lower = datetime.fromisoformat(m.group(1))
        upper = datetime.fromisoformat(m.group(2))
        parsed.append((lower, upper))

    # All bounds are month-aligned (day=1, all sub-day fields zero)
    for lower, upper in parsed:
        assert lower.day == 1 and lower.hour == 0 and lower.minute == 0
        assert upper.day == 1 and upper.hour == 0 and upper.minute == 0

    # Each upper == next lower (no gaps, no overlaps)
    for i in range(len(parsed) - 1):
        assert parsed[i][1] == parsed[i + 1][0], (
            f"gap or overlap between months {i} and {i+1}: "
            f"{parsed[i]} → {parsed[i+1]}"
        )


def test_generate_ddl_handles_year_boundary():
    """December partition's TO must be January 1st of the next year."""
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ddl = ctp.generate_partition_ddl(start, months_ahead=3)

    names = [name for name, _ in ddl]
    assert names == [
        "trades_observed_202611",
        "trades_observed_202612",
        "trades_observed_202701",
    ]

    # The Dec partition's upper bound must be 2027-01-01.
    dec_sql = ddl[1][1]
    assert "TO ('2027-01-01" in dec_sql


def test_generate_ddl_month_snap_ignores_day_in_start():
    """Passing 2026-05-31 should still produce a 2026-05 partition first
    (we snap to the first of the month)."""
    start = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
    ddl = ctp.generate_partition_ddl(start, months_ahead=1)
    name, sql = ddl[0]
    assert name == "trades_observed_202605"
    assert "FROM ('2026-05-01" in sql
    assert "TO ('2026-06-01" in sql


def test_generate_ddl_rejects_zero_or_negative_months():
    """Programmer error — months_ahead < 1 must raise, not silently emit nothing."""
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        ctp.generate_partition_ddl(start, months_ahead=0)
    with pytest.raises(ValueError):
        ctp.generate_partition_ddl(start, months_ahead=-3)


# --------------------------------------------------------------------------- #
# 2. apply_partitions wiring                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_apply_partitions_dry_run_does_not_connect():
    """dry_run=True must not open an asyncpg connection."""
    with patch.object(ctp.asyncpg, "connect", AsyncMock()) as connect_mock:
        applied = await ctp.apply_partitions(
            dsn="postgresql://ignored",
            start_month=datetime(2026, 5, 1, tzinfo=timezone.utc),
            months_ahead=2,
            dry_run=True,
        )
    assert applied == [
        "trades_observed_202605",
        "trades_observed_202606",
    ]
    connect_mock.assert_not_called()


class _FakeConn:
    """Minimal asyncpg connection stand-in. Records every SQL it sees."""

    def __init__(self, relkind: str = "p", exists: bool = False) -> None:
        self._relkind = relkind
        self._exists = exists
        self.executed: list[str] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        return "CREATE TABLE"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchvals.append((sql, args))
        # _verify_parent_is_partitioned reads relkind:
        if "relkind" in sql:
            return self._relkind
        # _table_exists reads "SELECT 1 …":
        return 1 if self._exists else None

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_apply_partitions_executes_one_create_per_month():
    """Non-dry-run path: one CREATE TABLE IF NOT EXISTS per target month."""
    fake = _FakeConn(relkind="p", exists=False)

    async def fake_connect(_dsn: str) -> _FakeConn:
        return fake

    with patch.object(ctp.asyncpg, "connect", fake_connect):
        applied = await ctp.apply_partitions(
            dsn="postgresql://ignored",
            start_month=datetime(2026, 5, 1, tzinfo=timezone.utc),
            months_ahead=3,
        )

    assert applied == [
        "trades_observed_202605",
        "trades_observed_202606",
        "trades_observed_202607",
    ]
    # 3 CREATE TABLE statements, each idempotent.
    creates = [s for s in fake.executed if "CREATE TABLE IF NOT EXISTS" in s]
    assert len(creates) == 3
    for sql in creates:
        assert "PARTITION OF trades_observed" in sql


@pytest.mark.asyncio
async def test_apply_partitions_refuses_unpartitioned_parent():
    """If the parent's relkind != 'p' (not partitioned), bail loudly —
    do NOT issue DDL that would explode at the first FOR VALUES clause."""
    fake = _FakeConn(relkind="r", exists=False)  # ordinary table, not partitioned

    async def fake_connect(_dsn: str) -> _FakeConn:
        return fake

    with patch.object(ctp.asyncpg, "connect", fake_connect):
        with pytest.raises(RuntimeError, match="not partitioned"):
            await ctp.apply_partitions(
                dsn="postgresql://ignored",
                start_month=datetime(2026, 5, 1, tzinfo=timezone.utc),
                months_ahead=1,
            )

    # No CREATE TABLE issued.
    assert not any("CREATE TABLE" in s for s in fake.executed)


@pytest.mark.asyncio
async def test_apply_partitions_refuses_missing_parent():
    """If trades_observed doesn't exist at all, bail with the same error path."""
    fake = _FakeConn(relkind=None, exists=False)  # type: ignore[arg-type]

    async def fake_connect(_dsn: str) -> _FakeConn:
        return fake

    with patch.object(ctp.asyncpg, "connect", fake_connect):
        with pytest.raises(RuntimeError, match="does not exist"):
            await ctp.apply_partitions(
                dsn="postgresql://ignored",
                start_month=datetime(2026, 5, 1, tzinfo=timezone.utc),
                months_ahead=1,
            )


# --------------------------------------------------------------------------- #
# 3. CLI plumbing                                                              #
# --------------------------------------------------------------------------- #


def test_cli_default_months_is_three():
    args = ctp._parse_cli_args([])
    assert args.months == 3
    assert args.dry_run is False
    assert args.start is None


def test_cli_months_override():
    args = ctp._parse_cli_args(["--months", "6"])
    assert args.months == 6


def test_cli_dry_run_flag():
    args = ctp._parse_cli_args(["--dry-run"])
    assert args.dry_run is True


def test_resolve_start_returns_aware_datetime():
    dt = ctp._resolve_start("2026-07-01")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 7


def test_resolve_start_none_returns_now_utc():
    dt = ctp._resolve_start(None)
    assert dt.tzinfo is not None


def test_resolve_start_rejects_garbage():
    with pytest.raises(SystemExit):
        ctp._resolve_start("not-a-date")


# --------------------------------------------------------------------------- #
# 4. Naming + helper functions                                                 #
# --------------------------------------------------------------------------- #


def test_partition_name_format():
    """Zero-padded YYYYMM, parent-prefixed."""
    assert ctp._partition_name(datetime(2026, 1, 1, tzinfo=timezone.utc)) == \
        "trades_observed_202601"
    assert ctp._partition_name(datetime(2026, 12, 1, tzinfo=timezone.utc)) == \
        "trades_observed_202612"


def test_month_floor_snaps_to_first_of_month():
    dt = ctp._month_floor(datetime(2026, 5, 17, 11, 59, tzinfo=timezone.utc))
    assert dt == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_next_month_advances_year_at_december():
    dt = ctp._next_month(datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert dt == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_next_month_normal_case():
    dt = ctp._next_month(datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert dt == datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
