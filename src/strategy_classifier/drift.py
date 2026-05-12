"""StrategyDriftDetector — JS divergence on classifier output vs baseline.

Round 8 (The Lens) — § 3.5 of the spec.

Per watched wallet, the daemon classifies daily. The drift detector
compares today's ``strategy_probs`` vector against the 30-day rolling
baseline of the same wallet's classifier outputs. If the Jensen-Shannon
divergence exceeds ``STRATEGY_DRIFT_JS_THRESHOLD`` (default 0.3), we
flag drift.

On drift the daemon:

* Stamps ``drift_detected = True`` on the new
  ``leader_strategy_history`` row.
* Increments ``polybot_strategy_drift_detected_total{from, to}``.
* Optionally marks the leader's R9 follower-edge entries as STALE
  (out of scope for the code-layer drop; documented in the audit doc).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from loguru import logger

from src.database.connection import get_db
from src.strategy_classifier.model import STRATEGY_CLASSES


@dataclass
class DriftReport:
    """Per-wallet drift evaluation."""

    wallet_address: str
    js_divergence: float
    drift_detected: bool
    baseline_window_days: int
    baseline_samples: int
    primary_strategy_now: str
    primary_strategy_baseline: str | None


def js_divergence(p: np.ndarray, q: np.ndarray, base: float = 2.0) -> float:
    """Jensen-Shannon divergence between two probability vectors.

    Uses ``log_2`` by default so the output is in [0, 1] (the JS
    distance² upper bound is log(2) under natural log). Robust to:

    * Zero probabilities (KL terms are masked when p == 0).
    * Floating-point drift away from sum == 1 (defensive renormalisation).
    """
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)
    if p.shape != q.shape:
        raise ValueError(f"p, q shape mismatch: {p.shape!r} vs {q.shape!r}")
    p_sum = p.sum()
    q_sum = q.sum()
    if p_sum <= 0 or q_sum <= 0:
        return 0.0
    p = p / p_sum
    q = q / q_sum
    m = 0.5 * (p + q)

    def _kl(x: np.ndarray, y: np.ndarray) -> float:
        mask = x > 0
        if not np.any(mask):
            return 0.0
        ratio = x[mask] / np.where(y[mask] > 0, y[mask], 1e-12)
        return float(np.sum(x[mask] * (np.log(ratio) / np.log(base))))

    return float(max(0.0, 0.5 * _kl(p, m) + 0.5 * _kl(q, m)))


class StrategyDriftDetector:
    """Compares today's classification against the 30-day rolling baseline."""

    def __init__(
        self,
        threshold: float = 0.3,
        baseline_window_days: int = 30,
        min_baseline_samples: int = 5,
    ) -> None:
        self.threshold = float(threshold)
        self.baseline_window_days = int(baseline_window_days)
        self.min_baseline_samples = int(min_baseline_samples)
        # The drift counter metric is optional — the daemon owns the
        # increment. We just return the report.

    async def evaluate(
        self,
        wallet_address: str,
        current_probs: dict[str, float],
        classified_at: datetime | None = None,
    ) -> DriftReport:
        """Return a :class:`DriftReport` for one wallet.

        Reads the last ``baseline_window_days`` of
        ``leader_strategy_history`` for the wallet, averages the
        per-class probabilities, and computes JS divergence vs the
        current row.

        When the baseline has fewer than ``min_baseline_samples`` rows
        (cold-start), drift is NEVER flagged — we don't have a stable
        reference yet.
        """
        if classified_at is None:
            classified_at = datetime.now(tz=timezone.utc)
        floor = classified_at - timedelta(days=self.baseline_window_days)

        primary_now = max(current_probs, key=current_probs.get) if current_probs else STRATEGY_CLASSES[0]

        baseline = await self._load_baseline(wallet_address, floor, classified_at)
        if len(baseline) < self.min_baseline_samples:
            return DriftReport(
                wallet_address=wallet_address,
                js_divergence=0.0,
                drift_detected=False,
                baseline_window_days=self.baseline_window_days,
                baseline_samples=len(baseline),
                primary_strategy_now=primary_now,
                primary_strategy_baseline=None,
            )

        baseline_avg = self._average_strategy_probs(baseline)
        p = np.array([current_probs.get(s, 0.0) for s in STRATEGY_CLASSES], dtype=float)
        q = np.array([baseline_avg.get(s, 0.0) for s in STRATEGY_CLASSES], dtype=float)
        js = js_divergence(p, q)
        drift = js > self.threshold

        # Pick the most frequent primary class in the baseline as the
        # "from" label for the metric. Ties broken alphabetically — fine
        # because the metric label is just informational.
        baseline_primaries: dict[str, int] = {}
        for row in baseline:
            cls = row.get("primary_strategy") or "unknown"
            baseline_primaries[cls] = baseline_primaries.get(cls, 0) + 1
        primary_baseline = (
            max(baseline_primaries.items(), key=lambda kv: kv[1])[0]
            if baseline_primaries
            else None
        )

        return DriftReport(
            wallet_address=wallet_address,
            js_divergence=round(js, 6),
            drift_detected=bool(drift),
            baseline_window_days=self.baseline_window_days,
            baseline_samples=len(baseline),
            primary_strategy_now=primary_now,
            primary_strategy_baseline=primary_baseline,
        )

    async def _load_baseline(
        self,
        wallet_address: str,
        floor: datetime,
        ceiling: datetime,
    ) -> list[dict[str, Any]]:
        """Pull recent classification rows from the history table."""
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT classified_at, primary_strategy, strategy_probs
                    FROM leader_strategy_history
                    WHERE wallet_address = $1
                      AND classified_at >= $2
                      AND classified_at < $3
                    ORDER BY classified_at ASC
                    """,
                    wallet_address,
                    floor,
                    ceiling,
                )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(
                f"StrategyDriftDetector: baseline load failed for "
                f"wallet={wallet_address}: {exc}"
            )
            return []

    @staticmethod
    def _average_strategy_probs(rows: list[dict[str, Any]]) -> dict[str, float]:
        """Mean of strategy_probs vectors across the baseline rows."""
        agg = {s: 0.0 for s in STRATEGY_CLASSES}
        if not rows:
            return agg
        n = 0
        for row in rows:
            probs = row.get("strategy_probs")
            if probs is None:
                continue
            if isinstance(probs, str):
                import json
                try:
                    probs = json.loads(probs)
                except json.JSONDecodeError:
                    continue
            for cls in STRATEGY_CLASSES:
                v = probs.get(cls)
                if v is None:
                    continue
                agg[cls] += float(v)
            n += 1
        if n == 0:
            return agg
        return {cls: agg[cls] / n for cls in STRATEGY_CLASSES}
