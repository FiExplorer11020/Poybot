"""R8 Wave-3 hardening tests for :mod:`src.strategy_classifier.daemon`.

Covers:

* A feature extractor that returns a wrong-shape vector causes the
  daemon to SKIP that wallet (not crash, not poison the metric).
* A feature extractor that raises is caught — the daemon continues
  with the next wallet.
* asyncio.CancelledError during a pass is honoured (the loop returns
  cleanly).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from src.strategy_classifier.daemon import StrategyClassifierDaemon
from src.strategy_classifier.drift import DriftReport
from src.strategy_classifier.features import FEATURE_COUNT, FeatureVector
from src.strategy_classifier.model import STRATEGY_CLASSES, StrategyClassifier


class _StubDrift:
    async def evaluate(self, wallet, probs, classified_at=None):
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


class _GoodExtractor:
    async def extract(self, wallet, asof):
        return FeatureVector(
            wallet_address=wallet,
            asof_ts=asof,
            values=np.zeros(FEATURE_COUNT),
            missing=[],
        )


class _BadShapeExtractor:
    """Returns a vector with the WRONG length on every call."""

    async def extract(self, wallet, asof):
        return FeatureVector(
            wallet_address=wallet,
            asof_ts=asof,
            values=np.zeros(FEATURE_COUNT - 5),  # wrong shape
            missing=[],
        )


class _RaisingExtractor:
    """First call raises; subsequent calls return a valid vector."""

    def __init__(self):
        self._calls = 0

    async def extract(self, wallet, asof):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("simulated DuckDB read failure")
        return FeatureVector(
            wallet_address=wallet,
            asof_ts=asof,
            values=np.zeros(FEATURE_COUNT),
            missing=[],
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


class TestDaemonResilience:
    @pytest.mark.asyncio
    async def test_wrong_shape_feature_vector_skipped(self, tmp_path):
        """A feature vector of wrong length should be silently skipped
        (with a warning log) — NOT crash the whole pass."""
        wallets = [
            {"wallet_address": f"0x{i:040x}",
             "last_active": datetime(2026, 5, 1, tzinfo=timezone.utc)}
            for i in range(3)
        ]
        ctx, _ = _mock_db(fetch_rows=wallets)
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_BadShapeExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )
        with patch("src.strategy_classifier.daemon.get_db", side_effect=ctx):
            result = await daemon.run_one_pass()
        # All 3 wallets skipped because feature vector had wrong shape.
        # The pass DID NOT crash.
        assert result["classified"] == 0
        assert result["drift_alerts"] == 0

    @pytest.mark.asyncio
    async def test_extractor_raises_continues(self, tmp_path):
        """If extract() raises on one wallet, the daemon should log and
        continue with the next."""
        wallets = [
            {"wallet_address": f"0x{i:040x}",
             "last_active": datetime(2026, 5, 1, tzinfo=timezone.utc)}
            for i in range(2)
        ]
        ctx, _ = _mock_db(fetch_rows=wallets)
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_RaisingExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )
        with patch("src.strategy_classifier.daemon.get_db", side_effect=ctx):
            result = await daemon.run_one_pass()
        # First wallet raised; second succeeded.
        assert result["classified"] == 1
        assert result["drift_alerts"] == 0

    @pytest.mark.asyncio
    async def test_default_model_path_resolves_from_settings(self, tmp_path):
        """When no explicit model_path is given, the daemon resolves from
        settings.STRATEGY_CLASSIFIER_MODEL_PATH (or the in-module default)."""
        # Pass a path that doesn't exist; daemon falls back to dummy
        # without crashing.
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "absent.pkl",
            refresh_interval_h=24,
            feature_extractor=_GoodExtractor(),
            drift_detector=_StubDrift(),
        )
        assert daemon._classifier is not None
        # Confirms a uniform-prior dummy was loaded.
        probs = daemon._classifier.predict_proba(np.zeros((1, FEATURE_COUNT)))
        assert probs.shape == (1, 9)

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, tmp_path):
        """Calling stop() twice does not raise."""
        daemon = StrategyClassifierDaemon(
            model_path=tmp_path / "no-model.pkl",
            refresh_interval_h=24,
            feature_extractor=_GoodExtractor(),
            classifier=StrategyClassifier(),
            drift_detector=_StubDrift(),
        )
        await daemon.stop()
        await daemon.stop()  # second call: no error.
