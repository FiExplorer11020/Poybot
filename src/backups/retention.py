"""
GFS (Grandfather-Father-Son) retention policy — pure logic, no I/O.

Inputs are a list of (object_key, datetime) tuples; outputs tell the
caller which objects to keep and which to delete. Tested exhaustively
by tests/test_backups/test_retention.py.

Policy
------
Given a stream of daily backups timestamped `YYYY-MM-DD`, keep:
    * the N_DAILY  most recent backups (regardless of weekday/day).
    * the N_WEEKLY most recent backups whose weekday == WEEKLY_DOW.
    * the N_MONTHLY most recent backups whose day == 1.
The same key can satisfy multiple buckets (e.g. April 6 is both a
Sunday and the most-recent Sunday) — we union the keep sets.

Anything not in the keep union is marked for deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class RetentionDecision:
    """What classify_keys returned."""

    keep: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    # For introspection / observability — which bucket triggered the
    # keep decision for each retained key.
    reasons: dict[str, set[str]] = field(default_factory=dict)


def classify_keys(
    items: Iterable[tuple[str, datetime]],
    *,
    daily: int,
    weekly: int,
    monthly: int,
    weekly_dow: int = 6,
) -> RetentionDecision:
    """Classify backup objects into keep/delete sets per GFS rules.

    Args:
        items: iterable of (key, ts) where ts is the backup time.
        daily: how many of the most recent backups to keep
            unconditionally.
        weekly: how many of the most recent weekly-anchor backups to
            keep (e.g. last 4 Sundays).
        monthly: how many of the most recent monthly-anchor backups
            to keep (e.g. last 3 first-of-month).
        weekly_dow: 0=Mon, 6=Sun. Backups whose weekday matches this
            count toward the weekly bucket.

    Returns:
        RetentionDecision with `keep`, `delete`, and per-key `reasons`.
        `keep` and `delete` are sorted by ts descending so the most
        recent surface first.
    """
    if daily < 0 or weekly < 0 or monthly < 0:
        raise ValueError("retention bounds must be non-negative")
    if not 0 <= weekly_dow <= 6:
        raise ValueError("weekly_dow must be in [0, 6]")

    # Sort newest first so "first N" == "most recent N".
    sorted_items = sorted(items, key=lambda pair: pair[1], reverse=True)

    keep: dict[str, set[str]] = {}

    def _mark(key: str, reason: str) -> None:
        keep.setdefault(key, set()).add(reason)

    # Daily — top N.
    for key, _ in sorted_items[:daily]:
        _mark(key, "daily")

    # Weekly — top N whose weekday == weekly_dow.
    weekly_kept = 0
    for key, ts in sorted_items:
        if weekly_kept >= weekly:
            break
        if ts.weekday() == weekly_dow:
            _mark(key, "weekly")
            weekly_kept += 1

    # Monthly — top N whose day == 1.
    monthly_kept = 0
    for key, ts in sorted_items:
        if monthly_kept >= monthly:
            break
        if ts.day == 1:
            _mark(key, "monthly")
            monthly_kept += 1

    keep_keys = [key for key, _ in sorted_items if key in keep]
    delete_keys = [key for key, _ in sorted_items if key not in keep]

    return RetentionDecision(keep=keep_keys, delete=delete_keys, reasons=keep)
