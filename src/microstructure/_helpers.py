"""Tiny shared helpers used by the deriver detectors and trackers.

Lives in its own module so :mod:`src.microstructure.derivers` and
:mod:`src.microstructure.trackers` can both import without circular
references. No public API surface — leading underscores everywhere.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.observer.clob_book_decoder import BookEvent


# Cap the per-key working set so a busy day on Polymarket doesn't bloat
# the daemon past the 400 MB envelope. The number is generous — the real
# back-pressure is the time-window pruning inside each detector.
_MAX_TRACKED_KEYS_PER_DETECTOR = 50_000


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_ts(event: BookEvent) -> float:
    """Unix seconds for the event's event_time."""
    try:
        return event.event_time.timestamp()
    except Exception:
        return time.time()


def truncate_to_bucket(ts: datetime, bucket_s: int) -> datetime:
    """Floor ``ts`` to the previous bucket boundary."""
    bucket_s = max(1, int(bucket_s))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch = ts.timestamp()
    bucket_epoch = math.floor(epoch / bucket_s) * bucket_s
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def next_bucket_boundary(now: datetime, bucket_s: int) -> datetime:
    """Return the next bucket boundary strictly after ``now``."""
    return truncate_to_bucket(now, bucket_s) + timedelta(seconds=bucket_s)
