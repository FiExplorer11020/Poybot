"""R8 Wave-3 hardening tests for :mod:`src.strategy_classifier.drift`.

These plug numerical-stability + edge-case gaps in the original R8 test
suite. They DO NOT touch DB I/O — every DB call is mocked.

Covers:

* JS divergence numerical stability on very-skewed distributions
  (one-vs-near-zero, two near-deltas at different indices).
* JS divergence bounds in [0, 1] under log_2 across many randomised
  pairs (Monte-Carlo invariance check).
* JS divergence returns 0 on all-zero inputs (early return path).
* JS divergence on 9-class uniform-vs-delta produces the value used in
  daemon drift thresholding (sanity reference value).
* Drift detector treats exactly ``min_baseline_samples`` rows as the
  threshold inclusive/exclusive boundary.
* Drift detector ignores rows with missing/None ``strategy_probs``
  without crashing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from src.strategy_classifier.drift import StrategyDriftDetector, js_divergence
from src.strategy_classifier.model import STRATEGY_CLASSES


class TestJSDivergenceNumericalStability:
    def test_very_skewed_near_delta_distributions(self):
        """Two near-delta distributions at different indices → near upper bound."""
        p = np.array([1.0 - 1e-12, 5e-13, 5e-13])
        q = np.array([5e-13, 1.0 - 1e-12, 5e-13])
        v = js_divergence(p, q)
        # Should be very close to 1.0 (log_2 upper bound).
        assert 0.999 < v <= 1.0 + 1e-9

    def test_one_vs_near_zero(self):
        """All mass on class 0 vs a tiny epsilon on class 1."""
        p = np.array([1.0, 0.0])
        q = np.array([1.0 - 1e-6, 1e-6])
        v = js_divergence(p, q)
        # Very small but strictly positive.
        assert 0.0 < v < 0.001

    def test_all_zero_returns_zero(self):
        """Defensive: if either side has zero total mass, JS = 0 (early return)."""
        assert js_divergence(np.zeros(9), np.array([1.0] + [0.0] * 8)) == 0.0
        assert js_divergence(np.array([1.0] + [0.0] * 8), np.zeros(9)) == 0.0

    def test_negative_inputs_normalised_away_via_sum_check(self):
        """Negative-summed inputs short-circuit to 0 (defensive)."""
        # If users supply unnormalised vectors that net to <= 0, we return 0.
        assert js_divergence(np.array([-1.0, -1.0]), np.array([0.5, 0.5])) == 0.0

    @pytest.mark.parametrize("seed", [0, 1, 7, 42, 100])
    def test_js_bounded_zero_to_one_under_log2(self, seed):
        """Monte-Carlo: JS(log_2) MUST stay in [0, 1] for any prob vectors."""
        rng = np.random.default_rng(seed)
        for _ in range(50):
            p = rng.dirichlet(np.ones(9))
            q = rng.dirichlet(np.ones(9))
            v = js_divergence(p, q)
            assert 0.0 <= v <= 1.0 + 1e-9, f"out of bounds: {v}"

    def test_js_uniform_vs_delta_9class_reference(self):
        """Reference value used by drift threshold tuning.

        JS(log_2)(U_9, e_0) is a fixed constant — when the operator picks
        threshold=0.3, this value (≈ 0.739) firmly exceeds it, so a
        wallet flipping from uniform to a delta on class 0 always drifts.
        """
        u = np.full(9, 1.0 / 9.0)
        d = np.zeros(9)
        d[0] = 1.0
        v = js_divergence(u, d)
        # Documented in the spec § 3.5 commentary; tune-knob sanity.
        assert 0.7 < v < 0.8, f"unexpected reference value {v}"


class TestDriftDetectorBoundaryConditions:
    @pytest.mark.asyncio
    async def test_exact_min_baseline_samples_evaluates(self):
        """When baseline has EXACTLY min_baseline_samples rows, the detector
        evaluates (does NOT cold-start-skip)."""
        d = StrategyDriftDetector(threshold=0.3, min_baseline_samples=5)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["market_maker"] = 1.0
        # Exactly 5 rows in baseline, all directional.
        baseline = [
            {"primary_strategy": "directional",
             "strategy_probs": {s: (1.0 if s == "directional" else 0.0) for s in STRATEGY_CLASSES}}
            for _ in range(5)
        ]
        with patch.object(d, "_load_baseline", new=AsyncMock(return_value=baseline)):
            report = await d.evaluate("0xabc", current)
        assert report.baseline_samples == 5
        # Class flip → drift fires.
        assert report.drift_detected is True

    @pytest.mark.asyncio
    async def test_one_less_than_min_baseline_samples_cold_starts(self):
        d = StrategyDriftDetector(threshold=0.3, min_baseline_samples=5)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["market_maker"] = 1.0
        baseline = [
            {"primary_strategy": "directional",
             "strategy_probs": {"directional": 1.0}}
            for _ in range(4)
        ]
        with patch.object(d, "_load_baseline", new=AsyncMock(return_value=baseline)):
            report = await d.evaluate("0xabc", current)
        assert report.baseline_samples == 4
        # Cold start: never fires even if probs are radically different.
        assert report.drift_detected is False
        assert report.js_divergence == 0.0

    @pytest.mark.asyncio
    async def test_missing_strategy_probs_in_baseline_row_ignored(self):
        """Defensive: a baseline row with ``strategy_probs=None`` is skipped
        from the average rather than crashing the evaluator."""
        d = StrategyDriftDetector(threshold=0.3, min_baseline_samples=3)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["directional"] = 1.0
        baseline = [
            {"primary_strategy": "directional", "strategy_probs": None},  # malformed
            {"primary_strategy": "directional",
             "strategy_probs": {"directional": 1.0}},
            {"primary_strategy": "directional",
             "strategy_probs": {"directional": 1.0}},
            {"primary_strategy": "directional",
             "strategy_probs": {"directional": 1.0}},
        ]
        with patch.object(d, "_load_baseline", new=AsyncMock(return_value=baseline)):
            report = await d.evaluate("0xabc", current)
        # No crash, baseline_samples reflects rows fetched (incl. the bad one).
        assert report.baseline_samples == 4
        # Same distribution → drift not fired.
        assert report.drift_detected is False
