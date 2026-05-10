"""
GFS retention policy — pure-logic tests (S4.12).

Covers:
    * Daily-only kept set (small N).
    * Weekly anchor (Sunday by default — overridable to Monday etc.).
    * Monthly anchor (day-of-month == 1).
    * Overlap: a Sunday that's also the most recent backup is kept by
      both buckets without dedup issues.
    * Empty input.
    * Validation of negative bounds.
    * Stable ordering — keep + delete sorted by ts descending.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backups.retention import classify_keys


def _ts(date: str) -> datetime:
    """Helper — build a tz-aware UTC datetime from `YYYY-MM-DD`."""
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc)


def _gen(span: list[str]) -> list[tuple[str, datetime]]:
    """Build a list of (key, ts) given ISO date strings."""
    return [(f"key-{d}", _ts(d)) for d in span]


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def test_negative_bounds_rejected():
    with pytest.raises(ValueError):
        classify_keys([], daily=-1, weekly=0, monthly=0)
    with pytest.raises(ValueError):
        classify_keys([], daily=0, weekly=-1, monthly=0)
    with pytest.raises(ValueError):
        classify_keys([], daily=0, weekly=0, monthly=-1)


def test_invalid_weekly_dow_rejected():
    with pytest.raises(ValueError):
        classify_keys([], daily=1, weekly=0, monthly=0, weekly_dow=7)
    with pytest.raises(ValueError):
        classify_keys([], daily=1, weekly=0, monthly=0, weekly_dow=-1)


def test_empty_input_returns_empty_decision():
    decision = classify_keys([], daily=7, weekly=4, monthly=3)
    assert decision.keep == []
    assert decision.delete == []
    assert decision.reasons == {}


# --------------------------------------------------------------------------- #
# Daily bucket                                                                 #
# --------------------------------------------------------------------------- #


def test_daily_keeps_top_n_regardless_of_weekday():
    items = _gen([f"2026-04-{d:02d}" for d in range(1, 11)])  # Apr 1..10
    decision = classify_keys(items, daily=3, weekly=0, monthly=0)
    # The 3 most recent are Apr 10, 9, 8.
    assert decision.keep == ["key-2026-04-10", "key-2026-04-09", "key-2026-04-08"]
    # Everything else deleted.
    assert len(decision.delete) == 7
    assert "key-2026-04-01" in decision.delete
    # Reasons annotated.
    for k in decision.keep:
        assert "daily" in decision.reasons[k]


def test_daily_with_more_buckets_than_items():
    items = _gen(["2026-04-01", "2026-04-02"])
    decision = classify_keys(items, daily=10, weekly=0, monthly=0)
    assert sorted(decision.keep) == ["key-2026-04-01", "key-2026-04-02"]
    assert decision.delete == []


# --------------------------------------------------------------------------- #
# Weekly bucket                                                                #
# --------------------------------------------------------------------------- #


def test_weekly_keeps_top_n_sundays():
    # 6 weeks of daily backups around Sundays.
    # Sundays in March/April 2026: 03-01 (no, that's Sunday let's check)
    # Actually: 2026-03-01 IS Sunday. 03-08, 03-15, 03-22, 03-29, 04-05.
    span = [f"2026-03-{d:02d}" for d in range(1, 32)] + [
        f"2026-04-{d:02d}" for d in range(1, 11)
    ]
    items = _gen(span)
    decision = classify_keys(items, daily=0, weekly=3, monthly=0, weekly_dow=6)
    # Should keep the 3 most recent Sundays: Apr 5, Mar 29, Mar 22.
    sundays_kept = sorted(
        [k for k, reasons in decision.reasons.items() if "weekly" in reasons],
        reverse=True,
    )
    assert sundays_kept == ["key-2026-04-05", "key-2026-03-29", "key-2026-03-22"]


def test_weekly_dow_monday():
    # 4 Mondays in April 2026: 04-06, 04-13, 04-20, 04-27.
    span = [f"2026-04-{d:02d}" for d in range(1, 30)]
    items = _gen(span)
    decision = classify_keys(items, daily=0, weekly=2, monthly=0, weekly_dow=0)
    mondays = [k for k, r in decision.reasons.items() if "weekly" in r]
    # 2 most recent Mondays = Apr 27, Apr 20.
    assert sorted(mondays, reverse=True) == ["key-2026-04-27", "key-2026-04-20"]


def test_weekly_does_not_double_count_when_zero():
    items = _gen([f"2026-04-{d:02d}" for d in range(1, 8)])
    decision = classify_keys(items, daily=2, weekly=0, monthly=0)
    # Weekly disabled — only daily survives.
    assert all("weekly" not in r for r in decision.reasons.values())


# --------------------------------------------------------------------------- #
# Monthly bucket                                                               #
# --------------------------------------------------------------------------- #


def test_monthly_keeps_top_n_first_of_month():
    # First-of-month entries spread over 5 months.
    items = _gen(
        [
            "2025-12-01",
            "2026-01-01",
            "2026-02-01",
            "2026-03-01",
            "2026-04-01",
            "2026-04-15",  # not 1st — only kept by daily/weekly if applicable
        ]
    )
    decision = classify_keys(items, daily=0, weekly=0, monthly=3)
    monthly = sorted(
        [k for k, r in decision.reasons.items() if "monthly" in r], reverse=True
    )
    assert monthly == ["key-2026-04-01", "key-2026-03-01", "key-2026-02-01"]
    assert "key-2026-04-15" in decision.delete


# --------------------------------------------------------------------------- #
# Overlap + dedup                                                              #
# --------------------------------------------------------------------------- #


def test_overlap_marks_multiple_reasons():
    # 2026-03-01 is a Sunday AND day=1 — when both weekly + monthly
    # buckets reach back far enough, it should be tagged with both.
    items = _gen(["2026-03-01", "2026-03-08", "2026-03-15"])
    decision = classify_keys(items, daily=0, weekly=3, monthly=1)
    reasons = decision.reasons["key-2026-03-01"]
    assert "weekly" in reasons
    assert "monthly" in reasons
    # Still only kept once in the keep list (no duplicates).
    assert decision.keep.count("key-2026-03-01") == 1


def test_full_gfs_default_config_keeps_at_most_14():
    """Sanity check on the production policy — even with 60 days of
    backups, we never end up keeping more than ~14 objects."""
    span = [f"2026-{m:02d}-{d:02d}" for m in (2, 3, 4) for d in range(1, 29)]
    items = _gen(span)
    decision = classify_keys(items, daily=7, weekly=4, monthly=3)
    # Bound = 7 + 4 + 3 = 14, but with overlap it's usually less.
    assert len(decision.keep) <= 14
    assert len(decision.keep) > 7  # weeklies + monthlies extend past daily window


def test_keep_and_delete_sorted_descending():
    items = _gen(["2026-04-05", "2026-04-01", "2026-04-10", "2026-04-02"])
    decision = classify_keys(items, daily=2, weekly=0, monthly=0)
    # keep is sorted by ts desc.
    assert decision.keep == ["key-2026-04-10", "key-2026-04-05"]
    assert decision.delete == ["key-2026-04-02", "key-2026-04-01"]
