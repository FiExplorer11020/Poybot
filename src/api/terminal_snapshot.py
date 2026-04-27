from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.config import settings

_LOGURU_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\|\s+"
    r"(?P<level>[A-Z]+)\s+\|\s+(?P<origin>.+?)\s+-\s+(?P<message>.+)$"
)

_READINESS_EXECUTABLE_STATES = {"CANDIDATE_SIGNAL", "PROBE_PAPER", "V1_GO_CANDIDATE"}
_READINESS_OPEN_STATES = {"CANDIDATE_SIGNAL", "PROBE_PAPER", "V1_GO_CANDIDATE", "HOLD"}
_LIVE_MARKET_FRESHNESS_MS = 15000


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    text = str(value).strip()
    return text or None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    if not items:
        return None
    return sum(items) / len(items)


def parse_loguru_line(line: str) -> dict[str, Any] | None:
    match = _LOGURU_RE.match((line or "").strip())
    if not match:
        return None

    timestamp = match.group("timestamp")
    origin = match.group("origin").strip()
    parts = origin.split(":")[0].split(".")
    if parts and parts[0] == "src":
        parts = parts[1:]
    category = ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)

    dt = _parse_dt(timestamp.replace(" ", "T") + "+00:00")
    return {
        "timestamp": _safe_iso(dt) or timestamp,
        "level": match.group("level").strip(),
        "category": category or "system",
        "message": match.group("message").strip(),
    }


def load_recent_log_entries(paths: Iterable[Path], limit: int = 120) -> list[dict[str, Any]]:
    entries: deque[dict[str, Any]] = deque(maxlen=max(1, int(limit)))
    for path in paths:
        try:
            if not path.exists() or not path.is_file():
                continue
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    parsed = parse_loguru_line(line.rstrip("\n"))
                    if parsed is not None:
                        entries.append(parsed)
        except OSError:
            continue

    return list(reversed(entries))[:limit]


def _map_decision_action(action: str | None) -> str:
    action_text = str(action or "").strip().lower()
    if action_text in {"follow", "fade", "open"}:
        return "open"
    if action_text in {"close", "exit"}:
        return "close"
    if action_text in {"reduce", "trim"}:
        return "reduce"
    return "skip"


def _map_readiness_action(state: str | None) -> str:
    state_text = str(state or "").strip().upper()
    if state_text in _READINESS_OPEN_STATES:
        return "open"
    if state_text == "REDUCE":
        return "reduce"
    if state_text == "INVALIDATE_SIGNAL":
        return "close"
    return "skip"


def _bot_status(health: dict[str, Any]) -> str:
    if health.get("db") and health.get("redis") and health.get("websocket_connected", False):
        return "running"
    return "stopped"


def _signal_strength(row: dict[str, Any]) -> float:
    freshness_ms = max(0.0, _to_float(row.get("freshness_ms"), 999999.0) or 999999.0)
    spread_bps = max(0.0, _to_float(row.get("spread_bps"), 99999.0) or 99999.0)
    activity = max(0.0, _to_float(row.get("messages_last_minute"), 0.0) or 0.0)
    observations = max(0.0, _to_float(row.get("observations"), 0.0) or 0.0)
    freshness_score = max(0.0, 1.0 - min(freshness_ms / 10000.0, 1.0))
    spread_score = max(0.0, 1.0 - min(spread_bps / 200.0, 1.0))
    activity_score = min(activity / 10.0, 1.0)
    observation_score = min(observations / 20.0, 1.0)
    signal = freshness_score * 0.35 + spread_score * 0.30 + activity_score * 0.20 + observation_score * 0.15
    return round(max(0.0, min(signal, 1.0)), 4)


def _market_decision_action(row: dict[str, Any], signal_strength: float) -> str:
    freshness_ms = _to_float(row.get("freshness_ms"), 999999.0) or 999999.0
    spread = _to_float(row.get("spread"), 999.0)
    observations = _to_int(row.get("observations"), 0)
    if signal_strength >= 0.75 and freshness_ms <= _LIVE_MARKET_FRESHNESS_MS and (spread is None or spread <= 0.04) and observations > 0:
        return "open"
    if signal_strength >= 0.55 and observations > 0:
        return "reduce"
    return "skip"


