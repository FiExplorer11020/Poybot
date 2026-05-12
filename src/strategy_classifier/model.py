"""StrategyClassifier — LightGBM 9-class softmax + isotonic calibration.

Round 8 (The Lens) — § 3.3 of the spec.

This module is the single ground-truth definition of:

* The 9 strategy classes (:class:`StrategyClass`,
  :data:`STRATEGY_CLASSES`).
* The default per-class trade-policy weights used by the confidence
  engine (:data:`STRATEGY_WEIGHTS`). These are hyperparameters, not
  learned — operator-tunable but with sensible defaults that match the
  spec § 3.6 commentary block.
* The classifier class itself (:class:`StrategyClassifier`).

LightGBM is OPTIONAL at runtime. We mirror the pattern from
:mod:`src.profiler.error_model` (phase-3 model) — if LightGBM is not
installed, the classifier falls back to a uniform-prior dummy so unit
tests and CI can run without the heavy dep, and the production path
raises a clear error when the operator tries to ``fit`` without it.
"""
from __future__ import annotations

import importlib.util
import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from loguru import logger

# --------------------------------------------------------------------------- #
# Strategy taxonomy. The order here is LOAD-BEARING:                          #
#                                                                             #
#   * LightGBM's `objective='multiclass'` sorts the unique label values it    #
#     sees at fit time and assigns class indices accordingly. We pass         #
#     strings, so the sort is lexicographic. To keep our `predict_proba`     #
#     output dict-keyed by class name regardless of LightGBM's internal       #
#     index, we always re-map via this tuple at serialise / deserialise       #
#     time.                                                                   #
#                                                                             #
#   * Migrations 026 + 027 hard-code the same 9 strings in CHECK              #
#     constraints. Updating this tuple requires a matching schema             #
#     migration.                                                              #
# --------------------------------------------------------------------------- #


class StrategyClass(str, Enum):
    """Enum of the 9 supported strategy classes.

    Inheriting from :class:`str` so ``StrategyClass.DIRECTIONAL == "directional"``
    holds and the value can be passed straight into SQL parameter binding
    without an explicit ``.value`` unwrap.
    """

    DIRECTIONAL = "directional"
    MOMENTUM = "momentum"
    CONTRARIAN = "contrarian"
    ARB_2WAY = "arb_2way"
    ARB_3WAY = "arb_3way"
    MARKET_MAKER = "market_maker"
    STRUCTURAL_BOT = "structural_bot"
    INFO_LEAK = "info_leak"
    SOCIAL_DRIVEN = "social_driven"


# Canonical ordered tuple. Used everywhere we need a stable class-index.
STRATEGY_CLASSES: tuple[str, ...] = tuple(s.value for s in StrategyClass)


# --------------------------------------------------------------------------- #
# Default per-strategy weights used by the confidence engine. These mirror   #
# the spec § 3.6 commentary table — operator-tunable via runtime config in   #
# a future iteration, but for now they live as a module-level dict so the     #
# engine import path is dependency-free.                                      #
#                                                                             #
# Convention: weights are RELATIVE multipliers on the Thompson-Sampling       #
# scores. 1.0 = unchanged. >1.0 = upweight that action for this strategy.    #
# 0.0 = effectively veto (structural_bot is excluded entirely upstream by    #
# the registry's `excluded=TRUE` stamp, but the 0.0 weights are a defence    #
# in depth in case a structural_bot wallet slips through the gate).          #
# --------------------------------------------------------------------------- #

STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    StrategyClass.DIRECTIONAL.value:    {"follow": 1.5, "fade": 0.5, "skip": 1.0},
    StrategyClass.MOMENTUM.value:       {"follow": 1.0, "fade": 1.0, "skip": 1.2},
    StrategyClass.CONTRARIAN.value:     {"follow": 1.2, "fade": 0.8, "skip": 1.0},
    StrategyClass.ARB_2WAY.value:       {"follow": 0.3, "fade": 0.3, "skip": 2.0},
    StrategyClass.ARB_3WAY.value:       {"follow": 0.2, "fade": 0.2, "skip": 2.0},
    StrategyClass.MARKET_MAKER.value:   {"follow": 0.2, "fade": 0.2, "skip": 2.0},
    StrategyClass.STRUCTURAL_BOT.value: {"follow": 0.0, "fade": 0.0, "skip": 10.0},
    StrategyClass.INFO_LEAK.value:      {"follow": 0.5, "fade": 2.0, "skip": 1.0},
    StrategyClass.SOCIAL_DRIVEN.value:  {"follow": 1.0, "fade": 1.0, "skip": 1.0},
}


