"""Round 13 (The Mirror) — Nightly calibration daemon.

Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.2 + § 7.

Orchestration:
  1. :class:`ModelLossAggregator.run_for_day(yesterday)` — populate
     calibration_loss_history for yesterday's decisions.
  2. :class:`ModelDriftMonitor.evaluate_day(yesterday)` — z-score each
     (model, strategy_class), update streak, alert + auto-disable when
     thresholds are met.
  3. Periodically: backfill the last 90 days if calibration_loss_history
     is empty (operator-friendly cold-start).

The daemon is one async loop with a configurable interval (default
24 h, fired at ``CALIBRATION_BATCH_HOUR_UTC:CALIBRATION_BATCH_MINUTE``).
Designed to be runnable both standalone (via systemd
``polymarket-calibration.service``) and embedded in the engine's
cron scheduler.

Run modes:
  * ``async run_once(target_day)`` — single batch pass, returns a
    summary dict for tests and ad-hoc invocations.
  * ``async run_forever()`` — daemon main loop. Wakes once per hour,
    checks whether yesterday's batch has been done, executes if not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.calibration.drift_detector import DriftAlert, ModelDriftMonitor
from src.calibration.loss_aggregator import LossRecord, ModelLossAggregator
from src.config import settings


@dataclass
class CalibrationRunSummary:
    """Summary returned from :meth:`CalibrationDaemon.run_once`."""

    target_day: date
    n_loss_records: int
    n_drift_alerts: int
    auto_disabled_models: list[str] = field(default_factory=list)
    loss_records: list[LossRecord] = field(default_factory=list)
    drift_alerts: list[DriftAlert] = field(default_factory=list)


class CalibrationDaemon:
    """Nightly calibration loop.

    Two collaborators (``aggregator``, ``drift_monitor``) are
    constructor-injected so tests can mock them. The daemon itself is
    a thin scheduling shell — no calibration math lives here.
    """

    def __init__(
        self,
        aggregator: Optional[ModelLossAggregator] = None,
        drift_monitor: Optional[ModelDriftMonitor] = None,
        notify_fn: Optional[Callable[[str], Awaitable[None]]] = None,
        poll_interval_s: float = 3600.0,
        backfill_window_days: int = 90,
    ) -> None:
        self._aggregator = aggregator or ModelLossAggregator()
        z_threshold = getattr(settings, "CALIBRATION_DRIFT_Z_THRESHOLD", 2.0)
        cdays = getattr(
            settings, "CALIBRATION_DRIFT_CONSECUTIVE_DAYS_FOR_DISABLE", 3
        )
        baseline_window = getattr(
            settings, "CALIBRATION_BASELINE_WINDOW_DAYS", 30
        )
        self._drift_monitor = drift_monitor or ModelDriftMonitor(
            z_threshold=z_threshold,
            consecutive_days_for_disable=cdays,
            baseline_window_days=baseline_window,
            notify_fn=notify_fn,
        )
        self._notify_fn = notify_fn
        self._poll_interval_s = float(poll_interval_s)
        self._backfill_window_days = int(backfill_window_days)
        self._last_run_for_day: Optional[date] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def run_once(
        self, target_day: Optional[date] = None
    ) -> CalibrationRunSummary:
        day = target_day or self._yesterday_utc()
        logger.info(f"CalibrationDaemon: starting batch for {day}")
        loss_records = await self._aggregator.run_for_day(day)
        drift_alerts = await self._drift_monitor.evaluate_day(day)
        auto_disabled = [
            a.model
            for a in drift_alerts
            if a.consecutive_breach_days
            >= self._drift_monitor._days_for_disable  # noqa: SLF001
        ]
        summary = CalibrationRunSummary(
            target_day=day,
            n_loss_records=len(loss_records),
            n_drift_alerts=len(drift_alerts),
            auto_disabled_models=auto_disabled,
            loss_records=loss_records,
            drift_alerts=drift_alerts,
        )
        self._last_run_for_day = day
        logger.info(
            f"CalibrationDaemon: done day={day} "
            f"records={summary.n_loss_records} "
            f"alerts={summary.n_drift_alerts} "
            f"auto_disabled={summary.auto_disabled_models}"
        )
        return summary

    async def run_forever(self) -> None:
        """Daemon main loop. Polls hourly; runs the batch when a new
        UTC day has rolled over since the last execution.
        """
        self._stop.clear()
        logger.info(
            f"CalibrationDaemon: starting run_forever "
            f"(poll_interval={self._poll_interval_s}s)"
        )
        await self._initial_backfill_if_needed()
        try:
            while not self._stop.is_set():
                try:
                    today = datetime.now(tz=timezone.utc).date()
                    yesterday = today - timedelta(days=1)
                    if self._last_run_for_day != yesterday:
                        await self.run_once(yesterday)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        f"CalibrationDaemon: run_once raised: {exc}"
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval_s
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            logger.info("CalibrationDaemon: run_forever exited")

    async def stop(self) -> None:
        self._stop.set()

    async def backfill_history(
        self, window_days: Optional[int] = None
    ) -> int:
        """Replay the last ``window_days`` of decision_predictions
        through the loss aggregator. Returns the number of LossRecord
        rows written.
        """
        n = int(window_days or self._backfill_window_days)
        logger.info(f"CalibrationDaemon: backfill_history window={n}d")
        total = await self._aggregator.backfill(window_days=n)
        logger.info(
            f"CalibrationDaemon: backfill_history wrote {total} records"
        )
        return total

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _yesterday_utc() -> date:
        return datetime.now(tz=timezone.utc).date() - timedelta(days=1)

    async def _initial_backfill_if_needed(self) -> None:
        """If calibration_loss_history is empty on startup, kick a
        90-day backfill from cold-tier predictions. Best-effort —
        failure here is logged but doesn't block run_forever.
        """
        try:
            from src.database.connection import get_db

            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM calibration_loss_history"
                )
                n = int(row["n"]) if row else 0
        except Exception as exc:
            logger.debug(
                f"CalibrationDaemon: history count fetch failed: {exc}"
            )
            return
        if n > 0:
            return
        logger.info(
            "CalibrationDaemon: calibration_loss_history empty; "
            "scheduling initial backfill"
        )
        try:
            await self.backfill_history()
        except Exception as exc:
            logger.warning(
                f"CalibrationDaemon: initial backfill failed: {exc}"
            )


# ---------------------------------------------------------------------------
# Module entry point — used by `python -m src.calibration`
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run :class:`CalibrationDaemon` forever, exiting cleanly on
    SIGINT/SIGTERM via :func:`asyncio.Task.cancel`.
    """
    daemon = CalibrationDaemon()
    try:
        await daemon.run_forever()
    except asyncio.CancelledError:
        await daemon.stop()
        raise


__all__ = [
    "CalibrationDaemon",
    "CalibrationRunSummary",
    "main",
]
