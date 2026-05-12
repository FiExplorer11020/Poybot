"""R8 Wave-3 hardening tests for :mod:`src.strategy_classifier.model`.

Covers:

* ``_lightgbm_available`` is queryable as a public-ish runtime check
  (regression-guards: it is the ONLY gate consulted by ``fit()``).
* save/load round-trip preserves the dummy uniform-prior state (i.e.,
  a dummy classifier loaded from disk still emits 1/9 uniformly).
* ``predict_one`` correctly handles a 2D-row input (one wallet packaged
  as ``(1, 42)``) and returns the same prediction as the 1D case.
* ``build_classification_json_patch`` rounds every probability to 4 dp
  (regression: avoid float-glob spam in the leaders table).
* ``to_history_row`` packs the correct columns (regression: schema
  drift between code and migration 026).
* Unknown LGBMClassifier class order is corrected at predict time
  (i.e., the column-reorder path).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from src.strategy_classifier.model import (
    MODEL_VERSION,
    STRATEGY_CLASSES,
    STRATEGY_WEIGHTS,
    StrategyClassifier,
    StrategyPrediction,
    _lightgbm_available,
)


class TestLightGBMDetection:
    def test_lightgbm_available_returns_bool(self):
        """The helper must always return a real boolean (regression: a
        truthy-but-not-bool return would still pass `if` checks but mess
        up serialisation downstream)."""
        v = _lightgbm_available()
        assert isinstance(v, bool)

    def test_lightgbm_available_matches_importlib_find_spec(self):
        """The contract: True iff lightgbm is importable via importlib.

        This guards against future refactors that swap to a bare
        ``import lightgbm`` try/except (which would fail differently in
        partial installs).
        """
        expected = importlib.util.find_spec("lightgbm") is not None
        assert _lightgbm_available() == expected


class TestDummyClassifierPersistence:
    def test_dummy_save_load_round_trip(self, tmp_path: Path):
        """Saving an unfitted classifier and loading it back preserves the
        uniform-prior behaviour."""
        clf = StrategyClassifier()
        p = tmp_path / "dummy.pkl"
        clf.save(p)
        assert p.exists()
        clf2 = StrategyClassifier.load(p)
        X = np.zeros((4, 42))
        np.testing.assert_allclose(
            clf.predict_proba(X), clf2.predict_proba(X), atol=1e-12
        )
        # Dummy → model is None and is_fitted() is False.
        assert clf2.is_fitted() is False

    def test_load_nonexistent_raises(self, tmp_path: Path):
        """Missing pickle path raises FileNotFoundError (regression: the
        daemon catches this and falls back to the dummy)."""
        with pytest.raises(FileNotFoundError):
            StrategyClassifier.load(tmp_path / "does_not_exist.pkl")


class TestPredictOneShapes:
    def test_predict_one_1d_and_2d_match(self):
        """Same vector packaged 1D vs 2D yields the same prediction."""
        clf = StrategyClassifier()
        x_1d = np.zeros(42)
        x_2d = np.zeros((1, 42))
        p_1d = clf.predict_one(x_1d)
        p_2d = clf.predict_one(x_2d)
        assert p_1d.primary_strategy == p_2d.primary_strategy
        assert p_1d.confidence == pytest.approx(p_2d.confidence)


class TestClassificationJsonPatch:
    def test_strategy_probs_rounded_to_four_dp(self):
        """The patch dict's strategy_probs values are all 4-dp floats (so
        the leaders.classification_json column doesn't accumulate float
        noise on every UPDATE)."""
        clf = StrategyClassifier()
        # Construct a synthetic prediction with full-precision probs.
        probs = {s: 1.0 / 9.0 + 1e-9 * i for i, s in enumerate(STRATEGY_CLASSES)}
        pred = StrategyPrediction(
            strategy_probs=probs,
            primary_strategy="directional",
            confidence=probs["directional"],
            model_version=MODEL_VERSION,
            fitted_at=None,
        )
        patch = clf.build_classification_json_patch(pred, drift_detected=True)
        for v in patch["strategy_probs"].values():
            # 4-dp rounding → at most 5 significant digits including the
            # leading 0.
            s = f"{v:.10f}".rstrip("0")
            decimals = s.split(".")[-1] if "." in s else ""
            assert len(decimals) <= 4, (
                f"strategy_probs value {v} has more than 4 dp"
            )
        # Round-trip via JSON to verify the dict serialises cleanly.
        json.dumps(patch)
        assert patch["drift_detected"] is True
        assert patch["primary_strategy"] == "directional"


class TestHistoryRowShape:
    def test_to_history_row_columns_match_migration_026(self):
        """The dict returned by to_history_row must contain exactly the
        columns the daemon INSERTs (regression: schema drift between
        Python and SQL)."""
        clf = StrategyClassifier()
        pred = StrategyPrediction(
            strategy_probs={s: 1.0 / 9.0 for s in STRATEGY_CLASSES},
            primary_strategy="directional",
            confidence=1.0 / 9.0,
            model_version=MODEL_VERSION,
            fitted_at=None,
        )
        asof = datetime(2026, 5, 1, tzinfo=timezone.utc)
        row = clf.to_history_row(
            wallet_address="0xabc",
            prediction=pred,
            asof_ts=asof,
            drift_js_divergence=0.1234,
            drift_detected=False,
        )
        # The migration 026 schema cols (minus auto-generated history_id):
        expected = {
            "wallet_address", "classified_at", "primary_strategy",
            "confidence", "strategy_probs", "model_version", "asof_ts",
            "drift_js_divergence", "drift_detected",
        }
        assert set(row.keys()) == expected
        # JSON-encoded probs round-trip.
        decoded = json.loads(row["strategy_probs"])
        assert set(decoded.keys()) == set(STRATEGY_CLASSES)
        assert row["asof_ts"] == asof
        assert row["drift_detected"] is False
        assert row["drift_js_divergence"] == pytest.approx(0.1234, abs=1e-9)

    def test_to_history_row_handles_none_divergence(self):
        clf = StrategyClassifier()
        pred = StrategyPrediction(
            strategy_probs={s: 1.0 / 9.0 for s in STRATEGY_CLASSES},
            primary_strategy="directional",
            confidence=1.0 / 9.0,
            model_version=MODEL_VERSION,
            fitted_at=None,
        )
        asof = datetime(2026, 5, 1, tzinfo=timezone.utc)
        row = clf.to_history_row(
            wallet_address="0xabc",
            prediction=pred,
            asof_ts=asof,
            drift_js_divergence=None,
            drift_detected=False,
        )
        assert row["drift_js_divergence"] is None


class TestColumnReorderingFitted:
    @pytest.mark.parametrize("seed", [0, 42])
    def test_lightgbm_columns_aligned_to_spec_order(self, seed: int):
        """When fitting on a label set whose lexicographic order differs
        from STRATEGY_CLASSES, the columns returned by predict_proba must
        still match STRATEGY_CLASSES.
        """
        pytest.importorskip("lightgbm")
        rng = np.random.default_rng(seed)
        # Build a 3-class subset whose lexicographic vs spec order differ.
        # spec_order subset (in STRATEGY_CLASSES order): directional,
        # momentum, info_leak. Lex order: directional, info_leak,
        # momentum. LightGBM uses lex order → without reordering, the
        # column for 'info_leak' would be index 1 in raw output but
        # index 7 in STRATEGY_CLASSES.
        subset = ["directional", "momentum", "info_leak"]
        n_per = 20
        X_list = []
        y_list = []
        for i, cls in enumerate(subset):
            c = np.zeros(42)
            c[i] = 5.0
            X_list.append(rng.normal(c, 0.3, size=(n_per, 42)))
            y_list.extend([cls] * n_per)
        X = np.vstack(X_list)
        clf = StrategyClassifier().fit(X, y_list)
        # Take one row from each true class; argmax of predict_proba must
        # be the true class as indexed into STRATEGY_CLASSES.
        spec_idx = {s: i for i, s in enumerate(STRATEGY_CLASSES)}
        # Use the first row of each cluster (centered on c[i]).
        for i, cls in enumerate(subset):
            row = X_list[i][0:1]
            probs = clf.predict_proba(row)[0]
            pred_idx = int(np.argmax(probs))
            assert pred_idx == spec_idx[cls], (
                f"column reorder bug: true {cls} (spec_idx {spec_idx[cls]}) "
                f"predicted index {pred_idx} ({STRATEGY_CLASSES[pred_idx]!r})"
            )
            # Total probability sums to 1 (within tolerance).
            assert probs.sum() == pytest.approx(1.0, abs=1e-3)


class TestStrategyWeightsCompleteness:
    def test_all_weights_are_numeric_and_nonnegative(self):
        """Defensive: no weight should be NaN or negative."""
        import math
        for s, w in STRATEGY_WEIGHTS.items():
            for k, v in w.items():
                assert isinstance(v, (int, float)), f"{s}.{k} not numeric"
                assert not math.isnan(v), f"{s}.{k} is NaN"
                assert v >= 0.0, f"{s}.{k} = {v} is negative"

    def test_structural_bot_has_huge_skip(self):
        """Defence-in-depth: structural_bot SKIP weight is 10x baseline."""
        assert STRATEGY_WEIGHTS["structural_bot"]["skip"] >= 5.0
