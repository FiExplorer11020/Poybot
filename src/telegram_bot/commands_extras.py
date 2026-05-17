"""
Extra command handlers for the Telegram bot (S3.11).

Split out of commands.py to keep each module under 500 lines. Same
shape as the original handlers: thin async functions taking
``CommandContext`` (and optional args list), returning a reply string.

Commands:
  /leaders [n]     — top N tracked leaders by Falcon Score
  /leader <wallet> — detail for a wallet prefix
  /health          — pipeline health snapshot
  /trades [n]      — last N closed paper trades
  /risk            — current risk knobs + env defaults
  /digest [scope]  — instant digest (daily by default, "hourly" for short form)
  /drift           — leaders with active CUSUM drift alert
  /market <id>     — market detail + our positions
  /set <k> <v>     — update a runtime_config key (validated)
  /verbosity <lvl> — change notifier verbosity tier
  /alert list|add|remove ...  — manage configurable alert rules
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from src.control.runtime_config import get_runtime_config
from src.telegram_bot import formatters
from src.telegram_bot.commands import CommandContext


# --------------------------------------------------------------------------- #
# Read-only observability commands                                             #
# --------------------------------------------------------------------------- #


async def cmd_leaders(ctx: CommandContext, args: list[str]) -> str:
    """/leaders [n] — top N tracked leaders by Falcon Score (default 10, max 25)."""
    from src.database.connection import get_db

    n = 10
    if args:
        try:
            n = max(1, min(25, int(args[0])))
        except (TypeError, ValueError):
            return "Usage: /leaders [n]  (n in 1..25)"
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                "SELECT wallet_address, falcon_score, excluded, exclude_reason "
                "FROM leaders "
                "WHERE on_watchlist = TRUE "
                "ORDER BY falcon_score DESC NULLS LAST "
                "LIMIT $1",
                n,
            )
        return formatters.format_leaders([dict(r) for r in rows])
    except Exception as e:
        logger.warning(f"/leaders failed: {e}")
        return f"❌ /leaders failed: {e}"


async def cmd_leader(ctx: CommandContext, args: list[str]) -> str:
    """/leader <wallet_prefix> — detail for a wallet (accepts a prefix)."""
    from src.database.connection import get_db

    if not args:
        return "Usage: /leader <wallet_prefix>"
    prefix = args[0].strip()
    if len(prefix) < 4:
        return "wallet prefix must be at least 4 chars"
    try:
        async with get_db() as conn:
            leader = await conn.fetchrow(
                "SELECT wallet_address, falcon_score, classification_json, excluded "
                "FROM leaders WHERE wallet_address LIKE $1 || '%' LIMIT 1",
                prefix,
            )
            if leader is None:
                return formatters.format_leader_detail({})
            profile = await conn.fetchrow(
                "SELECT profile_json, error_model_phase, trades_observed, "
                "       positions_resolved "
                "FROM leader_profiles WHERE wallet_address = $1",
                leader["wallet_address"],
            )
        payload = dict(leader)
        if profile is not None:
            payload["profile"] = profile["profile_json"]
            payload["error_model_phase"] = profile["error_model_phase"]
            payload["trades_observed"] = profile["trades_observed"]
            payload["positions_resolved"] = profile["positions_resolved"]
        return formatters.format_leader_detail(payload)
    except Exception as e:
        logger.warning(f"/leader failed: {e}")
        return f"❌ /leader failed: {e}"


async def cmd_health(ctx: CommandContext) -> str:
    """/health — pipeline health snapshot (redis ping, ws lag, msgs/min)."""
    payload: dict = {
        "redis_ok": False,
        "redis_latency_ms": None,
        "ws_lag_s": None,
        "msgs_per_min": 0,
        "engine_uptime_s": 0,
        "ingest_sources": [],
    }
    # Engine uptime from CommandContext (set by bot.py at start()).
    if ctx.engine_started_at > 0:
        payload["engine_uptime_s"] = int(time.time() - ctx.engine_started_at)

    # Redis ping latency.
    try:
        t0 = time.monotonic()
        await ctx.redis_client.ping()
        payload["redis_ok"] = True
        payload["redis_latency_ms"] = (time.monotonic() - t0) * 1000.0
    except Exception:
        payload["redis_ok"] = False

    # WS lag + msgs/min via known Redis keys maintained by trade_observer.
    try:
        ws_lag_raw = await ctx.redis_client.get("observer:ws:last_event_at")
        if ws_lag_raw:
            if isinstance(ws_lag_raw, bytes):
                ws_lag_raw = ws_lag_raw.decode()
            payload["ws_lag_s"] = max(0.0, time.time() - float(ws_lag_raw))
    except Exception:
        pass

    try:
        msgs_raw = await ctx.redis_client.get("observer:trades:msgs_per_min")
        if msgs_raw:
            if isinstance(msgs_raw, bytes):
                msgs_raw = msgs_raw.decode()
            payload["msgs_per_min"] = int(float(msgs_raw))
    except Exception:
        pass

    # Ingest source freshness (maintained by IngestHealthMonitor).
    sources = ["falcon_leaderboard", "polymarket_ws", "polymarket_rest", "social_x"]
    out_sources = []
    now = time.time()
    for s in sources:
        try:
            last_raw = await ctx.redis_client.get(f"ingest:last_event_at:{s}")
            if not last_raw:
                continue
            if isinstance(last_raw, bytes):
                last_raw = last_raw.decode()
            last_ts = float(last_raw)
            age = max(0, int(now - last_ts))
            out_sources.append({"source": s, "last_event_s": age, "ok": age < 300})
        except Exception:
            continue
    payload["ingest_sources"] = out_sources

    return formatters.format_health(payload)


async def cmd_trades(ctx: CommandContext, args: list[str]) -> str:
    """/trades [n] — last N closed paper trades (default 10, max 25)."""
    from src.database.connection import get_db

    n = 10
    if args:
        try:
            n = max(1, min(25, int(args[0])))
        except (TypeError, ValueError):
            return "Usage: /trades [n]  (n in 1..25)"
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                "SELECT id, market_id, strategy, direction, size_usdc, "
                "       entry_price, exit_price, pnl_usdc, close_reason "
                "FROM paper_trades WHERE status = 'closed' "
                "ORDER BY closed_at DESC LIMIT $1",
                n,
            )
        return formatters.format_trades([dict(r) for r in rows])
    except Exception as e:
        logger.warning(f"/trades failed: {e}")
        return f"❌ /trades failed: {e}"


async def cmd_risk(ctx: CommandContext) -> str:
    """/risk — show mutable risk knobs alongside env defaults."""
    from src.config import settings

    try:
        cfg = await get_runtime_config().effective()
    except Exception as e:
        return f"❌ /risk failed: {e}"
    defaults = {
        "risk_per_trade_pct": settings.MAX_POSITION_PCT,
        "max_total_exposure_pct": settings.MAX_MARKET_EXPOSURE_PCT,
        "kelly_fraction": 1.0,
        "max_drawdown_stop_pct": 0.20,
        "min_signal_strength": 0.0,
        "max_concurrent_positions": 10,
        "cooldown_seconds": 60,
        "max_consecutive_losses": 5,
        "max_recent_losses_per_market": 3,
        "fade_size_ratio": settings.FADE_SIZE_RATIO,
    }
    return formatters.format_risk(cfg, defaults)


async def cmd_digest(ctx: CommandContext, args: list[str]) -> str:
    """/digest [hourly|daily] — instant digest (default daily)."""
    from src.telegram_bot import digest as digest_mod

    scope = (args[0] if args else "daily").strip().lower()
    if scope not in ("daily", "hourly"):
        return "Usage: /digest [daily|hourly]"
    try:
        if scope == "hourly":
            payload = await digest_mod.build_hourly_digest(
                redis_client=ctx.redis_client, paper_trader=ctx.paper_trader
            )
            if payload is None:
                return "⏱ HOURLY DIGEST — no activity in the last 60 min"
            return formatters.format_digest_hourly(payload)
        payload = await digest_mod.build_daily_digest(
            redis_client=ctx.redis_client, paper_trader=ctx.paper_trader
        )
        return formatters.format_digest_daily(payload)
    except Exception as e:
        logger.warning(f"/digest failed: {e}")
        return f"❌ /digest failed: {e}"


async def cmd_drift(ctx: CommandContext) -> str:
    """/drift — leaders with active CUSUM drift alert."""
    from src.database.connection import get_db

    try:
        async with get_db() as conn:
            # profile_json -> runtime -> drift_alert / cusum_state. Stored
            # as JSONB so we use the standard ->'…' operators with default
            # fallback.
            rows = await conn.fetch(
                "SELECT wallet_address, error_model_phase, "
                "       (profile_json#>'{runtime,cusum_state}')::float AS cusum_state, "
                "       (profile_json#>'{runtime,drift_alert}')::bool AS drift_alert "
                "FROM leader_profiles "
                "WHERE (profile_json#>'{runtime,drift_alert}')::bool = TRUE "
                "ORDER BY wallet_address LIMIT 50"
            )
        return formatters.format_drift([dict(r) for r in rows])
    except Exception as e:
        logger.warning(f"/drift failed: {e}")
        return f"❌ /drift failed: {e}"


async def cmd_market(ctx: CommandContext, args: list[str]) -> str:
    """/market <id_prefix> — market info + our positions."""
    from src.database.connection import get_db

    if not args:
        return "Usage: /market <market_id_prefix>"
    prefix = args[0].strip()
    if len(prefix) < 4:
        return "market_id prefix must be at least 4 chars"
    try:
        async with get_db() as conn:
            market = await conn.fetchrow(
                "SELECT market_id, question, category, volume_24h, liquidity_score, "
                "       end_date FROM markets "
                "WHERE market_id LIKE $1 || '%' LIMIT 1",
                prefix,
            )
            if market is None:
                return formatters.format_market_detail({})
            mid = market["market_id"]
            paper_positions = await conn.fetch(
                "SELECT 'paper' AS venue, strategy, direction, entry_price, "
                "       size_usdc, status FROM paper_trades "
                "WHERE market_id = $1 AND status IN ('open', 'closed') "
                "ORDER BY opened_at DESC LIMIT 5",
                mid,
            )
            live_positions = await conn.fetch(
                "SELECT 'live' AS venue, strategy, direction, entry_price, "
                "       size_usdc, status FROM live_trades "
                "WHERE market_id = $1 ORDER BY opened_at DESC LIMIT 5",
                mid,
            )
        payload = dict(market)
        payload["positions"] = [dict(r) for r in paper_positions] + [
            dict(r) for r in live_positions
        ]
        return formatters.format_market_detail(payload)
    except Exception as e:
        logger.warning(f"/market failed: {e}")
        return f"❌ /market failed: {e}"


# --------------------------------------------------------------------------- #
# Write commands                                                               #
# --------------------------------------------------------------------------- #


async def cmd_set(ctx: CommandContext, args: list[str]) -> str:
    """/set <key> <value> — update a runtime_config knob.

    Delegates validation to RuntimeConfig.apply(), which enforces BOUNDS
    and rejects unknown keys. Persists to Redis + publishes
    runtime_config:changed (which the notifier surfaces back via
    the runtime_config:changed channel, so the operator sees the diff
    confirmed).
    """
    if len(args) < 2:
        return "Usage: /set <key> <value>"
    key = args[0].strip()
    value_raw = " ".join(args[1:]).strip()
    # Try float first, then int, fallback to string.
    parsed: object
    try:
        parsed = float(value_raw)
        if parsed.is_integer() and "." not in value_raw:
            parsed = int(parsed)
    except (TypeError, ValueError):
        parsed = value_raw

    rc = get_runtime_config()
    try:
        before = await rc.effective()
        old_value = before.get(key)
        new_state = await rc.apply({key: parsed}, actor="telegram_operator")
        new_value = new_state.get(key)
        if new_value == old_value and parsed != old_value:
            # apply() silently rejected (validation fail). The published
            # runtime_config:changed will tell us via the "rejected" log,
            # but we want to surface that here too.
            return formatters.format_set_rejected(
                key=key, reason="rejected by validator (out of bounds or unknown key)"
            )
        return formatters.format_set_ok(
            key=key, old_value=old_value, new_value=new_value
        )
    except Exception as e:
        logger.exception("/set failed")
        return formatters.format_set_rejected(key=key, reason=str(e))


async def cmd_verbosity(ctx: CommandContext, args: list[str]) -> str:
    """/verbosity <quiet|normal|verbose|debug> — change notifier filter."""
    if not args:
        return "Usage: /verbosity <quiet|normal|verbose|debug>"
    if ctx.notifier is None:
        return "❌ notifier not wired — verbosity is read-only in this build"
    new_level = args[0].strip().lower()
    try:
        old_level = ctx.notifier.current_verbosity()
        applied = ctx.notifier.set_verbosity(new_level)
        return formatters.format_verbosity_changed(old=old_level, new=applied)
    except ValueError as e:
        return f"❌ /verbosity: {e}"


async def cmd_alert(ctx: CommandContext, args: list[str]) -> str:
    """/alert list|add|remove ... — manage configurable alert rules."""
    if ctx.alerts_mgr is None:
        return "❌ alerts manager not wired"
    if not args:
        return formatters.format_alert_help()
    sub = args[0].strip().lower()
    if sub == "list":
        rules = await ctx.alerts_mgr.list_rules()
        return formatters.format_alert_list(rules)
    if sub == "add":
        if len(args) < 3:
            return formatters.format_alert_help()
        rule_type = args[1].strip().lower()
        try:
            threshold = float(args[2])
        except (TypeError, ValueError):
            return f"❌ /alert add: invalid threshold {args[2]!r}"
        try:
            rule = await ctx.alerts_mgr.add_rule(
                rule_type=rule_type, threshold=threshold
            )
            return formatters.format_alert_added(
                rule_id=rule.id,
                channel=rule.channel,
                condition=rule.condition,
                threshold=rule.threshold,
            )
        except ValueError as e:
            return f"❌ /alert add: {e}"
    if sub == "remove":
        if len(args) < 2:
            return "Usage: /alert remove <id>"
        rule_id = args[1].strip()
        ok = await ctx.alerts_mgr.remove_rule(rule_id)
        if not ok:
            return f"❌ no rule with id={rule_id}"
        return formatters.format_alert_removed(rule_id)
    return formatters.format_alert_help()
