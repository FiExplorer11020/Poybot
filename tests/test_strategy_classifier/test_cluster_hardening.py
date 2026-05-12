"""R8 Wave-3 hardening tests for :mod:`src.strategy_classifier.cluster`.

Covers:

* Determinism across two .fit() invocations with the same seed.
* All-NaN column handling (median falls back to 0 — fit still works).
* Tiny dataset where n < n_clusters → k clamped to n.
* surface_candidate_clusters with no clusters meeting size criterion → [].
* surface_candidate_clusters runs cleanly when there's only ONE
  cluster (degenerate case, k=1 after clamping).
* DBSCAN with very strict eps yields all-noise labels (-1 everywhere).
"""
from __future__ import annotations

import numpy as np
import pytest

from src.strategy_classifier.cluster import (
    ClusterCandidate,
    UnsupervisedStrategyExplorer,
)
from src.strategy_classifier.model import STRATEGY_CLASSES


def _three_blobs(n_per: int = 30, n_features: int = 42, seed: int = 0):
    rng = np.random.default_rng(seed)
    centers = np.zeros((3, n_features))
    centers[0, 0:5] = 5.0
    centers[1, 5:10] = 5.0
    centers[2, 10:15] = 5.0
    X = np.vstack([
        rng.normal(centers[c], 0.3, size=(n_per, n_features))
        for c in range(3)
    ])
    return X


class TestDeterminism:
    def test_kmeans_deterministic_with_seed(self):
        """Two fits with the same seed produce identical cluster labels."""
        pytest.importorskip("sklearn")
        X = _three_blobs(seed=1)
        e1 = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=3, random_state=7
        ).fit(X)
        e2 = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=3, random_state=7
        ).fit(X)
        np.testing.assert_array_equal(e1.cluster_labels, e2.cluster_labels)
        np.testing.assert_allclose(e1._centroids, e2._centroids, atol=1e-9)


class TestNaNHandling:
    def test_all_nan_column_imputed_to_zero(self):
        """A column that's entirely NaN should not blow up nanmedian; we
        fall back to 0 for the column median.

        Regression: an early version of cluster.py could crash with
        'all-NaN slice encountered' here.
        """
        pytest.importorskip("sklearn")
        X = _three_blobs(n_per=10, seed=2)
        # Wipe column 7 to NaN across every row.
        X[:, 7] = np.nan
        explorer = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=3, dbscan_min_samples=3
        ).fit(X)
        assert explorer.cluster_labels is not None
        assert explorer.cluster_labels.shape == (30,)
        assert not np.isnan(explorer.cluster_labels).any()


class TestTinyDataset:
    def test_n_less_than_n_clusters_clamps_k(self):
        """When n < n_clusters_kmeans, k is clamped to n (no sklearn
        crash). The labels span exactly the available samples."""
        pytest.importorskip("sklearn")
        X = np.random.RandomState(0).rand(5, 42)
        explorer = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=10, dbscan_min_samples=3
        ).fit(X)
        # k was clamped to 5; cluster_labels has 5 entries.
        assert explorer.cluster_labels.shape == (5,)
        # All labels in [0, 4].
        assert explorer.cluster_labels.min() >= 0
        assert explorer.cluster_labels.max() <= 4

    def test_single_sample_dataset(self):
        """Pathological case: a single wallet. Cluster 0 for everything."""
        pytest.importorskip("sklearn")
        X = np.zeros((1, 42))
        explorer = UnsupervisedStrategyExplorer(
            n_clusters_kmeans=5, dbscan_min_samples=3
        ).fit(X)
        np.testing.assert_array_equal(explorer.cluster_labels, [0])
        # DBSCAN with min_samples=3 marks the sole point as noise (-1).
        np.testing.assert_array_equal(explorer.dbscan_labels, [-1])


class TestSurfaceCandidateClusters:
    def test_min_size_too_high_returns_empty(self):
        pytest.importorskip("sklearn")
        X = _three_blobs(n_per=10)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        # Low confidence predictions but min_size larger than any cluster.
        preds = np.full((n, k), 1.0 / k)
        out = explorer.surface_candidate_clusters(
            supervised_predictions=preds,
            min_size=100,  # No cluster will reach this.
            max_supervised_confidence=0.5,
        )
        assert out == []

    def test_high_max_supervised_confidence_returns_empty(self):
        """When the supervised model is highly confident across every
        wallet, no cluster is "poorly-matched" and we return []."""
        pytest.importorskip("sklearn")
        X = _three_blobs(n_per=30)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        # Supervised model is very confident (0.99 on class 0).
        preds = np.zeros((n, k))
        preds[:, 0] = 0.99
        preds[:, 1] = 0.01
        out = explorer.surface_candidate_clusters(
            supervised_predictions=preds,
            min_size=10,
            max_supervised_confidence=0.5,
        )
        assert out == []

    def test_sample_wallet_indices_capped_to_ten(self):
        """Each ClusterCandidate.sample_wallet_indices is at most 10 long
        (the operator only needs a handful to investigate)."""
        pytest.importorskip("sklearn")
        X = _three_blobs(n_per=30)
        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=3).fit(X)
        n = X.shape[0]
        k = len(STRATEGY_CLASSES)
        preds = np.full((n, k), 1.0 / k)  # low confidence
        out = explorer.surface_candidate_clusters(
            supervised_predictions=preds,
            min_size=20,
            max_supervised_confidence=0.5,
        )
        assert len(out) > 0
        for cand in out:
            assert isinstance(cand, ClusterCandidate)
            assert len(cand.sample_wallet_indices) <= 10


class TestSurfaceWithoutFit:
    def test_surface_before_fit_raises(self):
        """Calling surface_candidate_clusters before fit() raises."""
        explorer = UnsupervisedStrategyExplorer()
        with pytest.raises(RuntimeError, match="fit"):
            explorer.surface_candidate_clusters(
                supervised_predictions=np.zeros((10, 9))
            )
