"""Unit tests for StrategyClassifier.

These cover:

* LightGBM-not-installed fallback (uniform prior, no fit available).
* Calibration smoke (fit on synthetic data, predictions sum to 1).
* save/load round-trip preserves predictions.
* predict_proba shape correctness (n_rows, 9 columns summing to 1).
* STRATEGY_WEIGHTS defaults match the spec § 3.6 table.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from src.strategy_classifier.model import (
    MODEL_VERSION,
    STRATEGY_CLASSES,
    STRATEGY_WEIGHTS,
    StrategyClass,
    StrategyClassifier,
)


def _lightgbm_installed() -> bool:
    return importlib.util.find_spec("lightgbm") is not None


class TestStrategyClasses:
    def test_strategy_classes_has_9(self):
        assert len(STRATEGY_CLASSES) == 9

    def test_strategy_classes_match_spec(self):
        # Hard-coded names from spec § 2. If this fails, migration 026's
        # CHECK constraint must be updated too.
        assert set(STRATEGY_CLASSES) == {
            "directional",
            "momentum",
            "contrarian",
            "arb_2way",
            "arb_3way",
            "market_maker",
            "structural_bot",
            "info_leak",
            "social_driven",
        }

    def test_enum_values_match_tuple(self):
        # StrategyClass.DIRECTIONAL.value must equal "directional".
        assert StrategyClass.DIRECTIONAL.value == "directional"
        assert all(s.value in STRATEGY_CLASSES for s in StrategyClass)


class TestStrategyWeights:
    def test_strategy_weights_cover_all_classes(self):
        # Every class must have a row.
        for s in STRATEGY_CLASSES:
            assert s in STRATEGY_WEIGHTS, f"missing weights for {s}"

    def test_strategy_weights_have_three_keys(self):
        for s, w in STRATEGY_WEIGHTS.items():
            assert set(w.keys()) == {"follow", "fade", "skip"}, (
                f"weights for {s} missing follow/fade/skip: {w}"
            )

    def test_directional_follow_upweighted(self):
        # Spec § 3.6 directional row: FOLLOW=1.5
        assert STRATEGY_WEIGHTS["directional"]["follow"] == pytest.approx(1.5)
        assert STRATEGY_WEIGHTS["directional"]["fade"] == pytest.approx(0.5)

    def test_info_leak_fade_upweighted(self):
        # Spec § 3.6 info_leak row: FADE=2.0
        assert STRATEGY_WEIGHTS["info_leak"]["fade"] == pytest.approx(2.0)

    def test_structural_bot_excluded(self):
        # Defence-in-depth: structural_bot follow/fade = 0
        assert STRATEGY_WEIGHTS["structural_bot"]["follow"] == 0.0
        assert STRATEGY_WEIGHTS["structural_bot"]["fade"] == 0.0


class TestStrategyClassifierDummy:
    def test_uniform_prior_when_unfitted(self):
        """Without a fitted model, predict_proba returns uniform prior."""
        clf = StrategyClassifier()
        X = np.zeros((3, 42))
        probs = clf.predict_proba(X)
        assert probs.shape == (3, 9)
        np.testing.assert_allclose(probs, np.full((3, 9), 1.0 / 9.0))
        # Row sums are 1.
        np.testing.assert_allclose(probs.sum(axis=1), np.ones(3))

    def test_predict_one_returns_dataclass(self):
        clf = StrategyClassifier()
        x = np.zeros(42)
        pred = clf.predict_one(x)
        # All probs are 1/9 → primary is the first class (argmax of ties).
        assert pred.primary_strategy in STRATEGY_CLASSES
        assert pred.confidence == pytest.approx(1.0 / 9.0)
        assert pred.model_version == MODEL_VERSION
        assert len(pred.strategy_probs) == 9

    def test_classification_json_patch_shape(self):
        clf = StrategyClassifier()
        x = np.zeros(42)
        pred = clf.predict_one(x)
        patch = clf.build_classification_json_patch(pred, drift_detected=True)
        assert patch["primary_strategy"] in STRATEGY_CLASSES
        assert "strategy_probs" in patch and len(patch["strategy_probs"]) == 9
        assert patch["drift_detected"] is True
        assert "classified_at" in patch
        assert "model_version" in patch

    def test_fit_without_lightgbm_raises(self):
        """When lightgbm is NOT installed, fit() raises with a clear message."""
        if _lightgbm_installed():
            pytest.skip("lightgbm is installed in this env — test irrelevant")
        clf = StrategyClassifier()
        X = np.random.rand(20, 42)
        y = ["directional"] * 10 + ["momentum"] * 10
        with pytest.raises(RuntimeError, match="lightgbm"):
            clf.fit(X, y)


class TestStrategyClassifierLightGBM:
    """These tests skip cleanly when LightGBM isn't installed.

    Use pytest.importorskip so we don't pollute the dummy-path tests
    with mandatory dep checks.
    """

    def test_calibration_smoke(self):
        pytest.importorskip("lightgbm")
        # Synthetic 9-class separable-ish dataset
        rng = np.random.default_rng(42)
        n_per_class = 20
        X_list = []
        y_list = []
        for i, cls in enumerate(STRATEGY_CLASSES):
            # Each class has its centroid at a different feature index.
            centroid = np.zeros(42)
            centroid[i % 42] = 5.0
            X_list.append(rng.normal(centroid, 0.5, size=(n_per_class, 42)))
            y_list.extend([cls] * n_per_class)
        X = np.vstack(X_list)
        clf = StrategyClassifier()
        clf.fit(X, y_list)
        assert clf.is_fitted()
        probs = clf.predict_proba(X)
        assert probs.shape == (n_per_class * 9, 9)
        # Rows sum to ~1
        np.testing.assert_allclose(probs.sum(axis=1), np.ones(n_per_class * 9), atol=1e-3)
        # Argmax is one of the 9 valid classes
        preds = clf.predict(X)
        for p in preds:
            assert p in STRATEGY_CLASSES

    def test_save_load_roundtrip(self, tmp_path: Path):
        pytest.importorskip("lightgbm")
        rng = np.random.default_rng(7)
        n = 30
        X_list = []
        y_list = []
        for i, cls in enumerate(STRATEGY_CLASSES):
            centroid = np.zeros(42)
            centroid[(i * 3) % 42] = 4.0
            X_list.append(rng.normal(centroid, 0.7, size=(n // 9 + 1, 42)))
            y_list.extend([cls] * (n // 9 + 1))
        X = np.vstack(X_list)
        clf = StrategyClassifier().fit(X, y_list)
        path = tmp_path / "sc.pkl"
        clf.save(path)
        assert path.exists()
        clf2 = StrategyClassifier.load(path)
        probs1 = clf.predict_proba(X)
        probs2 = clf2.predict_proba(X)
        np.testing.assert_allclose(probs1, probs2, atol=1e-9)

    def test_fit_rejects_unknown_class(self):
        pytest.importorskip("lightgbm")
        clf = StrategyClassifier()
        X = np.zeros((4, 42))
        y = ["directional", "momentum", "unknown_class", "directional"]
        with pytest.raises(ValueError, match="unknown_class"):
            clf.fit(X, y)


class TestColumnAlignment:
    def test_probs_column_order_matches_strategy_classes(self):
        """Even with the uniform-prior dummy, columns must be in spec order."""
        clf = StrategyClassifier()
        X = np.zeros((1, 42))
        probs = clf.predict_proba(X)
        # Every column is 1/9; verify shape and that .predict picks
        # STRATEGY_CLASSES[0].
        assert probs.shape[1] == len(STRATEGY_CLASSES)
        pred = clf.predict(X)[0]
        assert pred == STRATEGY_CLASSES[0]  # argmax of ties = idx 0
