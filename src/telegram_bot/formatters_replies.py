"""
Command-reply + digest formatters for the Telegram bot.

Split out of ``formatters.py`` (S3.11) to keep each module under 500
lines. Re-exported from ``formatters`` so callers keep their existing
``from src.telegram_bot import formatters`` imports unchanged.

Every function here builds a human-readable plain-text reply for one
command or auto-digest. Pure (input → string), no side effects, no
Telegram client.
"""

from __future__ import annotations

from typing import Optional

# Helpers live in formatters.py — single source of truth. We re-import
# them here as private names so callers see the same pretty output.
from src.telegram_bot.formatters import (
    _fmt_duration,
    _money,
    _money_abs,
    _pct,
    _price,
    _short,
)


# --------------------------------------------------------------------------- #
# Original (S3.9) command-reply formatters                                     #
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
    """Format the /summary reply (today's activity since UTC midnight)."""
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
        f"wins: {wins} (avg {_money(avg_win)})" if wins else "wins: 0"
    )
    lines.append(
        f"losses: {losses} (avg {_money(avg_loss)})" if losses else "losses: 0"
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
            losses_n = int(s.get("losses", 0) or 0)
            lines.append(f"  {strat}: {count} ({w}W {losses_n}L)")

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
        "\n"
        "▸ OBSERVABILITY\n"
        "/status           — mode, capital, open positions, killswitch\n"
        "/pnl              — realized + unrealized PnL (paper + live)\n"
        "/positions        — open positions (paper + live)\n"
        "/summary          — today's activity (wins/losses by strategy)\n"
        "/digest           — instant 24h digest (same content as 23:00 push)\n"
        "/trades [n]       — last N closed trades (default 10, max 25)\n"
        "/leaders [n]      — top N tracked leaders by Falcon Score\n"
        "/leader <wallet>  — detailed profile for a wallet\n"
        "/market <id>      — our position + market info for a market_id prefix\n"
        "/drift            — leaders with active CUSUM drift alert\n"
        "/health           — pipeline: Redis, WS lag, msgs/min, ingest sources\n"
        "/risk             — current risk parameters (mutable + env defaults)\n"
        "\n"
        "▸ CONTROL\n"
        "/mode <m>         — switch trading mode (paper|live|dual)\n"
        "/killswitch <s>   — flip the killswitch (on|off)\n"
        "/pause            — pause execution (observer keeps running)\n"
        "/resume           — resume execution\n"
        "/set <k> <v>      — update a mutable risk knob\n"
        "/verbosity <lvl>  — quiet|normal|verbose|debug (alert filter level)\n"
        "\n"
        "▸ ALERTS (proactive thresholds)\n"
        "/alert list       — list configured alert rules\n"
        "/alert add <type> <threshold> — add a rule (e.g. drawdown 0.05)\n"
        "/alert remove <id> — remove a rule by id\n"
        "\n"
        "/help             — this message"
    )


def format_unauthorized() -> str:
    """Unauthorized chats are silently ignored — kept for tests."""
    return "⛔ Unauthorized chat."


# --------------------------------------------------------------------------- #
# S3.11 command-reply formatters                                              #
# --------------------------------------------------------------------------- #


def format_leaders(rows: list[dict]) -> str:
    """Each row: {wallet_address, falcon_score, excluded, exclude_reason}."""
    lines = [f"👑 TOP LEADERS ({len(rows)})"]
    if not rows:
        lines.append("(none)")
        return "\n".join(lines)
    for r in rows[:25]:
        wallet = _short(r.get("wallet_address"), n=10)
        falcon = r.get("falcon_score")
        falcon_str = f"{float(falcon):.2f}" if falcon is not None else "?"
        excluded = r.get("excluded")
        flag = "❌" if excluded else "✅"
        lines.append(f"  {flag} {wallet}  falcon={falcon_str}")
    return "\n".join(lines)


