"""
Round 9 (The Web) — Follower-pool dynamics package.

This package owns the Round 9 follower-pool model:

    * ``kalman.py``           Per-(leader, pool_class) state-space model
                              on follower-deployed volume.
    * ``volume_predictor.py`` Headline API: given a leader trade,
                              predict next-30-min follower-pool volume.
    * ``drift.py``            HawkesCouplingDriftDetector — flags leaders
                              whose BIC test starts rejecting after
                              previously accepting.
    * ``daemon.py``           Nightly entrypoint that runs the
                              MultivariateHawkesFitter for the top-N
                              leaders.

The multivariate Hawkes fitter itself lives in
``src.graph.hawkes_multivariate`` (so it shares a directory with the
existing R5 bivariate fitter), but the Kalman state-space and the
volume-prediction logic that consumes it live here because they're
not "graph" code — they're forecasting code.

See ``docs/ROUND_9_MULTIVARIATE_HAWKES.md`` for the full spec.
"""

from __future__ import annotations

from src.follower_volume.kalman import FollowerPoolKalman, KalmanForecast
from src.follower_volume.volume_predictor import FollowerVolumePredictor
from src.follower_volume.drift import HawkesCouplingDriftDetector

__all__ = [
    "FollowerPoolKalman",
    "KalmanForecast",
    "FollowerVolumePredictor",
    "HawkesCouplingDriftDetector",
]
