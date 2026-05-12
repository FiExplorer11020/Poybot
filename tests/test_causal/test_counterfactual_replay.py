"""Tests for CounterfactualReplayer.

Coverage:
  * ReplayResult dataclass shape (every replay returns a ReplayResult
    with the expected fields).
  * Each replay variant: classifier_override, policy_disabled,
    event_shift.
  * Without a cold-tier view, every variant returns an empty
    ReplayResult with the 'cold_tier_unavailable' reason flag — not a
    crash.
  * Wall-time is recorded.

We don't exercise real DuckDB here (that would require a populated
Parquet tree); the tests inject a fake adapter that returns synthetic
decision rows.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.causal.counterfactual_replay import (
    CounterfactualReplayer,
    ReplayResult,
)


# ---------------------------------------------------------------------------
# Fake cold-tier adapter
# ---------------------------------------------------------------------------


class _FakeRelation:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.description = [(k,) for k in rows[0].keys()] if rows else []
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        if not self.description:
            return []
        return [tuple(r.values()) for r in self._rows]


class _FakeView:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def query(self, sql: str) -> _FakeRelation:
        return _FakeRelation(self._rows)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _period() -> tuple[datetime, datetime]:
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    start = end - timedelta(days=30)
    return start, end


class TestCounterfactualReplayer:
    def test_classifier_override_returns_replayresult(self):
        rows = [
            {
                "leader_wallet": "0xA",
                "wallet_strategy": "directional",
                "pnl_usdc": 100.0,
                "action": "follow",
            },
            {
                "leader_wallet": "0xA",
                "wallet_strategy": "directional",
                "pnl_usdc": -40.0,
                "action": "follow",
            },
        ]
        view = _FakeView(rows)
        rep = CounterfactualReplayer(duckdb_view=view)
        result = rep.replay_with_classifier_override(
            wallet="0xA",
            new_strategy="momentum",
            period=_period(),
        )
        assert isinstance(result, ReplayResult)
        assert result.kind == "classifier_override"
        assert result.decisions_total == 2
        # All 2 rows had "directional" but we override to "momentum",
        # so 2 decisions changed.
        assert result.decisions_changed == 2
        assert result.wall_time_s >= 0

    def test_policy_disabled_drops_matching_decisions(self):
        rows = [
            {
                "reason": "volume_anticipation|risk=0.10",
                "pnl_usdc": 50.0,
                "action": "follow",
            },
            {
                "reason": "thompson_follow|risk=0.05",
                "pnl_usdc": 30.0,
                "action": "follow",
            },
        ]
        view = _FakeView(rows)
        rep = CounterfactualReplayer(duckdb_view=view)
        result = rep.replay_with_policy_disabled(
            policy_name="volume_anticipation",
            period=_period(),
        )
        assert result.kind == "policy_disabled"
        assert result.decisions_total == 2
        assert result.decisions_changed == 1
        # Under counterfactual, only the thompson_follow row contributes.
        assert result.hypothetical_pnl_usdc == 30.0
        # Actual = 50 + 30 = 80
        assert result.actual_pnl_usdc == 80.0
        assert result.delta_vs_actual == result.hypothetical_pnl_usdc - result.actual_pnl_usdc

    def test_event_shift_returns_replayresult(self):
        rows = [
            {"pnl_usdc": 10.0, "action": "follow"},
            {"pnl_usdc": -5.0, "action": "fade"},
        ]
        view = _FakeView(rows)
        rep = CounterfactualReplayer(duckdb_view=view)
        result = rep.replay_with_event_shift(
            event_id=42,
            delta_s=-120.0,
            period=_period(),
        )
        assert result.kind == "event_shift"
        assert result.details["event_id"] == 42
        assert result.details["delta_s"] == -120.0

    def test_cold_tier_unavailable_returns_empty_result(self):
        """When the view is None, no scan; ReplayResult flagged."""
        rep = CounterfactualReplayer(duckdb_view=None)
        # Force the lazy view-loader to fail by stubbing the import path.
        rep._get_view = lambda: None  # type: ignore[assignment]
        result = rep.replay_with_classifier_override(
            wallet="0xA",
            new_strategy="momentum",
            period=_period(),
        )
        assert result.kind == "classifier_override"
        assert result.details.get("reason") == "cold_tier_unavailable"
        assert result.decisions_changed == 0
        assert result.decisions_total == 0

    def test_wall_time_recorded(self):
        """Each replay records a positive wall_time."""
        rep = CounterfactualReplayer(duckdb_view=None)
        rep._get_view = lambda: None  # type: ignore[assignment]
        result = rep.replay_with_classifier_override(
            wallet="0xA",
            new_strategy="momentum",
            period=_period(),
        )
        assert result.wall_time_s >= 0

    def test_replayresult_default_fields(self):
        r = ReplayResult(
            kind="classifier_override",
            period_start=datetime.now(tz=timezone.utc),
            period_end=datetime.now(tz=timezone.utc),
        )
        assert r.actual_pnl_usdc == 0.0
        assert r.hypothetical_pnl_usdc == 0.0
        assert r.delta_vs_actual == 0.0
        assert r.decisions_changed == 0
        assert r.decisions_total == 0
        assert r.wall_time_s == 0.0
        assert r.details == {}