def format_leader_detail(payload: dict) -> str:
    """Format the /leader <wallet> reply."""
    if not payload:
        return "leader not found — check the wallet prefix"
    wallet = _short(payload.get("wallet_address"), n=14)
    falcon = payload.get("falcon_score")
    falcon_str = f"{float(falcon):.2f}" if falcon is not None else "?"
    classification = payload.get("classification_json") or {}
    strategy = classification.get("strategy") or "?"
    horizon = classification.get("horizon") or "?"
    copiable = classification.get("copiable")
    profile = payload.get("profile") or {}
    accuracy = profile.get("accuracy") or {}
    overall = accuracy.get("overall")
    resolved = accuracy.get("resolved_count") or accuracy.get("resolved") or 0
    overall_str = f"{float(overall):.2%}" if overall is not None else "?"
    error_phase = payload.get("error_model_phase") or "?"
    trades_obs = payload.get("trades_observed") or 0
    excluded = payload.get("excluded")
    flag = "EXCLUDED" if excluded else "ACTIVE"
    return (
        f"👑 LEADER — {wallet}  [{flag}]\n"
        f"falcon: {falcon_str}  strategy: {strategy}  horizon: {horizon}  "
        f"copiable: {copiable}\n"
        f"error model: phase {error_phase}\n"
        f"trades observed: {trades_obs}  positions resolved: {resolved}\n"
        f"overall accuracy: {overall_str}"
    )


def format_health(payload: dict) -> str:
    """Pipeline health snapshot."""
    redis_ok = bool(payload.get("redis_ok"))
    redis_lat = payload.get("redis_latency_ms")
    ws_lag = payload.get("ws_lag_s")
    msgs = payload.get("msgs_per_min", 0)
    uptime = payload.get("engine_uptime_s", 0)
    sources = payload.get("ingest_sources") or []

    redis_str = "✅" if redis_ok else "❌"
    redis_lat_str = f"{float(redis_lat):.0f}ms" if redis_lat is not None else "?"
    ws_str = f"{float(ws_lag):.1f}s" if ws_lag is not None else "?"
    uptime_str = _fmt_duration(uptime)

    lines = ["🩺 PIPELINE HEALTH"]
    lines.append(f"redis: {redis_str} ({redis_lat_str})  ws lag: {ws_str}")
    lines.append(f"msgs/min: {msgs}  engine uptime: {uptime_str}")
    if sources:
        lines.append("")
        lines.append("ingest sources:")
        for s in sources[:8]:
            name = s.get("source", "?")
            ok = bool(s.get("ok"))
            last = s.get("last_event_s")
            last_str = _fmt_duration(last) if last is not None else "?"
            flag = "✅" if ok else "⚠️"
            lines.append(f"  {flag} {name}  last={last_str} ago")
    return "\n".join(lines)


def format_trades(rows: list[dict]) -> str:
    """Last-N closed trades reply."""
    lines = [f"📜 LAST {len(rows)} CLOSED"]
    if not rows:
        lines.append("(none)")
        return "\n".join(lines)
    for r in rows:
        trade_id = r.get("id", "?")
        market = _short(r.get("market_id"), n=12)
        strat = str(r.get("strategy", "?")).upper()
        direction = str(r.get("direction", "?")).upper()
        pnl = r.get("pnl_usdc")
        pnl_icon = "📈" if (pnl or 0) >= 0 else "📉"
        reason = r.get("close_reason", "?")
        size = _money_abs(r.get("size_usdc"))
        lines.append(
            f"  {pnl_icon} #{trade_id} {market} {strat}/{direction} "
            f"size={size} pnl={_money(pnl)} [{reason}]"
        )
    return "\n".join(lines)


def format_risk(cfg: dict, defaults: dict) -> str:
    """Mutable risk knobs side-by-side with env defaults (★ = override)."""
    keys = [
        "risk_per_trade_pct",
        "max_total_exposure_pct",
        "kelly_fraction",
        "max_drawdown_stop_pct",
        "min_signal_strength",
        "max_concurrent_positions",
        "cooldown_seconds",
        "max_consecutive_losses",
        "max_recent_losses_per_market",
        "fade_size_ratio",
    ]
    lines = ["⚙️ RISK CONFIG  (effective / env default)"]
    for k in keys:
        eff = cfg.get(k)
        dft = defaults.get(k)
        marker = "•" if eff == dft else "★"
        lines.append(f"  {marker} {k}: {eff}  ({dft})")
    return "\n".join(lines)


