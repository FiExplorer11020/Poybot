from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class HistoricalEvent:
    event_ts: datetime
    observed_ts: datetime
    kind: str
    payload: dict[str, Any]


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def causal_cutoff(decision_ts: datetime, observation_lag_s: float) -> datetime:
    return _aware_utc(decision_ts) - timedelta(seconds=observation_lag_s)


def visible_events_at(
    events: Iterable[HistoricalEvent],
    *,
    decision_ts: datetime,
    observation_lag_s: float,
) -> list[HistoricalEvent]:
    """Return only events observable at the decision timestamp under a lag model."""
    cutoff = causal_cutoff(decision_ts, observation_lag_s)
    visible = [
        event
        for event in events
        if _aware_utc(event.event_ts) <= cutoff and _aware_utc(event.observed_ts) <= cutoff
    ]
    return sorted(visible, key=lambda event: (event.event_ts, event.observed_ts, event.kind))


def enforce_no_lookahead(
    *,
    decision_ts: datetime,
    observation_lag_s: float,
    feature_timestamps: Iterable[datetime],
) -> None:
    cutoff = causal_cutoff(decision_ts, observation_lag_s)
    for feature_ts in feature_timestamps:
        if _aware_utc(feature_ts) > cutoff:
            raise ValueError(
                "lookahead feature timestamp after causal cutoff: "
                f"feature_ts={feature_ts.isoformat()} cutoff={cutoff.isoformat()}"
            )
