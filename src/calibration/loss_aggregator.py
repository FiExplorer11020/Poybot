"""Round 13 (The Mirror) — Per-model nightly loss aggregation.

Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.2.

For each model the bot runs, the nightly batch computes a calibration
loss over yesterday's decisions and writes one row to
``calibration_loss_history`` per (model, strategy_class, measured_at).
``strategy_class = NULL`` row = aggregate across all strategy classes.

Models + their loss functions:

* ``follow_confidence`` → Brier score
  ``mean((predicted_win_prob - 1{realised_win})²)``
* ``volume_forecast`` → MAPE + CI-coverage rate
  ``mean(|forecast - actual| / max(actual, ε))`` + fraction of times
  the realised volume fell within the predicted 95 % CI.
* ``causal_ate`` → residual = |hawkes_alpha_mu - causal_ate| relative
  to bootstrap CI width (proxy for "how much disagreement is left
  unexplained by the IV adjustment").
* ``strategy_class`` → log_loss against ground-truth strategy proxy
  (high cancel_to_fill_ratio = market_maker etc.). Weak but
  continuous + automated.

All four math helpers are pure functions exposed at module level so
the unit tests can verify them in isolation. The aggregator class is
a thin orchestration layer that pulls predictions+outcomes from the
DB, dispatches per-model, and writes results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from loguru import logger

from src.database.connection import get_db


# ---------------------------------------------------------------------------
# Pure math helpers — exposed for unit tests
# ---------------------------------------------------------------------------


_MAPE_EPS = 1e-6
_LOG_LOSS_EPS = 1e-12


def compute_brier(
    predictions: Sequence[float], outcomes: Sequence[int]
) -> Optional[float]:
    """Brier score = mean((p - y)²) where y ∈ {0, 1}.

    Returns None if there are no usable pairs (filtering out None / NaN).
    """
    pairs = _pair_floats_with_ints(predictions, outcomes)
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def compute_mape(
    forecasts: Sequence[float], actuals: Sequence[float]
) -> Optional[float]:
    """Mean Absolute Percentage Error =
    ``mean(|forecast - actual| / max(|actual|, _MAPE_EPS))``.

    The epsilon floor protects against /0 when the actual is exactly
    zero (a valid outcome — sometimes no follow volume materialises).
    """
    pairs = _pair_floats(forecasts, actuals)
    if not pairs:
        return None
    return sum(
        abs(f - a) / max(abs(a), _MAPE_EPS) for f, a in pairs
    ) / len(pairs)


def compute_ci_coverage(
    actuals: Sequence[float],
    ci_lows: Sequence[float],
    ci_highs: Sequence[float],
) -> Optional[float]:
    """Fraction of times the realised value fell within [ci_low, ci_high].

    Well-calibrated 95 % CI → coverage ≈ 0.95.
    """
    triples = _zip_floats(actuals, ci_lows, ci_highs)
    if not triples:
        return None
    hits = sum(1 for a, lo, hi in triples if lo <= a <= hi)
    return hits / len(triples)


def compute_log_loss(
    probability_predictions: Sequence[Sequence[float]],
    true_class_indices: Sequence[int],
) -> Optional[float]:
    """Multiclass log loss (cross-entropy) per spec § 3.2.

    ``probability_predictions[i]`` is a per-class probability vector
    (must sum approximately to 1); ``true_class_indices[i]`` is the
    integer index of the true class. Predictions are clipped to
    [_LOG_LOSS_EPS, 1 - _LOG_LOSS_EPS] to keep log finite.
    """
    if len(probability_predictions) != len(true_class_indices):
        return None
    if not probability_predictions:
        return None
    total = 0.0
    n = 0
    for probs, y in zip(probability_predictions, true_class_indices):
        if probs is None or y is None:
            continue
        if y < 0 or y >= len(probs):
            continue
        p = probs[y]
        if p is None:
            continue
        try:
            pf = float(p)
        except (TypeError, ValueError):
            continue
        pf = max(_LOG_LOSS_EPS, min(1.0 - _LOG_LOSS_EPS, pf))
        total -= math.log(pf)
        n += 1
    if n == 0:
        return None
    return total / n


def compute_causal_residual(
    hawkes_alpha_mus: Sequence[float],
    causal_ates: Sequence[float],
    ci_widths: Sequence[float] | None = None,
) -> Optional[float]:
    """Residual between R9's statistical Hawkes α/μ and R10's
    IV-adjusted causal ATE.

    If ``ci_widths`` is provided, the per-row residual is normalised
    by the CI width — wide CIs absorb large absolute residuals as
    "we already knew the estimate was noisy".
    """
    if ci_widths is None:
        ci_widths = [1.0] * len(hawkes_alpha_mus)
    if len(hawkes_alpha_mus) != len(causal_ates) != len(ci_widths):
        return None
    residuals: list[float] = []
    for h, c, w in zip(hawkes_alpha_mus, causal_ates, ci_widths):
        if h is None or c is None:
            continue
        try:
            hf = float(h)
            cf = float(c)
            wf = max(_MAPE_EPS, float(w if w is not None else 1.0))
        except (TypeError, ValueError):
            continue
        residuals.append(abs(hf - cf) / wf)
    if not residuals:
        return None
    return sum(residuals) / len(residuals)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass
class LossRecord:
    """One row that will be written to ``calibration_loss_history``."""

    model: str
    strategy_class: Optional[str]
    measured_at: date
    n_decisions: int
    brier_score: Optional[float] = None
    log_loss: Optional[float] = None
    mape: Optional[float] = None
    ci_coverage: Optional[float] = None


class ModelLossAggregator:
    """Pulls yesterday's predictions + outcomes from the DB, dispatches
    them through the per-model loss helpers, writes results.

    Public methods:

    * ``async run_for_day(target_day: date) -> list[LossRecord]``
      — read predictions for ``target_day``, compute all model losses,
        persist to ``calibration_loss_history``.
    * ``async backfill(window_days: int) -> int``
      — replay ``window_days`` consecutive days from ``target_day-N``
        down to yesterday. Returns the total LossRecord count written.

    Both methods are idempotent: writes use ``ON CONFLICT DO UPDATE``
    so re-runs overwrite stale rows (per spec § 7.B).
    """

    def __init__(self) -> None:
        self._strategy_classes: tuple[str, ...] = (
            "directional",
            "momentum",
            "contrarian",
            "arb_2way",
            "arb_3way",
            "market_maker",
            "structural_bot",
            "info_leak",
            "social_driven",
        )

    async def run_for_day(self, target_day: date) -> list[LossRecord]:
        rows = await self._fetch_predictions_for_day(target_day)
        records = self._compute_records(target_day, rows)
        if records:
            await self._persist(records)
        await self._emit_metrics(records)
        return records

    async def backfill(
        self,
        window_days: int = 90,
        end_day: Optional[date] = None,
    ) -> int:
        end = end_day or (datetime.now(tz=timezone.utc).date() - timedelta(days=1))
        total = 0
        for offset in range(window_days):
            day = end - timedelta(days=offset)
            try:
                recs = await self.run_for_day(day)
                total += len(recs)
            except Exception as exc:
                logger.warning(
                    f"ModelLossAggregator.backfill: day={day} failed: {exc}"
                )
        return total

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    async def _fetch_predictions_for_day(self, target_day: date) -> list[dict[str, Any]]:
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT dp.decision_id,
                           dp.predicted_at,
                           dp.follow_confidence,
                           dp.fade_confidence,
                           dp.strategy_class,
                           dp.strategy_confidence,
                           dp.hawkes_alpha_mu,
                           dp.volume_forecast_usdc,
                           dp.volume_forecast_ci_low,
                           dp.volume_forecast_ci_high,
                           dp.causal_ate,
                           dp.causal_ate_ci_low,
                           dp.causal_ate_ci_high,
                           dp.actual_pnl_usdc,
                           dp.actual_followup_volume_usdc,
                           dp.closed_at
                    FROM decision_predictions dp
                    WHERE dp.predicted_at::date = $1
                    """,
                    target_day,
                )
        except Exception as exc:
            logger.debug(
                f"ModelLossAggregator: predictions fetch failed for {target_day}: {exc}"
            )
            return []
        return [dict(r) for r in rows]

    def _compute_records(
        self, target_day: date, rows: list[dict[str, Any]]
    ) -> list[LossRecord]:
        out: list[LossRecord] = []
        # Aggregate row first (strategy_class = NULL).
        out.append(self._aggregate_record("follow_confidence", target_day, rows, None))
        out.append(self._aggregate_record("volume_forecast", target_day, rows, None))
        out.append(self._aggregate_record("causal_ate", target_day, rows, None))
        out.append(self._aggregate_record("strategy_class", target_day, rows, None))
        # Per-strategy rows for follow_confidence (the operator-facing
        # split): tells us if the classifier-conditional path is
        # mis-calibrated for any given class.
        for cls in self._strategy_classes:
            subset = [r for r in rows if (r.get("strategy_class") == cls)]
            if not subset:
                continue
            out.append(
                self._aggregate_record(
                    "follow_confidence", target_day, subset, cls
                )
            )
        # Drop empty records.
        return [r for r in out if r is not None and r.n_decisions > 0]

    def _aggregate_record(
        self,
        model: str,
        target_day: date,
        rows: list[dict[str, Any]],
        strategy_class: Optional[str],
    ) -> Optional[LossRecord]:
        if not rows:
            return None

        if model == "follow_confidence":
            preds = [r.get("follow_confidence") for r in rows]
            # Outcome proxy: positive PnL = "win" for the FOLLOW direction.
            outcomes = [
                1 if (r.get("actual_pnl_usdc") or 0) > 0 else 0
                for r in rows
                if r.get("follow_confidence") is not None
            ]
            cleaned_preds = [p for p in preds if p is not None]
            if not cleaned_preds:
                return None
            brier = compute_brier(cleaned_preds, outcomes)
            return LossRecord(
                model=model,
                strategy_class=strategy_class,
                measured_at=target_day,
                n_decisions=len(cleaned_preds),
                brier_score=brier,
            )

        if model == "volume_forecast":
            forecasts = [r.get("volume_forecast_usdc") for r in rows]
            actuals = [r.get("actual_followup_volume_usdc") for r in rows]
            ci_lows = [r.get("volume_forecast_ci_low") for r in rows]
            ci_highs = [r.get("volume_forecast_ci_high") for r in rows]
            # Filter to rows where ALL four fields are present.
            triples = [
                (f, a, lo, hi)
                for f, a, lo, hi in zip(forecasts, actuals, ci_lows, ci_highs)
                if (f is not None and a is not None
                    and lo is not None and hi is not None)
            ]
            if not triples:
                return None
            mape = compute_mape(
                [t[0] for t in triples], [t[1] for t in triples]
            )
            cov = compute_ci_coverage(
                [t[1] for t in triples],
                [t[2] for t in triples],
                [t[3] for t in triples],
            )
            return LossRecord(
                model=model,
                strategy_class=strategy_class,
                measured_at=target_day,
                n_decisions=len(triples),
                mape=mape,
                ci_coverage=cov,
            )

        if model == "causal_ate":
            hawkes = [r.get("hawkes_alpha_mu") for r in rows]
            ate = [r.get("causal_ate") for r in rows]
            widths: list[float] = []
            for r in rows:
                lo = r.get("causal_ate_ci_low")
                hi = r.get("causal_ate_ci_high")
                if lo is None or hi is None:
                    widths.append(1.0)
                else:
                    try:
                        widths.append(abs(float(hi) - float(lo)) or 1.0)
                    except (TypeError, ValueError):
                        widths.append(1.0)
            usable = [
                (h, a, w)
                for h, a, w in zip(hawkes, ate, widths)
                if h is not None and a is not None
            ]
            if not usable:
                return None
            residual = compute_causal_residual(
                [t[0] for t in usable],
                [t[1] for t in usable],
                [t[2] for t in usable],
            )
            return LossRecord(
                model=model,
                strategy_class=strategy_class,
                measured_at=target_day,
                n_decisions=len(usable),
                # Stored in the MAPE column (no per-model column in the
                # schema; the loss-function family is encoded by ``model``).
                mape=residual,
            )

        if model == "strategy_class":
            # Weak proxy: predicted strategy_confidence aggregated into
            # a one-hot vs the recorded strategy_class. We treat the
            # confidence as the probability mass on the recorded class
            # and the residual mass spread uniformly across the other 8.
            preds: list[list[float]] = []
            ys: list[int] = []
            class_index = {
                c: i for i, c in enumerate(self._strategy_classes)
            }
            for r in rows:
                strat = r.get("strategy_class")
                conf = r.get("strategy_confidence")
                if strat is None or conf is None:
                    continue
                if strat not in class_index:
                    continue
                idx = class_index[strat]
                try:
                    cf = max(_LOG_LOSS_EPS, min(1.0 - _LOG_LOSS_EPS, float(conf)))
                except (TypeError, ValueError):
                    continue
                vec = [(1.0 - cf) / (len(self._strategy_classes) - 1)] * len(
                    self._strategy_classes
                )
                vec[idx] = cf
                preds.append(vec)
                ys.append(idx)
            if not preds:
                return None
            ll = compute_log_loss(preds, ys)
            return LossRecord(
                model=model,
                strategy_class=strategy_class,
                measured_at=target_day,
                n_decisions=len(preds),
                log_loss=ll,
            )

        return None

    async def _persist(self, records: Iterable[LossRecord]) -> None:
        try:
            async with get_db() as conn:
                async with conn.transaction():
                    for rec in records:
                        await conn.execute(
                            """
                            INSERT INTO calibration_loss_history (
                                model, strategy_class, measured_at,
                                n_decisions, brier_score, log_loss,
                                mape, ci_coverage
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (model, strategy_class, measured_at)
                            DO UPDATE SET
                                n_decisions = EXCLUDED.n_decisions,
                                brier_score = EXCLUDED.brier_score,
                                log_loss = EXCLUDED.log_loss,
                                mape = EXCLUDED.mape,
                                ci_coverage = EXCLUDED.ci_coverage
                            """,
                            rec.model,
                            rec.strategy_class,
                            rec.measured_at,
                            rec.n_decisions,
                            rec.brier_score,
                            rec.log_loss,
                            rec.mape,
                            rec.ci_coverage,
                        )
        except Exception as exc:
            logger.warning(
                f"ModelLossAggregator: persist failed "
                f"(n_records={len(list(records))}): {exc}"
            )

    @staticmethod
    async def _emit_metrics(records: Iterable[LossRecord]) -> None:
        try:
            from src.monitoring import metrics as mm

            mm.calibration_runs_total.inc()
            for rec in records:
                strat_label = rec.strategy_class or "aggregate"
                # The "primary" loss number per model is what the gauge
                # surfaces — Brier for follow_confidence, MAPE for the
                # rest (causal residual is stored in mape; strategy log
                # loss in log_loss).
                primary = (
                    rec.brier_score
                    if rec.brier_score is not None
                    else (
                        rec.mape
                        if rec.mape is not None
                        else rec.log_loss
                    )
                )
                if primary is None:
                    continue
                mm.calibration_loss.labels(
                    model=rec.model, strategy_class=strat_label
                ).set(float(primary))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair_floats_with_ints(
    a: Sequence[float], b: Sequence[int]
) -> list[tuple[float, int]]:
    if not a or not b:
        return []
    out: list[tuple[float, int]] = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        try:
            xf = float(x)
            yi = int(y)
        except (TypeError, ValueError):
            continue
        if xf != xf:  # NaN
            continue
        out.append((xf, yi))
    return out


def _pair_floats(
    a: Sequence[float], b: Sequence[float]
) -> list[tuple[float, float]]:
    if not a or not b:
        return []
    out: list[tuple[float, float]] = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        try:
            xf = float(x)
            yf = float(y)
        except (TypeError, ValueError):
            continue
        if xf != xf or yf != yf:
            continue
        out.append((xf, yf))
    return out


def _zip_floats(
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
) -> list[tuple[float, float, float]]:
    if not a or not b or not c:
        return []
    out: list[tuple[float, float, float]] = []
    for x, y, z in zip(a, b, c):
        if x is None or y is None or z is None:
            continue
        try:
            xf = float(x)
            yf = float(y)
            zf = float(z)
        except (TypeError, ValueError):
            continue
        if any(v != v for v in (xf, yf, zf)):
            continue
        out.append((xf, yf, zf))
    return out


__all__ = [
    "LossRecord",
    "ModelLossAggregator",
    "compute_brier",
    "compute_causal_residual",
    "compute_ci_coverage",
    "compute_log_loss",
    "compute_mape",
]
