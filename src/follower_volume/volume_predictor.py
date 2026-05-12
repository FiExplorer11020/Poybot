"""
FollowerVolumePredictor — Round 9 (The Web) headline API.

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.3.

Given a leader trade event, returns the expected follower-pool volume
that will be deployed in the next 30 min, broken down by strategy class.

Combines three signals:

    1. Multivariate Hawkes intensity at time t (per pool)
       — from src.graph.hawkes_multivariate via cached fits in
       multivariate_hawkes_fits.
    2. Kalman state at time t per pool class (volume scale)
       — from src.follower_volume.kalman with state hydrated from
       follower_pool_state.
    3. Strategy classifier prior on which pools will react
       — from src.strategy_classifier (R8) — graceful fallback to
       single-pool when R8 is unavailable.

Output dict shape (spec § 3.3):

    {
        'total_volume_usdc': float,
        'ci_low': float,
        'ci_high': float,
        'by_pool': {pool_class: float, ...},   # sums to total
        'time_distribution': {                  # CDF buckets in [0,1]
            '0-5min':   float,
            '5-15min':  float,
            '15-30min': float,
            '30-60min': float,
        },
        'confidence': float,                    # in [0,1]
    }
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import numpy as np
from loguru import logger

from src.config import settings
from src.follower_volume.kalman import FollowerPoolKalman, KalmanForecast


# ---------------------------------------------------------------------------
# Time-distribution CDF
# ---------------------------------------------------------------------------
# Derived from an exponential kernel with the spec § 3.3 time buckets
# (0-5, 5-15, 15-30, 30-60 min) and the half-life carried by the Kalman
# state. We compute the integrated kernel between bucket edges and
# normalise so the 4 buckets sum to 1.0.
TIME_BUCKETS_S = [
    ("0-5min", 0.0, 5 * 60.0),
    ("5-15min", 5 * 60.0, 15 * 60.0),
    ("15-30min", 15 * 60.0, 30 * 60.0),
    ("30-60min", 30 * 60.0, 60 * 60.0),
]


def _time_distribution(half_life_s: float) -> dict[str, float]:
    """Bucketed CDF of a decay-rate kernel.

    Args:
        half_life_s: half-life of the exponential follow-response.

    Returns:
        dict[bucket_label, prob in [0,1]] summing to 1.0.
    """
    decay = np.log(2.0) / max(float(half_life_s), 1.0)
    # Probability mass inside [a, b] for an exp(decay) kernel on [0, ∞)
    # is exp(-decay*a) - exp(-decay*b). We renormalise to sum to 1 over
    # the four operator-facing buckets so the dashboard shows a clean
    # CDF, not a tail that flies off to 60min+.
    raw = {}
    for label, a, b in TIME_BUCKETS_S:
        raw[label] = float(
            np.exp(-decay * a) - np.exp(-decay * b)
        )
    total = sum(raw.values())
    if total <= 0:
        # Degenerate kernel — uniform fallback.
        return {label: 1.0 / len(TIME_BUCKETS_S) for label, _, _ in TIME_BUCKETS_S}
    return {k: v / total for k, v in raw.items()}


class FollowerVolumePredictor:
    """Headline predictor consumed by the decision_router.

    The class is **stateless across calls**: each forecast pulls fresh
    Kalman state from memory / DB and combines it with the latest
    Hawkes fit. State management is in
    :class:`src.follower_volume.kalman.FollowerPoolKalman`.

    Hawkes integration is **read-only** from the predictor's
    perspective: the nightly batch (src.follower_volume.daemon) writes
    to multivariate_hawkes_fits, and the predictor reads the latest
    converged fit per leader.

    Strategy prior is optional. If a leader has no
    classification_json.strategy_fingerprint (R8 hasn't classified them
    yet), the predictor collapses to a SINGLE pool ("all_followers")
    and runs a single Kalman filter against it — equivalent to the R5
    bivariate Hawkes shape with extra plumbing, per the spec § 6
    "graceful degradation" requirement.
    """

    DEFAULT_POOL_CLASS = "all_followers"

    def __init__(
        self,
        pool_classes: Optional[list[str]] = None,
        kalman_factory: Any = None,
    ) -> None:
        # pool_classes default to the R8 strategy classes (minus excluded
        # ones like structural_bot) but the caller can override for
        # testing or research.
        self.pool_classes: list[str] = list(
            pool_classes
            if pool_classes is not None
            else self._default_pool_classes()
        )
        # `kalman_factory(leader, pool_class) -> FollowerPoolKalman`. The
        # default produces a fresh filter per (leader, pool); tests can
        # inject a factory that returns pre-warmed filters.
        self._kalman_factory = kalman_factory or self._make_kalman

    @staticmethod
    def _make_kalman(leader_wallet: str, pool_class: str) -> FollowerPoolKalman:
        return FollowerPoolKalman(
            leader_wallet=leader_wallet, pool_class=pool_class
        )

    @staticmethod
    def _default_pool_classes() -> list[str]:
        """Default pool classes from R8 minus structural_bot / arb_*
        (those pools don't follow — they trade independently)."""
        try:
            from src.strategy_classifier.model import STRATEGY_CLASSES

            return [
                s
                for s in STRATEGY_CLASSES
                if s not in {"structural_bot", "market_maker", "arb_2way", "arb_3way"}
            ]
        except Exception:  # pragma: no cover — R8 not importable
            return [
                "directional",
                "momentum",
                "contrarian",
                "social_driven",
                "info_leak",
            ]

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def forecast(
        self,
        leader_wallet: str,
        trade_size_usdc: float = 0.0,
        asof_ts: Optional[datetime] = None,
        strategy_prior: Optional[Mapping[str, float]] = None,
        hawkes_fit: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Predict next-window follower-pool volume.

        Args:
            leader_wallet: the wallet whose trade triggers the forecast.
            trade_size_usdc: leader's trade size; modulates the
                forecast via a sqrt-scaling factor (larger leader trades
                excite proportionally more follower volume).
            asof_ts: optional timestamp; defaults to now (UTC).
            strategy_prior: optional dict[pool_class, weight_in_(0,1)].
                When provided, each pool's contribution is scaled by
                its weight (so a momentum-heavy leader pulls more
                momentum-pool volume). Weights are renormalised
                internally; missing pools get 0.
            hawkes_fit: optional pre-loaded fit. If None, we fall back
                to per-pool Kalman forecasts without Hawkes modulation
                (pool-prior-only). In production, the daemon writes
                fits and the predictor reads them.

        Returns:
            Spec § 3.3 dict.
        """
        asof = asof_ts or datetime.now(tz=timezone.utc)

        # Determine which pools to forecast. If strategy_prior provided
        # and non-empty, use its keys. Else fall back to the default
        # pool list. If the default list itself is empty (very stripped
        # env), collapse to the single all_followers pool.
        if strategy_prior:
            pool_keys = [
                k for k, v in strategy_prior.items() if v and v > 0
            ]
            if not pool_keys:
                pool_keys = list(self.pool_classes) or [self.DEFAULT_POOL_CLASS]
        else:
            pool_keys = list(self.pool_classes) or [self.DEFAULT_POOL_CLASS]

        # Strategy weights for blending. Defaults: equal across the
        # remaining pools. If strategy_prior is provided, we honour the
        # caller's choice and renormalise. If R8 hasn't classified yet,
        # callers pass None and we fall back to equal weighting.
        weights: dict[str, float]
        if strategy_prior:
            total = sum(v for v in strategy_prior.values() if v and v > 0)
            if total > 0:
                weights = {
                    k: float(strategy_prior.get(k, 0.0)) / total
                    for k in pool_keys
                }
            else:
                weights = {k: 1.0 / len(pool_keys) for k in pool_keys}
        else:
            weights = {k: 1.0 / len(pool_keys) for k in pool_keys}

        # Compute per-pool forecasts.
        by_pool: dict[str, float] = {}
        cis: list[tuple[float, float]] = []
        # Wave-3 fix: pick the DOMINANT-pool half-life (highest weighted
        # contribution) for the time distribution, rather than seed with
        # a default and ratchet UP via max() (which silently masks fast-
        # decay pools).
        dominant_half_life: float = 0.0
        dominant_contribution: float = -1.0
        confidence_terms: list[float] = []

        # Hawkes modulation: if a fit is provided, multiply each pool's
        # Kalman volume by (1 + α_{pool, leader} / max(μ_pool, ε)). This
        # is the "intensity boost from leader excitation" — pools the
        # leader strongly excites get scaled up.
        hawkes_mods = self._hawkes_modulators(hawkes_fit, pool_keys)

        # Trade-size modulation: sqrt scaling caps the influence of one
        # outlier huge leader trade. The 1000-USDC reference is a
        # calibration point; operator can tune via config.
        size_factor = float(
            np.sqrt(max(trade_size_usdc, 0.0) / 1000.0 + 1.0)
        )

        for pool in pool_keys:
            kf = self._kalman_factory(leader_wallet, pool)
            # Best-effort load. On cold start the default prior is used.
            try:
                await kf.load_state()
            except Exception:
                pass
            fc: KalmanForecast = kf.forecast(asof_ts=asof)

            hawkes_mod = hawkes_mods.get(pool, 1.0)
            pool_volume = (
                fc.expected_volume_usdc
                * weights[pool]
                * hawkes_mod
                * size_factor
            )
            by_pool[pool] = float(max(0.0, pool_volume))

            cis.append(
                (
                    fc.ci_low * weights[pool] * hawkes_mod * size_factor,
                    fc.ci_high * weights[pool] * hawkes_mod * size_factor,
                )
            )

            # Confidence proxy: tighter CI / larger E → higher
            # confidence. We use 1 - sigma/(mean+eps) and clamp to [0,1].
            if fc.expected_volume_usdc > 1.0:
                sigma = (fc.ci_high - fc.ci_low) / (2 * 1.96)
                conf = 1.0 - min(1.0, sigma / max(fc.expected_volume_usdc, 1.0))
            else:
                conf = 0.0
            confidence_terms.append(max(0.0, min(1.0, conf)))

            # Use the dominant-pool half-life for the time distribution —
            # the pool with the largest weighted volume contribution
            # drives the CDF shape (an info_leak pool with a 30-s
            # half-life should not be masked by a directional pool's
            # 1-h half-life if the leader excites info_leak much more
            # strongly).
            contribution = float(weights[pool]) * float(by_pool[pool])
            if contribution > dominant_contribution and weights[pool] > 0.0:
                dominant_contribution = contribution
                dominant_half_life = float(fc.half_life_s)

        total = float(sum(by_pool.values()))
        ci_low = float(sum(c[0] for c in cis))
        ci_high = float(sum(c[1] for c in cis))
        # Fallback: if no pool fired, default to a flat 30-min kernel.
        half_life_for_dist = (
            dominant_half_life if dominant_half_life > 0.0 else 1800.0
        )
        time_dist = _time_distribution(half_life_for_dist)
        confidence = float(np.mean(confidence_terms)) if confidence_terms else 0.0

        return {
            "total_volume_usdc": total,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "by_pool": by_pool,
            "time_distribution": time_dist,
            "confidence": confidence,
            "asof_ts": asof.isoformat(),
        }

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _hawkes_modulators(
        hawkes_fit: Optional[Mapping[str, Any]],
        pool_keys: Iterable[str],
    ) -> dict[str, float]:
        """Map each pool to an excitation modulator (>= 1.0) from the
        Hawkes fit.

        The modulator is 1 + α_{pool→0_leader} / max(μ_pool, ε). Pools
        with no leader-coupling (or no fit) get modulator = 1.0.

        Args:
            hawkes_fit: per-spec § 3.1 result dict. Process 0 is the
                leader, processes 1..K are the pools in
                ``process_labels`` order.
            pool_keys: pool labels to look up.

        Returns:
            dict[pool_label, modulator] with modulator >= 1.
        """
        mods: dict[str, float] = {k: 1.0 for k in pool_keys}
        if not hawkes_fit:
            return mods
        try:
            alpha = hawkes_fit.get("alpha_matrix", {}) or {}
            mu = hawkes_fit.get("mu_vector", {}) or {}
            labels = list(hawkes_fit.get("process_labels", []) or [])
            label_to_idx = {lbl: i for i, lbl in enumerate(labels)}
            for pool in pool_keys:
                i = label_to_idx.get(pool)
                if i is None or i == 0:
                    continue
                # α value comes back keyed by (i, j) tuples or by strings
                # like "(i, j)" depending on JSON round-trip.
                alpha_ij = (
                    alpha.get((i, 0))
                    if (i, 0) in alpha
                    else alpha.get(f"({i}, 0)", 0.0)
                )
                mu_i = float(mu.get(i, mu.get(str(i), 0.0)) or 0.0)
                if mu_i <= 0.0:
                    continue
                mod = 1.0 + max(0.0, float(alpha_ij or 0.0)) / max(mu_i, 1e-9)
                # Hard-cap at 10× so a runaway α doesn't blow up the
                # forecast. The dashboard's α-distribution metric will
                # surface outliers.
                mods[pool] = float(min(10.0, mod))
        except Exception as exc:  # pragma: no cover
            logger.debug(f"FollowerVolumePredictor: hawkes parse error: {exc}")
        return mods


__all__ = ["FollowerVolumePredictor"]
