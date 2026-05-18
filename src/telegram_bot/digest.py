"""
Digest builders for the Telegram bot (S3.11).

Two flavors:
  * hourly — last 60 minutes of activity; pushed by the scheduler if any
    activity exists. Used to keep the operator looped in without manual
    /summary polling.
  * daily — yesterday's full snapshot; pushed at TELEGRAM_DIGEST_DAILY_HOUR_UTC.

Both are also available via /digest. The /digest command picks daily by
default (richer); pass /digest hourly for the short form.

Pure I/O: each builder queries the DB + portfolio_state + counters, returns
a dict that the matching formatter in formatters_replies turns into a
message. Builders never send to Telegram themselves — the scheduler
calls notifier.push() with the formatted text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger


# Redis counters incremented by the publishers we already wired. They're
# rolling 24h windows maintained by the alerts evaluator (see alerts.py).
COUNTER_BREAKER_HITS = "telegram:counter:breaker_hits:{window}"
COUNTER_DRIFT_EVENTS = "telegram:counter:drift_events:{window}"
COUNTER_PHASE_TRANSITIONS = "telegram:counter:phase_transitions:{window}"
COUNTER_NEW_LEADERS = "telegram:counter:new_leaders:{window}"
# S3.12: silent-counted in the notifier (no instant Telegram message)
# and surfaced in the daily digest as "new followers" — operator
# explicitly asked to stop the per-event flood during cold-start.
COUNTER_FOLLOWER_CONFIRMED = "telegram:counter:follower_confirmed:{window}"


async def _read_counter(redis_client, key: str) -> int:
    """Read a counter Redis key; 0 if missing or unreachable."""
    if redis_client is None:
        return 0
    try:
        raw = await redis_client.get(key)
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(raw)
    except Exception:
        return 0


async def build_hourly_digest(*, redis_client, paper_trader=None) -> Optional[dict]:
    """Build the hourly-digest payload from the last 60 min of paper_trades.

    Returns None when the window is empty AND there are no notable
    events — the scheduler then skips the push so the operator doesn't
    get "0 trades, $0 PnL" hourly noise.
    """
    from src.database.connection import get_db

    payload: dict = {
        "trades_closed": 0,
        "trades_opened": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
        "top_market": None,
        "circuit_breaker_hits": await _read_counter(
            redis_client, COUNTER_BREAKER_HITS.format(window="1h")
        ),
        "drift_events": await _read_counter(
            redis_client, COUNTER_DRIFT_EVENTS.format(window="1h")
        ),
    }

    try:
        async with get_db() as conn:
            opened = await conn.fetchval(
                "SELECT COUNT(*) FROM paper_trades "
                "WHERE opened_at >= NOW() - INTERVAL '60 minutes'"
            )
            closed_row = await conn.fetchrow(
                "SELECT COUNT(*) AS n, "
                "       COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins, "
                "       COUNT(*) FILTER (WHERE pnl_usdc <= 0) AS losses, "
                "       COALESCE(SUM(pnl_usdc), 0) AS net "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= NOW() - INTERVAL '60 minutes'"
            )
            top_market_row = await conn.fetchrow(
                "SELECT market_id, COUNT(*) AS n FROM paper_trades "
                "WHERE opened_at >= NOW() - INTERVAL '60 minutes' "
                "GROUP BY market_id ORDER BY n DESC LIMIT 1"
            )
        payload["trades_opened"] = int(opened or 0)
        if closed_row:
            payload["trades_closed"] = int(closed_row["n"] or 0)
            payload["wins"] = int(closed_row["wins"] or 0)
            payload["losses"] = int(closed_row["losses"] or 0)
            payload["net_pnl"] = float(closed_row["net"] or 0.0)
        if top_market_row:
            payload["top_market"] = top_market_row["market_id"]
    except Exception as e:
        logger.warning(f"hourly digest: db query failed: {e}")

    # Skip push if no activity and no notable events.
    if (
        payload["trades_opened"] == 0
        and payload["trades_closed"] == 0
        and payload["circuit_breaker_hits"] == 0
        and payload["drift_events"] == 0
    ):
        return None
    return payload


async def build_daily_digest(*, redis_client, paper_trader=None) -> dict:
    """Build the daily-digest payload (today since UTC midnight)."""
    from src.database.connection import get_db
    from src.engine.portfolio_state import load_state

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    payload: dict = {
        "date": date_str,
        "trades_closed": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
        "win_rate": None,
        "cum_realized": None,
        "unrealized": None,
        "best_trade": None,
        "worst_trade": None,
        "top_leader": None,
        "circuit_breaker_hits": await _read_counter(
            redis_client, COUNTER_BREAKER_HITS.format(window="24h")
        ),
        "drift_events": await _read_counter(
            redis_client, COUNTER_DRIFT_EVENTS.format(window="24h")
        ),
        "phase_transitions": await _read_counter(
            redis_client, COUNTER_PHASE_TRANSITIONS.format(window="24h")
        ),
        "new_leaders": await _read_counter(
            redis_client, COUNTER_NEW_LEADERS.format(window="24h")
        ),
        "new_followers_confirmed": await _read_counter(
            redis_client, COUNTER_FOLLOWER_CONFIRMED.format(window="24h")
        ),
    }

    try:
        state = await load_state()
        payload["cum_realized"] = float(state.realized_pnl_cum)
    except Exception as e:
        logger.warning(f"daily digest: load_state failed: {e}")

    if paper_trader is not None:
        try:
            payload["unrealized"] = await paper_trader.compute_unrealized_pnl()
        except Exception as e:
            logger.warning(f"daily digest: unrealized failed: {e}")

    try:
        async with get_db() as conn:
            totals = await conn.fetchrow(
                "SELECT COUNT(*) AS n, "
                "       COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins, "
                "       COUNT(*) FILTER (WHERE pnl_usdc <= 0) AS losses, "
                "       COALESCE(SUM(pnl_usdc), 0) AS net "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
            )
            if totals:
                payload["trades_closed"] = int(totals["n"] or 0)
                payload["wins"] = int(totals["wins"] or 0)
                payload["losses"] = int(totals["losses"] or 0)
                payload["net_pnl"] = float(totals["net"] or 0.0)
                total_resolved = payload["wins"] + payload["losses"]
                if total_resolved > 0:
                    payload["win_rate"] = payload["wins"] / total_resolved

            best = await conn.fetchrow(
                "SELECT id, market_id, pnl_usdc FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                "ORDER BY pnl_usdc DESC NULLS LAST LIMIT 1"
            )
            worst = await conn.fetchrow(
                "SELECT id, market_id, pnl_usdc FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                "ORDER BY pnl_usdc ASC NULLS LAST LIMIT 1"
            )
            payload["best_trade"] = dict(best) if best else None
            payload["worst_trade"] = dict(worst) if worst else None

            # Top leader by net PnL today (paper trades attributed via
            # leader_wallet column).
            top_leader = await conn.fetchrow(
                "SELECT leader_wallet AS wallet_address, "
                "       COALESCE(SUM(pnl_usdc), 0) AS pnl_usdc "
                "FROM paper_trades "
                "WHERE status = 'closed' "
                "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                "  AND leader_wallet IS NOT NULL "
                "GROUP BY leader_wallet "
                "ORDER BY pnl_usdc DESC LIMIT 1"
            )
            payload["top_leader"] = dict(top_leader) if top_leader else None
    except Exception as e:
        logger.warning(f"daily digest: db query failed: {e}")

    return payload
