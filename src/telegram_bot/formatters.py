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
    """Format a 'position closed' alert."""
    pnl = payload.get("pnl_usdc")
    pnl_icon = "📈" if (pnl or 0) >= 0 else "📉"
    icon = "📄" if venue == "paper" else "💸"
    market = _short(payload.get("market_id"))
    reason = payload.get("close_reason", "?")
    exit_price = _price(payload.get("exit_price"))
    trade_id = payload.get("trade_id", "?")
    return (
        f"{icon}{pnl_icon} {venue.upper()} CLOSE — #{trade_id}\n"
        f"market: {market}\n"
        f"exit: {exit_price}  pnl: {_money(pnl)}  reason: {reason}"
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
