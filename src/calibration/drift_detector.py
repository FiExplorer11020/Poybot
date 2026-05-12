"""Round 13 (The Mirror) — Per-model drift detection.

Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.3.

For each (model, strategy_class), maintain a rolling 30-day baseline
of calibration loss. Compute today's z-score; emit a Prometheus gauge;
alert via Telegram when |z| > threshold (rate-limited 1/hour/model);
trigger auto-disable after 3 consecutive days of breach (and only for
the unprotected models — ``follow_confidence`` is shielded per
spec § 3.4).

State machine:

* normal → alert when |z| > z_threshold (default 2.0)
* alert → auto-disable when the (model, strategy_class) has accumulated
  ``CALIBRATION_DRIFT_CONSECUTIVE_DAYS_FOR_DISABLE`` consecutive breach
  days (default 3)

The consecutive-day counter lives in a small companion table written
by the daemon at the END of each nightly batch (see
:meth:`ModelDriftMonitor.persist_streak`). The drift monitor is
stateless across daemon invocations — every call re-reads the streak
from the DB.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from loguru import logger

from src.calibration.auto_disable import (
    PROTECTED_FROM_AUTO_DISABLE,
    get_auto_disabler,
)
from src.database.connection import get_db


@dataclass
class DriftBaseline:
    """Rolling baseline statistics for one (model, strategy_class)."""

    mean: float
    std: float
    n: int


@dataclass
class DriftAlert:
    """One alert event emitted by the drift monitor."""

    model: str
    strategy_class: Optional[str]
    today_loss: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    consecutive_breach_days: int
    measured_at: date


class ModelDriftMonitor:
    """Drift detection orchestration.

    Public methods:

    * ``async evaluate_day(target_day) -> list[DriftAlert]`` — for each
      (model, strategy_class) with a row at ``target_day``, compute the
      rolling baseline, derive z-score, update streak, optionally trigger
      auto-disable.
    * ``async build_baseline(model, strategy_class, end_day) -> DriftBaseline``
      — pure read of the 30-day rolling baseline. Exposed for the
      operator dashboard + test fixtures.
    """

    def __init__(
        self,
        z_threshold: float = 2.0,
        consecutive_days_for_disable: int = 3,
        baseline_window_days: int = 30,
        rate_limit_seconds: float = 3600.0,
        notify_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._z_threshold = float(z_threshold)
        self._days_for_disable = int(consecutive_days_for_disable)
        self._baseline_days = int(baseline_window_days)
        self._rate_limit_s = float(rate_limit_seconds)
        self._notify_fn = notify_fn
        # In-memory rate-limit table: (model, strategy) -> last_emit_t
        self._last_alert_at: dict[tuple[str, Optional[str]], float] = {}

    async def evaluate_day(self, target_day: date) -> list[DriftAlert]:
        rows = await self._fetch_today_losses(target_day)
        alerts: list[DriftAlert] = []
        for row in rows:
            model = row["model"]
            strat = row.get("strategy_class")
            today_loss = self._extract_primary_loss(row)
            if today_loss is None:
                continue
            baseline = await self.build_baseline(model, strat, target_day)
            z = self._z_score(today_loss, baseline)
            await self._emit_baseline_gauges(model, strat, baseline, z)
            if abs(z) <= self._z_threshold:
                # Today is clean — reset the streak.
                await self._reset_streak(model, strat)
                continue
            streak = await self._increment_streak(model, strat, target_day)
            alert = DriftAlert(
                model=model,
                strategy_class=strat,
                today_loss=today_loss,
                baseline_mean=baseline.mean,
                baseline_std=baseline.std,
                z_score=z,
                consecutive_breach_days=streak,
                measured_at=target_day,
            )
            alerts.append(alert)
            await self._maybe_alert_operator(alert)
            if (
                streak >= self._days_for_disable
                and model not in PROTECTED_FROM_AUTO_DISABLE
            ):
                await self._trigger_auto_disable(alert)
        return alerts

    async def build_baseline(
        self,
        model: str,
        strategy_class: Optional[str],
        end_day: date,
    ) -> DriftBaseline:
        start_day = end_day - timedelta(days=self._baseline_days)
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT brier_score, log_loss, mape, ci_coverage
                    FROM calibration_loss_history
                    WHERE model = $1
                      AND strategy_class IS NOT DISTINCT FROM $2
                      AND measured_at >= $3
                      AND measured_at < $4
                    """,
                    model,
                    strategy_class,
                    start_day,
                    end_day,
                )
        except Exception as exc:
            logger.debug(
                f"ModelDriftMonitor.build_baseline: fetch failed: {exc}"
            )
            return DriftBaseline(mean=0.0, std=0.0, n=0)
        values: list[float] = []
        for r in rows:
            v = self._extract_primary_loss(dict(r))
            if v is not None:
                values.append(v)
        if not values:
            return DriftBaseline(mean=0.0, std=0.0, n=0)
        mean = statistics.fmean(values)
        std = statistics.pstdev(values) if len(values) > 1 else 0.0
        return DriftBaseline(mean=mean, std=std, n=len(values))

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_primary_loss(row: dict) -> Optional[float]:
        """Pick the model's primary loss column, mirroring the gauge
        emission in :class:`ModelLossAggregator`."""
        for key in ("brier_score", "mape", "log_loss"):
            v = row.get(key)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def _z_score(self, today: float, baseline: DriftBaseline) -> float:
        """Z-score with a small-std safety floor.

        With < 3 baseline samples or a std of 0 the result is the raw
        signed difference (a coarse signal). The monitor never crashes
        on a single-sample baseline.
        """
        if baseline.n < 3:
            return today - baseline.mean
        std = baseline.std if baseline.std > 1e-9 else 1e-9
        return (today - baseline.mean) / std

    async def _fetch_today_losses(self, target_day: date) -> list[dict]:
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT model, strategy_class, brier_score, log_loss,
                           mape, ci_coverage
                    FROM calibration_loss_history
                    WHERE measured_at = $1
                    """,
                    target_day,
                )
        except Exception as exc:
            logger.debug(
                f"ModelDriftMonitor: today-loss fetch failed: {exc}"
            )
            return []
        return [dict(r) for r in rows]

    async def _reset_streak(
        self, model: str, strategy_class: Optional[str]
    ) -> None:
        await self._upsert_streak(model, strategy_class, 0, None)

    async def _increment_streak(
        self,
        model: str,
        strategy_class: Optional[str],
        measured_at: date,
    ) -> int:
        # Read-modify-write inside one transaction.
        try:
            async with get_db() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        SELECT consecutive_days, last_breach_at
                        FROM model_drift_streak
                        WHERE model = $1
                          AND strategy_class IS NOT DISTINCT FROM $2
                        FOR UPDATE
                        """,
                        model,
                        strategy_class,
                    )
                    prev_days = int(row["consecutive_days"]) if row else 0
                    prev_breach = row["last_breach_at"] if row else None
                    # Allow same-day re-runs to not double-increment.
                    if prev_breach == measured_at:
                        return prev_days
                    new_days = prev_days + 1
                    await conn.execute(
                        """
                        INSERT INTO model_drift_streak
                            (model, strategy_class, consecutive_days,
                             last_breach_at)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (model, strategy_class) DO UPDATE
                            SET consecutive_days = EXCLUDED.consecutive_days,
                                last_breach_at = EXCLUDED.last_breach_at
                        """,
                        model,
                        strategy_class,
                        new_days,
                        measured_at,
                    )
                    return new_days
        except Exception as exc:
            logger.debug(
                f"ModelDriftMonitor: streak increment failed for "
                f"{model}/{strategy_class}: {exc}"
            )
            return 1  # best-effort; alert still fires this run

    async def _upsert_streak(
        self,
        model: str,
        strategy_class: Optional[str],
        consecutive_days: int,
        last_breach_at: Optional[date],
    ) -> None:
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO model_drift_streak
                        (model, strategy_class, consecutive_days,
                         last_breach_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (model, strategy_class) DO UPDATE
                        SET consecutive_days = EXCLUDED.consecutive_days,
                            last_breach_at = EXCLUDED.last_breach_at
                    """,
                    model,
                    strategy_class,
                    consecutive_days,
                    last_breach_at,
                )
        except Exception as exc:
            logger.debug(
                f"ModelDriftMonitor: streak reset failed for "
                f"{model}/{strategy_class}: {exc}"
            )

    @staticmethod
    async def _emit_baseline_gauges(
        model: str,
        strategy_class: Optional[str],
        baseline: DriftBaseline,
        z: float,
    ) -> None:
        try:
            from src.monitoring import metrics as mm

            strat_label = strategy_class or "aggregate"
            mm.calibration_baseline_loss.labels(
                model=model, strategy_class=strat_label
            ).set(float(baseline.mean))
            mm.model_drift_score.labels(
                model=model, strategy_class=strat_label
            ).set(float(z))
        except Exception:
            pass

    async def _maybe_alert_operator(self, alert: DriftAlert) -> None:
        if self._notify_fn is None:
            return
        key = (alert.model, alert.strategy_class)
        now = time.monotonic()
        last = self._last_alert_at.get(key, 0.0)
        if now - last < self._rate_limit_s:
            return
        self._last_alert_at[key] = now
        msg = (
            f"Drift detected: model={alert.model} "
            f"strategy={alert.strategy_class or 'aggregate'} "
            f"z={alert.z_score:+.2f} "
            f"(today={alert.today_loss:.4f} vs baseline "
            f"{alert.baseline_mean:.4f}±{alert.baseline_std:.4f}) "
            f"streak={alert.consecutive_breach_days}d"
        )
        try:
            await self._notify_fn(msg)
        except Exception as exc:
            logger.debug(
                f"ModelDriftMonitor: notify_fn raised: {exc}"
            )

    async def _trigger_auto_disable(self, alert: DriftAlert) -> None:
        if alert.model in PROTECTED_FROM_AUTO_DISABLE:
            return
        disabler = get_auto_disabler()
        reason = (
            f"drift detected for {alert.consecutive_breach_days} "
            f"consecutive days (z={alert.z_score:+.2f})"
        )
        await disabler.disable_model(
            alert.model, reason=reason, auto_or_manual="auto"
        )


__all__ = [
    "DriftAlert",
    "DriftBaseline",
    "ModelDriftMonitor",
]
