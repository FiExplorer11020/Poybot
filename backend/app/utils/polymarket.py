from __future__ import annotations

import json
from typing import Any


def parse_json_list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def first_event_id_from_market(payload: dict[str, Any]) -> str | None:
    event_id = payload.get("eventId") or payload.get("event_id")
    if event_id:
        return str(event_id)

    events = parse_json_list_field(payload.get("events"))
    if not events:
        return None

    first_event = events[0]
    if not isinstance(first_event, dict):
        return None

    nested_event_id = first_event.get("id")
    return str(nested_event_id) if nested_event_id else None
