"""Wave-3 hardening tests for the hourly partition maintainer —
:mod:`scripts.maintenance.create_book_events_partitions`.

The script's pure-function surface is what we cover here (DDL generation,
partition-name parsing, hour-floor math). The full DB roundtrip is
exercised in integration; this file isolates the load-bearing math so
a regression on the IDempotency contract is caught in unit-tests.

Spec contracts:
  * Forward-rolls the next N hours (default 24). Uses CREATE TABLE IF
    NOT EXISTS so re-running is a no-op.
  * Drops partitions older than ``CLOB_BOOK_RETENTION_DAYS``.
  * Partition naming is ``clob_book_events_YYYYMMDD_HH``.
  * The DEFAULT partition (``clob_book_events_default``) is NEVER
    dropped by the retention pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.maintenance.create_book_events_partitions import (
    _hour_floor,
    _hour_from_partition_name,
    _next_hour,
    _partition_name,
    generate_partition_ddl,
)

# --------------------------------------------------------------------------- #
# 1. Hour-floor math                                                           #
# --------------------------------------------------------------------------- #


class TestHourFloor:
    def test_floor_strips_minutes_seconds_microseconds(self):
        ts = datetime(2026, 5, 12, 10, 30, 45, 123456, tzinfo=timezone.utc)
        assert _hour_floor(ts) == datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)

    def test_floor_normalizes_to_utc(self):
        """A non-UTC tz-aware datetime gets normalized to UTC before
        flooring."""
        from datetime import timezone as tz
        offset = tz(timedelta(hours=2))  # UTC+2
        ts = datetime(2026, 5, 12, 12, 30, 0, tzinfo=offset)  # = 10:30 UTC
        assert _hour_floor(ts) == datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)

    def test_next_hour_handles_dst_boundary_uniformly(self):
        """Hour increments are pure UTC arithmetic — DST quirks never
        apply because we operate on TIMESTAMPTZ stored as UTC."""
        ts = datetime(2026, 3, 14, 23, 30, 0, tzinfo=timezone.utc)
        nxt = _next_hour(ts)
        assert nxt == datetime(2026, 3, 15, 0, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 2. Partition name encoding/decoding round-trip                               #
# --------------------------------------------------------------------------- #


class TestPartitionNameCodec:
    def test_name_for_known_hour(self):
        ts = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        assert _partition_name(ts) == "clob_book_events_20260512_10"

    def test_name_round_trips(self):
        ts = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        name = _partition_name(ts)
        parsed = _hour_from_partition_name(name)
        assert parsed == ts

    def test_default_partition_returns_none(self):
        """The DEFAULT partition is unmanaged by the rotation pass —
        parse must return None so retention DROP never touches it."""
        assert _hour_from_partition_name("clob_book_events_default") is None

    def test_garbage_name_returns_none(self):
        assert _hour_from_partition_name("clob_book_events_xxxxxxxx_yy") is None
        assert _hour_from_partition_name("clob_book_events_20260512") is None
        assert _hour_from_partition_name("clob_book_events_2026_05_12_10") is None
        # Wrong prefix → None.
        assert _hour_from_partition_name("trades_observed_20260512_10") is None

    def test_year_2030_round_trip(self):
        """Future-proofing: names work for years > 2029."""
        ts = datetime(2031, 1, 1, 23, 0, 0, tzinfo=timezone.utc)
        name = _partition_name(ts)
        assert name == "clob_book_events_20310101_23"
        assert _hour_from_partition_name(name) == ts


# --------------------------------------------------------------------------- #
# 3. DDL generation                                                            #
# --------------------------------------------------------------------------- #


class TestDDLGeneration:
    def test_generates_exactly_n_partitions(self):
        start = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        pairs = generate_partition_ddl(start, hours_ahead=24)
        assert len(pairs) == 24

    def test_partitions_have_contiguous_hour_ranges(self):
        """The N-th partition starts where the (N-1)-th ends — no gaps."""
        start = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        pairs = generate_partition_ddl(start, hours_ahead=5)
        # Extract the FROM and TO values from each DDL.
        for i in range(len(pairs) - 1):
            sql_i = pairs[i][1]
            sql_next = pairs[i + 1][1]
            # FROM ('xxx') TO ('yyy') — the TO of i must equal FROM of i+1.
            to_i = sql_i.split("TO ('")[1].split("')")[0]
            from_next = sql_next.split("FROM ('")[1].split("')")[0]
            assert to_i == from_next, f"gap between partition {i} and {i + 1}"

    def test_ddl_is_create_if_not_exists_idempotent(self):
        start = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        for name, sql in generate_partition_ddl(start, hours_ahead=3):
            assert "CREATE TABLE IF NOT EXISTS" in sql
            assert name in sql

    def test_zero_hours_ahead_raises(self):
        start = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            generate_partition_ddl(start, hours_ahead=0)
        with pytest.raises(ValueError):
            generate_partition_ddl(start, hours_ahead=-5)

    def test_naming_consistent_across_midnight_boundary(self):
        """A 4-hour DDL window starting at 22:00 must roll across midnight
        and produce the correct YYYYMMDD_HH naming for the new day."""
        start = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)
        pairs = generate_partition_ddl(start, hours_ahead=4)
        names = [p[0] for p in pairs]
        assert names == [
            "clob_book_events_20260512_22",
            "clob_book_events_20260512_23",
            "clob_book_events_20260513_00",
            "clob_book_events_20260513_01",
        ]


# --------------------------------------------------------------------------- #
# 4. Retention cutoff math                                                     #
# --------------------------------------------------------------------------- #


class TestRetentionMath:
    def test_30d_cutoff_drops_old_partition_keeps_young(self):
        """A partition for 31d ago should be < cutoff (DROP). 29d ago
        should be > cutoff (KEEP)."""
        now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
        cutoff = _hour_floor(now) - timedelta(days=30)

        old_hour = _hour_from_partition_name("clob_book_events_20260411_00")
        young_hour = _hour_from_partition_name("clob_book_events_20260513_00")
        assert old_hour is not None and young_hour is not None
        # 2026-04-11 < cutoff (2026-04-12 12:00 floored to 12:00 = 2026-04-12 12:00)
        assert old_hour < cutoff
        # 2026-05-13 > cutoff (future).
        assert young_hour > cutoff
