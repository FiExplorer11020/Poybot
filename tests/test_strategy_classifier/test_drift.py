"""Unit tests for StrategyDriftDetector.

Cover:

* JS divergence math (basic identity, symmetric, monotone in
  difference).
* Threshold triggers correctly.
* Cold-start (< min_baseline_samples) does NOT flag drift.
* Same probabilities -> JS divergence ≈ 0.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import json
import numpy as np
import pytest

from src.strategy_classifier.drift import (
    StrategyDriftDetector,
    js_divergence,
)
from src.strategy_classifier.model import STRATEGY_CLASSES


class TestJSDivergence:
    def test_js_identical_distributions_is_zero(self):
        p = np.array([0.1, 0.2, 0.3, 0.4])
        q = p.copy()
        assert js_divergence(p, q) == pytest.approx(0.0, abs=1e-9)

    def test_js_orthogonal_is_one(self):
        # log_2 base; orthogonal distributions hit the upper bound 1.
        p = np.array([1.0, 0.0])
        q = np.array([0.0, 1.0])
        assert js_divergence(p, q) == pytest.approx(1.0, abs=1e-6)

    def test_js_symmetric(self):
        p = np.array([0.7, 0.2, 0.1])
        q = np.array([0.2, 0.6, 0.2])
        assert js_divergence(p, q) == pytest.approx(js_divergence(q, p), abs=1e-9)

    def test_js_handles_zero_probs(self):
        # Don't blow up on log(0)
        p = np.array([1.0, 0.0, 0.0])
        q = np.array([0.5, 0.5, 0.0])
        val = js_divergence(p, q)
        assert val > 0.0 and val < 1.0

    def test_js_normalises_unnormalised_inputs(self):
        p = np.array([2.0, 1.0])  # sums to 3
        q = np.array([1.0, 1.0])  # sums to 2
        val = js_divergence(p, q)
        # Normalised: p=(2/3, 1/3), q=(0.5, 0.5)
        ref = js_divergence(np.array([2/3, 1/3]), np.array([0.5, 0.5]))
        assert val == pytest.approx(ref, abs=1e-9)

    def test_js_shape_mismatch_raises(self):
        with pytest.raises(ValueError):
            js_divergence(np.array([0.5, 0.5]), np.array([0.5, 0.4, 0.1]))


class TestStrategyDriftDetector:
    @pytest.mark.asyncio
    async def test_cold_start_no_drift(self):
        """When there aren't enough baseline samples, drift never fires."""
        detector = StrategyDriftDetector(threshold=0.1, min_baseline_samples=5)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["directional"] = 1.0
        # Mock the baseline load to return only 2 rows (< 5)
        with patch.object(
            detector, "_load_baseline",
            new=AsyncMock(return_value=[
                {"primary_strategy": "directional", "strategy_probs": {"directional": 1.0}},
                {"primary_strategy": "directional", "strategy_probs": {"directional": 1.0}},
            ]),
        ):
            report = await detector.evaluate("0xabc", current)
        assert report.drift_detected is False
        assert report.baseline_samples == 2

    @pytest.mark.asyncio
    async def test_drift_detected_on_class_flip(self):
        """Wallet was always 'directional', now is 'market_maker'. JS
        divergence between (1,0,...) and (0,...,1) under log_2 = 1.0, which
        exceeds any reasonable threshold."""
        detector = StrategyDriftDetector(threshold=0.3, min_baseline_samples=3)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["market_maker"] = 1.0
        baseline_rows = []
        for _ in range(5):
            probs = {s: 0.0 for s in STRATEGY_CLASSES}
            probs["directional"] = 1.0
            baseline_rows.append({
                "primary_strategy": "directional",
                "strategy_probs": probs,
            })
        with patch.object(
            detector, "_load_baseline",
            new=AsyncMock(return_value=baseline_rows),
        ):
            report = await detector.evaluate("0xabc", current)
        assert report.drift_detected is True
        assert report.js_divergence > 0.3
        assert report.primary_strategy_now == "market_maker"
        assert report.primary_strategy_baseline == "directional"

    @pytest.mark.asyncio
    async def test_no_drift_on_stable_distribution(self):
        """Baseline and current are identical → JS divergence ≈ 0 → no drift."""
        detector = StrategyDriftDetector(threshold=0.3, min_baseline_samples=3)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["directional"] = 0.6
        current["momentum"] = 0.4
        baseline_rows = []
        for _ in range(10):
            baseline_rows.append({
                "primary_strategy": "directional",
                "strategy_probs": dict(current),
            })
        with patch.object(
            detector, "_load_baseline",
            new=AsyncMock(return_value=baseline_rows),
        ):
            report = await detector.evaluate("0xabc", current)
        assert report.drift_detected is False
        assert report.js_divergence < 0.01

    @pytest.mark.asyncio
    async def test_baseline_handles_json_string_probs(self):
        """leader_strategy_history.strategy_probs is JSONB; asyncpg may
        decode to dict OR string depending on driver settings. We accept
        both."""
        detector = StrategyDriftDetector(threshold=0.3, min_baseline_samples=3)
        current = {s: 0.0 for s in STRATEGY_CLASSES}
        current["directional"] = 1.0
        baseline_rows = [
            {
                "primary_strategy": "directional",
                "strategy_probs": json.dumps({"directional": 1.0}),
            }
            for _ in range(5)
        ]
        with patch.object(
            detector, "_load_baseline",
            new=AsyncMock(return_value=baseline_rows),
        ):
            report = await detector.evaluate("0xabc", current)
        # Same distribution → no drift
        assert report.drift_detected is False
