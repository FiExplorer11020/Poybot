"""Round 13 (The Mirror) — Decision counterfactual logger.

Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.1.

Two responsibilities, both surgical hooks on existing paths:

1. **At decision time**: capture every model's prediction (Thompson
   sample magnitudes, R8 strategy class + confidence, R9 Hawkes α/μ +
   volume forecast + CI, R10 causal ATE + CI) ATOMICALLY with the
   existing ``decision_log`` insert. The confidence_engine calls
   ``record_decision_predictions(conn, decision_id, predictions)``
   inside the SAME transaction that creates the decision_log row, so
   a crash mid-insert leaves the two tables consistent.

2. **At position-close time**: fill in ``actual_pnl_usdc``,
   ``actual_followup_volume_usdc``, ``closed_at`` via
   ``fill_actual_outcomes(conn, decision_id, ...)``. The
   position_tracker calls this from its close path.

The "actual_followup_volume_usdc" measurement is the SUM of follower
trade volume in the (predicted_at, predicted_at + window) bucket for
the leader/pool tied to this decision. The loss aggregator's MAPE /
CI-coverage computations for the volume_forecast model rely on this
column being populated; if it's NULL the aggregator silently skips
that decision.

This module deliberately avoids importing anything from
src.engine.* / src.observer.* to keep the call graph one-directional
(engine/observer call into calibration, not the other way around).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.database.connection import get_db


@dataclass
class DecisionPrediction:
    """Per-decision per-model snapshot.

    Every field is optional — when a model didn't run for a given
    decision (e.g. the causal flag was off) the field stays None and
    that model's loss is excluded from the nightly aggregation for
    that row. The dataclass keeps the call sites readable; the SQL
    layer maps None -> NULL.
    """

    follow_confidence: Optional[float] = None
    fade_confidence: Optional[float] = None
    strategy_class: Optional[str] = None
    strategy_confidence: Optional[float] = None
    hawkes_alpha_mu: Optional[float] = None
    volume_forecast_usdc: Optional[float] = None
    volume_forecast_ci_low: Optional[float] = None
    volume_forecast_ci_high: Optional[float] = None
    causal_ate: Optional[float] = None
    causal_ate_ci_low: Optional[float] = None
    causal_ate_ci_high: Optional[float] = None
    predicted_at: Optional[datetime] = None

    @classmethod
    def from_decision_context(cls, decision: Any) -> "DecisionPrediction":
        """Best-effort extraction from a ``Decision`` dataclass + its
        attached ``trade_context``. We avoid importing the Decision
        type to keep the module decoupled — duck typing is enough.
        """
        ctx = getattr(decision, "trade_context", None) or {}
        if not isinstance(ctx, dict):
            ctx = {}
        causal = ctx.get("causal_gate") or {}
        strat = ctx.get("strategy_weights_applied") or {}
        forecast = ctx.get("volume_forecast") or {}
        out = cls(
            follow_confidence=_safe_float(getattr(decision, "thompson_follow", None)),
            fade_confidence=_safe_float(getattr(decision, "thompson_fade", None)),
            strategy_class=_safe_str(
                ctx.get("wallet_strategy") or strat.get("primary_strategy")
            ),
            strategy_confidence=_safe_float(
                ctx.get("strategy_confidence")
                or strat.get("primary_strategy_confidence")
            ),
            hawkes_alpha_mu=_safe_float(
                causal.get("hawkes_alpha_mu")
                or ctx.get("hawkes_alpha_mu")
            ),
            volume_forecast_usdc=_safe_float(forecast.get("total_volume_usdc")),
            volume_forecast_ci_low=_safe_float(forecast.get("ci_low")),
            volume_forecast_ci_high=_safe_float(forecast.get("ci_high")),
            causal_ate=_safe_float(causal.get("ate")),
            causal_ate_ci_low=_safe_float(causal.get("ci_low")),
            causal_ate_ci_high=_safe_float(causal.get("ci_high")),
            predicted_at=datetime.now(tz=timezone.utc),
        )
        return out


class DecisionPredictionLogger:
    """Stateless façade for both call sites.

    The class itself holds no state — it's a namespace for the two
    public functions so the engine + observer can inject mocks in
    tests cleanly. Module-level convenience wrappers (below) match the
    spec's "record_decision_predictions" / "fill_actual_outcomes"
    naming for the hot path.
    """

    @staticmethod
    async def record(
        conn: Any,
        decision_id: int,
        predictions: DecisionPrediction,
    ) -> None:
        """Insert one decision_predictions row atomically.

        ``conn`` is the asyncpg connection currently holding the
        transaction that's writing to ``decision_log``. By taking it
        as a parameter we guarantee the two writes share a transaction
        — the caller controls commit/rollback boundaries.
        """
        if decision_id is None or decision_id <= 0:
            return
        await conn.execute(
            """
            INSERT INTO decision_predictions (
                decision_id, predicted_at,
                follow_confidence, fade_confidence,
                strategy_class, strategy_confidence,
                hawkes_alpha_mu,
                volume_forecast_usdc,
                volume_forecast_ci_low, volume_forecast_ci_high,
                causal_ate, causal_ate_ci_low, causal_ate_ci_high
            )
            VALUES (
                $1, COALESCE($2, NOW()),
                $3, $4,
                $5, $6,
                $7,
                $8, $9, $10,
                $11, $12, $13
            )
            ON CONFLICT (decision_id) DO NOTHING
            """,
            int(decision_id),
            predictions.predicted_at,
            predictions.follow_confidence,
            predictions.fade_confidence,
            predictions.strategy_class,
            predictions.strategy_confidence,
            predictions.hawkes_alpha_mu,
            predictions.volume_forecast_usdc,
            predictions.volume_forecast_ci_low,
            predictions.volume_forecast_ci_high,
            predictions.causal_ate,
            predictions.causal_ate_ci_low,
            predictions.causal_ate_ci_high,
        )

    @staticmethod
    async def fill_outcomes(
        conn: Any,
        decision_id: int,
        pnl_usdc: float | None,
        followup_volume_usdc: float | None,
        closed_at: datetime | None,
    ) -> None:
        """Backfill the outcome columns on an existing prediction row.

        Called from the position_tracker close path. If no prediction
        row exists for the given decision_id (i.e. the position pre-
        dates R13 deployment), the UPDATE is a silent no-op.
        """
        if decision_id is None or decision_id <= 0:
            return
        await conn.execute(
            """
            UPDATE decision_predictions
               SET actual_pnl_usdc = COALESCE($2, actual_pnl_usdc),
                   actual_followup_volume_usdc =
                       COALESCE($3, actual_followup_volume_usdc),
                   closed_at = COALESCE($4, closed_at)
             WHERE decision_id = $1
            """,
            int(decision_id),
            _safe_float(pnl_usdc),
            _safe_float(followup_volume_usdc),
            closed_at,
        )


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


async def record_decision_predictions(
    conn: Any,
    decision_id: int,
    predictions: DecisionPrediction,
) -> None:
    """Spec § 3.1 entry point. See DecisionPredictionLogger.record."""
    await DecisionPredictionLogger.record(conn, decision_id, predictions)


async def fill_actual_outcomes(
    conn: Any,
    decision_id: int,
    pnl_usdc: float | None,
    followup_volume_usdc: float | None,
    closed_at: datetime | None,
) -> None:
    """Spec § 3.1 entry point. See DecisionPredictionLogger.fill_outcomes."""
    await DecisionPredictionLogger.fill_outcomes(
        conn, decision_id, pnl_usdc, followup_volume_usdc, closed_at
    )


async def fill_actual_outcomes_for_position(
    wallet_address: str,
    market_id: str,
    open_time: datetime,
    pnl_usdc: float | None,
    followup_volume_usdc: float | None,
    closed_at: datetime | None,
) -> int:
    """Convenience hook for the observer/position_tracker close path.

    The position_tracker doesn't have a decision_id in hand at close
    time — it has (wallet, market, open_time). We resolve the
    matching decision_log row (most recent before open_time, same
    wallet+market) and update its prediction row.

    Returns the decision_id that was updated, or 0 if no matching
    decision was found (e.g. close before R13 deployment).
    """
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT id
                FROM decision_log
                WHERE leader_wallet = $1
                  AND market_id = $2
                  AND time <= $3
                  AND action IN ('follow', 'fade')
                ORDER BY time DESC
                LIMIT 1
                """,
                wallet_address,
                market_id,
                open_time,
            )
            if not row:
                return 0
            decision_id = int(row["id"])
            await fill_actual_outcomes(
                conn,
                decision_id,
                pnl_usdc,
                followup_volume_usdc,
                closed_at,
            )
            return decision_id
    except Exception as exc:
        logger.debug(
            f"fill_actual_outcomes_for_position failed for "
            f"{wallet_address[:10]}/{market_id}: {exc}"
        )
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


__all__ = [
    "DecisionPrediction",
    "DecisionPredictionLogger",
    "fill_actual_outcomes",
    "fill_actual_outcomes_for_position",
    "record_decision_predictions",
]
