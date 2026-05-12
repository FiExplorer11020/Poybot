"""Hardening tests for :mod:`src.calibration.loss_aggregator`.

Wave-3 reviewer additions:

* Cross-validate Brier / log_loss / MAPE against sklearn.metrics — the
  load-bearing numerics for the drift detector.
* Pin the chained-comparison fix in ``compute_causal_residual`` — the
  previous ``a != b != c`` form silently accepted (a == c, a != b).
"""

from __future__ import annotations

import math

import pytest

from src.calibration.loss_aggregator import (
    compute_brier,
    compute_causal_residual,
    compute_log_loss,
    compute_mape,
)


# --------------------------------------------------------------------------- #
# sklearn cross-validation — Brier, log_loss, MAPE                            #
# --------------------------------------------------------------------------- #

sklearn = pytest.importorskip("sklearn.metrics", reason="sklearn not installed")


def test_brier_matches_sklearn_brier_score_loss():
    from sklearn.metrics import brier_score_loss

    p = [0.1, 0.4, 0.9, 0.7, 0.3]
    y = [0, 0, 1, 1, 0]
    ours = compute_brier(p, y)
    theirs = brier_score_loss(y, p)
    assert ours == pytest.approx(theirs, rel=1e-9)


def test_brier_matches_sklearn_on_uniform_predictions():
    """A uniform 0.5 forecaster has Brier = 0.25 (mean((0.5 - y)²))."""
    from sklearn.metrics import brier_score_loss

    p = [0.5] * 10
    y = [0, 0, 1, 1, 0, 1, 0, 1, 0, 1]
    ours = compute_brier(p, y)
    theirs = brier_score_loss(y, p)
    assert ours == pytest.approx(theirs, rel=1e-9)
    assert ours == pytest.approx(0.25, rel=1e-9)


def test_log_loss_matches_sklearn_log_loss():
    import numpy as np
    from sklearn.metrics import log_loss

    probs = [[0.9, 0.1], [0.6, 0.4], [0.1, 0.9], [0.3, 0.7], [0.7, 0.3]]
    ys_idx = [0, 0, 1, 1, 0]
    ours = compute_log_loss(probs, ys_idx)
    theirs = log_loss(ys_idx, np.array(probs))
    assert ours == pytest.approx(theirs, rel=1e-9)


def test_log_loss_matches_sklearn_multiclass():
    import numpy as np
    from sklearn.metrics import log_loss

    probs = [
        [0.7, 0.2, 0.1],
        [0.1, 0.8, 0.1],
        [0.2, 0.3, 0.5],
        [0.6, 0.3, 0.1],
    ]
    ys_idx = [0, 1, 2, 0]
    ours = compute_log_loss(probs, ys_idx)
    theirs = log_loss(ys_idx, np.array(probs), labels=[0, 1, 2])
    assert ours == pytest.approx(theirs, rel=1e-9)


def test_mape_matches_sklearn_mean_absolute_percentage_error():
    from sklearn.metrics import mean_absolute_percentage_error

    forecasts = [110.0, 90.0, 50.0, 200.0]
    actuals = [100.0, 100.0, 50.0, 220.0]
    ours = compute_mape(forecasts, actuals)
    theirs = mean_absolute_percentage_error(actuals, forecasts)
    assert ours == pytest.approx(theirs, rel=1e-9)


# --------------------------------------------------------------------------- #
# compute_causal_residual — chained-comparison guard                          #
# --------------------------------------------------------------------------- #


def test_causal_residual_rejects_a_eq_c_but_b_diff():
    """Regression: ``len(a) != len(b) != len(c)`` is the Python
    chained-comparison trap. It evaluates to ``(a != b) and (b != c)``,
    which silently ACCEPTS the case (len(a) == len(c), len(a) != len(b)).
    Verify the fixed code rejects this malformed input.
    """
    # len(a)=2, len(b)=3, len(c)=2 — should return None (length mismatch).
    out = compute_causal_residual([0.5, 0.6], [1.0, 1.1, 1.2], [0.1, 0.2])
    assert out is None


def test_causal_residual_rejects_b_eq_c_but_a_diff():
    """Mirror case: len(a) differs while len(b) == len(c)."""
    out = compute_causal_residual([0.5], [1.0, 1.1], [0.2, 0.3])
    assert out is None


def test_causal_residual_accepts_matched_lengths():
    out = compute_causal_residual([0.5, 0.6, 0.7], [1.0, 1.1, 1.2], [0.5, 0.5, 0.5])
    assert out is not None
    assert math.isfinite(out)


# --------------------------------------------------------------------------- #
# compute_brier — NaN filtering                                               #
# --------------------------------------------------------------------------- #


def test_brier_filters_nan_predictions():
    """NaN predictions are silently dropped (defensive against bad data)."""
    out = compute_brier([0.5, float("nan"), 0.5], [1, 0, 1])
    assert out == pytest.approx(0.25, rel=1e-9)  # 2 usable pairs


# --------------------------------------------------------------------------- #
# compute_mape — eps floor                                                    #
# --------------------------------------------------------------------------- #


def test_mape_eps_floor_protects_zero_actuals():
    """Even when every actual is 0, no ZeroDivisionError surfaces."""
    out = compute_mape([1.0, 2.0, 3.0], [0.0, 0.0, 0.0])
    assert out is not None
    assert math.isfinite(out)
    # eps = 1e-6 → 1/1e-6 + 2/1e-6 + 3/1e-6 = 6e6, /3 = 2e6
    assert out == pytest.approx(2e6, rel=1e-6)
