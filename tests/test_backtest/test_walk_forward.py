from datetime import datetime, timedelta, timezone

import pytest

from src.backtest.walk_forward import HistoricalEvent, enforce_no_lookahead, visible_events_at


def test_visible_events_at_excludes_events_after_observation_cutoff():
    t0 = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        HistoricalEvent(
            event_ts=t0 - timedelta(seconds=180),
            observed_ts=t0 - timedelta(seconds=170),
            kind="trade",
            payload={"id": "old"},
        ),
        HistoricalEvent(
            event_ts=t0 - timedelta(seconds=10),
            observed_ts=t0 - timedelta(seconds=5),
            kind="trade",
            payload={"id": "future"},
        ),
    ]

    visible = visible_events_at(events, decision_ts=t0, observation_lag_s=60)

    assert [event.payload["id"] for event in visible] == ["old"]


def test_enforce_no_lookahead_rejects_feature_timestamp_after_cutoff():
    decision_ts = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="lookahead"):
        enforce_no_lookahead(
            decision_ts=decision_ts,
            observation_lag_s=120,
            feature_timestamps=[
                decision_ts - timedelta(seconds=180),
                decision_ts - timedelta(seconds=30),
            ],
        )


def test_enforce_no_lookahead_accepts_only_causal_features():
    decision_ts = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    enforce_no_lookahead(
        decision_ts=decision_ts,
        observation_lag_s=120,
        feature_timestamps=[
            decision_ts - timedelta(seconds=180),
            decision_ts - timedelta(seconds=121),
        ],
    )
