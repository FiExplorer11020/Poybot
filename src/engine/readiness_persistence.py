"""Persistence helpers for the V1 Readiness & Control Plane."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.engine.neural_readiness import DecisionState
from src.economics.models import ECONOMIC_MODEL_VERSION, StrategyTrack


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, list):
        return value
    return []


def _float_pct(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _beliefs_for_market(market: dict[str, Any]) -> dict[str, float]:
    bars = market.get("bars") or {}
    first_position = _float_pct(bars.get("first_position_readiness_pct")) / 100.0
    state = str(market.get("state") or DecisionState.OBSERVE_ONLY.value)
    no_go = 1.0 if state.startswith("NO_GO") or state == DecisionState.INVALIDATE_SIGNAL.value else 0.0
    follow = 0.0 if no_go else first_position
    skip = max(0.0, 1.0 - max(follow, no_go))
    return {
        "belief_follow": round(follow, 6),
        "belief_fade": 0.0,
        "belief_skip": round(skip, 6),
        "belief_no_go": round(no_go, 6),
    }


async def persist_readiness_snapshot(
    conn,
    snapshot: dict[str, Any],
    *,
    trigger_event_type: str = "neural_readiness_snapshot",
    trigger_event_ref: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Upsert market readiness states and record transitions only when state changes."""
    trigger_ref_json = json.dumps(trigger_event_ref or {})
    states_persisted = 0
    transitions_inserted = 0

    for market in snapshot.get("markets") or []:
        market_id = str(market.get("market_id") or "")
        strategy_track = str(market.get("strategy_track") or StrategyTrack.LEADER_SWING.value)
        current_state = str(market.get("state") or DecisionState.OBSERVE_ONLY.value)
        if not market_id:
            continue

        previous = await conn.fetchrow(
            """
            SELECT current_state, blockers
            FROM market_belief_states
            WHERE market_id = $1 AND strategy_track = $2
            """,
            market_id,
            strategy_track,
        )
        previous_state = _row_get(previous, "current_state")
        previous_blockers = _json_list(_row_get(previous, "blockers", []))
        blockers = list(market.get("blockers") or [])
        bars = market.get("bars") or {}
        beliefs = _beliefs_for_market(market)

        await conn.execute(
            """
            INSERT INTO market_belief_states
                (market_id, strategy_track, current_state,
                 belief_follow, belief_fade, belief_skip, belief_no_go,
                 data_readiness_pct, first_position_readiness_pct,
                 belief_stability_pct, portfolio_readiness_pct, v1_go_no_go_pct,
                 expected_gross_edge_bps, expected_net_edge_bps, oscillation_score,
                 blockers, last_transition_reason, economic_model_version, updated_at)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                 $13, $14, $15, $16::jsonb, $17, $18, NOW())
            ON CONFLICT (market_id, strategy_track) DO UPDATE SET
                current_state = EXCLUDED.current_state,
                belief_follow = EXCLUDED.belief_follow,
                belief_fade = EXCLUDED.belief_fade,
                belief_skip = EXCLUDED.belief_skip,
                belief_no_go = EXCLUDED.belief_no_go,
                data_readiness_pct = EXCLUDED.data_readiness_pct,
                first_position_readiness_pct = EXCLUDED.first_position_readiness_pct,
                belief_stability_pct = EXCLUDED.belief_stability_pct,
                portfolio_readiness_pct = EXCLUDED.portfolio_readiness_pct,
                v1_go_no_go_pct = EXCLUDED.v1_go_no_go_pct,
                expected_gross_edge_bps = EXCLUDED.expected_gross_edge_bps,
                expected_net_edge_bps = EXCLUDED.expected_net_edge_bps,
                oscillation_score = EXCLUDED.oscillation_score,
                blockers = EXCLUDED.blockers,
                last_transition_reason = EXCLUDED.last_transition_reason,
                economic_model_version = EXCLUDED.economic_model_version,
                updated_at = NOW()
            """,
            market_id,
            strategy_track,
            current_state,
            beliefs["belief_follow"],
            beliefs["belief_fade"],
            beliefs["belief_skip"],
            beliefs["belief_no_go"],
            _float_pct(bars.get("data_accumulation_pct")),
            _float_pct(bars.get("first_position_readiness_pct")),
            _float_pct(bars.get("belief_stability_pct")),
            _float_pct(bars.get("portfolio_accumulation_pct")),
            _float_pct(bars.get("v1_go_no_go_pct")),
            market.get("expected_gross_edge_bps"),
            market.get("expected_net_edge_bps"),
            float(market.get("oscillation_score") or 0.0),
            json.dumps(blockers),
            market.get("last_transition_reason") or current_state.lower(),
            market.get("economic_model_version") or ECONOMIC_MODEL_VERSION,
        )
        states_persisted += 1

        if previous_state == current_state:
            continue

        await conn.execute(
            """
            INSERT INTO decision_state_transitions
                (market_id, strategy_track, from_state, to_state, reason,
                 trigger_event_type, trigger_event_ref, blockers_before,
                 blockers_after, economic_model_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10)
            """,
            market_id,
            strategy_track,
            previous_state or DecisionState.OBSERVE_ONLY.value,
            current_state,
            market.get("last_transition_reason") or current_state.lower(),
            trigger_event_type,
            trigger_ref_json,
            json.dumps(previous_blockers),
            json.dumps(blockers),
            market.get("economic_model_version") or ECONOMIC_MODEL_VERSION,
        )
        transitions_inserted += 1

    return {
        "market_belief_states": states_persisted,
        "decision_state_transitions": transitions_inserted,
    }


async def load_recent_persisted_transitions(conn, *, limit: int = 8) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT market_id, strategy_track, from_state, to_state, reason, created_at
        FROM decision_state_transitions
        ORDER BY created_at DESC
        LIMIT $1
        """,
        max(1, min(int(limit), 100)),
    )
    return [
        {
            "market_id": _row_get(row, "market_id"),
            "strategy_track": _row_get(row, "strategy_track"),
            "from_state": _row_get(row, "from_state"),
            "to_state": _row_get(row, "to_state"),
            "reason": _row_get(row, "reason"),
            "created_at": _iso(_row_get(row, "created_at")),
        }
        for row in rows
    ]
