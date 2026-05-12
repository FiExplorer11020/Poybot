"""UnsupervisedStrategyExplorer — K-means + DBSCAN candidate discovery.

Round 8 (The Lens) — § 3.4 of the spec.

Purpose: discover strategies we forgot to include in the 9-class
taxonomy. This module is **NOT used in production decision flow**.
It's a research / operator tool: clusters that are SIZABLE (≥ N
wallets) yet POORLY-MATCHED by the supervised classifier (mean
predicted-class confidence < threshold) are candidate new classes
worth investigating.

Example pattern the spec calls out: K-means surfaces a cluster of 50
wallets with high social signal density but also `tweet_to_trade_lag <
0` (tweets AFTER the trade). That's a "shill / tout" class we missed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger


@dataclass
class ClusterCandidate:
    """One candidate cluster surfaced for operator review."""

    cluster_id: int
    size: int
    mean_supervised_confidence: float
    sample_wallet_indices: list[int]  # indices into the original X matrix
    centroid: np.ndarray | None  # only K-means clusters carry a centroid


class UnsupervisedStrategyExplorer:
    """K-means + DBSCAN on the feature matrix produced by
    :class:`src.strategy_classifier.features.LeaderFeatureExtractor`.

    Lifecycle:

        explorer = UnsupervisedStrategyExplorer(n_clusters_kmeans=10)
        explorer.fit(X)
        # explorer.cluster_labels: np.ndarray of K-means assignments
        # explorer.dbscan_labels:  np.ndarray of DBSCAN assignments (-1 = noise)
        candidates = explorer.surface_candidate_clusters(
            supervised_predictions=clf.predict_proba(X),
            min_size=20,
            max_supervised_confidence=0.5,
        )

    The sklearn import is deferred to fit() / predict() so the module
    can be imported in environments where sklearn isn't fully available
    (it almost always is, since LightGBM transitively requires it).
    """

    def __init__(
        self,
        n_clusters_kmeans: int = 12,
        dbscan_eps: float = 1.5,
        dbscan_min_samples: int = 10,
        random_state: int = 42,
    ) -> None:
        self.n_clusters_kmeans = int(n_clusters_kmeans)
        self.dbscan_eps = float(dbscan_eps)
        self.dbscan_min_samples = int(dbscan_min_samples)
        self.random_state = int(random_state)

        self.cluster_labels: np.ndarray | None = None
        self.dbscan_labels: np.ndarray | None = None
        self._centroids: np.ndarray | None = None
        self._fitted_X: np.ndarray | None = None

    def fit(self, X: np.ndarray) -> "UnsupervisedStrategyExplorer":
        """Fit K-means + DBSCAN on the feature matrix.

        ``X`` should have shape ``(n_samples, n_features)``. NaNs are
        imputed to column-medians before clustering — K-means and DBSCAN
        don't tolerate missing values (unlike LightGBM).
        """
        from sklearn.cluster import DBSCAN, KMeans  # type: ignore[import]
        from sklearn.preprocessing import StandardScaler  # type: ignore[import]

        X_arr = np.asarray(X, dtype=float)
        if X_arr.ndim != 2:
            raise ValueError(f"X must be 2D, got shape {X_arr.shape!r}")
        if X_arr.shape[0] == 0:
            self.cluster_labels = np.array([], dtype=int)
            self.dbscan_labels = np.array([], dtype=int)
            self._centroids = None
            self._fitted_X = X_arr
            return self

        # Impute NaNs to per-column medians. nanmedian over an all-NaN
        # column gives NaN; fall back to 0 in that case.
        col_medians = np.nanmedian(X_arr, axis=0)
        col_medians = np.where(np.isnan(col_medians), 0.0, col_medians)
        X_filled = np.where(np.isnan(X_arr), col_medians, X_arr)

        # Standardise — K-means is scale-sensitive, and our features
        # span six orders of magnitude (seconds in holding period vs
        # ratios in [0,1]).
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_filled)

        # K-means
        # n_clusters must be <= n_samples. sklearn raises otherwise.
        k = min(self.n_clusters_kmeans, X_arr.shape[0])
        if k >= 2:
            km = KMeans(
                n_clusters=k,
                random_state=self.random_state,
                n_init=10,
            )
            self.cluster_labels = km.fit_predict(X_scaled)
            self._centroids = km.cluster_centers_
        else:
            # Pathological — single sample. Everyone gets cluster 0.
            self.cluster_labels = np.zeros(X_arr.shape[0], dtype=int)
            self._centroids = None

        # DBSCAN — density-based; emits -1 for noise points.
        db = DBSCAN(eps=self.dbscan_eps, min_samples=self.dbscan_min_samples)
        self.dbscan_labels = db.fit_predict(X_scaled)

        self._fitted_X = X_arr
        logger.info(
            f"UnsupervisedStrategyExplorer fitted: n={X_arr.shape[0]} "
            f"kmeans_clusters={k} dbscan_clusters="
            f"{len(set(self.dbscan_labels)) - (1 if -1 in self.dbscan_labels else 0)} "
            f"dbscan_noise={int(np.sum(self.dbscan_labels == -1))}"
        )
        return self

    def surface_candidate_clusters(
        self,
        supervised_predictions: np.ndarray,
        min_size: int = 20,
        max_supervised_confidence: float = 0.5,
    ) -> list[ClusterCandidate]:
        """Return K-means clusters that are SIZABLE AND poorly-matched
        by the supervised classifier.

        Args:
            supervised_predictions: ``(n, k)`` probability matrix from
                :meth:`src.strategy_classifier.model.StrategyClassifier.predict_proba`.
            min_size: minimum cluster size to surface.
            max_supervised_confidence: maximum mean per-cluster confidence
                (max-of-row over supervised probs). Clusters where the
                supervised model is on average MORE confident than this
                are already well-covered by the existing taxonomy.

        Returns:
            List of :class:`ClusterCandidate`, sorted by size DESC.
        """
        if self.cluster_labels is None:
            raise RuntimeError(
                "Call .fit(X) before surface_candidate_clusters()."
            )

        preds = np.asarray(supervised_predictions, dtype=float)
        if preds.shape[0] != self.cluster_labels.shape[0]:
            raise ValueError(
                f"supervised_predictions has {preds.shape[0]} rows but "
                f"explorer was fit on {self.cluster_labels.shape[0]} rows."
            )
        # Per-row max probability (the "confidence" of the supervised model).
        row_max = preds.max(axis=1)

        candidates: list[ClusterCandidate] = []
        unique_clusters = sorted(set(self.cluster_labels.tolist()))
        for cid in unique_clusters:
            mask = self.cluster_labels == cid
            size = int(mask.sum())
            if size < int(min_size):
                continue
            mean_conf = float(row_max[mask].mean())
            if mean_conf > float(max_supervised_confidence):
                continue
            indices = np.where(mask)[0].tolist()
            sample = indices[: min(10, len(indices))]
            centroid = (
                self._centroids[cid] if self._centroids is not None and cid < len(self._centroids) else None
            )
            candidates.append(
                ClusterCandidate(
                    cluster_id=int(cid),
                    size=size,
                    mean_supervised_confidence=round(mean_conf, 4),
                    sample_wallet_indices=sample,
                    centroid=centroid,
                )
            )
        # Largest cluster first — operator scans top-down.
        candidates.sort(key=lambda c: c.size, reverse=True)
        return candidates

    def n_unmatched_clusters(
        self,
        supervised_predictions: np.ndarray,
        min_size: int = 20,
        max_supervised_confidence: float = 0.5,
    ) -> int:
        """Count of clusters meeting the candidate criteria. Used by the
        daemon to publish ``polybot_unsupervised_clusters_unmatched``.
        """
        return len(
            self.surface_candidate_clusters(
                supervised_predictions=supervised_predictions,
                min_size=min_size,
                max_supervised_confidence=max_supervised_confidence,
            )
        )
