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
    construction time and passed to each handler call."""
    redis_client: object
    killswitch: KillswitchService
    paper_trader: object = None  # PaperTrader instance, optional
    live_trader: object = None   # LiveTrader instance, optional


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
    """/pnl — realized + unrealized PnL across paper and live."""
    from src.database.connection import get_db
    from src.engine.portfolio_state import load_state

    paper_realized = None
    paper_unrealized = None
    try:
        state = await load_state()
        paper_realized = float(state.realized_pnl_cum)
        # Unrealized = current capital - (initial PAPER_CAPITAL - realized)
        # Approximate; the precise mark-to-market lives in PaperTrader.
        if ctx.paper_trader is not None:
            current = float(ctx.paper_trader.capital)
            # rough estimate of float value of open positions
            open_value = sum(
                float(t.size_usdc) for t in ctx.paper_trader.open_trades
            )
            paper_unrealized = (current + open_value) - (
                float(settings.PAPER_CAPITAL_USDC) + paper_realized
            )
    except Exception as e:
        logger.warning(f"telegram cmd: paper pnl failed: {e}")
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
