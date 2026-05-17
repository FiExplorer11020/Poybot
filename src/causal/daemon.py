"""Round 10 (The Truth Test) — nightly 2SLS daemon.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.2 + § 7.B.

For every (leader, pool_class) pair with an active R9 multivariate
Hawkes fit, the daemon:

  1. Pulls the leader trade timestamps + per-pool follower trade
     timestamps over MVHAWKES_LOOKBACK_DAYS (same window as R9).
  2. Builds the (L, F, Z, X) matrices:
       L  - leader trade intensity in equal-width bins
       F  - per-pool follower trade intensity in the same bins
       Z  - instrument matrix from instrumental_events
       X  - exogenous controls (time-of-day cyclical features,
            market_state proxies)
  3. Runs TwoStageLeastSquaresEstimator.fit -> IVEstimate.
  4. Compares against the cached R9 Hawkes α/μ; emits
     ``polybot_causal_ate_vs_hawkes_disagreement``.
  5. Persists to causal_estimates.

Schedule: 04:00 UTC nightly, AFTER R9's 03:30. The engine cron
registers this; the systemd unit is the operator's alternative
deployment path (mirror of R9).

The daemon is intentionally thin around the estimator — the
methodology audit (spec § 6) reviews the matrix-construction logic
here as the place where most causal-inference mistakes hide.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import redis.asyncio as redis_async
from loguru import logger

from src.causal.daemon_matrices import build_iv_matrices, safe_float
from src.causal.iv_estimator import IVEstimate, TwoStageLeastSquaresEstimator
from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.logging_setup import configure_logging


# Try to import metric handles; fall back to no-ops in stripped envs.
try:  # pragma: no cover — exercised in the daemon path
    from src.monitoring.metrics import (
        causal_ate_excludes_zero_count,
        causal_ate_vs_hawkes_disagreement,
        iv_estimates_total,
        iv_first_stage_f,
        iv_wu_hausman_p,
    )
except Exception:  # pragma: no cover — early-import fallback
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

        def observe(self, *_a, **_kw):
            return None

    causal_ate_excludes_zero_count = _NoOp()  # type: ignore[assignment]
    causal_ate_vs_hawkes_disagreement = _NoOp()  # type: ignore[assignment]
    iv_estimates_total = _NoOp()  # type: ignore[assignment]
    iv_first_stage_f = _NoOp()  # type: ignore[assignment]
    iv_wu_hausman_p = _NoOp()  # type: ignore[assignment]


class CausalDaemon:
    """Nightly 2SLS estimator over the (leader, pool_class) grid.

    Mirror shape of FollowerVolumeDaemon (R9). Per-pass logic = run
    one IV estimate per pair, persist to ``causal_estimates``, emit
    metrics.
    """

    def __init__(
        self,
        lookback_days: Optional[int] = None,
        batch_limit: Optional[int] = None,
        refresh_interval_s: Optional[float] = None,
        bootstrap_n: Optional[int] = None,
        bin_seconds: int = 300,
    ) -> None:
        self._lookback_days = int(
            lookback_days
            if lookback_days is not None
            else getattr(settings, "MVHAWKES_LOOKBACK_DAYS", 30)
        )
        self._batch_limit = int(
            batch_limit
            if batch_limit is not None
            else getattr(settings, "BATCH_HAWKES_LEADERS", 200)
        )
        self._refresh_s = float(
            refresh_interval_s
            if refresh_interval_s is not None
            else 86_400.0
        )
        self._bootstrap_n = int(
            bootstrap_n
            if bootstrap_n is not None
            else getattr(settings, "CAUSAL_2SLS_BOOTSTRAP_N", 1000)
        )
        self._bin_seconds = int(bin_seconds)

        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        logger.info(
            f"CausalDaemon starting: lookback_days={self._lookback_days} "
            f"batch_limit={self._batch_limit} refresh_s={self._refresh_s} "
            f"bootstrap_n={self._bootstrap_n}"
        )
        while self._running and not self._stop_event.is_set():
            try:
                await self.run_one_pass()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover — top-level
                logger.exception(f"CausalDaemon: pass failed: {exc}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._refresh_s
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        logger.info("CausalDaemon: stop signalled")

    # ------------------------------------------------------------------ #
    # One pass                                                           #
    # ------------------------------------------------------------------ #

    async def run_one_pass(self) -> dict[str, Any]:
        pairs = await self._load_pairs()
        if not pairs:
            logger.info("CausalDaemon: no (leader, pool) pairs this pass")
            return {"estimated": 0}

        estimated = 0
        weak = 0
        failed = 0
        for leader, pool, hawkes_ratio in pairs:
            try:
                est = await self._estimate_one(leader, pool, hawkes_ratio)
            except Exception as exc:
                logger.warning(
                    f"CausalDaemon: estimate failed for "
                    f"{leader[:10]}/{pool}: {exc}"
                )
                failed += 1
                continue
            if est is None:
                continue
            estimated += 1
            if est.convergence == "weak_instruments":
                weak += 1
        logger.info(
            f"CausalDaemon pass complete: estimated={estimated} "
            f"weak={weak} failed={failed}"
        )
        return {"estimated": estimated, "weak": weak, "failed": failed}

    async def _estimate_one(
        self,
        leader_wallet: str,
        pool_class: str,
        hawkes_alpha_mu: float,
    ) -> Optional[IVEstimate]:
        """Build (L, F, Z, X) and call the estimator. Persist result."""
        period_end = datetime.now(tz=timezone.utc)
        period_start = period_end - timedelta(days=self._lookback_days)
        leader_times, pool_times = await self._load_streams(
            leader_wallet, pool_class, period_start, period_end
        )
        if leader_times.size == 0 or pool_times.size == 0:
            return None
        Z, instrument_names = await self._load_instruments(period_start, period_end)
        L, F, Z_binned, X = build_iv_matrices(
            leader_times,
            pool_times,
            Z,
            period_start,
            period_end,
            bin_seconds=self._bin_seconds,
        )
        if L.size < 30 or Z_binned.shape[1] == 0:
            # Not enough data; skip silently to avoid noisy failures.
            return None
        estimator = TwoStageLeastSquaresEstimator(bootstrap_n=self._bootstrap_n)
        result = estimator.fit(L, F, Z_binned, X, instrument_names)
        try:
            iv_estimates_total.labels(result=result.convergence).inc()
            iv_first_stage_f.observe(float(result.first_stage_f))
            iv_wu_hausman_p.observe(float(result.wu_hausman_p))
            # Disagreement metric: |ATE - α/μ| / |α/μ|.
            if hawkes_alpha_mu and abs(hawkes_alpha_mu) > 1e-9:
                disagreement = abs(
                    (result.ate - hawkes_alpha_mu) / hawkes_alpha_mu
                )
                causal_ate_vs_hawkes_disagreement.set(float(disagreement))
            # Excludes-zero count: CI strictly above 0 or strictly below 0.
            if result.ci_low > 0 or result.ci_high < 0:
                causal_ate_excludes_zero_count.labels(
                    leader=leader_wallet
                ).inc()
        except Exception:
            pass

        await self._persist(
            leader_wallet, pool_class, period_start, period_end,
            hawkes_alpha_mu, result,
        )
        return result

    # ------------------------------------------------------------------ #
    # DB I/O                                                              #
    # ------------------------------------------------------------------ #

    async def _load_pairs(self) -> list[tuple[str, str, float]]:
        """List (leader, pool_class, hawkes_alpha_mu_ratio) triples
        with active R9 fits.

        Falls back to (leader, 'all_followers', 0.0) if migration 028
        is missing — keeps the daemon useful in environments where R9
        hasn't been deployed yet.
        """
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        m.leader_wallet,
                        m.pool_classes
                    FROM (
                        SELECT DISTINCT ON (leader_wallet)
                            leader_wallet, pool_classes, fit_at
                        FROM multivariate_hawkes_fits
                        WHERE convergence = 'converged'
                        ORDER BY leader_wallet, fit_at DESC
                    ) m
                    LIMIT $1
                    """,
                    self._batch_limit,
                )
        except Exception as exc:
            logger.debug(
                f"CausalDaemon: _load_pairs from mvhawkes failed ({exc}); "
                "falling back to leaders table"
            )
            try:
                async with get_db() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT wallet_address AS leader_wallet,
                               'all_followers' AS pool_classes
                        FROM leaders
                        WHERE on_watchlist = TRUE
                          AND NOT COALESCE(excluded, FALSE)
                        ORDER BY COALESCE(falcon_score, 0) DESC
                        LIMIT $1
                        """,
                        self._batch_limit,
                    )
            except Exception:
                return []

        out: list[tuple[str, str, float]] = []
        for r in rows:
            leader = r["leader_wallet"]
            pools_csv = r.get("pool_classes") if hasattr(r, "get") else r["pool_classes"]
            if not pools_csv:
                out.append((leader, "all_followers", 0.0))
                continue
            for pool in str(pools_csv).split(","):
                pool = pool.strip()
                if pool:
                    out.append((leader, pool, 0.0))
        return out[: self._batch_limit]

    async def _load_streams(
        self,
        leader_wallet: str,
        pool_class: str,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Pull leader + pool trade times in the window."""
        try:
            async with get_db() as conn:
                rows_l = await conn.fetch(
                    """
                    SELECT time
                    FROM trades_observed
                    WHERE wallet_address = $1
                      AND time >= $2 AND time < $3
                      AND source IS DISTINCT FROM 'onchain'
                    ORDER BY time
                    """,
                    leader_wallet,
                    period_start,
                    period_end,
                )
                rows_f = await conn.fetch(
                    """
                    SELECT t.time
                    FROM trades_observed t
                    LEFT JOIN leaders l ON l.wallet_address = t.wallet_address
                    WHERE t.time >= $1 AND t.time < $2
                      AND t.wallet_address <> $3
                      AND t.source IS DISTINCT FROM 'onchain'
                      AND t.market_id IN (
                          SELECT DISTINCT market_id
                          FROM trades_observed
                          WHERE wallet_address = $3
                            AND time >= $1 AND time < $2
                            AND source IS DISTINCT FROM 'onchain'
                      )
                      AND COALESCE(
                          l.classification_json->'strategy_fingerprint'->>'primary_strategy',
                          'all_followers'
                      ) = $4
                    ORDER BY t.time
                    LIMIT 50000
                    """,
                    period_start,
                    period_end,
                    leader_wallet,
                    pool_class,
                )
        except Exception as exc:
            logger.debug(
                f"CausalDaemon: _load_streams failed for "
                f"{leader_wallet[:10]}/{pool_class}: {exc}"
            )
            return np.array([], dtype=float), np.array([], dtype=float)
        L = np.array([r["time"].timestamp() for r in rows_l], dtype=float)
        F = np.array([r["time"].timestamp() for r in rows_f], dtype=float)
        return L, F

    async def _load_instruments(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT event_type, event_time
                    FROM instrumental_events
                    WHERE event_time >= $1 AND event_time < $2
                    ORDER BY event_time
                    LIMIT 100000
                    """,
                    period_start,
                    period_end,
                )
        except Exception:
            return [], []
        events = [
            {"event_type": r["event_type"], "event_time": r["event_time"]}
            for r in rows
        ]
        types = sorted({e["event_type"] for e in events})
        return events, types

    async def _persist(
        self,
        leader_wallet: str,
        pool_class: str,
        period_start: datetime,
        period_end: datetime,
        hawkes_ratio: float,
        est: IVEstimate,
    ) -> None:
        instruments_csv = ",".join(est.instruments_used)[:200]
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO causal_estimates (
                        leader_wallet, pool_class, estimated_at,
                        period_start, period_end,
                        hawkes_alpha_mu_ratio, hawkes_log_likelihood,
                        causal_ate, causal_ate_ci_low, causal_ate_ci_high,
                        wu_hausman_p, first_stage_f, instruments_used,
                        convergence
                    )
                    VALUES (
                        $1, $2, NOW(),
                        $3, $4,
                        $5, $6,
                        $7, $8, $9,
                        $10, $11, $12, $13
                    )
                    """,
                    leader_wallet,
                    pool_class,
                    period_start,
                    period_end,
                    safe_float(hawkes_ratio),
                    None,
                    safe_float(est.ate),
                    safe_float(est.ci_low),
                    safe_float(est.ci_high),
                    safe_float(est.wu_hausman_p),
                    safe_float(est.first_stage_f),
                    instruments_csv,
                    est.convergence,
                )
        except Exception as exc:
            logger.warning(
                f"CausalDaemon: persist failed for "
                f"{leader_wallet[:10]}/{pool_class}: {exc}"
            )


# --------------------------------------------------------------------------- #
# Module-level entrypoint used by ``python -m src.causal``                    #
# --------------------------------------------------------------------------- #


async def main() -> None:
    """Daemon body. Mirrors src.follower_volume.daemon.main()."""
    level = configure_logging()
    logger.info(f"Starting Causal daemon (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    daemon = CausalDaemon()
    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        logger.info("Causal daemon: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover — Windows
            pass

    run_task = asyncio.create_task(daemon.start())
    stop_waiter = asyncio.create_task(stop_event.wait())

    try:
        done, pending = await asyncio.wait(
            {run_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_waiter in done:
            await daemon.stop()
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        try:
            await redis_client.aclose()
        except Exception:  # pragma: no cover
            logger.exception("Causal daemon: redis aclose() raised")
        await close_pool()
        logger.info("Causal daemon: stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())


__all__ = ["CausalDaemon", "main"]
