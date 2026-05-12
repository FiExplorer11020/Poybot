"""Round 8 — The Lens. Per-leader strategy fingerprinting.

This package classifies every tier-0 / tier-1 wallet in the universe
into one of 9 strategy classes (directional, momentum, contrarian,
arb_2way, arb_3way, market_maker, structural_bot, info_leak,
social_driven) with a calibrated probability vector.

Public exports — anything not listed here is internal:

* :class:`StrategyClass`            — enum of the 9 supported classes.
* :data:`STRATEGY_CLASSES`          — canonical ordered tuple. Index in
                                       this tuple matches LightGBM's
                                       class index after sorted-label
                                       fit.
* :data:`STRATEGY_WEIGHTS`          — default {strategy -> {follow, fade,
                                       skip}} multipliers used by
                                       :mod:`src.engine.confidence_engine`
                                       when the runtime flag
                                       ``strategy_conditional_confidence_enabled``
                                       is True. Hyperparameters, not
                                       learned — operator-tunable.
* :class:`LeaderFeatureExtractor`   — the ~42-dim feature vector.
* :class:`StrategyClassifier`       — LightGBM-9-class + isotonic
                                       calibration wrapper. Falls back
                                       to a uniform-prior dummy when
                                       LightGBM is not installed (tests).
* :class:`UnsupervisedStrategyExplorer` — K-means + DBSCAN cluster
                                       discovery for surfacing new
                                       strategy classes.
* :class:`StrategyDriftDetector`    — JS-divergence drift watcher.

The daemon entrypoint is ``python -m src.strategy_classifier`` (see
:mod:`src.strategy_classifier.daemon` and :mod:`src.strategy_classifier.__main__`).
"""
from __future__ import annotations

from src.strategy_classifier.cluster import UnsupervisedStrategyExplorer
from src.strategy_classifier.drift import StrategyDriftDetector
from src.strategy_classifier.features import LeaderFeatureExtractor
from src.strategy_classifier.model import (
    STRATEGY_CLASSES,
    STRATEGY_WEIGHTS,
    StrategyClass,
    StrategyClassifier,
)

__all__ = [
    "LeaderFeatureExtractor",
    "STRATEGY_CLASSES",
    "STRATEGY_WEIGHTS",
    "StrategyClass",
    "StrategyClassifier",
    "StrategyDriftDetector",
    "UnsupervisedStrategyExplorer",
]
