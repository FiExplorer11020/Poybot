"""
Hawkes Process Fitter — batch job for causal follower detection.
Fits univariate Hawkes process to follower timestamp series.
Runs daily on confirmed edges. Uses scipy.optimize for MLE.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
from loguru import logger
from scipy.optimize import minimize

from src.config import settings
from src.database.connection import get_db


def hawkes_log_likelihood(params: np.ndarray, timestamps: np.ndarray, window_end: float) -> float:
    """
    Negative log-likelihood for a univariate Hawkes process:
       lambda(t) = mu + alpha * sum_{t_i < t} exp(-beta * (t - t_i))

    params = [mu, alpha, beta]  (all > 0)
    timestamps: 1D sorted array of event times (seconds from start)
    window_end: observation window end time (seconds)

    Returns a large value (1e10) for invalid params or empty data.
    """
    mu, alpha, beta = params
    if mu <= 0 or alpha <= 0 or beta <= 0:
        return 1e10

    n = len(timestamps)
    if n == 0:
        return 1e10

    # Integral term: mu*T + (alpha/beta) * sum(1 - exp(-beta*(T - t_i)))
    integral = mu * window_end + (alpha / beta) * np.sum(
        1.0 - np.exp(-beta * (window_end - timestamps))
    )

    # Recursive log-intensity computation
    log_sum = 0.0
    excitation = 0.0  # R_i = sum_{j < i} exp(-beta*(t_i - t_j))
    for i in range(n):
        if i > 0:
            excitation = np.exp(-beta * (timestamps[i] - timestamps[i - 1])) * (1.0 + excitation)
        lam_i = mu + alpha * excitation
        if lam_i <= 0:
            return 1e10
        log_sum += np.log(lam_i)

    return -(log_sum - integral)


class HawkesFitter:
    """
    Fits a univariate Hawkes process to follower trade timestamps.
    The alpha/mu ratio is the key output: > 1.0 suggests strong self-excitation
    (causal follower behavior), < 0.3 suggests coincidence.
    """

    async def fit_edge(self, leader_wallet: str, follower_wallet: str) -> dict | None:
        """
        Fetch follower timestamps from DB. Fit Hawkes process.
        Returns dict with mu, alpha, beta, alpha_mu_ratio, or None if insufficient data.
        """
        lookback = timedelta(days=settings.HAWKES_LOOKBACK_DAYS)
        since = datetime.now(tz=timezone.utc) - lookback

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT time FROM trades_observed
                    WHERE wallet_address = $1 AND time >= $2
                    ORDER BY time
                    """,
                    leader_wallet,
                    since,
                )
                leader_times = np.array([r["time"].timestamp() for r in rows])

                rows2 = await conn.fetch(
                    """
                    SELECT time FROM trades_observed
                    WHERE wallet_address = $1 AND time >= $2
                    ORDER BY time
                    """,
                    follower_wallet,
                    since,
                )
                follower_times = np.array([r["time"].timestamp() for r in rows2])
        except Exception as e:
            logger.error(f"Failed to fetch timestamps for Hawkes fit: {e}")
            return None

        if len(leader_times) < 5 or len(follower_times) < 5:
            return None

        # Fit Hawkes on follower timestamps (self-excitation driven by leader influence)
        all_times = np.sort(follower_times)
        t0 = all_times[0]
        timestamps = all_times - t0
        window_end = float(timestamps[-1]) if len(timestamps) > 0 else 1.0

        result = self._fit(timestamps, window_end)
        if result is None:
            return None

        mu, alpha, beta = result
        alpha_mu_ratio = alpha / mu if mu > 0 else 0.0

        return {
            "mu": float(mu),
            "alpha": float(alpha),
            "beta": float(beta),
            "alpha_mu_ratio": float(alpha_mu_ratio),
        }

    def _fit(self, timestamps: np.ndarray, window_end: float) -> tuple | None:
        """MLE fit via scipy L-BFGS-B with 5 random restarts to avoid local minima."""
        best_result = None
        best_loss = float("inf")

        rng = np.random.default_rng(seed=42)
        for _ in range(5):
            x0 = rng.uniform(0.01, 0.5, size=3)
            try:
                res = minimize(
                    hawkes_log_likelihood,
                    x0,
                    args=(timestamps, window_end),
                    method="L-BFGS-B",
                    bounds=[(1e-6, None), (1e-6, None), (1e-6, None)],
                    options={"maxiter": 200, "ftol": 1e-8},
                )
                if res.success and res.fun < best_loss:
                    best_loss = res.fun
                    best_result = res.x
            except Exception:
                continue

        return tuple(best_result) if best_result is not None else None

    async def run_batch(self) -> int:
        """
        Fit Hawkes for all confirmed edges (co_occurrences >= MIN_CO_OCCURRENCES).
        Updates hawkes_alpha_mu column. Returns number of edges updated.
        """
        updated = 0
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT leader_wallet, follower_wallet
                    FROM follower_edges
                    WHERE co_occurrences >= $1
                    ORDER BY co_occurrences DESC
                    LIMIT $2
                    """,
                    settings.MIN_CO_OCCURRENCES,
                    settings.BATCH_HAWKES_LEADERS,
                )
        except Exception as e:
            logger.error(f"Failed to fetch edges for Hawkes batch: {e}")
            return 0

        for row in rows:
            result = await self.fit_edge(row["leader_wallet"], row["follower_wallet"])
            if result is None:
                continue
            try:
                async with get_db() as conn:
                    await conn.execute(
                        """
                        UPDATE follower_edges
                        SET hawkes_alpha_mu = $1
                        WHERE leader_wallet = $2 AND follower_wallet = $3
                        """,
                        round(result["alpha_mu_ratio"], 6),
                        row["leader_wallet"],
                        row["follower_wallet"],
                    )
                    updated += 1
            except Exception as e:
                logger.warning(
                    f"Failed to update hawkes_alpha_mu for "
                    f"{row['leader_wallet']}→{row['follower_wallet']}: {e}"
                )

        logger.info(f"Hawkes batch complete: {updated} edges updated")
        return updated
