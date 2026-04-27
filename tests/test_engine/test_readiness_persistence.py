from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.engine.neural_readiness import ReadinessInputs, build_neural_readiness_snapshot
from src.engine.readiness_persistence import (
    load_recent_persisted_transitions,
    persist_readiness_snapshot,
)


@pytest.mark.asyncio
async def test_persist_readiness_snapshot_upserts_state_and_records_first_transition():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": None,
                "fee_snapshot_coverage_pct": 100.0,
                "token_map_coverage_pct": 50.0,
            },
            activation=[
                {
                    "wallet_address": "0xabc",
                    "follow_readiness_pct": 72,
                    "fade_readiness_pct": 12,
                }
            ],
            risk={"open_count": 0, "drawdown_pct": 0},
            ml={},
            now=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    result = await persist_readiness_snapshot(conn, snapshot)

    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO market_belief_states" in sql for sql in sql_calls)
    assert any("INSERT INTO decision_state_transitions" in sql for sql in sql_calls)
    assert result["market_belief_states"] == 1
    assert result["decision_state_transitions"] == 1


@pytest.mark.asyncio
async def test_persist_readiness_snapshot_does_not_duplicate_unchanged_transition():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": None,
                "fee_snapshot_coverage_pct": 100.0,
                "token_map_coverage_pct": 50.0,
            },
            activation=[{"wallet_address": "0xabc", "follow_readiness_pct": 72}],
            risk={"open_count": 0, "drawdown_pct": 0},
            ml={},
        )
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"current_state": "NO_GO_DATA", "blockers": []})
    conn.execute = AsyncMock()

    result = await persist_readiness_snapshot(conn, snapshot)

    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO market_belief_states" in sql for sql in sql_calls)
    assert not any("INSERT INTO decision_state_transitions" in sql for sql in sql_calls)
    assert result["market_belief_states"] == 1
    assert result["decision_state_transitions"] == 0


@pytest.mark.asyncio
async def test_load_recent_persisted_transitions_maps_rows():
    created = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "market_id": "mkt",
                "strategy_track": "leader_swing",
                "from_state": "OBSERVE_ONLY",
                "to_state": "NO_GO_DATA",
                "reason": "missing_book_freshness",
                "created_at": created,
            }
        ]
    )

    rows = await load_recent_persisted_transitions(conn, limit=5)

    assert rows == [
        {
            "market_id": "mkt",
            "strategy_track": "leader_swing",
            "from_state": "OBSERVE_ONLY",
            "to_state": "NO_GO_DATA",
            "reason": "missing_book_freshness",
            "created_at": created.isoformat(),
        }
    ]
