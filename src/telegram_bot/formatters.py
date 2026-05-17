"""
Alert formatters for the Telegram bot (S3.9 + S3.11).

Pure functions that take a Redis pub/sub payload (dict) and return a
plain-text string ready to send. Kept separate from the notifier and
command handlers so they're trivial to unit-test without a Telegram
client.

This module covers ONLY notification-driven (channel-routed) alerts.
Command-reply formatters live in ``formatters_replies`` and are
re-exported below so callers using ``from src.telegram_bot import
formatters; formatters.format_status(...)`` keep working.

Formatting choices:
  * Plain text (no MarkdownV2). MarkdownV2 requires escaping every
    `_*[]()~`>#+-=|{}.!` and we'd rather have readable code than
    perfect bold formatting. Use unicode glyphs (✅ ⚠️ ❌ 📈 📉) for
    visual scanning instead.
  * Truncate market_id to 14 chars — full hashes are unreadable on
    mobile and we only need them for cross-referencing in the DB.
  * Money formatted to 2 decimals; prices to 4; pct to 1.
"""

from __future__ import annotations

from typing import Optional


# --------------------------------------------------------------------------- #
# Helpers (also re-used by formatters_replies via private import)              #
# --------------------------------------------------------------------------- #


def _short(market_id: Optional[str], n: int = 14) -> str:
    if not market_id:
        return "?"
    return market_id[:n] + ("…" if len(market_id) > n else "")


def _money(x: Optional[float]) -> str:
    if x is None:
        return "?"
    f = float(x)
    return f"{f:+.2f}$" if f != 0 else "0.00$"


