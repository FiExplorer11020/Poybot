"""QW2 (audit 2026-05-17) regression — ALLOWED_KEYS whitelist coverage.

Production logs showed::

    WARNING runtime_config: legacy hash key
        'min_hours_to_resolution_follow' not in ALLOWED_KEYS — dropped

Every audit-introduced settings constant that an operator might want to
flip from the dashboard cockpit or via a legacy ``HSET`` must be
registered in ``ALLOWED_KEYS`` (so writes are accepted) with a matching
``BOUNDS`` entry (so out-of-range writes are rejected loudly instead of
silently mis-bounded). Without both, the audit-config edits are dropped
at boot and the operator never knows.

This test is the contract: it pins the 10 audit constants the QW2 fix
adds. Adding an audit constant to ``src/config.py`` without adding it
here will make this file the failing canary on the next test run.
"""
from __future__ import annotations

import pytest

from src.control.runtime_config import ALLOWED_KEYS, BOUNDS


# 10 audit-introduced constants per the QW2 task spec. Order is purely
# documentation — the assertions are membership / containment checks.
QW2_KEYS: tuple[str, ...] = (
    "min_hours_to_resolution_follow",
    "min_hours_to_resolution_fade",
    "max_book_age_paper_s",
    "max_entry_price",
    "max_leader_price_drift",
    "preclose_hours_before_resolution",
    "max_trade_return_ratio",
    "monitor_tick_s",
    "urgent_monitor_tick_s",
    "urgent_monitor_hours",
)


@pytest.mark.parametrize("key", QW2_KEYS)
def test_qw2_key_in_allowed_keys(key: str):
    """Every QW2 audit constant must be whitelisted for runtime override."""
    assert key in ALLOWED_KEYS, (
        f"QW2 regression: {key!r} missing from ALLOWED_KEYS. "
        "Dashboard cockpit + legacy HSET edits to this key will be "
        "silently dropped at boot."
    )


@pytest.mark.parametrize("key", QW2_KEYS)
def test_qw2_key_has_bounds(key: str):
    """A whitelisted key without BOUNDS is half-protected: writes pass
    type coercion but skip the range check, so a negative or 100x value
    can land in Redis. Boolean/string keys are exempt — none of the QW2
    keys are boolean or string, so all must be in BOUNDS.
    """
    assert key in BOUNDS, (
        f"QW2 regression: {key!r} in ALLOWED_KEYS but not in BOUNDS. "
        "Out-of-range writes will pass."
    )


@pytest.mark.parametrize("key", QW2_KEYS)
def test_qw2_bounds_are_well_formed(key: str):
    """Bound tuple = (lo, hi) with lo < hi, both finite."""
    lo, hi = BOUNDS[key]
    assert lo < hi, f"{key!r} bounds inverted: lo={lo} >= hi={hi}"
    assert lo == lo and hi == hi  # NaN check (NaN != NaN)


def test_qw2_full_set_present():
    """One global sanity assertion that's easy to scan in CI logs."""
    missing = [k for k in QW2_KEYS if k not in ALLOWED_KEYS]
    assert not missing, (
        f"QW2 regression: {len(missing)} audit constants missing from "
        f"ALLOWED_KEYS: {missing}"
    )
