"""
Message formatters for the Telegram bot (S3.9).

Pure functions that take an event payload (dict) and return a
plain-text or MarkdownV2 string ready to send. Kept separate from the
notifier / command handlers so they're trivial to unit-test without a
Telegram client.

Formatting choices:
  * Plain text (no MarkdownV2). MarkdownV2 requires escaping every
    `_*[]()~`>#+-=|{}.!` and we'd rather have readable code than
    perfect bold formatting. We use light unicode glyphs (✅ ⚠️ ❌ 📈
    📉) for visual scanning instead.
  * Truncate market_id to 14 chars — full hashes are unreadable on
    mobile and we only need them for cross-referencing in the DB.
  * Money formatted to 2 decimals; prices to 4; pct to 1.
"""

from __future__ import annotations

from typing import Optional


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _short(market_id: Optional[str], n: int = 14) -> str:
    if not market_id:
        return "?"
    return market_id[:n] + ("…" if len(market_id) > n else "")


def _money(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{float(x):+.2f}$" if x < 0 or x > 0 else "0.00$"


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


# --------------------------------------------------------------------------- #
# Notifier formatters                                                          #
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
    """Format a 'position closed' alert.

    Includes strategy, size, entry→exit prices, and pnl_pct so the user
    can immediately judge magnitude vs exposure. Prior format showed only
    pnl_usdc + exit_price + reason; without size + pct the operator had
    no way to tell whether ``-198$`` was a 99% loss on $200 or a 12%
    loss on $1650.
    """
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
    """Format a Phase 3 Task D ingest_gap alert.

    Payload shape (produced by IngestHealthMonitor recovery callback):
      {"source": "falcon_leaderboard",
       "duration_s": 2400.0,
       "severity": "warning" | "critical",
       "threshold_s": 2100}
    """
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
# Command formatters                                                           #
# --------------------------------------------------------------------------- #


def format_status(
    *,
    mode: str,
    paper_capital: Optional[float],
    paper_open: int,
    live_open: int,
    killswitch_exec: bool,
    killswitch_real: bool,
) -> str:
    """Format the /status reply."""
    return (
        f"📊 STATUS\n"
        f"mode: {mode}\n"
        f"paper: capital={_money_abs(paper_capital)} open={paper_open}\n"
        f"live: open={live_open}\n"
        f"killswitch: exec={'ON' if killswitch_exec else 'OFF'}, "
        f"real={'ON' if killswitch_real else 'OFF'}"
    )


def format_pnl(
    *,
    paper_realized: Optional[float],
    paper_unrealized: Optional[float],
    live_realized: Optional[float],
    live_shadow_count: int,
    live_real_count: int,
) -> str:
    """Format the /pnl reply."""
    return (
        f"💰 PnL\n"
        f"paper realized:   {_money(paper_realized)}\n"
        f"paper unrealized: {_money(paper_unrealized)}\n"
        f"live realized:    {_money(live_realized)}\n"
        f"live trades: shadow={live_shadow_count}  real={live_real_count}"
    )


def format_positions(
    *,
    paper_positions: list[dict],
    live_positions: list[dict],
) -> str:
    """Format the /positions reply. Each position dict must have:
    market_id, strategy, direction, entry_price, size_usdc."""
    lines = ["📋 OPEN POSITIONS"]
    if not paper_positions and not live_positions:
        lines.append("(none)")
        return "\n".join(lines)
    if paper_positions:
        lines.append(f"\nPAPER ({len(paper_positions)})")
        for p in paper_positions[:10]:
            lines.append(
                f"  • {_short(p.get('market_id'))} "
                f"{p.get('strategy', '?')}/{p.get('direction', '?')} "
                f"size={_money_abs(p.get('size_usdc'))} "
                f"@ {_price(p.get('entry_price'))}"
            )
        if len(paper_positions) > 10:
            lines.append(f"  … +{len(paper_positions) - 10} more")
    if live_positions:
        lines.append(f"\nLIVE ({len(live_positions)})")
        for p in live_positions[:10]:
            lines.append(
                f"  • {_short(p.get('market_id'))} "
                f"{p.get('strategy', '?')}/{p.get('direction', '?')} "
                f"size={_money_abs(p.get('size_usdc'))} "
                f"@ {_price(p.get('entry_price'))} "
                f"[{p.get('status', '?')}]"
            )
        if len(live_positions) > 10:
            lines.append(f"  … +{len(live_positions) - 10} more")
    return "\n".join(lines)


def format_summary(payload: dict) -> str:
    """Format the /summary reply.

    Expected payload shape (all keys optional; missing fields render as
    ``?`` or skipped sections):
      {
        "trades_closed_today": int,
        "trades_open": int,
        "wins": int,
        "losses": int,
        "avg_win": float | None,
        "avg_loss": float | None,
        "net_today": float,
        "cum_realized": float | None,
        "unrealized": float | None,
        "by_reason":  [{"reason": str, "count": int, "avg_pnl": float | None}, ...],
        "by_strategy": [{"strategy": str, "count": int, "wins": int, "losses": int}, ...],
      }
    """
    n_closed = int(payload.get("trades_closed_today", 0) or 0)
    n_open = int(payload.get("trades_open", 0) or 0)
    wins = int(payload.get("wins", 0) or 0)
    losses = int(payload.get("losses", 0) or 0)
    avg_win = payload.get("avg_win")
    avg_loss = payload.get("avg_loss")
    net_today = payload.get("net_today", 0.0)
    cum_realized = payload.get("cum_realized")
    unrealized = payload.get("unrealized")

    lines = ["📊 TODAY'S SUMMARY (UTC since 00:00)"]
    lines.append(f"trades: {n_closed} closed, {n_open} open")
    lines.append(
        f"wins: {wins} (avg {_money(avg_win)})" if wins
        else "wins: 0"
    )
    lines.append(
        f"losses: {losses} (avg {_money(avg_loss)})" if losses
        else "losses: 0"
    )
    lines.append(f"net realized: {_money(net_today)} (today)")
    if cum_realized is not None:
        lines.append(f"cum realized: {_money(cum_realized)} (lifetime)")
    if unrealized is not None:
        lines.append(f"unrealized: {_money(unrealized)} ({n_open} open)")

    by_reason = payload.get("by_reason") or []
    if by_reason:
        lines.append("")
        lines.append("by close reason:")
        for r in by_reason:
            reason = str(r.get("reason", "?"))
            count = int(r.get("count", 0) or 0)
            avg_pnl = r.get("avg_pnl")
            lines.append(f"  {reason}: {count} (avg {_money(avg_pnl)})")

    by_strategy = payload.get("by_strategy") or []
    if by_strategy:
        lines.append("")
        lines.append("by strategy:")
        for s in by_strategy:
            strat = str(s.get("strategy", "?"))
            count = int(s.get("count", 0) or 0)
            w = int(s.get("wins", 0) or 0)
            l = int(s.get("losses", 0) or 0)
            lines.append(f"  {strat}: {count} ({w}W {l}L)")

    return "\n".join(lines)


def format_mode_change(*, old_mode: Optional[str], new_mode: str) -> str:
    """Format the /mode reply."""
    return (
        f"🔀 MODE CHANGED\n"
        f"{old_mode or '?'} → {new_mode}\n"
        f"(takes effect on the next decision; runtime override key set)"
    )


def format_help() -> str:
    return (
        "🤖 POLYMARKET BOT — COMMANDS\n"
        "/status           — current mode, capital, open positions\n"
        "/pnl              — realized + unrealized PnL\n"
        "/positions        — list of open positions\n"
        "/summary          — today's trading activity (UTC)\n"
        "/mode <m>         — switch trading mode (paper|live|dual)\n"
        "/killswitch <s>   — flip the killswitch (on|off)\n"
        "/pause            — stop the engine (observer keeps running)\n"
        "/resume           — restart the engine\n"
        "/help             — this message"
    )


def format_unauthorized() -> str:
    """We don't actually send this — unauthorized chats are silently
    ignored — but kept for tests and possible future debug output."""
    return "⛔ Unauthorized chat."