def format_drift(rows: list[dict]) -> str:
    """Leaders with active CUSUM drift alert."""
    if not rows:
        return "🩺 DRIFT — no leaders currently flagged"
    lines = [f"⚠️ DRIFT — {len(rows)} leaders flagged"]
    for r in rows[:15]:
        wallet = _short(r.get("wallet_address"), n=10)
        phase = r.get("error_model_phase", "?")
        cusum = r.get("cusum_state")
        cusum_str = f"{float(cusum):.3f}" if cusum is not None else "?"
        lines.append(f"  {wallet}  phase={phase}  CUSUM={cusum_str}")
    if len(rows) > 15:
        lines.append(f"  … +{len(rows) - 15} more")
    return "\n".join(lines)


def format_market_detail(payload: dict) -> str:
    """/market <id> reply."""
    if not payload:
        return "market not found — check the market_id prefix"
    market_id = _short(payload.get("market_id"), n=18)
    question = payload.get("question") or "?"
    category = payload.get("category") or "?"
    vol_24h = payload.get("volume_24h")
    liquidity = payload.get("liquidity_score")
    end_date = payload.get("end_date") or "?"
    positions = payload.get("positions") or []
    lines = [
        f"📊 MARKET — {market_id}",
        f"q: {question[:80]}",
        f"category: {category}  vol_24h: {_money_abs(vol_24h)}  "
        f"liquidity: {liquidity if liquidity is not None else '?'}",
        f"end: {end_date}",
    ]
    if positions:
        lines.append("")
        lines.append(f"our positions ({len(positions)}):")
        for p in positions[:5]:
            venue = str(p.get("venue", "?")).upper()
            strat = str(p.get("strategy", "?")).upper()
            direction = str(p.get("direction", "?")).upper()
            size = _money_abs(p.get("size_usdc"))
            entry = _price(p.get("entry_price"))
            status = p.get("status", "?")
            lines.append(
                f"  {venue} {strat}/{direction} size={size} @ {entry} [{status}]"
            )
    else:
        lines.append("our positions: (none)")
    return "\n".join(lines)


def format_set_ok(*, key: str, old_value, new_value) -> str:
    return (
        f"✅ {key} updated\n"
        f"{old_value} → {new_value}\n"
        f"(propagates within 5s via runtime_config:changed)"
    )


def format_set_rejected(*, key: str, reason: str) -> str:
    return f"❌ /set {key} rejected: {reason}"


def format_verbosity_changed(*, old: str, new: str) -> str:
    return (
        f"🔈 VERBOSITY changed\n"
        f"{old} → {new}\n"
        f"(filters which alert tiers reach Telegram)"
    )


def format_alert_added(*, rule_id: str, channel: str, condition: str, threshold) -> str:
    return (
        f"🔔 ALERT ADDED  id={rule_id}\n"
        f"channel: {channel}  cond: {condition}  threshold: {threshold}"
    )


def format_alert_list(rules: list[dict]) -> str:
    if not rules:
        return "🔔 ALERTS — no rules configured\nuse /alert add <type> <threshold>"
    lines = [f"🔔 ALERTS ({len(rules)} active)"]
    for r in rules:
        lines.append(
            f"  id={r.get('id')}  channel={r.get('channel')}  "
            f"cond={r.get('condition')}  threshold={r.get('threshold')}"
        )
    return "\n".join(lines)


def format_alert_removed(rule_id: str) -> str:
    return f"🔕 ALERT REMOVED  id={rule_id}"