MODEL_VERSION = "sc.v1.0"


def _lightgbm_available() -> bool:
    """Return True if LightGBM is importable in this process."""
    return importlib.util.find_spec("lightgbm") is not None


@dataclass
class StrategyPrediction:
    """One row of classifier output. Returned by
    :meth:`StrategyClassifier.predict_proba_single`.
    """

    strategy_probs: dict[str, float]
    primary_strategy: str
    confidence: float
    model_version: str
    fitted_at: str | None


class StrategyClassifier:
    """LightGBM 9-class softmax + isotonic calibration wrapper.

    Lifecycle:

        clf = StrategyClassifier()
        clf.fit(X_train, y_train)                  # heavy; needs lightgbm
        probs = clf.predict_proba(X_val)           # (n, 9) numpy
        pred  = clf.predict(X_val)                 # length-n list of strings
        clf.save("model.pkl")
        clf2 = StrategyClassifier.load("model.pkl")

    When LightGBM is NOT installed:

        * ``fit`` raises ``RuntimeError`` with a clear message — the
          production path needs the real model.
        * ``predict_proba`` / ``predict`` STILL work; they return the
          uniform prior (1/9 each, primary = first class). This keeps
          downstream code (daemon, drift detector) testable without
          the heavy dep.
    """

    def __init__(self) -> None:
        # The fitted LightGBM model (possibly wrapped in CalibratedClassifierCV).
        self._model: Any | None = None
        self._fitted_at: datetime | None = None
        # LightGBM may sort classes differently from STRATEGY_CLASSES depending
        # on the labels present in y_train. We capture its idx -> name mapping
        # at fit time and reorder columns at predict time.
        self._lgb_classes: tuple[str, ...] | None = None

    # ------------------------------------------------------------------ #
    # Training                                                           #
    # ------------------------------------------------------------------ #

    def fit(self, X: np.ndarray, y: Iterable[str]) -> "StrategyClassifier":
        """Fit on (X, y).

        X: (n_samples, n_features) numeric array. ``np.nan`` entries are
        allowed — LightGBM handles them natively. Match the shape
        produced by :class:`src.strategy_classifier.features.LeaderFeatureExtractor`.

        y: iterable of strategy strings. Each value MUST be in
        :data:`STRATEGY_CLASSES` — we don't soft-accept misspellings.

        Returns self so the caller can chain ``.save(path)``.
        """
        if not _lightgbm_available():
            raise RuntimeError(
                "StrategyClassifier.fit() requires lightgbm. "
                "Install via `pip install lightgbm==4.3.0` (already pinned "
                "in project requirements; CI uses pytest.importorskip)."
            )

        from lightgbm import LGBMClassifier  # type: ignore[import]
        from sklearn.calibration import CalibratedClassifierCV  # type: ignore[import]

        X_arr = np.asarray(X, dtype=float)
        y_list = list(y)
        for label in y_list:
            if label not in STRATEGY_CLASSES:
                raise ValueError(
                    f"Unknown strategy label {label!r}. Expected one of "
                    f"{STRATEGY_CLASSES!r}."
                )

        # Base LightGBM. Class weights set to 'balanced' so minority classes
        # (info_leak, arb_3way) don't get drowned out — spec § 6 risk table
        # explicitly flags class imbalance as high-risk.
        base = LGBMClassifier(
            objective="multiclass",
            num_class=len(STRATEGY_CLASSES),
            n_estimators=200,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=5,
            class_weight="balanced",
            verbose=-1,
        )

        # Isotonic calibration — spec § 3.3 calls this out by name. We use
        # cv='prefit' is not possible here because we want to fit the base
        # AND calibrate in one pass; CalibratedClassifierCV handles that
        # via 3-fold internal CV by default.
        # NOTE: at very small training sizes (n < cv * min_class_count) sklearn
        # raises. We catch that and fall back to a single fit() with raw probs.
        try:
            self._model = CalibratedClassifierCV(
                estimator=base, method="isotonic", cv=3
            )
            self._model.fit(X_arr, y_list)
        except Exception as exc:  # pragma: no cover — exercised only on tiny datasets
            logger.warning(
                f"StrategyClassifier: isotonic calibration failed ({exc}); "
                "falling back to raw LightGBM probabilities. Operator MUST "
                "label more wallets before promoting this model to live."
            )
            base.fit(X_arr, y_list)
            self._model = base

        # Capture LightGBM's class order so we can rearrange columns later.
        if hasattr(self._model, "classes_"):
            self._lgb_classes = tuple(self._model.classes_)
        else:
            self._lgb_classes = STRATEGY_CLASSES

        self._fitted_at = datetime.now(tz=timezone.utc)
        logger.info(
            f"StrategyClassifier fitted: n_samples={X_arr.shape[0]} "
            f"n_features={X_arr.shape[1]} classes={self._lgb_classes}"
        )
        return self

    # ------------------------------------------------------------------ #
    # Inference                                                          #
    # ------------------------------------------------------------------ #

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return (n_samples, 9) probability matrix. Columns are aligned to
        :data:`STRATEGY_CLASSES` regardless of LightGBM's internal order.

        Rows always sum to ~1.0 (within float tolerance). When the model is
        unfitted AND LightGBM is unavailable we return a uniform prior
        (1/9 in every cell) so downstream code (daemon, drift detector,
        engine) is exercisable in unit tests.
        """
        X_arr = np.asarray(X, dtype=float)
        n = X_arr.shape[0]
        k = len(STRATEGY_CLASSES)

        if self._model is None:
            # Uniform prior — calibrated dummy. Tests rely on this branch.
            return np.full((n, k), 1.0 / k, dtype=float)

        probs = np.asarray(self._model.predict_proba(X_arr), dtype=float)

        # Re-order columns to the canonical STRATEGY_CLASSES order. When the
        # model was trained on a STRICT SUBSET of the 9 classes (common
        # during the early labelling sprint when info_leak / arb_3way etc.
        # have zero labelled wallets), missing classes get zero-filled —
        # NOT crashed via ``tuple.index`` raising ValueError. Spec § 7.A
        # rollout explicitly anticipates this regime.
        if self._lgb_classes is not None and tuple(self._lgb_classes) != STRATEGY_CLASSES:
            aligned = np.zeros((probs.shape[0], k), dtype=float)
            for j, cls in enumerate(STRATEGY_CLASSES):
                if cls in self._lgb_classes:
                    aligned[:, j] = probs[:, self._lgb_classes.index(cls)]
            probs = aligned

        # Defensive renormalisation (calibration + missing-class zero-fill
        # both leave row sums ≠ 1.0).
        row_sums = probs.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums > 0, row_sums, 1.0)
        return probs / row_sums

    def predict(self, X: np.ndarray) -> list[str]:
        """Argmax over the calibrated probability vector. Returns a list of
        strategy strings, length ``X.shape[0]``.
        """
        probs = self.predict_proba(X)
        idx = np.argmax(probs, axis=1)
        return [STRATEGY_CLASSES[i] for i in idx]

    def predict_one(self, x: np.ndarray) -> StrategyPrediction:
        """Convenience: classify a single feature vector. Returns a
        :class:`StrategyPrediction` dataclass shaped to land directly in
        ``leaders.classification_json -> strategy_fingerprint``.
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        probs = self.predict_proba(x)[0]
        idx = int(np.argmax(probs))
        return StrategyPrediction(
            strategy_probs={STRATEGY_CLASSES[i]: float(probs[i]) for i in range(len(STRATEGY_CLASSES))},
            primary_strategy=STRATEGY_CLASSES[idx],
            confidence=float(probs[idx]),
            model_version=MODEL_VERSION,
            fitted_at=self._fitted_at.isoformat() if self._fitted_at else None,
        )

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def save(self, path: str | Path) -> None:
        """Pickle the fitted model + metadata to ``path``. Atomic-ish via
        write-then-rename so a crash mid-save can't corrupt an existing
        artefact.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "model": self._model,
            "lgb_classes": self._lgb_classes,
            "fitted_at": self._fitted_at.isoformat() if self._fitted_at else None,
            "model_version": MODEL_VERSION,
            "strategy_classes": STRATEGY_CLASSES,
        }
        with tmp.open("wb") as fh:
            pickle.dump(payload, fh)
        tmp.replace(path)
        logger.info(f"StrategyClassifier saved to {path}")

    @classmethod
    def load(cls, path: str | Path) -> "StrategyClassifier":
        """Load a previously saved model. Raises FileNotFoundError if the
        path doesn't exist. If the pickle is from a different MODEL_VERSION
        we log a warning and keep going — the format is forward-compatible
        for now.
        """
        path = Path(path)
        with path.open("rb") as fh:
            payload = pickle.load(fh)
        clf = cls()
        clf._model = payload.get("model")
        clf._lgb_classes = (
            tuple(payload["lgb_classes"]) if payload.get("lgb_classes") else None
        )
        fitted_at = payload.get("fitted_at")
        if fitted_at:
            try:
                clf._fitted_at = datetime.fromisoformat(fitted_at)
            except ValueError:
                clf._fitted_at = None
        loaded_version = payload.get("model_version")
        if loaded_version and loaded_version != MODEL_VERSION:
            logger.warning(
                f"StrategyClassifier: loading model_version={loaded_version} "
                f"into runtime expecting {MODEL_VERSION}. Forward-compatible "
                "for now, but operator should retrain after upgrades."
            )
        return clf

    # ------------------------------------------------------------------ #
    # Convenience helpers used by the daemon                             #
    # ------------------------------------------------------------------ #

    def build_classification_json_patch(
        self,
        prediction: StrategyPrediction,
        drift_detected: bool = False,
    ) -> dict[str, Any]:
        """Build the ``strategy_fingerprint`` sub-object that goes into
        ``leaders.classification_json`` (migration 027 schema).

        Used by the daemon when writing back to the registry. Separated so
        the daemon doesn't need to know the exact key names — if we ever
        rename ``primary_strategy`` to ``class`` (spec § 3.3 keeps the
        former for now) only this method changes.
        """
        return {
            "primary_strategy": prediction.primary_strategy,
            "confidence": round(prediction.confidence, 4),
            "strategy_probs": {
                k: round(v, 4) for k, v in prediction.strategy_probs.items()
            },
            "model_version": prediction.model_version,
            "classified_at": datetime.now(tz=timezone.utc).isoformat(),
            "drift_detected": bool(drift_detected),
        }

    def is_fitted(self) -> bool:
        return self._model is not None

    def to_history_row(
        self,
        wallet_address: str,
        prediction: StrategyPrediction,
        asof_ts: datetime,
        drift_js_divergence: float | None = None,
        drift_detected: bool = False,
    ) -> dict[str, Any]:
        """Pack a prediction into the column dict for
        ``leader_strategy_history`` (migration 026). The daemon uses this
        to INSERT in one go.
        """
        return {
            "wallet_address": wallet_address,
            "classified_at": datetime.now(tz=timezone.utc),
            "primary_strategy": prediction.primary_strategy,
            "confidence": round(prediction.confidence, 4),
            "strategy_probs": json.dumps(prediction.strategy_probs),
            "model_version": prediction.model_version,
            "asof_ts": asof_ts,
            "drift_js_divergence": (
                round(float(drift_js_divergence), 6)
                if drift_js_divergence is not None
                else None
            ),
            "drift_detected": bool(drift_detected),
        }