def _normalize_market_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        signal_strength = _signal_strength(row)
        decision_action = row.get("decision_action") or _market_decision_action(row, signal_strength)
        normalized.append(
            {
                "market_id": row.get("market_id"),
                "token_id": row.get("token_id"),
                "title": row.get("title") or row.get("question") or row.get("market_question") or "Unknown market",
                "direction": row.get("direction"),
                "mid_price": _to_float(row.get("mid_price")),
                "spread": _to_float(row.get("spread")),
                "spread_bps": _to_float(row.get("spread_bps")),
                "best_bid": _to_float(row.get("best_bid")),
                "best_ask": _to_float(row.get("best_ask")),
                "freshness_ms": _to_int(row.get("freshness_ms"), 0),
                "source_delay_ms": _to_int(row.get("source_delay_ms"), 0),
                "observations": _to_int(row.get("observations"), 0),
                "messages_last_minute": _to_int(row.get("messages_last_minute"), 0),
                "detected": bool(row.get("detected", False)),
                "quote_source": row.get("quote_source") or "book_quality_snapshots",
                "signal_strength": signal_strength,
                "decision_action": decision_action,
                "expected_edge": _to_float(row.get("expected_edge")),
                "entry_threshold": _to_float(row.get("entry_threshold")),
                "z_score": _to_float(row.get("z_score")),
                "regime": row.get("regime") or row.get("market_type") or row.get("category") or "unknown",
            }
        )
    normalized.sort(
        key=lambda item: (
            item["decision_action"] == "open",
            item["signal_strength"],
            item["messages_last_minute"],
            item["observations"],
        ),
        reverse=True,
    )
    return normalized


