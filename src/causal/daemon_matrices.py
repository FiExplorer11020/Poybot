"""Matrix-construction helpers for the R10 causal daemon.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.2 + § 7.B.

Split out of ``src/causal/daemon.py`` so the file stays under the
500-LOC project limit. The methodology audit should spend most of its
time here — most causal-inference application errors hide in how you
bin event streams and choose exogenous controls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import numpy as np


def build_iv_matrices(
    leader_times: np.ndarray,
    pool_times: np.ndarray,
    instrument_events: list[dict[str, Any]],
    period_start: datetime,
    period_end: datetime,
    bin_seconds: int = 300,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """Bin event streams + instruments into equal-width windows.

    Parameters
    ----------
    leader_times : (n_events,) ndarray of POSIX timestamps
    pool_times   : (n_events,) ndarray of POSIX timestamps for the pool
    instrument_events : list of {event_type, event_time}
    period_start, period_end : datetime endpoints of the window
    bin_seconds : bin width in seconds (default 300 = 5 min)

    Returns
    -------
    L : (n_bins,) leader event count per bin
    F : (n_bins,) follower event count per bin
    Z : (n_bins, q) instrument indicators (1 iff event_time in bin)
    X : (n_bins, p) exogenous controls; here time-of-day sin/cos.
    """
    start_s = period_start.timestamp()
    end_s = period_end.timestamp()
    bin_w = float(bin_seconds)
    n_bins = max(1, int((end_s - start_s) / bin_w))
    bins = np.linspace(start_s, end_s, n_bins + 1)
    L, _ = np.histogram(leader_times, bins=bins)
    F, _ = np.histogram(pool_times, bins=bins)

    # Group instruments by type so each becomes its own column. This
    # is where the methodology audit looks first: the gate's
    # "instrument exogeneity" claim depends on these columns being
    # truly random across leaders / pool classes.
    by_type: dict[str, np.ndarray] = {}
    for ev in instrument_events:
        t = ev["event_time"].timestamp()
        kind = str(ev["event_type"])
        arr = by_type.get(kind)
        if arr is None:
            arr = np.zeros(n_bins, dtype=float)
            by_type[kind] = arr
        idx = int((t - start_s) / bin_w)
        if 0 <= idx < n_bins:
            arr[idx] = 1.0
    if by_type:
        Z = np.column_stack(list(by_type.values()))
    else:
        Z = np.zeros((n_bins, 0), dtype=float)

    # Exogenous controls: time-of-day sin/cos so the gate doesn't
    # confound diurnal patterns with the IV. Future work (operator-
    # deliverable per the methodology audit): add day-of-week and
    # market-category dummies.
    bin_centers_s = (bins[:-1] + bins[1:]) / 2.0
    hours = (bin_centers_s % 86400) / 3600.0
    sin_h = np.sin(2 * np.pi * hours / 24.0)
    cos_h = np.cos(2 * np.pi * hours / 24.0)
    X = np.column_stack([sin_h, cos_h])
    return L.astype(float), F.astype(float), Z, X


def safe_float(v: float | None) -> float | None:
    """Coerce a Python value to a finite float or None.

    Strips NaN and infinity so the asyncpg NUMERIC conversion doesn't
    raise. Used by the daemon when writing IVEstimate fields to
    ``causal_estimates``.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except Exception:
        return None
    if f != f:  # NaN check
        return None
    if f == float("inf") or f == float("-inf"):
        return None
    return f


__all__ = ["build_iv_matrices", "safe_float"]
