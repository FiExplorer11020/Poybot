"""Smoke tests for StrategyClassifierDaemon.

The daemon does I/O against multiple subsystems (DB, classifier model,
drift detector). We mock the DB and inject a dummy classifier to verify
the daemon shape — graceful cancel, one-pass returns a summary dict,
metric handles don't crash on no-op fallback.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.strategy_classifier.daemon import StrategyClassifierDaemon
from src.strategy_classifier.features import FEATURE_COUNT, FeatureVector
from src.strategy_classifier.model import (
    STRATEGY_CLASSES,
    StrategyClassifier,
)
import numpy as np


class _DummyExtractor:
    async def extract(self, wallet, asof):
        return FeatureVector(
            wallet_address=wallet,
            asof_ts=asof,
            values=np.zeros(FEATURE_COUNT),
            missing=[],
        )


class _StubDrift:
    async def evaluate(self, wallet, probs, classified_at=None):
        from src.strategy_classifier.drift import DriftReport
        primary = max(probs, key=probs.get) if probs else STRATEGY_CLASSES[0]
        return DriftReport(
            wallet_address=wallet,
            js_divergence=0.0,
            drift_detected=False,
            baseline_window_days=30,
            baseline_samples=0,
            primary_strategy_now=primary,
            primary_strategy_baseline=None,
        )


def _mock_db(fetch_rows=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestDaemonShape:
    @pytest.mark.asyncio
    async def test_one_pass_empty_universe(self, tmp_path):
        """Daemon with no wallets returns {classified: 0, drift_alerts: 0}."""
        ctx, _ = _mock_db(fetch_rows=[])
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_DummyExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )
        with patch("src.strategy_classifier.daemon.get_db", side_effect=ctx):
            result = await daemon.run_one_pass()
        assert result == {"classified": 0, "drift_alerts": 0}

    @pytest.mark.asyncio
    async def test_one_pass_classifies_each_wallet(self, tmp_path):
        wallets_rows = [
            {"wallet_address": f"0x{i:040x}",
             "last_active": datetime(2026, 5, 1, tzinfo=timezone.utc)}
            for i in range(3)
        ]
        ctx, _ = _mock_db(fetch_rows=wallets_rows)
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_DummyExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )
        with patch("src.strategy_classifier.daemon.get_db", side_effect=ctx):
            result = await daemon.run_one_pass()
        assert result["classified"] == 3
        assert result["drift_alerts"] == 0

    @pytest.mark.asyncio
    async def test_graceful_cancel(self, tmp_path):
        """Daemon.start() returns when stop() is called from another task."""
        ctx, _ = _mock_db(fetch_rows=[])
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_DummyExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )

        async def _stop_after_short_delay():
            await asyncio.sleep(0.05)
            await daemon.stop()

        with patch("src.strategy_classifier.daemon.get_db", side_effect=ctx):
            await asyncio.wait_for(
                asyncio.gather(daemon.start(), _stop_after_short_delay()),
                timeout=2.0,
            )

    def test_loads_dummy_classifier_when_no_model_file(self, tmp_path):
        """Missing model file → uniform-prior dummy, with warning."""
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "missing.pkl",
            refresh_interval_h=24,
            feature_extractor=_DummyExtractor(),
            drift_detector=_StubDrift(),
        )
        assert daemon._classifier is not None
        assert daemon._classifier.is_fitted() is False
        # Dummy returns uniform-prior probabilities.
        probs = daemon._classifier.predict_proba(np.zeros((1, FEATURE_COUNT)))
        assert probs.shape == (1, 9)
        np.testing.assert_allclose(probs, np.full((1, 9), 1 / 9))