def _build_recent_trades(
    positions: dict[str, Any],
    observed_trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []

    for row in positions.get("closed", []) or []:
        timestamp = row.get("closed_at") or row.get("opened_at")
        trades.append(
            {
                "id": row.get("id"),
                "timestamp": timestamp,
                "market_title": row.get("question") or row.get("market_title") or row.get("market_question"),
                "side": "BUY" if str(row.get("direction", "")).lower() == "yes" else "SELL",
                "price": _to_float(row.get("exit_price"), _to_float(row.get("entry_price"))),
                "notional": _to_float(row.get("size_usdc")),
                "fees": _to_float(row.get("fee_paid_usdc")),
                "pnl_abs": _to_float(row.get("pnl_usdc")),
                "pnl_pct": (_to_float(row.get("pnl_pct")) or 0.0) / 100.0 if row.get("pnl_pct") is not None else None,
                "execution_mode": "paper",
                "status": row.get("status") or "closed",
            }
        )

    for row in observed_trades or []:
        trades.append(
            {
                "id": row.get("id"),
                "timestamp": row.get("timestamp") or row.get("time"),
                "market_title": row.get("market_title") or row.get("market_question"),
                "side": row.get("side"),
                "price": _to_float(row.get("price")),
                "notional": _to_float(row.get("notional"), _to_float(row.get("size_usdc"))),
                "fees": _to_float(row.get("fees")),
                "pnl_abs": _to_float(row.get("pnl_abs")),
                "pnl_pct": _to_float(row.get("pnl_pct")),
                "execution_mode": row.get("execution_mode") or "observed",
                "status": row.get("status") or "observed",
            }
        )

    trades.sort(key=lambda item: _parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return trades[:100]


def _build_positions_payload(positions_live: list[dict[str, Any]], paper_capital: float) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    capital_in_trade = 0.0
    for row in positions_live or []:
        notional = _to_float(row.get("size_usdc"), 0.0) or 0.0
        capital_in_trade += notional
        entry_price = _to_float(row.get("entry_price"), 0.0) or 0.0
        size = round(notional / entry_price, 2) if entry_price > 0 else notional
        unrealized = _to_float(row.get("unrealized_pnl"))
        items.append(
            {
                "trade_id": row.get("id"),
                "market_title": row.get("question") or row.get("market_title") or row.get("market_question"),
                "side": str(row.get("direction", "")).upper(),
                "entry_price": entry_price,
                "size": size,
                "notional": notional,
                "unrealized_pnl_abs": unrealized,
                "unrealized_pnl_pct": (unrealized / notional) if unrealized is not None and notional > 0 else None,
                "decision_action": "open",
                "decision_summary": (
                    f"{row.get('strategy', 'paper')} / conf {(_to_float(row.get('confidence'), 0.0) or 0.0):.2f}"
                ),
            }
        )
    exposure_pct = (capital_in_trade / paper_capital) if paper_capital > 0 else 0.0
    return {
        "items": items,
        "open_count": len(items),
        "capital_in_trade": round(capital_in_trade, 2),
        "exposure_pct": round(exposure_pct, 4),
    }


def _build_decision_engine(
    decisions: list[dict[str, Any]],
    readiness: dict[str, Any],
    positions_payload: dict[str, Any],
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    for row in decisions or []:
        trace = row.get("trace") or {}
        action = _map_decision_action(row.get("action"))
        refusal_reason = trace.get("refusal_reason")
        executable = trace.get("gate_result") == "accepted" and action != "skip"
        reasons = [item for item in [row.get("reason")] if item]
        reasons.extend(item for item in (row.get("ml_snapshot") or {}).get("reason_codes", []) if item)
        ranked.append(
            {
                "market_id": row.get("market_id"),
                "title": row.get("question") or row.get("title") or row.get("market_id"),
                "action": action,
                "side": (row.get("side") or row.get("signal_audit", {}).get("side")),
                "confidence": _to_float(row.get("confidence"), 0.0) or 0.0,
                "executable": executable,
                "cooldown_remaining_ms": 0,
                "summary": row.get("reason") or refusal_reason or trace.get("execution_result") or "decision",
                "reasons": reasons,
                "rejections": [refusal_reason] if refusal_reason else [],
            }
        )

    if not ranked:
        for row in readiness.get("markets", []) or []:
            blockers = list(row.get("blockers") or [])
            state = str(row.get("state") or "")
            ranked.append(
                {
                    "market_id": row.get("market_id"),
                    "title": row.get("question") or row.get("market_id"),
                    "action": _map_readiness_action(state),
                    "side": None,
                    "confidence": ((_to_float((row.get("bars") or {}).get("first_position_readiness_pct"), 0.0) or 0.0) / 100.0),
                    "executable": state in _READINESS_EXECUTABLE_STATES and not blockers,
                    "cooldown_remaining_ms": 0,
                    "summary": row.get("last_transition_reason") or state.lower(),
                    "reasons": [row.get("last_transition_reason")] if row.get("last_transition_reason") else [],
                    "rejections": blockers,
                }
            )

    open_count = sum(1 for item in ranked if item["action"] == "open")
    close_count = sum(1 for item in ranked if item["action"] == "close")
    reduce_count = sum(1 for item in ranked if item["action"] == "reduce")
    reject_count = sum(1 for item in ranked if item["action"] == "skip")
    max_positions = 10
    return {
        "summary": {
            "actionable_count": sum(1 for item in ranked if item["action"] != "skip"),
            "open_count": open_count,
            "close_count": close_count,
            "reduce_count": reduce_count,
            "reject_count": reject_count,
            "slots_remaining": max(0, max_positions - _to_int(positions_payload.get("open_count"), 0)),
            "exposure_remaining": max(
                0.0,
                round(settings.MAX_MARKET_EXPOSURE_PCT - (_to_float(positions_payload.get("exposure_pct"), 0.0) or 0.0), 4),
            ),
        },
        "ranked": ranked[:100],
    }


def _build_analytics(
    market_rows: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    top_signal = max((row.get("signal_strength") or 0.0) for row in market_rows) if market_rows else 0.0
    top_edge_values = [row.get("expected_edge") for row in market_rows if row.get("expected_edge") is not None]
    opportunity_count = sum(1 for row in market_rows if row.get("decision_action") == "open")
    avg_freshness = _mean([float(row.get("freshness_ms") or 0.0) for row in market_rows if row.get("freshness_ms") is not None])
    avg_spread = _mean([float(row.get("spread") or 0.0) for row in market_rows if row.get("spread") is not None])
    return {
        "summary": {
            "tracked_markets": _to_int((data_quality.get("markets") or {}).get("total"), len(market_rows)),
            "opportunity_count": opportunity_count,
            "top_signal_score": round(top_signal, 4),
            "top_edge": round(max(top_edge_values), 4) if top_edge_values else None,
            "avg_freshness_ms": round(avg_freshness, 2) if avg_freshness is not None else None,
            "avg_volatility": round(avg_spread, 4) if avg_spread is not None else None,
        },
        "opportunities": market_rows[:50],
        "leaderboard": sorted(
            market_rows,
            key=lambda item: (item.get("messages_last_minute", 0), item.get("observations", 0), item.get("signal_strength", 0.0)),
            reverse=True,
        )[:50],
    }


def _build_ingestion(
    market_rows: list[dict[str, Any]],
    data_quality: dict[str, Any],
    health: dict[str, Any],
) -> dict[str, Any]:
    live_markets = sum(1 for row in market_rows if _to_int(row.get("freshness_ms"), 999999) <= _LIVE_MARKET_FRESHNESS_MS)
    stale_markets = max(0, len(market_rows) - live_markets)
    updates_last_minute = sum(_to_int(row.get("messages_last_minute"), 0) for row in market_rows)
    avg_freshness = _mean([float(row.get("freshness_ms") or 0.0) for row in market_rows])
    stage_health = health.get("pipeline_stage_health") or {}
    stage_status = stage_health.get("stage_status") or {}
    ws_age_s = _to_float(health.get("last_message_age_s"), 0.0) or 0.0

    sources = [
        {
            "name": "CLOB WebSocket",
            "status": "healthy" if health.get("websocket_connected") else "degraded",
            "lag_ms": int(round(ws_age_s * 1000)),
            "messages_last_minute": updates_last_minute,
            "note": None,
        },
        {
            "name": "Book Snapshots",
            "status": stage_status.get("book_capture") or "unknown",
            "lag_ms": int(round((_to_float(stage_health.get("last_book_snapshot_age_s"), 0.0) or 0.0) * 1000)),
            "messages_last_minute": _to_int(stage_health.get("book_quality_snapshots_5m"), 0) // 5,
            "note": None,
        },
        {
            "name": "Data Quality",
            "status": (data_quality.get("status") or "unknown"),
            "lag_ms": int(round((_to_float((data_quality.get("feed") or {}).get("last_trade_age_s"), 0.0) or 0.0) * 1000)),
            "messages_last_minute": None,
            "note": f"{_to_int(data_quality.get('issues_count'), 0)} issues" if data_quality.get("issues_count") is not None else None,
        },
    ]

    return {
        "total_markets": _to_int((data_quality.get("markets") or {}).get("total"), len(market_rows)),
        "live_markets": live_markets,
        "stale_market_count": stale_markets,
        "updates_last_minute": updates_last_minute,
        "avg_freshness_ms": round(avg_freshness, 2) if avg_freshness is not None else None,
        "sources": sources,
        "markets": market_rows[:80],
    }


def _build_bot_payload(
    health: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": _bot_status(health),
        "execution_enabled": False,
        "uptime_seconds": _to_int(runtime.get("uptime_seconds"), 0),
        "latency_ms": round((_to_float(health.get("last_message_age_s"), 0.0) or 0.0) * 1000, 2),
        "cycle_latency_ms": _to_float(runtime.get("cycle_latency_ms"), 0.0) or 0.0,
        "started_at": runtime.get("started_at"),
        "accumulated_run_seconds": _to_int(runtime.get("uptime_seconds"), 0),
        "last_command_at": runtime.get("last_command_at"),
        "paper_only": True,
        "control_available": bool(runtime.get("control_available", False)),
        "config_mutable": bool(runtime.get("config_mutable", False)),
    }


def _build_risk_config(
    runtime: dict[str, Any],
) -> dict[str, Any]:
    return {
        "risk_per_trade_pct": settings.MAX_POSITION_PCT,
        "max_total_exposure_pct": settings.MAX_MARKET_EXPOSURE_PCT,
        "kelly_fraction": None,
        "max_drawdown_stop_pct": 0.20,
        "base_entry_threshold": None,
        "spread_cap": None,
        "fee_bps": None,
        "min_signal_strength": settings.FADE_MIN_CONFIDENCE,
        "max_concurrent_positions": 10,
        "max_positions_per_tick": None,
        "cooldown_seconds": settings.PAPER_REENTRY_COOLDOWN_S,
        "max_holding_seconds": None,
        "paper_only": True,
        "config_mutable": bool(runtime.get("config_mutable", False)),
    }


def build_terminal_snapshot(
    *,
    overview: dict[str, Any],
    ml: dict[str, Any],
    system: dict[str, Any],
    positions_live: list[dict[str, Any]],
    positions: dict[str, Any],
    decisions: list[dict[str, Any]],
    decision_stats: dict[str, Any],
    risk: dict[str, Any],
    readiness: dict[str, Any],
    data_quality: dict[str, Any],
    health: dict[str, Any],
    market_rows: list[dict[str, Any]],
    observed_trades: list[dict[str, Any]],
    runtime: dict[str, Any],
    logs: list[dict[str, Any]],
) -> dict[str, Any]:
    paper_capital = _to_float(risk.get("paper_capital"), settings.PAPER_CAPITAL_USDC) or settings.PAPER_CAPITAL_USDC
    positions_payload = _build_positions_payload(positions_live, paper_capital)
    normalized_markets = _normalize_market_rows(market_rows)
    analytics = _build_analytics(normalized_markets, data_quality)
    ingestion = _build_ingestion(normalized_markets, data_quality, health)
    decision_engine = _build_decision_engine(decisions, readiness, positions_payload)

    equity = _to_float(overview.get("equity"), paper_capital) or paper_capital
    pnl_pct = ((equity - settings.PAPER_CAPITAL_USDC) / settings.PAPER_CAPITAL_USDC) if settings.PAPER_CAPITAL_USDC > 0 else 0.0

    return {
        "clock": {
            "updated_at": _safe_iso(datetime.now(timezone.utc)),
        },
        "meta": {
            "paper_only": True,
            "leaders_active": _to_int((system.get("leaders") or {}).get("active"), 0),
            "readiness_blockers": list((readiness.get("global") or {}).get("blockers", [])),
        },
        "bot": _build_bot_payload(health, runtime),
        "stats": {
            "total_pnl": round(_to_float(overview.get("total_pnl"), 0.0) or 0.0, 2),
            "win_rate": _to_float(overview.get("win_rate"), 0.0) or 0.0,
            "active_markets": ingestion["live_markets"],
            "open_positions": positions_payload["open_count"],
            "portfolio_total": round(equity, 2),
            "pnl_percent": round(pnl_pct, 4),
            "detected_arbs_today": _to_int((decision_stats.get("totals") or {}).get("total"), 0),
            "capital_in_trade": positions_payload["capital_in_trade"],
        },
        "analytics": analytics,
        "positions": positions_payload,
        "recent_trades": _build_recent_trades(positions, observed_trades),
        "decision_engine": decision_engine,
        "risk_config": _build_risk_config(runtime),
        "ingestion": ingestion,
        "logs": logs,
    }
