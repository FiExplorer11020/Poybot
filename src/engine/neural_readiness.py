"""
Neural Readiness Layer V1.

This module converts existing V1 health/risk/activation signals into a stable
dashboard contract. It is intentionally deterministic: the first version is a
control plane and gate explainer, not a hidden trading model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.economics.models import ECONOMIC_MODEL_VERSION, StrategyTrack


class DecisionState(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    CANDIDATE_SIGNAL = "CANDIDATE_SIGNAL"
    PROBE_PAPER = "PROBE_PAPER"
    EXPAND_PAPER = "EXPAND_PAPER"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    INVALIDATE_SIGNAL = "INVALIDATE_SIGNAL"
    NO_GO_DATA = "NO_GO_DATA"
    NO_GO_ECONOMICS = "NO_GO_ECONOMICS"
    NO_GO_RISK = "NO_GO_RISK"
    TRACK_DISABLED = "TRACK_DISABLED"
    V1_GO_CANDIDATE = "V1_GO_CANDIDATE"


@dataclass(frozen=True)
class ReadinessInputs:
    health: dict[str, Any] = field(default_factory=dict)
    activation: list[dict[str, Any]] = field(default_factory=list)
    risk: dict[str, Any] = field(default_factory=dict)
    ml: dict[str, Any] = field(default_factory=dict)
    now: datetime | None = None


def _pct(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_pct(value: float) -> int:
    return int(round(max(0.0, min(100.0, value))))


def _coverage_score(value: Any) -> tuple[float, str | None]:
    if value is None:
        return 0.0, None
    coverage = max(0.0, min(100.0, _pct(value)))
    return coverage, None


def _book_score(book_age_p95_s: Any) -> tuple[float, str | None]:
    if book_age_p95_s is None:
        return 0.0, "missing_book_freshness"
    age = _pct(book_age_p95_s, default=999.0)
    if age <= 2:
        return 100.0, None
    if age <= 5:
        return 85.0, None
    if age <= 10:
        return 65.0, "book_freshness_warning"
    if age <= 30:
        return 35.0, "stale_book"
    return 10.0, "stale_book"


def _max_activation_pct(activation: list[dict[str, Any]]) -> int:
    if not activation:
        return 0
    values: list[float] = []
    for row in activation:
        values.append(_pct(row.get("follow_readiness_pct")))
        values.append(_pct(row.get("fade_readiness_pct")))
    return _clamp_pct(max(values or [0.0]))


def _ml_score(ml: dict[str, Any]) -> int:
    follow = ml.get("follow") or {}
    fade = ml.get("fade") or {}
    samples = _pct(follow.get("samples")) + _pct(fade.get("samples"))
    coverage = min(100.0, samples * 2.5)
    win_rate = max(_pct(follow.get("win_rate")), _pct(fade.get("win_rate")))
    quality = max(0.0, min(100.0, win_rate * 100.0))
    if samples <= 0:
        return 0
    return _clamp_pct((coverage * 0.45) + (quality * 0.55))


def _global_blockers(
    *,
    fee_coverage: Any,
    token_coverage: Any,
    book_reason: str | None,
    drawdown_pct: float,
) -> list[str]:
    blockers: list[str] = []
    if fee_coverage is None:
        blockers.append("missing_fee_snapshot")
    elif _pct(fee_coverage) < 95:
        blockers.append("low_fee_snapshot_coverage")

    if token_coverage is None:
        blockers.append("missing_token_map")
    elif _pct(token_coverage) < 95:
        blockers.append("low_token_map_coverage")

    if book_reason:
        blockers.append(book_reason)

    if drawdown_pct >= 5:
        blockers.append("risk_drawdown_high")

    return blockers


def _first_position_readiness(
    *,
    activation_pct: int,
    fee_score: float,
    token_score: float,
    book_score: float,
    risk_score: float,
    missing_hard_inputs: bool,
) -> int:
    readiness = (
        activation_pct * 0.38
        + fee_score * 0.18
        + token_score * 0.18
        + book_score * 0.14
        + risk_score * 0.12
    )
    if missing_hard_inputs:
        readiness = min(readiness, 50.0)
    return _clamp_pct(readiness)


def _portfolio_readiness(first_position_pct: int, risk: dict[str, Any], ml: dict[str, Any]) -> int:
    drawdown_pct = _pct(risk.get("drawdown_pct"))
    open_count = _pct(risk.get("open_count"))
    learned = _ml_score(ml)
    risk_score = max(0.0, 100.0 - drawdown_pct * 12.0 - open_count * 4.0)
    readiness = min(first_position_pct, (risk_score * 0.55) + (learned * 0.45))
    if drawdown_pct >= 5:
        readiness = min(readiness, 25.0)
    return _clamp_pct(readiness)


def _belief_stability(book_score: float, risk: dict[str, Any], ml: dict[str, Any]) -> int:
    drawdown_pct = _pct(risk.get("drawdown_pct"))
    drift_alerts = _pct(ml.get("drift_alerts"))
    stability = book_score * 0.55 + max(0.0, 100.0 - drawdown_pct * 10.0) * 0.3
    stability += max(0.0, 100.0 - drift_alerts * 25.0) * 0.15
    return _clamp_pct(stability)


def _v1_go_no_go_pct(
    *,
    data_accumulation_pct: int,
    portfolio_pct: int,
    ml: dict[str, Any],
    blockers: list[str],
) -> int:
    hard_blocked = any(
        reason
        in {
            "missing_fee_snapshot",
            "missing_token_map",
            "missing_book_freshness",
            "stale_book",
            "risk_drawdown_high",
        }
        for reason in blockers
    )
    score = data_accumulation_pct * 0.35 + portfolio_pct * 0.35 + _ml_score(ml) * 0.30
    if hard_blocked:
        score = min(score, 35.0)
    return _clamp_pct(score)


def _state_for_candidate(readiness_pct: int, blockers: list[str]) -> DecisionState:
    hard_data = {
        "missing_fee_snapshot",
        "missing_token_map",
        "missing_book_freshness",
        "stale_book",
        "low_fee_snapshot_coverage",
        "low_token_map_coverage",
    }
    if any(b in hard_data for b in blockers):
        return DecisionState.NO_GO_DATA
    if "risk_drawdown_high" in blockers:
        return DecisionState.NO_GO_RISK
    if readiness_pct >= 90:
        return DecisionState.PROBE_PAPER
    if readiness_pct >= 50:
        return DecisionState.CANDIDATE_SIGNAL
    return DecisionState.OBSERVE_ONLY


def _candidate_markets(
    activation: list[dict[str, Any]],
    global_bars: dict[str, int],
    global_blockers: list[str],
) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for row in activation[:10]:
        wallet = str(row.get("wallet_address") or row.get("leader_wallet") or "unknown")
        follow_pct = _clamp_pct(_pct(row.get("follow_readiness_pct")))
        fade_pct = _clamp_pct(_pct(row.get("fade_readiness_pct")))
        readiness = min(max(follow_pct, fade_pct), global_bars["first_position_readiness_pct"])
        state = _state_for_candidate(readiness, global_blockers)
        market_id = str(row.get("market_id") or f"leader:{wallet}")
        markets.append(
            {
                "market_id": market_id,
                "question": row.get("question") or f"Leader readiness candidate {wallet}",
                "strategy_track": StrategyTrack.LEADER_SWING.value,
                "state": state.value,
                "bars": {
                    "data_accumulation_pct": global_bars["data_accumulation_pct"],
                    "first_position_readiness_pct": readiness,
                    "belief_stability_pct": global_bars["belief_stability_pct"],
                    "portfolio_accumulation_pct": global_bars["portfolio_accumulation_pct"],
                    "v1_go_no_go_pct": 0,
                },
                "blockers": list(global_blockers),
                "leader_wallet": wallet,
                "last_transition_reason": (
                    "leader_signal_detected"
                    if state == DecisionState.CANDIDATE_SIGNAL
                    else state.value.lower()
                ),
                "economic_model_version": ECONOMIC_MODEL_VERSION,
            }
        )
    return markets


def _state_counts(markets: list[dict[str, Any]], blockers: list[str]) -> dict[str, int]:
    counts = {state.value: 0 for state in DecisionState}
    if not markets:
        state = DecisionState.NO_GO_DATA if blockers else DecisionState.OBSERVE_ONLY
        counts[state.value] = 1
        return counts
    for market in markets:
        state = str(market.get("state") or DecisionState.OBSERVE_ONLY.value)
        counts[state] = counts.get(state, 0) + 1
    return counts


def build_neural_readiness_snapshot(inputs: ReadinessInputs) -> dict[str, Any]:
    now = inputs.now or datetime.now(tz=timezone.utc)
    health = inputs.health or {}
    risk = inputs.risk or {}
    ml = inputs.ml or {}
    activation = inputs.activation or []

    fee_score, _ = _coverage_score(health.get("fee_snapshot_coverage_pct"))
    token_score, _ = _coverage_score(health.get("token_map_coverage_pct"))
    book_quality_score, book_reason = _book_score(health.get("book_age_p95_s"))
    activation_pct = _max_activation_pct(activation)
    drawdown_pct = _pct(risk.get("drawdown_pct"))
    risk_score = max(0.0, 100.0 - drawdown_pct * 12.0 - _pct(risk.get("open_count")) * 4.0)

    blockers = _global_blockers(
        fee_coverage=health.get("fee_snapshot_coverage_pct"),
        token_coverage=health.get("token_map_coverage_pct"),
        book_reason=book_reason,
        drawdown_pct=drawdown_pct,
    )
    missing_hard_inputs = any(
        reason in blockers
        for reason in {
            "missing_fee_snapshot",
            "missing_token_map",
            "missing_book_freshness",
            "stale_book",
        }
    )

    data_accumulation = _clamp_pct(
        fee_score * 0.24 + token_score * 0.24 + book_quality_score * 0.28 + activation_pct * 0.24
    )
    first_position = _first_position_readiness(
        activation_pct=activation_pct,
        fee_score=fee_score,
        token_score=token_score,
        book_score=book_quality_score,
        risk_score=risk_score,
        missing_hard_inputs=missing_hard_inputs,
    )
    belief_stability = _belief_stability(book_quality_score, risk, ml)
    portfolio = _portfolio_readiness(first_position, risk, ml)
    v1_go_no_go = _v1_go_no_go_pct(
        data_accumulation_pct=data_accumulation,
        portfolio_pct=portfolio,
        ml=ml,
        blockers=blockers,
    )
    global_bars = {
        "data_accumulation_pct": data_accumulation,
        "first_position_readiness_pct": first_position,
        "belief_stability_pct": belief_stability,
        "portfolio_accumulation_pct": portfolio,
        "v1_go_no_go_pct": v1_go_no_go,
    }

    markets = _candidate_markets(activation, global_bars, blockers)
    leader_candidates = [m["market_id"] for m in markets if m["strategy_track"] == "leader_swing"][:5]

    micro_data = data_accumulation
    micro_blockers: list[str] = []
    if book_reason:
        micro_blockers.append(book_reason)
        micro_data = min(micro_data, 40)

    tracks = {
        StrategyTrack.LEADER_SWING.value: {
            "bars": dict(global_bars),
            "blockers": list(blockers),
            "top_candidates": leader_candidates,
        },
        StrategyTrack.MICRO_REACTIVE.value: {
            "bars": {
                "data_accumulation_pct": _clamp_pct(micro_data),
                "first_position_readiness_pct": 0 if book_reason else min(first_position, 45),
                "belief_stability_pct": 0 if book_reason else belief_stability,
                "portfolio_accumulation_pct": 0,
                "v1_go_no_go_pct": 0,
            },
            "blockers": micro_blockers or ["micro_reactive_capture_not_proven"],
            "top_candidates": [],
        },
    }

    transitions = []
    for market in markets[:8]:
        transitions.append(
            {
                "market_id": market["market_id"],
                "strategy_track": market["strategy_track"],
                "from_state": DecisionState.OBSERVE_ONLY.value,
                "to_state": market["state"],
                "reason": market["last_transition_reason"],
                "created_at": now.isoformat(),
            }
        )

    return {
        "global": {
            "bars": global_bars,
            "blockers": blockers,
            "state_counts": _state_counts(markets, blockers),
            "data_accumulation": {
                "counts": dict(health.get("data_accumulation_counts") or {}),
                "fee_snapshot_coverage_source": health.get("fee_snapshot_coverage_source"),
                "book_age_p95_s": health.get("book_age_p95_s"),
                "fee_snapshot_coverage_pct": health.get("fee_snapshot_coverage_pct"),
                "token_map_coverage_pct": health.get("token_map_coverage_pct"),
            },
            "economic_model_version": ECONOMIC_MODEL_VERSION,
            "updated_at": now.isoformat(),
        },
        "tracks": tracks,
        "markets": markets,
        "transitions": transitions,
    }
