"""
Round 9 (The Web) — Multivariate Hawkes nightly daemon.

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 6 / Rollout § 7.

For each top-N leader, the daemon:

    1. Pulls leader trade times from trades_observed over the last
       MVHAWKES_LOOKBACK_DAYS.
    2. Pulls follower-pool trade times (one stream per strategy class
       from R8) for the same window.
    3. Runs MultivariateHawkesFitter to fit the N-dim Hawkes with the
       block-sparse mask.
    4. Persists the fit to multivariate_hawkes_fits.

The daemon is a thin shell around the fitter. Kalman state updates
happen continuously in the engine's hot path (when a leader trade is
observed and the engine sees follower-pool responses), NOT here — this
unit only refits the Hawkes structure.

This entry point is run by ``infra/systemd/polymarket-follower-volume.service``
via ``python -m src.follower_volume``.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.graph.hawkes_multivariate import (
    MultivariateHawkesFitter,
    build_default_mask,
)
from src.logging_setup import configure_logging


# Try to import metric handles; fall back to no-ops in stripped envs.
try:  # pragma: no cover — exercised in the daemon path
    from src.monitoring.metrics import (
        mvhawkes_alpha_value,
        mvhawkes_bic_statistic,
        mvhawkes_couplings_accepted,
        mvhawkes_fit_duration_seconds,
        mvhawkes_fits_total,
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

        def dec(self, *_a, **_kw):
            return None

    mvhawkes_alpha_value = _NoOp()  # type: ignore[assignment]
    mvhawkes_bic_statistic = _NoOp()  # type: ignore[assignment]
    mvhawkes_couplings_accepted = _NoOp()  # type: ignore[assignment]
    mvhawkes_fit_duration_seconds = _NoOp()  # type: ignore[assignment]
    mvhawkes_fits_total = _NoOp()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class FollowerVolumeDaemon:
    """Nightly batch: refit the multivariate Hawkes for top-N leaders.

    Mirror shape of StrategyClassifierDaemon — same start/stop
    lifecycle, same per-pass logic.
    """

    def __init__(
        self,
        lookback_days: Optional[int] = None,
        batch_limit: Optional[int] = None,
        refresh_interval_s: Optional[float] = None,
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
        # Default daily refresh = 24h; the systemd service / scheduler
        # can override.
        self._refresh_s = float(
            refresh_interval_s
            if refresh_interval_s is not None
            else getattr(settings, "MVHAWKES_REFRESH_INTERVAL_S", 86400)
        )

        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Run the refit loop until ``stop()`` is called."""
        self._running = True
        self._stop_event.clear()
        logger.info(
            f"FollowerVolumeDaemon starting: lookback_days={self._lookback_days} "
            f"batch_limit={self._batch_limit} refresh_s={self._refresh_s}"
        )
        while self._running and not self._stop_event.is_set():
            try:
                await self.run_one_pass()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover — top-level
                logger.exception(f"FollowerVolumeDaemon: pass failed: {exc}")
            # Cancellable sleep.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._refresh_s
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        """Gracefully end the loop. Idempotent."""
        self._running = False
        self._stop_event.set()
        logger.info("FollowerVolumeDaemon: stop signalled")

    # ------------------------------------------------------------------ #
    # One pass                                                           #
    # ------------------------------------------------------------------ #

    async def run_one_pass(self) -> dict[str, Any]:
        """Refit the multivariate Hawkes for every active leader.

        Returns a small summary dict the operator can grep in journalctl.
        """
        leaders = await self._load_leaders()
        if not leaders:
            logger.info("FollowerVolumeDaemon: no leaders to refit this pass")
            return {"refit": 0}

        refit = 0
        rejected = 0
        failed = 0
        for leader in leaders:
            try:
                outcome = await self._refit_one(leader)
            except Exception as exc:
                logger.warning(
                    f"FollowerVolumeDaemon: refit failed for {leader}: {exc}"
                )
                failed += 1
                continue
            if outcome is None:
                continue
            refit += 1
            if outcome.get("convergence") == "bic_rejected":
                rejected += 1

        logger.info(
            f"FollowerVolumeDaemon pass complete: refit={refit} "
            f"rejected={rejected} failed={failed}"
        )
        return {"refit": refit, "rejected": rejected, "failed": failed}

    async def _refit_one(self, leader_wallet: str) -> Optional[dict[str, Any]]:
        """Fit one leader's multivariate Hawkes and persist the result."""
        # 1. Load timestamp streams.
        leader_times, pool_streams = await self._load_streams(leader_wallet)
        if leader_times.size == 0 and not any(t.size for t in pool_streams.values()):
            logger.debug(
                f"FollowerVolumeDaemon: no data for {leader_wallet[:10]}"
            )
            return None

        pool_classes = list(pool_streams.keys())
        process_labels = ["leader"] + pool_classes
        n_proc = len(process_labels)

        fitter = MultivariateHawkesFitter(n_processes=n_proc)
        times_by_proc = [leader_times] + [pool_streams[k] for k in pool_classes]

        # 2. Fit.
        t0 = time.perf_counter()
        result = fitter.fit_arrays(
            times_by_proc=times_by_proc, process_labels=process_labels
        )
        elapsed = time.perf_counter() - t0
        try:
            mvhawkes_fit_duration_seconds.observe(elapsed)
            mvhawkes_fits_total.labels(result=result["convergence"]).inc()
            mvhawkes_bic_statistic.observe(float(result["bic_statistic"]))
        except Exception:
            pass

        # 3. Histogram α values per pool — sanity check.
        try:
            for (i, j), v in result["alpha_matrix"].items():
                if i > 0 and j == 0 and v > 0.0 and i - 1 < len(pool_classes):
                    mvhawkes_alpha_value.labels(
                        pool_class=pool_classes[i - 1]
                    ).observe(float(v))
        except Exception:
            pass

        # 4. Persist.
        await self._persist_fit(leader_wallet, result, pool_classes)
        return result

    # ------------------------------------------------------------------ #
    # DB I/O                                                              #
    # ------------------------------------------------------------------ #

    async def _load_leaders(self) -> list[str]:
        """Top-N active, non-excluded leaders by Falcon Score."""
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT wallet_address
                    FROM leaders
                    WHERE on_watchlist = TRUE
                      AND NOT COALESCE(excluded, FALSE)
                    ORDER BY COALESCE(falcon_score, 0) DESC
                    LIMIT $1
                    """,
                    self._batch_limit,
                )
            return [r["wallet_address"] for r in rows]
        except Exception as exc:
            logger.warning(f"FollowerVolumeDaemon: _load_leaders failed: {exc}")
            return []

    async def _load_streams(
        self, leader_wallet: str
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Pull leader timestamps + per-pool follower timestamps for the
        last lookback_days.

        Pool grouping uses the R8 strategy classifier output stored in
        leaders.classification_json.strategy_fingerprint.primary_strategy.
        Wallets without a strategy fingerprint fall into 'all_followers'
        (graceful R8-missing degradation).
        """
        since = datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)
        leader_times = np.array([], dtype=float)
        pool_streams: dict[str, list[float]] = {}

        try:
            async with get_db() as conn:
                # Leader stream.
                rows_l = await conn.fetch(
                    """
                    SELECT time
                    FROM trades_observed
                    WHERE wallet_address = $1 AND time >= $2
                    ORDER BY time
                    """,
                    leader_wallet,
                    since,
                )
                leader_times = np.array(
                    [r["time"].timestamp() for r in rows_l], dtype=float
                )

                # Follower streams: any wallet that traded the SAME markets
                # as the leader inside the lookback window. We bucket each
                # follower by their strategy fingerprint.
                rows_f = await conn.fetch(
                    """
                    SELECT t.time, t.wallet_address,
                           COALESCE(
                               l.classification_json->'strategy_fingerprint'->>'primary_strategy',
                               'all_followers'
                           ) AS pool_class
                    FROM trades_observed t
                    LEFT JOIN leaders l ON l.wallet_address = t.wallet_address
                    WHERE t.time >= $1
                      AND t.wallet_address <> $2
                      AND t.market_id IN (
                          SELECT DISTINCT market_id
                          FROM trades_observed
                          WHERE wallet_address = $2 AND time >= $1
                      )
                    ORDER BY t.time
                    LIMIT 50000
                    """,
                    since,
                    leader_wallet,
                )
                for r in rows_f:
                    pool = str(r["pool_class"] or "all_followers")
                    pool_streams.setdefault(pool, []).append(
                        r["time"].timestamp()
                    )
        except Exception as exc:
            logger.warning(
                f"FollowerVolumeDaemon: _load_streams failed for "
                f"{leader_wallet[:10]}: {exc}"
            )
            return leader_times, {}

        return leader_times, {
            k: np.array(v, dtype=float) for k, v in pool_streams.items()
        }

    async def _persist_fit(
        self,
        leader_wallet: str,
        result: dict[str, Any],
        pool_classes: list[str],
    ) -> None:
        """Write the fit result to multivariate_hawkes_fits."""
        # Serialize α/μ/accepted with string keys so JSONB round-trip is
        # clean (tuples are not JSON-serialisable).
        alpha_json = {
            f"({i}, {j})": float(v) for (i, j), v in result["alpha_matrix"].items()
        }
        mu_json = {str(i): float(v) for i, v in result["mu_vector"].items()}
        accepted_json = {
            f"({i}, {j})": bool(v)
            for (i, j), v in result["accepted_couplings"].items()
        }

        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO multivariate_hawkes_fits (
                        leader_wallet, fit_at, pool_classes,
                        alpha_matrix_json, mu_vector_json, beta,
                        log_likelihood, bic_threshold, bic_statistic,
                        accepted_couplings_json, n_events_total, convergence
                    )
                    VALUES (
                        $1, NOW(), $2,
                        $3::jsonb, $4::jsonb, $5,
                        $6, $7, $8,
                        $9::jsonb, $10, $11
                    )
                    """,
                    leader_wallet,
                    ",".join(pool_classes),
                    json.dumps(alpha_json),
                    json.dumps(mu_json),
                    float(result["beta"]),
                    float(result["log_likelihood"]),
                    float(result["bic_threshold"]),
                    float(result["bic_statistic"]),
                    json.dumps(accepted_json),
                    int(result.get("n_events_total", 0)),
                    str(result["convergence"]),
                )
        except Exception as exc:
            logger.warning(
                f"FollowerVolumeDaemon: persist_fit failed for "
                f"{leader_wallet[:10]}: {exc}"
            )


# --------------------------------------------------------------------------- #
# Module-level entrypoint used by ``python -m src.follower_volume``           #
# --------------------------------------------------------------------------- #


async def main() -> None:
    """Daemon body. Mirrors src.strategy_classifier.daemon.main()."""
    level = configure_logging()
    logger.info(f"Starting FollowerVolume daemon (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    daemon = FollowerVolumeDaemon()

    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        logger.info("FollowerVolume daemon: shutdown signal received")
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
            logger.exception("FollowerVolume daemon: redis aclose() raised")
        await close_pool()
        logger.info("FollowerVolume daemon: stopped")


if __name__ == "__main__":  # pragma: no cover — module run path
    asyncio.run(main())


__all__ = ["FollowerVolumeDaemon", "main"]
