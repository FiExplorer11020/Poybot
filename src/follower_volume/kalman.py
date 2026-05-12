"""
FollowerPoolKalman — Round 9 (The Web).

Per-(leader, pool_class) state-space model on follower-deployed volume.

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.2.

The multivariate Hawkes fit (src/graph/hawkes_multivariate.py) tells us
**whether** a leader excites a follower pool. It does NOT tell us **how
much volume** that pool will deploy. The Kalman state-space model fills
that gap.

State vector (3D):

    x = [pool_size_usdc,        # capital available in the pool
         recent_response_pct,   # what fraction of pool reacted to last
                                # leader trade (typically 0.01..0.50)
         decay_rate]            # how fast the response decays in t_seconds

Dynamics (linear Gaussian):

    x_{t+1} = F · x_t + w_t        (state evolution, w_t ~ N(0, Q))
    y_t    = H · x_t + v_t        (observation,   v_t ~ N(0, R))

with

    F = [[1, 0, 0],          # pool_size: slow random walk
         [0, 0.95, 0],       # response_pct: AR(1)-ish with mean reversion
         [0, 0, 0.99]]       # decay_rate: nearly-constant

    H = [pool_size_usdc · recent_response_pct]   # implicit nonlinear product

Because the observation is the PRODUCT of two state components, this is
strictly an Extended Kalman Filter (EKF): we linearise H around the
current state estimate. The Jacobian is

    H_jac = [recent_response_pct, pool_size_usdc, 0]

so a 1-USDC change in pool size is one full unit of "% response" away
from a 1-USDC change in volume.

The implementation uses only ``numpy`` — no ``filterpy``, no
``pomegranate``. The 3-state size makes hand-rolled EKF math trivial and
deps-free.

Persistence:
    * ``follower_pool_state``         current state (UPSERT on every update)
    * ``follower_pool_state_history`` append-only snapshot (INSERT on every
                                      update; see migration 029)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
from loguru import logger

from src.config import settings
from src.database.connection import get_db


# ---------------------------------------------------------------------------
# Defaults — exposed at module level so tests can override.
# ---------------------------------------------------------------------------

# State evolution matrix F: slow decay on every component.
DEFAULT_F = np.array(
    [
        [1.00, 0.00, 0.00],
        [0.00, 0.95, 0.00],
        [0.00, 0.00, 0.99],
    ],
    dtype=float,
)

# Process noise covariance Q. Diagonal because the three state
# components are nominally independent. The pool-size variance is the
# largest contribution by far (mood swings in the follower pool's
# capital have the biggest impact on the forecast). recent_response_pct
# is bounded in (0, 1) so its variance is tight.
DEFAULT_Q = np.diag([1.0e4, 1.0e-3, 1.0e-6])

# Observation noise variance R. The default is set so the Kalman gain
# strikes a reasonable balance on a 30-minute window: small enough that
# the filter actually updates on each observation, large enough that
# pure noise doesn't whip the state.
DEFAULT_R = 5.0e4

# Default initial state. pool_size_usdc = 0 means "no information"; the
# first observation will lift it. The 0.10 response_pct prior says "10%
# of the pool reacts to each leader trade by default" — a calibration
# point we measure against the innovation magnitude.
DEFAULT_X0 = np.array([0.0, 0.10, 1.0 / 1800.0], dtype=float)

# Initial covariance: diffuse prior on pool_size (let the first
# observation dominate), tighter on response_pct (we have a sensible
# prior).
DEFAULT_P0 = np.diag([1.0e8, 1.0e-2, 1.0e-8])


# ---------------------------------------------------------------------------
# Forecast dataclass
# ---------------------------------------------------------------------------


@dataclass
class KalmanForecast:
    """Result of a forecast call. Mirrors spec § 3.2's prediction
    interface."""

    expected_volume_usdc: float
    ci_low: float
    ci_high: float
    time_to_peak_s: float
    half_life_s: float
    state_at_forecast: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class FollowerPoolKalman:
    """Extended Kalman filter on follower-pool deployed volume.

    Lifecycle:

        kf = FollowerPoolKalman(
            leader_wallet="0xLEADER",
            pool_class="directional",
        )
        await kf.load_state()              # hydrate from DB (or use prior)
        for trade in stream:
            await kf.update(y_observed=trade.follow_volume_usdc)
        fc = kf.forecast(asof_ts=datetime.now(...))

    The class is INTENTIONALLY single-pair: one filter per (leader, pool).
    Multi-pair coordination lives one layer up in
    :class:`src.follower_volume.volume_predictor.FollowerVolumePredictor`.

    Persistence is async (via ``src.database.connection.get_db``). All
    DB writes use parameterized SQL per master CLAUDE.md § 10.
    """

    def __init__(
        self,
        leader_wallet: str,
        pool_class: str,
        F: Optional[np.ndarray] = None,
        Q: Optional[np.ndarray] = None,
        R: Optional[float] = None,
        x0: Optional[np.ndarray] = None,
        P0: Optional[np.ndarray] = None,
    ) -> None:
        self.leader_wallet = str(leader_wallet)
        self.pool_class = str(pool_class)
        self.F = np.asarray(F if F is not None else DEFAULT_F, dtype=float).copy()
        self.Q = np.asarray(Q if Q is not None else DEFAULT_Q, dtype=float).copy()
        self.R = float(R if R is not None else DEFAULT_R)
        self.x = np.asarray(x0 if x0 is not None else DEFAULT_X0, dtype=float).copy()
        self.P = np.asarray(P0 if P0 is not None else DEFAULT_P0, dtype=float).copy()
        self.n_observations: int = 0
        self.last_innovation: float = 0.0

    # ------------------------------------------------------------------ #
    # Math primitives                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _observation_jacobian(x: np.ndarray) -> np.ndarray:
        """H_jac at the current state for y = pool_size · response_pct."""
        pool_size, response_pct, _decay = float(x[0]), float(x[1]), float(x[2])
        return np.array([response_pct, pool_size, 0.0], dtype=float)

    @staticmethod
    def _observation(x: np.ndarray) -> float:
        """y_predicted = pool_size · response_pct (decay does not enter)."""
        return float(x[0] * x[1])

    def predict(self) -> tuple[np.ndarray, np.ndarray]:
        """Step state forward by one period: x → F x; P → F P F^T + Q.

        Returns the predicted (x, P) WITHOUT mutating internal state.
        Internal state is updated by ``update``.
        """
        x_pred = self.F @ self.x
        P_pred = self.F @ self.P @ self.F.T + self.Q
        return x_pred, P_pred

    async def update(
        self,
        y_observed: float,
        persist: bool = True,
        asof_ts: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Fold a new observation into the state.

        Standard EKF update:
            1. Predict:        x⁻ = F x;   P⁻ = F P Fᵀ + Q
            2. Linearise:      H = ∂y/∂x at x⁻
            3. Innovation:     ε = y_obs - h(x⁻)
            4. Innovation cov: S = H P⁻ Hᵀ + R
            5. Kalman gain:    K = P⁻ Hᵀ / S
            6. Update state:   x = x⁻ + K ε
            7. Update cov:     P = (I - K H) P⁻

        Clamps response_pct to (0, 1) and pool_size to ≥ 0 after each
        update (the state should never escape its physical bounds).

        Returns a dict {x_post, innovation, K, S} for metrics + tests.
        """
        y_observed = float(y_observed)
        x_pred, P_pred = self.predict()
        H = self._observation_jacobian(x_pred)
        y_pred = self._observation(x_pred)

        innovation = y_observed - y_pred
        S = float(H @ P_pred @ H + self.R)
        if S <= 0.0:
            S = self.R  # numerical safety
        K = (P_pred @ H) / S  # shape (3,)
        x_post = x_pred + K * innovation
        I3 = np.eye(3)
        P_post = (I3 - np.outer(K, H)) @ P_pred

        # Physical-bound clamps.
        x_post[0] = max(x_post[0], 0.0)
        x_post[1] = float(np.clip(x_post[1], 1e-4, 1.0))
        x_post[2] = max(x_post[2], 1e-6)

        self.x = x_post
        self.P = P_post
        self.n_observations += 1
        self.last_innovation = float(innovation)

        if persist:
            try:
                await self._persist(asof_ts=asof_ts)
            except Exception as exc:
                # Persist failures must NEVER take the hot path down. The
                # state is in-memory; downstream consumers can still read it.
                logger.warning(
                    f"FollowerPoolKalman: persist failed for "
                    f"leader={self.leader_wallet[:10]} pool={self.pool_class}: {exc}"
                )

        return {
            "x_post": x_post.copy(),
            "innovation": innovation,
            "K": K.copy(),
            "S": S,
        }

    def forecast(
        self,
        asof_ts: Optional[datetime] = None,
        horizon_s: Optional[float] = None,
    ) -> KalmanForecast:
        """Predict expected next-window follower volume + 95% CI.

        Spec § 3.2 ``forecast`` interface. The "next window" is the
        30-min observation window (settings.KALMAN_OBSERVATION_WINDOW_S
        by default). The forecast is:

            E[y] = pool_size · response_pct
            Var[y] ≈ H P_post Hᵀ + R

        with 95% CI = E ± 1.96 · sqrt(Var).

        ``time_to_peak_s`` and ``half_life_s`` are derived from the
        current decay_rate state component:

            time_to_peak_s = 0  (Hawkes-like immediate peak)
            half_life_s    = log(2) / decay_rate

        Args:
            asof_ts: passed through to consumers; ignored by the math.
            horizon_s: prediction window in seconds (not used directly
                — the response_pct already encodes how much fires in
                the 30-min window — but available for callers that want
                to scale).

        Returns:
            KalmanForecast.
        """
        _ = asof_ts  # signature compat
        H = self._observation_jacobian(self.x)
        y_pred = self._observation(self.x)
        var_y = float(H @ self.P @ H + self.R)
        if var_y <= 0.0:
            var_y = self.R
        sigma = float(np.sqrt(var_y))
        ci_low = max(0.0, y_pred - 1.96 * sigma)
        ci_high = y_pred + 1.96 * sigma

        decay_rate = max(float(self.x[2]), 1e-6)
        half_life_s = float(np.log(2.0) / decay_rate)
        # Peak at t=0+ for an immediate excitation kernel.
        time_to_peak_s = 0.0

        return KalmanForecast(
            expected_volume_usdc=float(max(0.0, y_pred)),
            ci_low=float(ci_low),
            ci_high=float(ci_high),
            time_to_peak_s=time_to_peak_s,
            half_life_s=half_life_s,
            state_at_forecast=[float(v) for v in self.x],
        )

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #

    async def load_state(self) -> bool:
        """Hydrate the filter from ``follower_pool_state``.

        Returns True if a prior state was found and loaded; False if
        this is a cold start (the constructor defaults stay in place).
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT pool_size_usdc, recent_response_pct, decay_rate,
                           state_cov_json, n_observations, last_innovation
                    FROM follower_pool_state
                    WHERE leader_wallet = $1 AND pool_class = $2
                    """,
                    self.leader_wallet,
                    self.pool_class,
                )
        except Exception as exc:
            logger.warning(
                f"FollowerPoolKalman: load_state failed (cold start): {exc}"
            )
            return False

        if row is None:
            return False

        try:
            self.x = np.array(
                [
                    float(row["pool_size_usdc"] or 0.0),
                    float(row["recent_response_pct"] or DEFAULT_X0[1]),
                    float(row["decay_rate"] or DEFAULT_X0[2]),
                ],
                dtype=float,
            )
            cov_json = row["state_cov_json"]
            if cov_json:
                if isinstance(cov_json, (bytes, bytearray)):
                    cov_json = cov_json.decode("utf-8")
                if isinstance(cov_json, str):
                    cov = json.loads(cov_json)
                else:
                    cov = cov_json
                arr = np.array(cov, dtype=float).reshape(3, 3)
                self.P = arr
            self.n_observations = int(row["n_observations"] or 0)
            self.last_innovation = float(row["last_innovation"] or 0.0)
            return True
        except Exception as exc:  # pragma: no cover — corrupt row
            logger.warning(
                f"FollowerPoolKalman: load_state parse error, cold-starting: {exc}"
            )
            return False

    async def _persist(self, asof_ts: Optional[datetime] = None) -> None:
        """Write current state to follower_pool_state and append to
        follower_pool_state_history."""
        ts = asof_ts or datetime.now(tz=timezone.utc)
        cov_flat = [float(v) for v in self.P.flatten().tolist()]
        cov_json = json.dumps(cov_flat)
        async with get_db() as conn:
            await conn.execute(
                """
                INSERT INTO follower_pool_state (
                    leader_wallet, pool_class, updated_at,
                    pool_size_usdc, recent_response_pct, decay_rate,
                    state_cov_json, n_observations, last_innovation
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                ON CONFLICT (leader_wallet, pool_class) DO UPDATE
                SET updated_at = EXCLUDED.updated_at,
                    pool_size_usdc = EXCLUDED.pool_size_usdc,
                    recent_response_pct = EXCLUDED.recent_response_pct,
                    decay_rate = EXCLUDED.decay_rate,
                    state_cov_json = EXCLUDED.state_cov_json,
                    n_observations = EXCLUDED.n_observations,
                    last_innovation = EXCLUDED.last_innovation
                """,
                self.leader_wallet,
                self.pool_class,
                ts,
                float(self.x[0]),
                float(self.x[1]),
                float(self.x[2]),
                cov_json,
                int(self.n_observations),
                float(self.last_innovation),
            )
            await conn.execute(
                """
                INSERT INTO follower_pool_state_history (
                    leader_wallet, pool_class, snapshot_at,
                    pool_size_usdc, recent_response_pct, decay_rate,
                    state_cov_json, n_observations, last_innovation
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                """,
                self.leader_wallet,
                self.pool_class,
                ts,
                float(self.x[0]),
                float(self.x[1]),
                float(self.x[2]),
                cov_json,
                int(self.n_observations),
                float(self.last_innovation),
            )


__all__ = [
    "FollowerPoolKalman",
    "KalmanForecast",
    "DEFAULT_F",
    "DEFAULT_Q",
    "DEFAULT_R",
    "DEFAULT_X0",
    "DEFAULT_P0",
]