def _money_abs(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{float(x):.2f}$"


def _price(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{float(x):.4f}"


def _pct(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{float(x) * 100:+.1f}%"


def _fmt_duration(seconds) -> str:
    """Render a duration in s/m/h/d. Tolerates None / weird types."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


# --------------------------------------------------------------------------- #
# Original (S3.9) alert formatters                                             #
# --------------------------------------------------------------------------- #


def format_position_opened(*, venue: str, payload: dict) -> str:
    """Format a 'position opened' alert. `venue` is 'paper' or 'live'."""
    icon = "📄" if venue == "paper" else "💸"
    strategy = payload.get("strategy", "?")
    direction = payload.get("direction", "?").upper()
    market = _short(payload.get("market_id"))
    size = _money_abs(payload.get("size_usdc"))
    entry = _price(payload.get("entry_price"))
    confidence = payload.get("confidence")
    leader = _short(payload.get("leader_wallet"), n=10)
    trade_id = payload.get("trade_id", "?")
    conf_str = f"{float(confidence):.2f}" if confidence is not None else "?"
    return (
        f"{icon} {venue.upper()} OPEN — {strategy.upper()} #{trade_id}\n"
        f"market: {market}\n"
        f"dir: {direction}  size: {size}  entry: {entry}\n"
        f"leader: {leader}  confidence: {conf_str}"
    )


def format_position_closed(*, venue: str, payload: dict) -> str:
    """Format a 'position closed' alert."""
    pnl = payload.get("pnl_usdc")
    pnl_icon = "📈" if (pnl or 0) >= 0 else "📉"
    icon = "📄" if venue == "paper" else "💸"
    market = _short(payload.get("market_id"))
    reason = payload.get("close_reason", "?")
    exit_price = _price(payload.get("exit_price"))
    entry_price = _price(payload.get("entry_price"))
    trade_id = payload.get("trade_id", "?")
    strategy = str(payload.get("strategy", "?")).upper()
    direction = str(payload.get("direction", "?")).upper()
    size = _money_abs(payload.get("size_usdc"))
    pnl_pct_raw = payload.get("pnl_pct")
    try:
        pnl_pct_str = (
            f"{float(pnl_pct_raw):+.1f}%" if pnl_pct_raw is not None else "?"
        )
    except (TypeError, ValueError):
        pnl_pct_str = "?"
    return (
        f"{icon}{pnl_icon} {venue.upper()} CLOSE — {strategy} #{trade_id}\n"
        f"market: {market}  dir: {direction}  size: {size}\n"
        f"entry: {entry_price} → exit: {exit_price}\n"
        f"pnl: {_money(pnl)} ({pnl_pct_str})  reason: {reason}"
    )


def format_killswitch_changed(payload: dict) -> str:
    """Format a killswitch state-change alert."""
    exec_on = bool(payload.get("execution_enabled"))
    real_on = bool(payload.get("real_execution_enabled"))
    actor = payload.get("updated_by") or "?"
    reason = payload.get("paused_reason") or "—"
    icon = "✅" if exec_on else "🛑"
    return (
        f"{icon} KILLSWITCH FLIP\n"
        f"execution: {'ON' if exec_on else 'OFF'}\n"
        f"real:      {'ON' if real_on else 'OFF'}\n"
        f"actor: {actor}  reason: {reason}"
    )


def format_engine_crash(payload: dict) -> str:
    """Format a critical engine/observer crash alert."""
    component = payload.get("component", "engine")
    error = payload.get("error", "unknown")
    error_type = payload.get("error_type", "Exception")
    return (
        f"❌ CRITICAL — {component} CRASH\n"
        f"{error_type}: {error}"
    )


def format_ingest_gap(payload: dict) -> str:
    """Format a Phase 3 Task D ingest_gap alert."""
    source = payload.get("source", "?")
    duration_s = float(payload.get("duration_s", 0) or 0)
    severity = payload.get("severity", "warning")
    threshold_s = payload.get("threshold_s")
    icon = "⚠️" if severity != "critical" else "🚨"
    minutes = duration_s / 60.0
    thr_str = (
        f" (threshold {int(threshold_s)}s)" if threshold_s else ""
    )
    return (
        f"{icon} INGEST GAP — {source}\n"
        f"silent for {minutes:.1f} min{thr_str}\n"
        f"severity: {severity}"
    )


# --------------------------------------------------------------------------- #
# S3.11 alert formatters (operator-visibility expansion)                       #
# --------------------------------------------------------------------------- #


def format_suspicious_close(payload: dict) -> str:
    """Triggered by PaperTrader when |pnl_pct| exceeds MAX_TRADE_RETURN_RATIO
    on a non-resolution close (likely stale-cache exit)."""
    trade_id = payload.get("trade_id", "?")
    pnl_pct = payload.get("pnl_pct")
    pnl_str = _pct(pnl_pct) if pnl_pct is not None else "?"
    entry = _price(payload.get("entry_price"))
    exit_ = _price(payload.get("exit_price"))
    reason = payload.get("close_reason", "?")
    strategy = str(payload.get("strategy", "?")).upper()
    market = _short(payload.get("market_id"))
    return (
        f"🚨 SUSPICIOUS CLOSE — #{trade_id} ({strategy})\n"
        f"market: {market}\n"
        f"entry: {entry} → exit: {exit_} ({pnl_str})\n"
        f"reason: {reason} — likely stale-cache exit; review before trusting PnL"
    )


def format_risk_breaker(payload: dict) -> str:
    """RiskManager circuit-breaker trip.

    Payload: {"breaker": str, "value": float, "threshold": float,
              "market_id": str | None}
    """
    breaker = str(payload.get("breaker", "?"))
    value = payload.get("value")
    threshold = payload.get("threshold")
    market_id = payload.get("market_id")
    pct_like = breaker in {"drawdown", "market_exposure"}
    val_str = (
        f"{float(value):.2%}" if isinstance(value, (int, float)) and pct_like
        else (str(int(value)) if isinstance(value, (int, float)) else "?")
    )
    thr_str = (
        f"{float(threshold):.2%}" if isinstance(threshold, (int, float)) and pct_like
        else (str(int(threshold)) if isinstance(threshold, (int, float)) else "?")
    )
    extra = f"\nmarket: {_short(market_id)}" if market_id else ""
    return (
        f"🛑 RISK BREAKER — {breaker}\n"
        f"value: {val_str}  threshold: {thr_str}\n"
        f"trade refused — system is protecting capital{extra}"
    )


def format_drawdown_threshold(payload: dict) -> str:
    """Portfolio drawdown crossing a tier threshold (3 / 5 / 10%)."""
    dd_pct = payload.get("drawdown_pct")
    threshold = payload.get("threshold")
    peak = payload.get("peak_capital")
    current = payload.get("current_capital")
    dd_str = f"{float(dd_pct) * 100:.1f}%" if dd_pct is not None else "?"
    thr_str = f"{float(threshold) * 100:.1f}%" if threshold is not None else "?"
    return (
        f"📉 DRAWDOWN — {dd_str} (crossed {thr_str} threshold)\n"
        f"peak: {_money_abs(peak)}  current: {_money_abs(current)}"
    )


def format_backfill_lag_alert(payload: dict) -> str:
    """``backfill_resolved_outcomes`` lag exceeds operator threshold.

    Fired when the 30-min maintenance pass finishes and the count of
    markets with ``active=FALSE AND resolved_outcome IS NULL`` is still
    above ``BACKFILL_LAG_ALERT_THRESHOLD``. Indicates Gamma is rate-
    limiting us, the batch size is too small for the inflow, or the
    endpoint is degraded — operator should investigate.
    """
    missing = payload.get("missing_count", "?")
    threshold = payload.get("threshold", "?")
    try:
        missing_str = f"{int(missing):,}"
    except (TypeError, ValueError):
        missing_str = str(missing)
    try:
        thr_str = f"{int(threshold):,}"
    except (TypeError, ValueError):
        thr_str = str(threshold)
    return (
        f"🚧 BACKFILL LAG — resolved_outcome catch-up falling behind\n"
        f"missing: {missing_str}  threshold: {thr_str}\n"
        f"check Gamma 429s / batch size / endpoint health"
    )


def format_drift_detected(payload: dict) -> str:
    """Profiler CUSUM drift detection event."""
    wallet = _short(payload.get("wallet"), n=10)
    phase_before = payload.get("phase_before", "?")
    phase_after = payload.get("phase_after", "?")
    cusum = payload.get("cusum_value")
    cusum_str = f"{float(cusum):.3f}" if cusum is not None else "?"
    return (
        f"⚠️ DRIFT — error model downgraded\n"
        f"wallet: {wallet}\n"
        f"phase: {phase_before} → {phase_after}  CUSUM: {cusum_str}\n"
        f"leader's behavior changed — model collecting fresh data"
    )


def format_phase_upgraded(payload: dict) -> str:
    """error_model phase upgrade (1→2 or 2→3)."""
    wallet = _short(payload.get("wallet"), n=10)
    old_phase = payload.get("old_phase", "?")
    new_phase = payload.get("new_phase", "?")
    resolved = payload.get("positions_resolved", "?")
    model_name = {1: "Beta", 2: "BayesianRidge", 3: "LightGBM+Platt"}.get(
        new_phase if isinstance(new_phase, int) else -1, "?"
    )
    return (
        f"📈 ERROR MODEL UPGRADED\n"
        f"wallet: {wallet}  phase {old_phase} → {new_phase} ({model_name})\n"
        f"positions resolved: {resolved}"
    )


def format_watchdog_restart(payload: dict) -> str:
    """Watchdog coroutine-restart event."""
    component = payload.get("component", "?")
    reason = payload.get("reason", "?")
    restart_count = payload.get("restart_count", "?")
    max_restarts = payload.get("max_restarts", "?")
    return (
        f"🔁 WATCHDOG RESTART — {component}\n"
        f"reason: {reason}\n"
        f"restart {restart_count}/{max_restarts}"
    )


def format_follower_confirmed(payload: dict) -> str:
    """graph_engine new-follower-edge-confirmed event."""
    leader = _short(payload.get("leader_wallet") or payload.get("leader"), n=10)
    follower = _short(payload.get("follower_wallet") or payload.get("follower"), n=10)
    prob = payload.get("follow_probability")
    same_dir = payload.get("same_direction_rate")
    co_occ = payload.get("co_occurrences", "?")
    prob_str = f"{float(prob):.2f}" if prob is not None else "?"
    sd_str = _pct(same_dir) if same_dir is not None else "?"
    return (
        f"🔗 FOLLOWER CONFIRMED\n"
        f"{leader} → {follower}\n"
        f"P(follow)={prob_str}  same_dir={sd_str}  co_occ={co_occ}"
    )


def format_leader_added(payload: dict) -> str:
    """leader_registry leader-added event."""
    wallet = _short(payload.get("wallet_address"), n=10)
    falcon = payload.get("falcon_score")
    falcon_str = f"{float(falcon):.2f}" if falcon is not None else "?"
    source = payload.get("source", "leaderboard")
    return (
        f"➕ LEADER ADDED\n"
        f"wallet: {wallet}\n"
        f"falcon_score: {falcon_str}  source: {source}"
    )


def format_leader_excluded(payload: dict) -> str:
    """leader_registry leader-excluded event."""
    wallet = _short(payload.get("wallet_address"), n=10)
    reason = payload.get("exclude_reason", "?")
    return (
        f"➖ LEADER EXCLUDED\n"
        f"wallet: {wallet}\n"
        f"reason: {reason}"
    )


def format_runtime_config_changed(payload: dict) -> str:
    """runtime_config edit event.

    Payload: {"actor": str, "edits": {key: value, ...}, "ts": float}
    """
    actor = payload.get("actor", "?")
    edits = payload.get("edits") or {}
    if not isinstance(edits, dict) or not edits:
        return f"⚙️ CONFIG CHANGED by {actor} — (no diff)"
    lines = [f"⚙️ CONFIG CHANGED by {actor}"]
    for k, v in list(edits.items())[:10]:
        lines.append(f"  {k} = {v}")
    if len(edits) > 10:
        lines.append(f"  … +{len(edits) - 10} more")
    return "\n".join(lines)


def format_market_resolved_position(payload: dict) -> str:
    """Market-resolved event for a market we hold a position in."""
    market = _short(payload.get("market_id"))
    outcome = str(payload.get("outcome", "?")).upper()
    direction = str(payload.get("our_direction", "?")).upper()
    size = _money_abs(payload.get("size_usdc"))
    pnl = payload.get("pnl_usdc")
    pnl_icon = "📈" if (pnl or 0) >= 0 else "📉"
    venue = str(payload.get("venue", "?")).upper()
    return (
        f"🏁{pnl_icon} MARKET RESOLVED — {venue}\n"
        f"market: {market}\n"
        f"outcome: {outcome}  our_dir: {direction}  size: {size}\n"
        f"pnl: {_money(pnl)}"
    )


# --------------------------------------------------------------------------- #
# Re-export command-reply + digest formatters for backwards compat.            #
# This keeps `from src.telegram_bot import formatters; formatters.format_*`    #
# working for every caller that doesn't care about the file split.             #
# --------------------------------------------------------------------------- #

from src.telegram_bot.formatters_replies import (  # noqa: E402, F401
    format_status,
    format_pnl,
    format_positions,
    format_summary,
    format_mode_change,
    format_help,
    format_unauthorized,
    format_leaders,
    format_leader_detail,
    format_health,
    format_trades,
    format_risk,
    format_drift,
    format_market_detail,
    format_set_ok,
    format_set_rejected,
    format_verbosity_changed,
    format_alert_added,
    format_alert_list,
    format_alert_removed,
    format_alert_help,
    format_digest_hourly,
    format_digest_daily,
)
