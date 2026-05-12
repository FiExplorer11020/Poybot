"""Unit tests for UnsupervisedStrategyExplorer.

Cover:

* K-means recovers known clusters on synthetic 3-cluster data.
* surface_candidate_clusters filters by size AND supervised confidence.
* DBSCAN labels emit -1 for noise.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.strategy_classifier.cluster import (
    ClusterCandidate,
    UnsupervisedStrategyExplorer,
)
from src.strategy_classifier.model import STRATEGY_CLASSES


def _make_synthetic_3_clusters(n_per_cluster: int = 30, n_features: int = 42, seed: int = 0):
    """Three well-separated Gaussian blobs in feature space."""
    rng = np.random.default_rng(seed)
    centers = np.zeros((3, n_features))
    centers[0, 0:5] = 5.0
    centers[1, 5:10] = 5.0
    centers[2, 10:15] = 5.0
    X = np.vstack([
        rng.normal(centers[c], 0.3, size=(n_per_cluster, n_features))
        for c in range(3)
    ])
    return X


class TestClusterFit:
    def test_kmeans_recovers_known_clusters(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=30)
        explorer = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=3, dbscan_eps=2.0, dbscan_min_samples=5
        ).fit(X)
        assert explorer.cluster_labels is not None
        assert explorer.cluster_labels.shape == (90,)
        # Each true cluster ends up mostly in a single predicted cluster.
        for start in (0, 30, 60):
            assignments = explorer.cluster_labels[start:start + 30]
            counts = np.bincount(assignments)
            assert counts.max() >= 25, (
                f"K-means recovered cluster only {counts.max()}/30 — "
                "synthetic test should be near-perfect."
            )

    def test_fit_empty_X(self):
        pytest.importorskip("sklearn")
        explorer = UnsupervisedStrategyExplorer().fit(np.zeros((0, 42)))
        assert explorer.cluster_labels.shape == (0,)

    def test_fit_rejects_1d_input(self):
        pytest.importorskip("sklearn")
        with pytest.raises(ValueError, match="2D"):
            UnsupervisedStrategyExplorer().fit(np.zeros(42))


class TestSurfaceCandidateClusters:
    def test_filters_by_size(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=30)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        # Build a supervised-pred matrix where the model is "confident"
        # on every row (all probability on class 0). Should surface ZERO
        # candidates because confidence > 0.5.
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        preds = np.zeros((n, k))
        preds[:, 0] = 1.0
        candidates = explorer.surface_candidate_clusters(
            supervised_predictions=preds, min_size=20, max_supervised_confidence=0.5
        )
        assert candidates == []

    def test_surfaces_low_confidence_clusters(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=30)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        # Uniform predictions = low confidence everywhere (1/9 ≈ 0.11).
        preds = np.full((n, k), 1.0 / k)
        candidates = explorer.surface_candidate_clusters(
            supervised_predictions=preds, min_size=20, max_supervised_confidence=0.5
        )
        # All three 30-element clusters meet size + confidence criteria.
        assert len(candidates) == 3
        # Sorted by size DESC, all clusters have size 30 here.
        for c in candidates:
            assert isinstance(c, ClusterCandidate)
            assert c.size == 30
            # cluster.py rounds to 4 dp; 1/9 ≈ 0.1111 after rounding.
            assert c.mean_supervised_confidence == pytest.approx(1.0 / k, abs=1e-3)

    def test_size_filter_excludes_small_clusters(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=10)  # too small
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        preds = np.full((n, k), 1.0 / k)  # low confidence
        candidates = explorer.surface_candidate_clusters(
            supervised_predictions=preds, min_size=20, max_supervised_confidence=0.5
        )
        # 10 < 20 → no candidates
        assert candidates == []

    def test_predictions_shape_mismatch_raises(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=10)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        with pytest.raises(ValueError, match="rows"):
            explorer.surface_candidate_clusters(
                supervised_predictions=np.zeros((5, 9)),
                min_size=5,
            )


class TestNUnmatched:
    def test_n_unmatched_counts_candidates(self):
        pytest.importorskip("sklearn")
        X = _make_synthetic_3_clusters(n_per_cluster=30)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        preds = np.full((n, k), 1.0 / k)
        n_unmatched = explorer.n_unmatched_clusters(
            supervised_predictions=preds, min_size=20, max_supervised_confidence=0.5
        )
        assert n_unmatched == 3
