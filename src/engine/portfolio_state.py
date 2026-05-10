"""
Portfolio state persistence.

The PaperTrader's bankroll, peak capital, and consecutive-loss counter used to
live only in memory, so every restart reset the P&L and broke the drawdown
circuit breaker.  This module owns the `portfolio_state` singleton row plus
the `portfolio_equity` time-series used by the dashboard equity curve.

`save_state` and `record_equity` accept an optional `conn`; when supplied,
they run on the caller's connection so the write can participate in an
existing `conn.transaction()` block (see paper_trader's open/close trade
flows). When omitted, they acquire their own pooled connection as before.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from src.config import settings
from src.database.connection import get_db

_SINGLETON_ID = 1


@asynccontextmanager
async def _conn_ctx(conn):
    """Yield `conn` if provided, else acquire one from the pool.

    Keeps caller code identical whether or not the connection is being
    threaded through from an outer transaction.
    """
    if conn is not None:
        yield conn
        return
    async with get_db() as acquired:
        yield acquired


@dataclass
class PortfolioState:
    capital: float
    peak_capital: float
    realized_pnl_cum: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    updated_at: datetime | None = None

    @classmethod
    def default(cls) -> "PortfolioState":
        cap = float(settings.PAPER_CAPITAL_USDC)
        return cls(capital=cap, peak_capital=cap, realized_pnl_cum=0.0)


async def load_state() -> PortfolioState:
    """Read the singleton row.  If missing, create it with defaults."""
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT capital, peak_capital, realized_pnl_cum,
                       consecutive_losses, open_positions, updated_at
                FROM portfolio_state
                WHERE id = $1
                """,
                _SINGLETON_ID,
            )
            if row is None:
                state = PortfolioState.default()
                await save_state(state)
                return state
            return PortfolioState(
                capital=float(row["capital"]),
                peak_capital=float(row["peak_capital"]),
                realized_pnl_cum=float(row["realized_pnl_cum"] or 0),
                consecutive_losses=int(row["consecutive_losses"] or 0),
                open_positions=int(row["open_positions"] or 0),
                updated_at=row["updated_at"],
            )
    except Exception as exc:
        logger.warning(f"portfolio_state load failed, using defaults: {exc}")
        return PortfolioState.default()


async def save_state(state: PortfolioState, *, conn=None) -> None:
    """Upsert the singleton row.

    If `conn` is given, the UPSERT runs on the caller's connection (and
    therefore inside any active `conn.transaction()` it owns). Otherwise a
    pooled connection is acquired and released here.
    """
    try:
        async with _conn_ctx(conn) as c:
            await c.execute(
                """
                INSERT INTO portfolio_state
                    (id, capital, peak_capital, realized_pnl_cum,
                     consecutive_losses, open_positions, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    capital            = EXCLUDED.capital,
                    peak_capital       = EXCLUDED.peak_capital,
                    realized_pnl_cum   = EXCLUDED.realized_pnl_cum,
                    consecutive_losses = EXCLUDED.consecutive_losses,
                    open_positions     = EXCLUDED.open_positions,
                    updated_at         = NOW()
                """,
                _SINGLETON_ID,
                round(state.capital, 2),
                round(state.peak_capital, 2),
                round(state.realized_pnl_cum, 2),
                int(state.consecutive_losses),
                int(state.open_positions),
            )
    except Exception as exc:
        logger.error(f"portfolio_state save failed: {exc}")


async def record_equity(
    *,
    capital: float,
    unrealized_pnl: float,
    realized_pnl_cum: float,
    open_positions: int,
    when: datetime | None = None,
    conn=None,
) -> None:
    """Append a mark-to-market sample to `portfolio_equity`.

    Pass `conn` to participate in an outer transaction; omit to acquire a
    fresh pooled connection.
    """
    ts = when or datetime.now(tz=timezone.utc)
    equity = capital + unrealized_pnl
    try:
        async with _conn_ctx(conn) as c:
            await c.execute(
                """
                INSERT INTO portfolio_equity
                    (time, capital, equity, unrealized_pnl,
                     realized_pnl_cum, open_positions)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (time) DO UPDATE SET
                    capital          = EXCLUDED.capital,
                    equity           = EXCLUDED.equity,
                    unrealized_pnl   = EXCLUDED.unrealized_pnl,
                    realized_pnl_cum = EXCLUDED.realized_pnl_cum,
                    open_positions   = EXCLUDED.open_positions
                """,
                ts,
                round(capital, 2),
                round(equity, 2),
                round(unrealized_pnl, 2),
                round(realized_pnl_cum, 2),
                int(open_positions),
            )
    except Exception as exc:
        logger.warning(f"portfolio_equity insert failed: {exc}")
