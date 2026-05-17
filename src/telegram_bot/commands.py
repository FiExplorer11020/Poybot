"""
Command handlers for the Telegram bot (S3.9).

Each handler is a thin async function taking (ctx, args) — the bot.py
adapter wraps them into python-telegram-bot CommandHandler signatures.
Splitting the logic from the framework lets us unit-test commands
without spinning up a real Telegram client.

Read commands (status / pnl / positions) hit the DB. Write commands
(mode / killswitch / pause / resume) mutate Redis or the killswitch
service. All errors are caught and turned into a user-friendly reply
— a crashed handler must NOT take down the long-poll loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.config import settings
from src.control.killswitch import KillswitchService
from src.engine.decision_router import TradingMode
from src.telegram_bot import formatters


# --------------------------------------------------------------------------- #
# Context bundle injected by TelegramBot                                       #
# --------------------------------------------------------------------------- #


@dataclass
class CommandContext:
    """Everything a command handler may need. Built once at TelegramBot
    construction time and passed to each handler call.

    The notifier/alerts_mgr fields are wired in S3.11 to support the
    new /verbosity, /digest, and /alert commands. They're Optional so
    older test fixtures (which build a context with just redis +
    killswitch) keep working.
    """
    redis_client: object
    killswitch: KillswitchService
    paper_trader: object = None  # PaperTrader instance, optional
    live_trader: object = None   # LiveTrader instance, optional
    notifier: object = None      # TelegramNotifier, for /verbosity + /digest
    alerts_mgr: object = None    # AlertsManager, for /alert list|add|remove
    engine_started_at: float = 0.0  # monotonic-ish for /health uptime


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


async def _read_active_mode(ctx: CommandContext) -> str:
    """Resolve the current effective TRADING_MODE. Identical resolution
    order to DecisionRouter._active_mode but read-only."""
    try:
        raw = await ctx.redis_client.get(settings.TRADING_MODE_OVERRIDE_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode()
        parsed = TradingMode.parse(raw)
        if parsed is not None:
            return parsed.value
    except Exception:
        pass
    env = TradingMode.parse(settings.TRADING_MODE)
    return env.value if env else "paper"


async def _count_live_trades(ctx: CommandContext, status: str) -> int:
    from src.database.connection import get_db

    try:
        async with get_db() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM live_trades WHERE status = $1", status
            )
        return int(n or 0)
    except Exception as e:
        logger.warning(f"telegram cmd: live count failed: {e}")
        return 0


async def _live_realized_pnl(ctx: CommandContext) -> Optional[float]:
    from src.database.connection import get_db

    try:
        async with get_db() as conn:
            v = await conn.fetchval(
                "SELECT COALESCE(SUM(pnl_usdc), 0) FROM live_trades "
                "WHERE status = 'closed'"
            )
        return float(v or 0.0)
    except Exception as e:
        logger.warning(f"telegram cmd: live pnl failed: {e}")
        return None


async def _open_positions_snapshot(ctx: CommandContext, table: str) -> list[dict]:
    """Pull a readable snapshot of open positions from a paper_trades or
    live_trades table — sorted by opened_at DESC, capped at 25."""
    from src.database.connection import get_db

    if table == "paper_trades":
        where = "status = 'open'"
        cols = "market_id, strategy, direction, entry_price, size_usdc, " "'open' AS status"
    elif table == "live_trades":
        where = "status IN ('pending', 'open', 'shadow')"
        cols = "market_id, strategy, direction, entry_price, size_usdc, status"
    else:
        return []
    try:
        async with get_db() as conn:
            rows = await conn.fetch(
                f"SELECT {cols} FROM {table} WHERE {where} "
                f"ORDER BY opened_at DESC LIMIT 25"
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"telegram cmd: snapshot {table} failed: {e}")
        return []


# --------------------------------------------------------------------------- #
# Read commands                                                                #
# --------------------------------------------------------------------------- #


async def cmd_status(ctx: CommandContext) -> str:
    """/status — current mode, capital, open-position counts, killswitch."""
    mode = await _read_active_mode(ctx)
    paper_capital = None
    paper_open = 0
    live_open = 0
    if ctx.paper_trader is not None:
        try:
            paper_capital = float(ctx.paper_trader.capital)
        except Exception:
            paper_capital = None
        try:
            paper_open = len(ctx.paper_trader.open_trades)
        except Exception:
            paper_open = 0
    if ctx.live_trader is not None:
        try:
            live_open = len(ctx.live_trader.open_trades)
        except Exception:
            live_open = 0
    try:
        ks = await ctx.killswitch.get_state()
        ks_exec = bool(ks.execution_enabled)
        ks_real = bool(ks.real_execution_enabled)
    except Exception:
        ks_exec, ks_real = False, False
    return formatters.format_status(
        mode=mode,
        paper_capital=paper_capital,
        paper_open=paper_open,
        live_open=live_open,
        killswitch_exec=ks_exec,
        killswitch_real=ks_real,
    )


async def cmd_pnl(ctx: CommandContext) -> str:
    """/pnl — realized + unrealized PnL across paper and live.

    Unrealized PnL is the mark-to-market value of open positions, computed
    by `PaperTrader.compute_unrealized_pnl` (sum of
    `(current_price - entry_price)/entry_price * size_usdc` per trade).
    The earlier cost-basis approximation always returned ~$0 regardless of
    price movement; see docs/AUDIT_PAPER_TRADING_2026_05_17.md.
    """
    from src.database.connection import get_db
    from src.engine.portfolio_state import load_state

    paper_realized = None
    paper_unrealized = None
    try:
        state = await load_state()
        paper_realized = float(state.realized_pnl_cum)
    except Exception as e:
        logger.warning(f"telegram cmd: paper realized pnl failed: {e}")
    if ctx.paper_trader is not None:
        try:
            paper_unrealized = await ctx.paper_trader.compute_unrealized_pnl()
        except Exception as e:
            logger.warning(f"telegram cmd: paper unrealized pnl failed: {e}")
    live_realized = await _live_realized_pnl(ctx)
    shadow_n = await _count_live_trades(ctx, "shadow")
    real_n = 0
    try:
        async with get_db() as conn:
            real_n = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM live_trades "
                    "WHERE status IN ('open', 'closed') "
                    "AND status != 'shadow'"
                )
                or 0
            )
    except Exception:
        real_n = 0
    return formatters.format_pnl(
        paper_realized=paper_realized,
        paper_unrealized=paper_unrealized,
        live_realized=live_realized,
        live_shadow_count=shadow_n,
        live_real_count=real_n,
    )


async def cmd_positions(ctx: CommandContext) -> str:
    """/positions — list of open positions across paper + live."""
    paper = await _open_positions_snapshot(ctx, "paper_trades")
    live = await _open_positions_snapshot(ctx, "live_trades")
    return formatters.format_positions(paper_positions=paper, live_positions=live)


async def cmd_summary(ctx: CommandContext) -> str:
    """/summary — today's trading activity (UTC since 00:00).

    Aggregates closed paper_trades since UTC midnight: wins/losses, average
    PnL, breakdown by close_reason and strategy. Adds the cumulative
    lifetime realized PnL and the current unrealized PnL on open positions
    so the operator gets a single-screen view of where the portfolio
    stands without scrolling through close alerts.
    """
    from src.database.connection import get_db
    from src.engine.portfolio_state import load_state

    payload: dict = {
        "trades_closed_today": 0,
        "trades_open": 0,
        "wins": 0,
        "losses": 0,
        "avg_win": None,
        "avg_loss": None,
        "net_today": 0.0,
        "cum_realized": None,
        "unrealized": None,
        "by_reason": [],   # list of {reason, count, avg_pnl}
        "by_strategy": [], # list of {strategy, count, wins, losses}
    }

    # Open count + unrealized PnL come straight from the paper trader.
    if ctx.paper_trader is not None:
        try:
            payload["trades_open"] = len(ctx.paper_trader.open_trades)
        except Exception:
            payload["trades_open"] = 0
        try:
            payload["unrealized"] = await ctx.paper_trader.compute_unrealized_pnl()
        except Exception as e:
            logger.warning(f"telegram cmd: summary unrealized failed: {e}")

    # Lifetime realized PnL from portfolio_state.
    try:
        state = await load_state()
        payload["cum_realized"] = float(state.realized_pnl_cum)
    except Exception as e:
        logger.warning(f"telegram cmd: summary cum realized failed: {e}")

    # Today's aggregates + breakdowns.
    try:
        async with get_db() as conn:
            totals = await conn.fetchrow(
                "SELECT "
                "  COUNT(*) AS n_closed, "
                "  COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins, "
                "  COUNT(*) FILTER (WHERE pnl_usdc <= 0) AS losses, "
                "  COALESCE(SUM(pnl_usdc), 0) AS net_today, "
                "  AVG(pnl_usdc) FILTER (WHERE pnl_usdc > 0) AS avg_win, "
                "  AVG(pnl_usdc) FILTER (WHERE pnl_usdc <= 0) AS avg_loss "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
            )
            if totals is not None:
                payload["trades_closed_today"] = int(totals["n_closed"] or 0)
                payload["wins"] = int(totals["wins"] or 0)
                payload["losses"] = int(totals["losses"] or 0)
                payload["net_today"] = float(totals["net_today"] or 0.0)
                payload["avg_win"] = (
                    float(totals["avg_win"]) if totals["avg_win"] is not None else None
                )
                payload["avg_loss"] = (
                    float(totals["avg_loss"]) if totals["avg_loss"] is not None else None
                )

            reason_rows = await conn.fetch(
                "SELECT close_reason, COUNT(*) AS n, AVG(pnl_usdc) AS avg_pnl "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                "GROUP BY close_reason "
                "ORDER BY n DESC"
            )
            payload["by_reason"] = [
                {
                    "reason": r["close_reason"] or "?",
                    "count": int(r["n"] or 0),
                    "avg_pnl": float(r["avg_pnl"]) if r["avg_pnl"] is not None else None,
                }
                for r in reason_rows
            ]

            strat_rows = await conn.fetch(
                "SELECT strategy, "
                "       COUNT(*) AS n, "
                "       COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins, "
                "       COUNT(*) FILTER (WHERE pnl_usdc <= 0) AS losses "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                "GROUP BY strategy "
                "ORDER BY n DESC"
            )
            payload["by_strategy"] = [
                {
                    "strategy": r["strategy"] or "?",
                    "count": int(r["n"] or 0),
                    "wins": int(r["wins"] or 0),
                    "losses": int(r["losses"] or 0),
                }
                for r in strat_rows
            ]
    except Exception as e:
        logger.warning(f"telegram cmd: summary aggregation failed: {e}")

    return formatters.format_summary(payload)


# --------------------------------------------------------------------------- #
# Write commands                                                               #
# --------------------------------------------------------------------------- #


async def cmd_mode(ctx: CommandContext, args: list[str]) -> str:
    """/mode <paper|live|dual> — flip the runtime override key."""
    if not args:
        return "Usage: /mode <paper|live|dual>"
    target = TradingMode.parse(args[0])
    if target is None:
        return f"Invalid mode: {args[0]!r}. Use paper, live, or dual."
    old = await _read_active_mode(ctx)
    try:
        await ctx.redis_client.set(settings.TRADING_MODE_OVERRIDE_KEY, target.value)
    except Exception as e:
        logger.error(f"telegram cmd: failed to set override: {e}")
        return f"❌ Failed to set mode: {e}"
    return formatters.format_mode_change(old_mode=old, new_mode=target.value)


async def cmd_killswitch(ctx: CommandContext, args: list[str]) -> str:
    """/killswitch <on|off> — flip the master execution switch."""
    if not args:
        return "Usage: /killswitch <on|off>"
    arg = args[0].strip().lower()
    if arg not in {"on", "off"}:
        return f"Invalid arg: {args[0]!r}. Use on or off."
    enabled = arg == "on"
    try:
        new_state = await ctx.killswitch.set_execution_enabled(
            enabled,
            reason=f"telegram_command:{arg}",
            actor="telegram_operator",
        )
    except Exception as e:
        logger.error(f"telegram cmd: killswitch flip failed: {e}")
        return f"❌ Failed to flip killswitch: {e}"
    return (
        f"✅ Killswitch flipped to {arg.upper()}\n"
        f"execution: {'ON' if new_state.execution_enabled else 'OFF'}\n"
        f"real:      {'ON' if new_state.real_execution_enabled else 'OFF'}"
    )


async def cmd_pause(ctx: CommandContext) -> str:
    """/pause — temporary engine-only stop. Disables execution_enabled
    via killswitch, keeping the observer running."""
    try:
        await ctx.killswitch.set_execution_enabled(
            False,
            reason="telegram_command:pause",
            actor="telegram_operator",
        )
    except Exception as e:
        return f"❌ Pause failed: {e}"
    return "⏸️ Engine paused (execution=OFF). Use /resume to restart."


async def cmd_resume(ctx: CommandContext) -> str:
    """/resume — re-enable execution_enabled. Note: real_execution_enabled
    stays in whatever state it was in (we don't auto-flip live trading)."""
    try:
        await ctx.killswitch.set_execution_enabled(
            True,
            reason="telegram_command:resume",
            actor="telegram_operator",
        )
    except Exception as e:
        return f"❌ Resume failed: {e}"
    return "▶️ Engine resumed (execution=ON)."


async def cmd_help(ctx: CommandContext) -> str:
    return formatters.format_help()