def format_alert_help() -> str:
    return (
        "🔔 /alert subcommands:\n"
        "  /alert list\n"
        "  /alert add <type> <threshold>\n"
        "    types: drawdown, daily_loss, win_rate_below, idle_minutes\n"
        "    example: /alert add drawdown 0.05\n"
        "  /alert remove <id>"
    )


def format_digest_hourly(payload: dict) -> str:
    """Hourly auto-digest. Renders even if window is sparse — the
    scheduler decides whether to actually push it."""
    closed = int(payload.get("trades_closed", 0) or 0)
    opened = int(payload.get("trades_opened", 0) or 0)
    net = payload.get("net_pnl", 0.0)
    wins = int(payload.get("wins", 0) or 0)
    losses = int(payload.get("losses", 0) or 0)
    top = payload.get("top_market")
    breakers = int(payload.get("circuit_breaker_hits", 0) or 0)
    drifts = int(payload.get("drift_events", 0) or 0)

    lines = ["⏱ HOURLY DIGEST (last 60 min)"]
    lines.append(f"trades: {opened} opened, {closed} closed")
    lines.append(f"wins/losses: {wins}W/{losses}L  net: {_money(net)}")
    if top:
        lines.append(f"top market: {_short(top)}")
    if breakers > 0:
        lines.append(f"⚠️ circuit breakers tripped: {breakers}")
    if drifts > 0:
        lines.append(f"⚠️ drift events: {drifts}")
    return "\n".join(lines)


def format_digest_daily(payload: dict) -> str:
    """Daily auto-digest — full end-of-day snapshot."""
    date = payload.get("date", "?")
    closed = int(payload.get("trades_closed", 0) or 0)
    wins = int(payload.get("wins", 0) or 0)
    losses = int(payload.get("losses", 0) or 0)
    net = payload.get("net_pnl", 0.0)
    cum = payload.get("cum_realized")
    unrealized = payload.get("unrealized")
    win_rate = payload.get("win_rate")
    win_rate_str = _pct(win_rate) if win_rate is not None else "?"
    breakers = int(payload.get("circuit_breaker_hits", 0) or 0)
    drifts = int(payload.get("drift_events", 0) or 0)
    transitions = int(payload.get("phase_transitions", 0) or 0)
    new_leaders = int(payload.get("new_leaders", 0) or 0)
    new_followers = int(payload.get("new_followers_confirmed", 0) or 0)

    lines = [f"📅 DAILY DIGEST — {date}"]
    lines.append(
        f"trades closed: {closed}  ({wins}W / {losses}L  win rate: {win_rate_str})"
    )
    lines.append(f"net pnl: {_money(net)}")
    if cum is not None:
        lines.append(f"cum realized (lifetime): {_money(cum)}")
    if unrealized is not None:
        lines.append(f"unrealized (open): {_money(unrealized)}")

    best = payload.get("best_trade")
    worst = payload.get("worst_trade")
    if best:
        lines.append(
            f"best: #{best.get('id', '?')} {_short(best.get('market_id'))} "
            f"{_money(best.get('pnl_usdc'))}"
        )
    if worst:
        lines.append(
            f"worst: #{worst.get('id', '?')} {_short(worst.get('market_id'))} "
            f"{_money(worst.get('pnl_usdc'))}"
        )
    top = payload.get("top_leader")
    if top:
        wallet = _short(top.get("wallet_address"), n=10)
        pnl = top.get("pnl_usdc")
        lines.append(f"top leader: {wallet} ({_money(pnl)})")

    extras = []
    if breakers > 0:
        extras.append(f"breakers={breakers}")
    if drifts > 0:
        extras.append(f"drift={drifts}")
    if transitions > 0:
        extras.append(f"phase_up={transitions}")
    if new_leaders > 0:
        extras.append(f"new_leaders={new_leaders}")
    if new_followers > 0:
        # S3.12: surface the per-day total since we no longer fire an
        # instant alert per confirmed follower edge.
        extras.append(f"new_followers={new_followers}")
    if extras:
        lines.append("events: " + "  ".join(extras))

    return "\n".join(lines)
