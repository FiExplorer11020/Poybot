"""
All database queries for the API layer.
Each function accepts an asyncpg connection and returns plain dicts/lists.
No SQL lives outside this module.
"""

import asyncio
import json
import time as _time_module  # avoid shadowing `time` columns/locals
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.economics.versioning import (
    valid_decision_filter,
    valid_paper_trade_filter,
    valid_position_filter,
    valid_profile_learning_filter,
)
from src.profiler.behavior_profiler import _reason_penalty_from_profile

V1_PAPER_TRADE_SQL = valid_paper_trade_filter()
V1_PAPER_TRADE_PT_SQL = valid_paper_trade_filter("pt")
V1_DECISION_D_SQL = valid_decision_filter("d")
V1_POSITION_SQL = valid_position_filter()
V1_PROFILE_P_SQL = valid_profile_learning_filter("p")
V1_PROFILE_TABLE_SQL = valid_profile_learning_filter("leader_profiles")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _json_dict(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if raw is None:
        return {}
    try:
        return dict(raw)
    except Exception:
        return {}


def _json_value(raw: Any, key: str, default: Any = None) -> Any:
    return _json_dict(raw).get(key, default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _market_type_label(category: Any, question: Any = None) -> str:
    category_text = str(category or "").strip()
    text = f"{category_text} {question or ''}".lower()
    sports_tokens = (
        " vs ",
        " o/u ",
        "map ",
        "set ",
        "grand prix",
        "premier league",
        "champions league",
        "world cup",
        "tennis",
        "soccer",
        "football",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "cup",
        "fc",
        "winner",
        " win on 20",
    )
    if any(
        token in text for token in ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp")
    ):
        return "crypto"
    if any(token in text for token in sports_tokens):
        return "sports"
    if any(
        token in text
        for token in ("election", "president", "senate", "vote", "parliament", "mayor")
    ):
        return "politics"
    if any(token in text for token in ("fed", "inflation", "cpi", "rate cut", "recession", "gdp")):
        return "macro"
    if any(token in text for token in ("movie", "album", "oscar", "grammy", "tv", "show")):
        return "entertainment"
    if category_text and category_text.lower() != "unknown":
        return category_text
    return "unknown"


def _wallet_status(row: Any) -> str:
    if bool(_row_get(row, "excluded", False)):
        return "excluded"
    if bool(_row_get(row, "on_watchlist", False)):
        return "active"
    if bool(_row_get(row, "is_leader", False)):
        return "watching"
    return "external"


def _decision_bucket_view(bucket: dict) -> dict:
    if not isinstance(bucket, dict):
        bucket = {}
    wins = _to_int(bucket.get("wins", 0))
    losses = _to_int(bucket.get("losses", 0))
    samples = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "samples": samples,
        "win_rate": round(wins / samples, 4) if samples else 0.0,
        "beta_a": _to_float(bucket.get("beta_a", 1.0), 1.0),
        "beta_b": _to_float(bucket.get("beta_b", 1.0), 1.0),
        "avg_win_pnl": _to_float(bucket.get("avg_win_pnl", 0.0), 0.0),
        "avg_loss_pnl": _to_float(bucket.get("avg_loss_pnl", 0.0), 0.0),
        "avg_win_confidence": _to_float(bucket.get("avg_win_confidence", 0.0), 0.0),
        "avg_loss_confidence": _to_float(bucket.get("avg_loss_confidence", 0.0), 0.0),
        "top_reasons": _top_reason_stats(bucket.get("reason_stats", {})),
    }


def _top_reason_stats(reason_stats: dict, limit: int = 5) -> list[dict]:
    rows: list[dict] = []
    for code, stats in (reason_stats or {}).items():
        wins = _to_int(stats.get("wins", 0))
        losses = _to_int(stats.get("losses", 0))
        samples = wins + losses
        if samples <= 0:
            continue
        rows.append(
            {
                "code": code,
                "wins": wins,
                "losses": losses,
                "samples": samples,
                "loss_rate": round(losses / samples, 4),
                "avg_pnl": _to_float(stats.get("avg_pnl", 0.0), 0.0),
            }
        )
    rows.sort(key=lambda item: (item["losses"], item["samples"]), reverse=True)
    return rows[:limit]


def _extract_ml_snapshot(raw_context: Any) -> dict:
    context = _json_dict(raw_context)
    trade_context = context.get("trade_context") or {}
    if not isinstance(trade_context, dict):
        trade_context = {}
    reason_codes = trade_context.get("reason_codes") or []
    if not isinstance(reason_codes, list):
        reason_codes = []
    return {
        "action": context.get("action"),
        "context_penalty": _to_float(
            context.get("context_penalty", trade_context.get("context_penalty", 0.0)),
            0.0,
        ),
        "process_penalty": _to_float(trade_context.get("process_penalty", 0.0), 0.0),
        "process_score": _to_float(trade_context.get("process_score", 0.0), 0.0),
        "p_error": trade_context.get("p_error"),
        "error_confidence": trade_context.get("error_confidence"),
        "error_phase": trade_context.get("error_phase"),
        "deviation_score": trade_context.get("deviation_score"),
        "size_ratio": trade_context.get("size_ratio"),
        "liquidity_score": trade_context.get("liquidity_score"),
        "category": trade_context.get("category"),
        "reason_codes": list(reason_codes),
        "profile_maturity": trade_context.get("profile_maturity"),
        "confirmed_followers": trade_context.get("confirmed_followers"),
        "is_contrarian": bool(trade_context.get("is_contrarian", False)),
        "trade_age_s": _to_float(trade_context.get("trade_age_s"), 0.0)
        if trade_context.get("trade_age_s") is not None
        else None,
        "live_candidate": trade_context.get("live_candidate"),
        "trade_source": trade_context.get("trade_source"),
    }


def _is_invalid_paper_trade_record(row: Any, ml_snapshot: dict) -> bool:
    if _row_get(row, "status") != "closed":
        return False
    if _row_get(row, "close_reason") != "market_resolved":
        return False

    live_candidate = ml_snapshot.get("live_candidate")
    if live_candidate is False:
        return True

    trade_age_s = ml_snapshot.get("trade_age_s")
    if trade_age_s is not None and _to_float(trade_age_s, 0.0) > float(
        settings.LIVE_DECISION_MAX_TRADE_AGE_S
    ):
        return True

    age_s = _to_int(_row_get(row, "age_s", 0), 0)
    if age_s > int(settings.INVALID_LEARNING_CLOSE_WINDOW_S):
        return False

    opened_at = _parse_dt(_row_get(row, "opened_at"))
    end_date = _parse_dt(_row_get(row, "end_date"))
    if opened_at and end_date and opened_at > end_date:
        return True
    return False


def _match_recent_loss(
    profile: dict,
    strategy: str,
    market_id: str,
    close_reason: str | None,
    closed_at: Any,
) -> dict | None:
    recent_losses = (
        (profile or {}).get("loss_analysis", {}).get("recent_losses", [])
        if isinstance(profile, dict)
        else []
    )
    closed_dt = _parse_dt(closed_at)
    for item in recent_losses:
        if item.get("action") != strategy:
            continue
        if item.get("market_id") != market_id:
            continue
        if close_reason and item.get("close_reason") != close_reason:
            continue
        item_dt = _parse_dt(item.get("time"))
        if closed_dt and item_dt:
            if abs((closed_dt - item_dt).total_seconds()) > 900:
                continue
        return item
    return None


def _aggregate_ml_profiles(rows: list[Any]) -> dict:
    summary = {
        "leaders_with_process": 0,
        "leaders_with_decision_learning": 0,
        "drift_alerts": 0,
        "phase2_leaders": 0,
        "phase3_leaders": 0,
        "avg_process_score": 0.0,
        "follow": {"wins": 0, "losses": 0, "samples": 0, "win_rate": 0.0},
        "fade": {"wins": 0, "losses": 0, "samples": 0, "win_rate": 0.0},
        "top_loss_reasons": {"follow": [], "fade": []},
    }
    process_scores: list[float] = []
    reason_agg = {
        "follow": {},
        "fade": {},
    }

    for row in rows:
        profile = _json_dict(_row_get(row, "profile_json", {}))
        phase = _to_int(_row_get(row, "error_model_phase", 0), 0)
        if phase == 2:
            summary["phase2_leaders"] += 1
        elif phase >= 3:
            summary["phase3_leaders"] += 1

        process = profile.get("decision_process", {})
        orders_seen = _to_int(process.get("orders_seen", 0))
        process_score = _to_float(process.get("process_score_ewma", 0.0), 0.0)
        if orders_seen > 0:
            summary["leaders_with_process"] += 1
            process_scores.append(process_score)

        runtime = profile.get("error_model_runtime", {})
        if runtime.get("drift_alert"):
            summary["drift_alerts"] += 1

        learning = profile.get("decision_learning", {})
        learned = False
        for action in ("follow", "fade"):
            bucket = learning.get(action, {})
            if not isinstance(bucket, dict):
                bucket = {}
            wins = _to_int(bucket.get("wins", 0))
            losses = _to_int(bucket.get("losses", 0))
            samples = wins + losses
            if samples > 0:
                learned = True
            summary[action]["wins"] += wins
            summary[action]["losses"] += losses
            summary[action]["samples"] += samples
            for code, stats in (bucket.get("reason_stats", {}) or {}).items():
                agg = reason_agg[action].setdefault(
                    code,
                    {"wins": 0, "losses": 0, "samples": 0, "avg_pnl_sum": 0.0},
                )
                rwins = _to_int(stats.get("wins", 0))
                rlosses = _to_int(stats.get("losses", 0))
                rsamples = rwins + rlosses
                agg["wins"] += rwins
                agg["losses"] += rlosses
                agg["samples"] += rsamples
                agg["avg_pnl_sum"] += _to_float(stats.get("avg_pnl", 0.0), 0.0) * rsamples
        if learned:
            summary["leaders_with_decision_learning"] += 1

    if process_scores:
        summary["avg_process_score"] = round(sum(process_scores) / len(process_scores), 4)

    for action in ("follow", "fade"):
        samples = summary[action]["samples"]
        summary[action]["win_rate"] = (
            round(summary[action]["wins"] / samples, 4) if samples else 0.0
        )
        top = []
        for code, stats in reason_agg[action].items():
            samples = stats["samples"]
            top.append(
                {
                    "code": code,
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "samples": samples,
                    "loss_rate": round(stats["losses"] / samples, 4) if samples else 0.0,
                    "avg_pnl": round(stats["avg_pnl_sum"] / samples, 2) if samples else 0.0,
                }
            )
        top.sort(key=lambda item: (item["losses"], item["samples"]), reverse=True)
        summary["top_loss_reasons"][action] = top[:5]

    return summary


async def ml_summary(conn) -> dict:
    rows = await conn.fetch(
        f"""
        SELECT wallet_address, error_model_phase, profile_json
        FROM leader_profiles
        WHERE {V1_PROFILE_TABLE_SQL}
        """
    )
    return _aggregate_ml_profiles(list(rows))


async def _fetch_portfolio_snapshot(conn) -> dict:
    """Return the persisted portfolio_state singleton, or sensible defaults."""
    default_cap = float(settings.PAPER_CAPITAL_USDC)
    try:
        row = await conn.fetchrow(
            """
            SELECT capital, peak_capital, realized_pnl_cum,
                   consecutive_losses, open_positions
            FROM portfolio_state
            WHERE id = 1
            """
        )
    except Exception:
        row = None
    if row is None:
        return {
            "capital": default_cap,
            "peak_capital": default_cap,
            "realized_pnl_cum": 0.0,
            "consecutive_losses": 0,
            "drawdown_pct": 0.0,
            "open_positions": 0,
        }
    capital = float(row["capital"] or default_cap)
    peak = float(row["peak_capital"] or default_cap)
    drawdown = round((peak - capital) / peak, 4) if peak > 0 else 0.0
    return {
        "capital": capital,
        "peak_capital": peak,
        "realized_pnl_cum": float(row["realized_pnl_cum"] or 0),
        "consecutive_losses": int(row["consecutive_losses"] or 0),
        "drawdown_pct": drawdown,
        "open_positions": int(row["open_positions"] or 0),
    }


async def _fetch_equity_curve(conn, limit: int = 500) -> list[dict]:
    """Return the most recent mark-to-market samples for the dashboard chart."""
    try:
        rows = await conn.fetch(
            """
            SELECT time, capital, equity, unrealized_pnl,
                   realized_pnl_cum, open_positions
            FROM portfolio_equity
            ORDER BY time DESC
            LIMIT $1
            """,
            int(limit),
        )
    except Exception:
        return []
    # Reverse back to ascending order for time-series charts.
    return [
        {
            "time": r["time"].isoformat(),
            "capital": float(r["capital"] or 0),
            "equity": float(r["equity"] or 0),
            "unrealized_pnl": float(r["unrealized_pnl"] or 0),
            "realized_pnl_cum": float(r["realized_pnl_cum"] or 0),
            "open_positions": int(r["open_positions"] or 0),
        }
        for r in reversed(rows)
    ]


async def _compute_unrealized_pnl_total(conn, redis_client=None) -> float:
    """Sum direction-aware unrealized PnL across all open paper trades.

    Prefers the websocket-fed Redis price cache (`price:{market}:{token}`,
    populated by `observer.trade_observer`), which reflects the latest book-side
    price and stays fresh on markets that haven't printed a trade in minutes.
    Falls back to the most recent `trades_observed` row per (market, token).
    """
    try:
        rows = await conn.fetch(
            f"""
            WITH last_px AS (
                SELECT DISTINCT ON (market_id, token_id)
                    market_id, token_id, price, time
                FROM trades_observed
                WHERE time >= NOW() - INTERVAL '24 hours'
                ORDER BY market_id, token_id, time DESC
            )
            SELECT pt.market_id, pt.token_id, pt.direction,
                   pt.entry_price, pt.size_usdc, lp.price
            FROM paper_trades pt
            LEFT JOIN last_px lp
                   ON lp.market_id = pt.market_id
                  AND lp.token_id  = pt.token_id
            WHERE pt.status = 'open'
              AND {V1_PAPER_TRADE_PT_SQL}
            """
        )
    except Exception:
        return 0.0

    total = 0.0
    for r in rows:
        price = r["price"]
        entry = r["entry_price"]
        size = r["size_usdc"]
        if entry is None or not entry:
            continue
        # Prefer Redis cache — the websocket-fed price is fresher than the
        # latest trades_observed row on low-liquidity markets.
        if redis_client is not None:
            try:
                cached = await redis_client.get(f"price:{r['market_id']}:{r['token_id']}")
                if cached is not None:
                    price = cached
            except Exception:
                pass
        if price is None:
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        direction = r["direction"]
        pct = (
            (price_f - float(entry)) / float(entry)
            if direction == "yes"
            else (float(entry) - price_f) / float(entry)
        )
        total += pct * float(size or 0)
    return round(total, 2)


async def overview(conn, redis_client=None) -> dict:
    """Compose the V1 dashboard overview snapshot.

    PERFORMANCE NOTES (post-Phase-1 audit):
    - `COUNT(*) FROM trades_observed` (full partitioned table aggregate)
      was the dominant cost — EXPLAIN ANALYZE measured **2.9s per call**.
      We now read `pg_class.reltuples` (planner stats, ~±5% accuracy) in
      one micro-query (<1ms). The dashboard's `total_trades` counter is
      an order-of-magnitude indicator, not an accountancy figure, so
      ±5% drift is acceptable.
    - The 11 sequential sub-queries on one connection are kept in this
      arrangement (still on the caller's `conn`) because asyncpg
      connections cannot multiplex. Parallel rebuild is achieved upstream
      in `_get_terminal_snapshot()` which gathers section-level fetches
      across distinct pool connections.

    Together with the cache TTL bump (1s → 5s) and the killswitch DB
    pool fix, this should bring live-summary cold from 30s → ~3s.
    """

    # --- Paper trade aggregate (one query for total_pnl + win_rate +
    #     pnl_daily series, replacing three separate fetches) ----------
    paper_aggregate = await conn.fetchrow(
        f"""
        SELECT
            COALESCE(SUM(pnl_usdc) FILTER (WHERE status='closed'), 0)::float AS total_pnl,
            (COUNT(*) FILTER (WHERE status='closed' AND pnl_usdc > 0)::float /
             NULLIF(COUNT(*) FILTER (WHERE status='closed'), 0))             AS win_rate,
            COUNT(*) FILTER (WHERE status='open')::int                       AS open_positions
        FROM paper_trades
        WHERE {V1_PAPER_TRADE_SQL}
        """
    )
    # Defensive: when paper_trades is empty AND the mock layer returns
    # None instead of a record with NULL fields, treat it as zeros.
    if paper_aggregate is None:
        total_pnl = 0.0
        win_rate_row = {"win_rate": None}
        open_positions = 0
    else:
        total_pnl = float(_row_get(paper_aggregate, "total_pnl") or 0)
        win_rate_row = {"win_rate": _row_get(paper_aggregate, "win_rate")}
        open_positions = int(_row_get(paper_aggregate, "open_positions") or 0)

    active_leaders = await conn.fetchval(
        "SELECT COUNT(*) FROM leaders WHERE on_watchlist = TRUE AND excluded = FALSE"
    )
    pnl_rows = await conn.fetch(
        f"""
        SELECT DATE(closed_at) AS day, SUM(pnl_usdc) AS pnl
        FROM paper_trades
        WHERE status='closed'
          AND {V1_PAPER_TRADE_SQL}
        GROUP BY 1
        ORDER BY 1
        """
    )
    activity_rows = await conn.fetch(
        """
        WITH follower_map AS (
            SELECT
                follower_wallet,
                (
                    ARRAY_AGG(
                        leader_wallet
                        ORDER BY follow_probability DESC, co_occurrences DESC, last_observed DESC
                    )
                )[1] AS mapped_leader_wallet,
                MAX(follow_probability) AS mapped_follow_probability,
                COUNT(*) AS mapped_edge_count
            FROM follower_edges
            WHERE follow_probability > 0.6 AND co_occurrences >= 5
            GROUP BY follower_wallet
        ),
        ranked AS (
            SELECT
                t.time,
                t.market_id,
                t.wallet_address,
                t.side,
                t.size_usdc,
                t.is_leader,
                m.question,
                m.category,
                l.classification_json,
                l.on_watchlist,
                l.excluded,
                fm.mapped_leader_wallet,
                fm.mapped_follow_probability,
                COALESCE(fm.mapped_edge_count, 0) AS mapped_edge_count,
                CASE
                    WHEN t.is_leader THEN 2
                    WHEN fm.follower_wallet IS NOT NULL THEN 1
                    ELSE 0
                END AS wallet_priority,
                ROW_NUMBER() OVER (
                    PARTITION BY t.market_id
                    ORDER BY
                        CASE
                            WHEN t.is_leader THEN 2
                            WHEN fm.follower_wallet IS NOT NULL THEN 1
                            ELSE 0
                        END DESC,
                        t.time DESC
                ) AS market_rank
            FROM trades_observed t
            LEFT JOIN markets m USING (market_id)
            LEFT JOIN leaders l ON l.wallet_address = t.wallet_address
            LEFT JOIN follower_map fm ON fm.follower_wallet = t.wallet_address
            WHERE t.time > NOW() - INTERVAL '20 minutes'
        )
        SELECT *
        FROM ranked
        WHERE market_rank <= 2
        ORDER BY time DESC, wallet_priority DESC
        LIMIT 20
        """
    )
    last_trade_row = await conn.fetchrow("SELECT MAX(time) AS last_trade FROM trades_observed WHERE time >= NOW() - INTERVAL '2 days'")
    # PERF: replace the full aggregate COUNT(*) (measured 2.9s on
    # 580k-row partitioned table) with the planner's stats estimate.
    # Postgres maintains `pg_class.reltuples` via auto-ANALYZE; the
    # value is approximate (±5% typically) but updated continuously
    # and reads in <1ms. For a dashboard "total trades observed"
    # counter, the precision is more than adequate.
    total_trades = await conn.fetchval(
        """
        SELECT COALESCE(SUM(reltuples)::bigint, 0)::bigint
        FROM pg_class
        WHERE relname LIKE 'trades_observed_%'
          AND relkind = 'r'  -- only regular partitioned tables, not the parent
        """
    )

    # --- Portfolio state (persisted) + live mark-to-market ------------------
    portfolio = await _fetch_portfolio_snapshot(conn)
    equity_curve = await _fetch_equity_curve(conn)
    unrealized_pnl_total = await _compute_unrealized_pnl_total(conn, redis_client=redis_client)

    return {
        "total_pnl": float(total_pnl or 0),
        "unrealized_pnl_total": float(unrealized_pnl_total or 0),
        "capital": portfolio["capital"],
        "peak_capital": portfolio["peak_capital"],
        "equity": portfolio["capital"] + float(unrealized_pnl_total or 0),
        "drawdown_pct": portfolio["drawdown_pct"],
        "equity_curve": equity_curve,
        "win_rate": float(win_rate_row["win_rate"] or 0) if win_rate_row else 0.0,
        "active_leaders": int(active_leaders or 0),
        "open_positions": int(open_positions or 0),
        "pnl_series": [{"day": str(r["day"]), "pnl": float(r["pnl"] or 0)} for r in pnl_rows],
        "activity_feed": [
            {
                "time": r["time"].isoformat(),
                "market_id": r["market_id"],
                "wallet": r["wallet_address"],
                "wallet_address": r["wallet_address"],
                "wallet_type": (
                    "leader"
                    if bool(r["is_leader"])
                    else "follower"
                    if _row_get(r, "mapped_leader_wallet")
                    else "market_participant"
                ),
                "wallet_status": (
                    _wallet_status(r)
                    if bool(r["is_leader"])
                    else "mapped"
                    if _row_get(r, "mapped_leader_wallet")
                    else "external"
                ),
                "wallet_strategy": _json_value(_row_get(r, "classification_json", {}), "strategy"),
                "wallet_horizon": _json_value(_row_get(r, "classification_json", {}), "horizon"),
                "wallet_influence": _json_value(
                    _row_get(r, "classification_json", {}), "influence"
                ),
                "mapped_leader_wallet": _row_get(r, "mapped_leader_wallet"),
                "mapped_follow_probability": _to_float(
                    _row_get(r, "mapped_follow_probability", 0), 0.0
                ),
                "mapped_edge_count": _to_int(_row_get(r, "mapped_edge_count", 0), 0),
                "side": r["side"],
                "size_usdc": float(r["size_usdc"] or 0),
                "market_question": _row_get(r, "question")
                or ((_row_get(r, "market_id", "")[:30] + "…") if _row_get(r, "market_id") else "—"),
                "market_category": _row_get(r, "category") or "unknown",
                "market_type": _market_type_label(_row_get(r, "category"), _row_get(r, "question")),
                "is_leader": bool(_row_get(r, "is_leader", False)),
            }
            for r in activity_rows
        ],
        "total_trades": int(total_trades or 0),
        "last_trade_at": last_trade_row["last_trade"].isoformat()
        if last_trade_row and last_trade_row["last_trade"]
        else None,
    }


async def leaders(conn, limit: int | None = None, offset: int = 0) -> list[dict]:
    """List all watched leaders with their profile + follower counts.

    PERFORMANCE: prior to pagination this returned 1560 rows × 24 fields
    = 961 KB. The Wallet Scanner table (V1) and the Scanner sub-tab (V2)
    consume this — neither needs more than the top-N by score on first
    paint. The default `limit=None` keeps backward compatibility (old
    `/api/leaders` callers still get all rows), but the V1/V2 frontends
    now pass `?limit=100&offset=0` and paginate.
    """
    limit_sql = f"LIMIT {int(limit)}" if limit is not None and int(limit) > 0 else ""
    offset_sql = f"OFFSET {int(offset)}" if offset and int(offset) > 0 else ""
    rows = await conn.fetch(
        f"""
        SELECT
            l.wallet_address,
            l.falcon_score,
            l.classification_json,
            l.excluded,
            l.on_watchlist,
            l.last_refresh,
            l.exclude_reason,
            COALESCE(p.profile_maturity, 0)   AS profile_maturity,
            COALESCE(p.error_model_phase, 0)   AS error_model_phase,
            COALESCE(p.trades_observed, 0)     AS trades_observed,
            COALESCE(p.positions_resolved, 0)  AS positions_resolved,
            p.profile_json,
            p.last_updated,
            COUNT(e.id) FILTER (
                WHERE e.follow_probability > 0.6 AND e.co_occurrences >= 5
            ) AS confirmed_followers
        FROM leaders l
        LEFT JOIN leader_profiles p
          ON p.wallet_address = l.wallet_address
         AND {V1_PROFILE_P_SQL}
        LEFT JOIN follower_edges  e ON e.leader_wallet = l.wallet_address
        GROUP BY
            l.wallet_address, l.falcon_score, l.classification_json,
            l.excluded, l.on_watchlist, l.last_refresh, l.exclude_reason,
            p.profile_maturity, p.error_model_phase,
            p.trades_observed, p.positions_resolved, p.profile_json, p.last_updated
        ORDER BY
            COUNT(e.id) FILTER (WHERE e.follow_probability > 0.6 AND e.co_occurrences >= 5) DESC,
            l.falcon_score DESC NULLS LAST
        {limit_sql} {offset_sql}
        """
    )
    return [_leader_row(r) for r in rows]


def _leader_row(r: Any) -> dict:
    clf = _json_dict(_row_get(r, "classification_json", {}))
    profile = _json_dict(_row_get(r, "profile_json", {}))
    process = profile.get("decision_process", {})
    learning = profile.get("decision_learning", {})
    runtime = profile.get("error_model_runtime", {})
    follow_view = _decision_bucket_view(learning.get("follow", {}))
    fade_view = _decision_bucket_view(learning.get("fade", {}))
    return {
        "wallet_address": _row_get(r, "wallet_address"),
        "falcon_score": _to_float(_row_get(r, "falcon_score", 0), 0.0),
        "strategy": clf.get("strategy", "—"),
        "horizon": clf.get("horizon", "—"),
        "influence": clf.get("influence", "—"),
        "copiable": clf.get("copiable", False),
        "excluded": bool(_row_get(r, "excluded", False)),
        "exclude_reason": _row_get(r, "exclude_reason"),
        "on_watchlist": bool(_row_get(r, "on_watchlist", False)),
        "last_refresh": _row_get(r, "last_refresh").isoformat()
        if _row_get(r, "last_refresh")
        else None,
        "profile_maturity": _to_float(_row_get(r, "profile_maturity", 0), 0.0),
        "error_model_phase": _to_int(_row_get(r, "error_model_phase", 0), 0),
        "trades_observed": _to_int(_row_get(r, "trades_observed", 0), 0),
        "positions_resolved": _to_int(_row_get(r, "positions_resolved", 0), 0),
        "confirmed_followers": _to_int(_row_get(r, "confirmed_followers", 0), 0),
        "process_score": _to_float(process.get("process_score_ewma", 0.0), 0.0),
        "orders_seen": _to_int(process.get("orders_seen", 0), 0),
        "follow_learning_samples": follow_view["samples"],
        "follow_learning_win_rate": follow_view["win_rate"],
        "fade_learning_samples": fade_view["samples"],
        "fade_learning_win_rate": fade_view["win_rate"],
        "drift_alert": bool(runtime.get("drift_alert", False)),
        "last_fit_at": runtime.get("last_fit_at"),
        "last_updated": _row_get(r, "last_updated").isoformat()
        if _row_get(r, "last_updated")
        else None,
    }


async def leader_detail(conn, wallet: str) -> dict | None:
    leader_row = await conn.fetchrow("SELECT * FROM leaders WHERE wallet_address = $1", wallet)
    if not leader_row:
        return None

    profile_row = await conn.fetchrow(
        f"""
        SELECT * FROM leader_profiles
        WHERE wallet_address = $1
          AND {V1_PROFILE_TABLE_SQL}
        """,
        wallet,
    )
    follower_rows = await conn.fetch(
        """
        SELECT follower_wallet, follow_probability, avg_delay_s,
               trapped_rate, same_direction_rate, co_occurrences
        FROM follower_edges
        WHERE leader_wallet = $1
          AND follow_probability > 0.6
          AND co_occurrences >= 5
        ORDER BY follow_probability DESC, co_occurrences DESC
        LIMIT 10
        """,
        wallet,
    )
    open_positions_rows = await conn.fetch(
        f"""
        SELECT market_id, token_id, direction, open_time,
               entry_price, size_usdc
        FROM positions_reconstructed
        WHERE wallet_address = $1 AND close_time IS NULL
          AND {V1_POSITION_SQL}
        ORDER BY open_time DESC
        LIMIT 10
        """,
        wallet,
    )
    paper_rows = await conn.fetch(
        f"""
        SELECT id, opened_at, closed_at, market_id, token_id, direction, entry_price,
               exit_price, size_usdc, strategy, confidence, status, pnl_usdc,
               close_reason, leader_context
        FROM paper_trades
        WHERE leader_wallet = $1
          AND {V1_PAPER_TRADE_SQL}
        ORDER BY COALESCE(closed_at, opened_at) DESC
        LIMIT 10
        """,
        wallet,
    )
    closed_paper_rows = await conn.fetch(
        f"""
        SELECT id, closed_at, market_id, pnl_usdc, strategy, close_reason
        FROM paper_trades
        WHERE leader_wallet = $1
          AND status = 'closed'
          AND {V1_PAPER_TRADE_SQL}
        ORDER BY closed_at DESC
        LIMIT 10
        """,
        wallet,
    )
    last_decision = await conn.fetchrow(
        f"""
        SELECT action, thompson_follow, thompson_fade, kelly_fraction,
               confidence, reason, outcome, time
        FROM decision_log
        WHERE leader_wallet = $1
          AND {valid_decision_filter()}
        ORDER BY time DESC
        LIMIT 1
        """,
        wallet,
    )

    clf = _json_dict(_row_get(leader_row, "classification_json", {}))
    profile = _json_dict(_row_get(profile_row, "profile_json", {})) if profile_row else {}
    process = profile.get("decision_process", {})
    learning = profile.get("decision_learning", {})
    runtime = profile.get("error_model_runtime", {})
    loss_analysis = profile.get("loss_analysis", {})
    follow_view = _decision_bucket_view(learning.get("follow", {}))
    fade_view = _decision_bucket_view(learning.get("fade", {}))
    recent_losses = list(loss_analysis.get("recent_losses", []))[:10]

    return {
        "wallet_address": wallet,
        "falcon_score": _to_float(_row_get(leader_row, "falcon_score", 0), 0.0),
        "classification": clf,
        "excluded": bool(_row_get(leader_row, "excluded", False)),
        "exclude_reason": _row_get(leader_row, "exclude_reason"),
        "on_watchlist": bool(_row_get(leader_row, "on_watchlist", False)),
        "last_refresh": _row_get(leader_row, "last_refresh").isoformat()
        if _row_get(leader_row, "last_refresh")
        else None,
        "profile": {
            "maturity": _to_float(_row_get(profile_row, "profile_maturity", 0), 0.0)
            if profile_row
            else 0.0,
            "error_model_phase": _to_int(_row_get(profile_row, "error_model_phase", 0), 0)
            if profile_row
            else 0,
            "trades_observed": _to_int(_row_get(profile_row, "trades_observed", 0), 0)
            if profile_row
            else 0,
            "positions_resolved": _to_int(_row_get(profile_row, "positions_resolved", 0), 0)
            if profile_row
            else 0,
            "accuracy": profile.get("accuracy", {}),
            "preferred_categories": profile.get("preferred_categories", {}),
            "sizing": profile.get("sizing", {}),
            "entry_patterns": profile.get("entry_patterns", {}),
            "decision_process": {
                "orders_seen": _to_int(process.get("orders_seen", 0), 0),
                "process_score": _to_float(process.get("process_score_ewma", 0.0), 0.0),
                "avg_order_size": _to_float(process.get("avg_order_size", 0.0), 0.0),
                "ewma_order_size": _to_float(process.get("ewma_order_size", 0.0), 0.0),
                "avg_interarrival_s": _to_float(process.get("avg_interarrival_s", 0.0), 0.0),
                "flip_rate": _to_float(process.get("flip_rate", 0.0), 0.0),
                "scale_in_rate": _to_float(process.get("scale_in_rate", 0.0), 0.0),
                "buy_count": _to_int(process.get("buy_count", 0), 0),
                "sell_count": _to_int(process.get("sell_count", 0), 0),
                "top_categories": sorted(
                    (
                        {
                            "category": category,
                            "count": _to_int(count, 0),
                        }
                        for category, count in (process.get("category_counts", {}) or {}).items()
                    ),
                    key=lambda item: item["count"],
                    reverse=True,
                )[:5],
            },
            "decision_learning": {
                "follow": follow_view,
                "fade": fade_view,
            },
            "loss_analysis": {
                "recent_losses": recent_losses,
                "last_position_loss_at": loss_analysis.get("last_position_loss_at"),
            },
            "error_model_runtime": {
                "cusum_state": _to_float(runtime.get("cusum_state", 0.0), 0.0),
                "drift_alert": bool(runtime.get("drift_alert", False)),
                "last_fit_at": runtime.get("last_fit_at"),
                "last_fit_phase": _to_int(runtime.get("last_fit_phase", 0), 0),
                "training_samples": _to_int(runtime.get("training_samples", 0), 0),
                "last_downgraded_at": runtime.get("last_downgraded_at"),
                "last_prediction_error": runtime.get("last_prediction_error"),
                "last_outcome_at": runtime.get("last_outcome_at"),
            },
        },
        "followers": [
            {
                "follower_wallet": _row_get(r, "follower_wallet"),
                "follow_probability": _to_float(_row_get(r, "follow_probability", 0), 0.0),
                "avg_delay_s": _to_float(_row_get(r, "avg_delay_s", 0), 0.0),
                "trapped_rate": _to_float(_row_get(r, "trapped_rate", 0), 0.0),
                "same_direction_rate": _to_float(_row_get(r, "same_direction_rate", 0), 0.0),
                "co_occurrences": _to_int(_row_get(r, "co_occurrences", 0), 0),
            }
            for r in follower_rows
        ],
        "open_positions": [
            {
                "market_id": _row_get(r, "market_id"),
                "token_id": _row_get(r, "token_id"),
                "direction": _row_get(r, "direction"),
                "open_time": _row_get(r, "open_time").isoformat(),
                "entry_price": _to_float(_row_get(r, "entry_price", 0), 0.0),
                "size_usdc": _to_float(_row_get(r, "size_usdc", 0), 0.0),
            }
            for r in open_positions_rows
        ],
        "paper_trades": [
            {
                "id": _row_get(r, "id"),
                "opened_at": _row_get(r, "opened_at").isoformat()
                if _row_get(r, "opened_at")
                else None,
                "closed_at": _row_get(r, "closed_at").isoformat()
                if _row_get(r, "closed_at")
                else None,
                "market_id": _row_get(r, "market_id"),
                "token_id": _row_get(r, "token_id"),
                "direction": _row_get(r, "direction"),
                "entry_price": _to_float(_row_get(r, "entry_price", 0), 0.0),
                "exit_price": _to_float(_row_get(r, "exit_price", 0), 0.0)
                if _row_get(r, "exit_price") is not None
                else None,
                "size_usdc": _to_float(_row_get(r, "size_usdc", 0), 0.0),
                "strategy": _row_get(r, "strategy"),
                "confidence": _to_float(_row_get(r, "confidence", 0), 0.0),
                "status": _row_get(r, "status"),
                "pnl_usdc": _to_float(_row_get(r, "pnl_usdc", 0), 0.0)
                if _row_get(r, "pnl_usdc") is not None
                else None,
                "close_reason": _row_get(r, "close_reason"),
                "ml_snapshot": _extract_ml_snapshot(_row_get(r, "leader_context")),
            }
            for r in paper_rows
        ],
        "recent_closed_paper_trades": [
            {
                "id": _row_get(r, "id"),
                "closed_at": _row_get(r, "closed_at").isoformat()
                if _row_get(r, "closed_at")
                else None,
                "market_id": _row_get(r, "market_id"),
                "strategy": _row_get(r, "strategy"),
                "pnl_usdc": _to_float(_row_get(r, "pnl_usdc", 0), 0.0),
                "close_reason": _row_get(r, "close_reason"),
            }
            for r in closed_paper_rows
        ],
        "last_decision": {
            "action": _row_get(last_decision, "action"),
            "thompson_follow": _to_float(_row_get(last_decision, "thompson_follow", 0), 0.0),
            "thompson_fade": _to_float(_row_get(last_decision, "thompson_fade", 0), 0.0),
            "kelly_fraction": _to_float(_row_get(last_decision, "kelly_fraction", 0), 0.0),
            "confidence": _to_float(_row_get(last_decision, "confidence", 0), 0.0),
            "reason": _row_get(last_decision, "reason"),
            "outcome": _row_get(last_decision, "outcome"),
            "time": _row_get(last_decision, "time").isoformat(),
        }
        if last_decision
        else None,
    }


async def positions(conn) -> dict:
    rows = await conn.fetch(
        f"""
        SELECT pt.id, pt.opened_at, pt.closed_at, pt.market_id, pt.token_id,
               pt.direction, pt.entry_price, pt.exit_price, pt.size_usdc,
               pt.pnl_usdc, pt.fee_paid_usdc, pt.strategy, pt.leader_wallet,
               pt.confidence, pt.status, pt.close_reason, pt.leader_context,
               EXTRACT(EPOCH FROM (COALESCE(pt.closed_at, NOW()) - pt.opened_at))::int AS age_s,
               m.question, m.category, m.fee_rate_pct, m.end_date
        FROM paper_trades pt
        LEFT JOIN markets m USING (market_id)
        WHERE {V1_PAPER_TRADE_PT_SQL}
        ORDER BY CASE WHEN pt.status='open' THEN 0 ELSE 1 END, pt.opened_at DESC
        """
    )
    leader_wallets = sorted(
        {_row_get(r, "leader_wallet") for r in rows if _row_get(r, "leader_wallet")}
    )
    profile_map: dict[str, dict] = {}
    if leader_wallets:
        profile_rows = await conn.fetch(
            f"""
            SELECT wallet_address, profile_json
            FROM leader_profiles
            WHERE wallet_address = ANY($1::varchar[])
              AND {V1_PROFILE_TABLE_SQL}
            """,
            leader_wallets,
        )
        profile_map = {
            _row_get(r, "wallet_address"): _json_dict(_row_get(r, "profile_json", {}))
            for r in profile_rows
        }
    open_list = []
    closed_list = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    best = None
    worst = None

    for r in rows:
        pnl = _to_float(_row_get(r, "pnl_usdc", 0), 0.0)
        leader_wallet = _row_get(r, "leader_wallet")
        profile = profile_map.get(leader_wallet, {})
        ml_snapshot = _extract_ml_snapshot(_row_get(r, "leader_context"))
        if _is_invalid_paper_trade_record(r, ml_snapshot):
            continue
        matched_loss = None
        if _row_get(r, "status") == "closed":
            matched_loss = _match_recent_loss(
                profile=profile,
                strategy=_row_get(r, "strategy", ""),
                market_id=_row_get(r, "market_id", ""),
                close_reason=_row_get(r, "close_reason"),
                closed_at=_row_get(r, "closed_at"),
            )
        reason_codes = list(
            (matched_loss or {}).get("reason_codes", []) or ml_snapshot.get("reason_codes", [])
        )
        current_penalty = (
            _reason_penalty_from_profile(profile, _row_get(r, "strategy", ""), reason_codes)
            if reason_codes
            else ml_snapshot.get("context_penalty", 0.0)
        )
        rec = {
            "id": _row_get(r, "id"),
            "opened_at": _row_get(r, "opened_at").isoformat() if _row_get(r, "opened_at") else None,
            "closed_at": _row_get(r, "closed_at").isoformat() if _row_get(r, "closed_at") else None,
            "market_id": _row_get(r, "market_id"),
            "question": _row_get(r, "question")
            or ((_row_get(r, "market_id", "")[:20] + "…") if _row_get(r, "market_id") else "—"),
            "category": _row_get(r, "category"),
            "direction": _row_get(r, "direction"),
            "entry_price": _to_float(_row_get(r, "entry_price", 0), 0.0),
            "exit_price": _to_float(_row_get(r, "exit_price", 0), 0.0)
            if _row_get(r, "exit_price") is not None
            else None,
            "size_usdc": _to_float(_row_get(r, "size_usdc", 0), 0.0),
            "pnl_usdc": pnl,
            "pnl_pct": round(pnl / _to_float(_row_get(r, "size_usdc", 0), 1.0) * 100, 2)
            if _to_float(_row_get(r, "size_usdc", 0), 0.0)
            else 0,
            "fee_paid_usdc": _to_float(_row_get(r, "fee_paid_usdc", 0), 0.0),
            "strategy": _row_get(r, "strategy"),
            "leader_wallet": leader_wallet,
            "confidence": _to_float(_row_get(r, "confidence", 0), 0.0),
            "status": _row_get(r, "status"),
            "close_reason": _row_get(r, "close_reason"),
            "age_s": _to_int(_row_get(r, "age_s", 0), 0),
            "ml_snapshot": ml_snapshot,
            "loss_reasons": reason_codes,
            "current_penalty": round(_to_float(current_penalty, 0.0), 4),
        }
        if _row_get(r, "status") == "open":
            open_list.append(rec)
        else:
            closed_list.append(rec)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            if best is None or pnl > best:
                best = pnl
            if worst is None or pnl < worst:
                worst = pnl

    return {
        "open": open_list,
        "closed": closed_list,
        "stats": {
            "total_pnl": round(total_pnl, 2),
            "wins": wins,
            "losses": losses,
            "best_trade": best,
            "worst_trade": worst,
        },
    }


async def decisions(conn, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = await conn.fetch(
        f"""
        SELECT d.id, d.time, d.leader_wallet, d.market_id, d.action,
               d.thompson_follow, d.thompson_fade, d.kelly_fraction,
               d.confidence, d.reason, d.outcome, d.strategy_track,
               d.economic_model_version, d.signal_audit,
               m.question,
               pt.leader_context, pt.paper_trade_id, pt.paper_status,
               pt.close_reason, pt.pnl_usdc, pt.opened_at, pt.closed_at
        FROM decision_log d
        LEFT JOIN markets m USING (market_id)
        LEFT JOIN LATERAL (
            SELECT id AS paper_trade_id, leader_context, status AS paper_status,
                   close_reason, pnl_usdc, opened_at, closed_at
            FROM paper_trades pt
            WHERE pt.leader_wallet = d.leader_wallet
              AND pt.market_id = d.market_id
              AND pt.strategy = d.action
              AND {V1_PAPER_TRADE_PT_SQL}
              AND pt.opened_at BETWEEN d.time - INTERVAL '10 minutes'
                                   AND d.time + INTERVAL '10 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (pt.opened_at - d.time))) ASC
            LIMIT 1
        ) pt ON d.action IN ('follow', 'fade')
        WHERE {V1_DECISION_D_SQL}
        ORDER BY d.time DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    result = []
    for r in rows:
        ml_snapshot = _extract_ml_snapshot(_row_get(r, "leader_context"))
        signal_audit = _json_dict(_row_get(r, "signal_audit", {}))
        size_usdc = round(
            min(_to_float(_row_get(r, "kelly_fraction", 0), 0.0) * 10000.0, 200.0), 2
        )
        gate_result = (
            "accepted"
            if signal_audit.get("accepted") is True
            else "refused"
            if signal_audit
            else "not_audited"
        )
        paper_status = _row_get(r, "paper_status")
        close_reason = _row_get(r, "close_reason")
        pnl_usdc = _row_get(r, "pnl_usdc")
        refusal_reason = signal_audit.get("reject_reason") if signal_audit else None
        execution_result = paper_status or ("blocked" if gate_result == "refused" else "pending")
        trace = {
            "input_trade": {
                "leader_wallet": _row_get(r, "leader_wallet"),
                "market_id": _row_get(r, "market_id"),
                "token_id": signal_audit.get("token_id"),
            },
            "market_metadata": {
                "question": _row_get(r, "question"),
                "strategy_track": _row_get(r, "strategy_track") or signal_audit.get("strategy_track"),
                "economic_model_version": _row_get(r, "economic_model_version")
                or signal_audit.get("economic_model_version"),
            },
            "profiling": ml_snapshot,
            "gate_result": gate_result,
            "action": _row_get(r, "action"),
            "size_usdc": size_usdc,
            "refusal_reason": refusal_reason,
            "execution_result": execution_result,
            "paper_trade_id": _row_get(r, "paper_trade_id"),
            "close_result": {
                "status": paper_status,
                "close_reason": close_reason,
                "pnl_usdc": _to_float(pnl_usdc, 0.0) if pnl_usdc is not None else None,
                "opened_at": _row_get(r, "opened_at").isoformat()
                if _row_get(r, "opened_at")
                else None,
                "closed_at": _row_get(r, "closed_at").isoformat()
                if _row_get(r, "closed_at")
                else None,
            },
            "feedback_learning": {
                "outcome": _row_get(r, "outcome"),
                "reason_codes": ml_snapshot.get("reason_codes", []),
            },
        }
        result.append(
            {
                "id": _row_get(r, "id"),
                "time": _row_get(r, "time").isoformat(),
                "leader_wallet": _row_get(r, "leader_wallet"),
                "market_id": _row_get(r, "market_id"),
                "question": _row_get(r, "question")
                or (
                    (_row_get(r, "market_id", "")[:30] + "…")
                    if _row_get(r, "market_id")
                    else "—"
                ),
                "action": _row_get(r, "action"),
                "strategy_track": _row_get(r, "strategy_track")
                or signal_audit.get("strategy_track"),
                "economic_model_version": _row_get(r, "economic_model_version")
                or signal_audit.get("economic_model_version"),
                "thompson_follow": _to_float(_row_get(r, "thompson_follow", 0), 0.0),
                "thompson_fade": _to_float(_row_get(r, "thompson_fade", 0), 0.0),
                "kelly_fraction": _to_float(_row_get(r, "kelly_fraction", 0), 0.0),
                "size_usdc": size_usdc,
                "confidence": _to_float(_row_get(r, "confidence", 0), 0.0),
                "reason": _row_get(r, "reason"),
                "outcome": _row_get(r, "outcome"),
                "signal_audit": signal_audit,
                "trace": trace,
                "ml_snapshot": ml_snapshot,
            }
        )
    return result


async def decisions_stats(conn, window_hours: int = 24) -> dict:
    """Aggregate decision-log telemetry for the Signal Stream tab.

    Returns per-action counts, win-rates, and pending volume inside a rolling
    window so the dashboard can surface signal quality at a glance rather than
    forcing the operator to eyeball individual rows.
    """
    window_hours = max(1, min(int(window_hours or 24), 24 * 30))
    try:
        rows = await conn.fetch(
            f"""
            SELECT
                d.action,
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE d.outcome = 'win')::int  AS wins,
                COUNT(*) FILTER (WHERE d.outcome = 'loss')::int AS losses,
                COUNT(*) FILTER (WHERE d.outcome IS NULL
                                   OR d.outcome NOT IN ('win','loss'))::int AS pending,
                AVG(d.confidence)::float AS avg_confidence,
                AVG(d.kelly_fraction)::float AS avg_kelly
            FROM decision_log d
            WHERE d.time >= NOW() - ($1 || ' hours')::interval
              AND {V1_DECISION_D_SQL}
            GROUP BY d.action
            """,
            str(window_hours),
        )
    except Exception as exc:
        logger.debug(f"decisions_stats query failed: {exc}")
        rows = []

    out: dict[str, dict] = {}
    totals = {"total": 0, "wins": 0, "losses": 0, "pending": 0}
    for r in rows:
        action = (r["action"] or "skip").lower()
        total = int(r["total"] or 0)
        wins = int(r["wins"] or 0)
        losses = int(r["losses"] or 0)
        pending = int(r["pending"] or 0)
        resolved = wins + losses
        win_rate = (wins / resolved) if resolved > 0 else None
        out[action] = {
            "action": action,
            "total": total,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": None if win_rate is None else round(win_rate, 4),
            "avg_confidence": round(float(r["avg_confidence"] or 0.0), 4),
            "avg_kelly": round(float(r["avg_kelly"] or 0.0), 4),
        }
        totals["total"] += total
        totals["wins"] += wins
        totals["losses"] += losses
        totals["pending"] += pending

    # Guarantee all three buckets exist so the dashboard can render a stable strip.
    for action in ("follow", "fade", "skip"):
        out.setdefault(
            action,
            {
                "action": action,
                "total": 0,
                "wins": 0,
                "losses": 0,
                "pending": 0,
                "win_rate": None,
                "avg_confidence": 0.0,
                "avg_kelly": 0.0,
            },
        )

    resolved_total = totals["wins"] + totals["losses"]
    return {
        "window_hours": window_hours,
        "buckets": out,
        "totals": {
            **totals,
            "win_rate": (
                round(totals["wins"] / resolved_total, 4) if resolved_total > 0 else None
            ),
        },
    }


async def risk(conn) -> dict:
    daily = await conn.fetchrow(f"""
        SELECT
            COALESCE(SUM(pnl_usdc) FILTER (WHERE pnl_usdc < 0), 0)  AS daily_loss,
            COALESCE(SUM(pnl_usdc), 0)                               AS daily_pnl,
            COUNT(*) FILTER (WHERE status='closed')                   AS closed_today,
            COUNT(*) FILTER (WHERE status='open')                     AS open_count
        FROM paper_trades
        WHERE opened_at >= CURRENT_DATE
          AND {V1_PAPER_TRADE_SQL}
    """)
    exposure = await conn.fetch(f"""
        SELECT market_id, SUM(size_usdc) AS exposure
        FROM paper_trades
        WHERE status='open'
          AND {V1_PAPER_TRADE_SQL}
        GROUP BY market_id ORDER BY exposure DESC LIMIT 10
    """)
    last_trades = await conn.fetch(f"""
        SELECT pnl_usdc FROM paper_trades
        WHERE status='closed'
          AND {V1_PAPER_TRADE_SQL}
        ORDER BY closed_at DESC LIMIT 20
    """)
    strategy_stats = await conn.fetch(f"""
        SELECT strategy,
            COUNT(*)                                              AS total,
            COUNT(*) FILTER (WHERE pnl_usdc > 0)                 AS wins,
            COALESCE(SUM(pnl_usdc), 0)                           AS pnl
        FROM paper_trades
        WHERE status='closed'
          AND {V1_PAPER_TRADE_SQL}
        GROUP BY strategy
    """)
    portfolio = await _fetch_portfolio_snapshot(conn)
    # Use persisted bankroll; fall back to the configured starting capital.
    paper_capital = portfolio["capital"] or float(settings.PAPER_CAPITAL_USDC)
    daily_loss_pct = abs(float(daily["daily_loss"])) / paper_capital * 100

    consecutive_losses = 0
    for t in last_trades:
        if float(t["pnl_usdc"] or 0) < 0:
            consecutive_losses += 1
        else:
            break

    return {
        "daily_pnl": float(daily["daily_pnl"] or 0),
        "daily_loss_pct": round(daily_loss_pct, 2),
        "circuit_breaker_active": daily_loss_pct > 5.0,
        "open_count": int(daily["open_count"] or 0),
        "consecutive_losses": consecutive_losses,
        "paper_capital": paper_capital,
        "peak_capital": portfolio["peak_capital"],
        "drawdown_pct": portfolio["drawdown_pct"],
        "per_market_exposure": [
            {"market_id": r["market_id"], "exposure": float(r["exposure"] or 0)} for r in exposure
        ],
        "strategy_stats": {
            r["strategy"]: {
                "total": int(r["total"]),
                "wins": int(r["wins"]),
                "pnl": float(r["pnl"] or 0),
                "win_rate": round(int(r["wins"]) / int(r["total"]) * 100, 1) if r["total"] else 0,
            }
            for r in strategy_stats
        },
    }


async def system_status(conn, redis_client=None) -> dict:
    """Canonical system status block (Batch 2 fix #2).

    The dashboard sidebar and the `/api/portfolio/pipeline_status`
    endpoint used to compute `bot_status` / `ws_status` independently
    from different source data, leading to "RUNNING / UNKNOWN"
    discrepancies. The block returned here is the single source of
    truth used by both `_build_bot_payload` (snapshot) and
    `portfolio_pipeline_status` (pipeline endpoint).

    Canonical fields added (besides the legacy leaders/graph/batch_steps):

    * ``bot_status``  : "RUNNING" | "STOPPED" | "DEGRADED"
    * ``ws_status``   : "LIVE" | "DEGRADED" | "DOWN"
    * ``ingestion``   : { live_markets, ws_last_message_age_s, observed_trades_24h }
    * ``reconciliation``: { verdict, delta_abs } (best-effort, recon_summary cached upstream)
    """
    falcon_agents = await conn.fetch("""
        SELECT 'leaders_refresh' AS step, MAX(COALESCE(last_refresh, first_seen)) AS last_run
        FROM leaders
        UNION ALL
        SELECT 'profiles', MAX(last_updated) FROM leader_profiles
        UNION ALL
        SELECT 'graph_edges', MAX(last_observed) FROM follower_edges
    """)
    total_leaders = await conn.fetchval(
        "SELECT COUNT(*) FROM leaders WHERE on_watchlist=TRUE OR excluded=TRUE"
    )
    active = await conn.fetchval(
        "SELECT COUNT(*) FROM leaders WHERE on_watchlist=TRUE AND excluded=FALSE"
    )
    excluded = await conn.fetchval("SELECT COUNT(*) FROM leaders WHERE excluded=TRUE")
    pending_activation = await conn.fetchval("""
        WITH leader_state AS (
            SELECT
                l.wallet_address,
                COALESCE(p.trades_observed, 0) AS trades_observed,
                COALESCE(p.positions_resolved, 0) AS positions_resolved,
                COUNT(e.id) FILTER (
                    WHERE e.follow_probability > 0.6 AND e.co_occurrences >= 5
                ) AS confirmed_followers
            FROM leaders l
            LEFT JOIN leader_profiles p USING(wallet_address)
            LEFT JOIN follower_edges e ON e.leader_wallet = l.wallet_address
            WHERE l.on_watchlist = TRUE AND l.excluded = FALSE
            GROUP BY l.wallet_address, p.trades_observed, p.positions_resolved
        )
        SELECT COUNT(*)
        FROM leader_state
        WHERE NOT (
            (trades_observed >= 50 AND confirmed_followers >= 5)
            OR positions_resolved >= 50
        )
    """)
    confirmed_edges = await conn.fetchval(
        "SELECT COUNT(*) FROM follower_edges WHERE follow_probability > 0.6 AND co_occurrences >= 5"
    )
    pending_edges = await conn.fetchval(
        "SELECT COUNT(*) FROM follower_edges WHERE follow_probability <= 0.6 OR co_occurrences < 5"
    )
    recent_edges = await conn.fetch(
        """
        SELECT leader_wallet, follower_wallet, follow_probability, same_direction_rate,
               co_occurrences, last_observed
        FROM follower_edges
        WHERE follow_probability > 0.6 AND co_occurrences >= 5
        ORDER BY last_observed DESC
        LIMIT 8
        """
    )
    # ──────────────────────────────────────────────────────────────────
    # Batch 2 fix #2 — canonical bot/ws status block. Both
    # `_build_bot_payload` (terminal snapshot) and
    # `portfolio_pipeline_status` now reuse this block so the dashboard
    # never shows "RUNNING / UNKNOWN" again.
    #
    # We deliberately fetch the counters/ws keys via Redis here (with
    # short timeouts + graceful fallback) so a single Redis hiccup
    # degrades the field rather than blowing up the leaders/graph data
    # the rest of the snapshot needs.
    # ──────────────────────────────────────────────────────────────────
    bot_status = "STOPPED"
    ws_status = "DOWN"
    ws_last_message_age_s: float | None = None
    observed_trades_24h: int | None = None
    exec_trades_24h: int | None = None

    if redis_client is not None:
        try:
            ts = await asyncio.wait_for(
                redis_client.get("ws:market:last_message_ts"), timeout=0.3
            )
            if ts is not None:
                ws_last_message_age_s = max(
                    0.0,
                    float(datetime.now(timezone.utc).timestamp()) - float(ts),
                )
                if ws_last_message_age_s <= 30:
                    ws_status = "LIVE"
                elif ws_last_message_age_s <= 120:
                    ws_status = "DEGRADED"
                else:
                    ws_status = "DOWN"
        except Exception:
            ws_last_message_age_s = None
            ws_status = "DOWN"

        try:
            v = await asyncio.wait_for(
                redis_client.get("metrics:trades_observed_24h"), timeout=0.3
            )
            if v is not None:
                observed_trades_24h = int(v)
        except Exception:
            observed_trades_24h = None

    # exec_trades_24h: paper_trades opened in the last 24h. Distinct
    # from observed_trades_24h (the raw firehose, ingest-side).
    try:
        exec_trades_24h = await conn.fetchval(
            f"""
            SELECT COUNT(*)::int FROM paper_trades
            WHERE opened_at >= NOW() - INTERVAL '24 hours'
              AND {V1_PAPER_TRADE_SQL}
            """
        )
    except Exception:
        exec_trades_24h = None

    # bot_status: RUNNING if any executor activity was emitted in the
    # last hour AND ws is at least DEGRADED. The killswitch path is
    # checked by the pipeline endpoint, not here, so we don't read it
    # twice.
    try:
        last_exec = await conn.fetchval(
            "SELECT MAX(opened_at) FROM paper_trades "
            "WHERE opened_at >= NOW() - INTERVAL '7 days'"
        )
        engine_active = False
        if last_exec is not None:
            age = (datetime.now(timezone.utc) - last_exec).total_seconds()
            engine_active = age < 3600
        # Even without recent trades, RUNNING if WS is alive — the bot
        # may just be in a low-conviction window.
        if ws_status in ("LIVE", "DEGRADED") or engine_active:
            bot_status = "RUNNING"
        else:
            bot_status = "STOPPED"
    except Exception:
        # Engine activity probe is best-effort. Default to RUNNING if WS
        # is at least DEGRADED so we don't flap on a single SQL hiccup.
        bot_status = "RUNNING" if ws_status in ("LIVE", "DEGRADED") else "STOPPED"

    # A12 (2026-05-18) — bootstrap maturity signal. Aggregates four
    # learning-progress signals into a single 0..1 score so the dashboard
    # can tell the operator "the bot is still learning, low trading is
    # expected" instead of letting them stare at zeros and assume a bug.
    #
    # Composition (each clamped to [0, 1]):
    #   * profiles_pct       — resolved positions vs target (5000)
    #   * sample_eff_pct     — leaders with >=20 observed trades vs total
    #   * cat_coverage_pct   — distinct categories with >=3 leaders vs 6
    #   * decision_health_pct— 1.0 if any decisions in last 24h else 0.0
    #
    # The overall_pct is the unweighted mean. Sub-30% = bootstrap, 30-70% =
    # ramping up, 70%+ = ready. The tooltip surfaces the four signals so
    # the operator understands *why* the bot is still learning.
    try:
        maturity = await _compute_maturity(conn, decisions_24h_hint=exec_trades_24h)
    except Exception as exc:  # noqa: BLE001 — best-effort field
        logger.debug(f"system_status maturity probe failed: {exc}")
        maturity = {
            "profiles_pct": 0.0,
            "sample_eff_pct": 0.0,
            "cat_coverage_pct": 0.0,
            "decision_health_pct": 0.0,
            "overall_pct": 0.0,
            "tier": "bootstrap",
            "error": "probe_failed",
        }

    # Batch 2 fix #8 — best-effort publish of SystemStatusChanged. The
    # helper is fully isolated (failure swallowed) so it never breaks
    # the canonical return below.
    try:
        await _maybe_publish_system_status_change(
            redis_client,
            bot_status,
            ws_status,
            observed_trades_24h,
            exec_trades_24h,
            ws_last_message_age_s,
        )
    except Exception:
        pass

    return {
        "leaders": {
            "total": int(total_leaders or 0),
            "active": int(active or 0),
            "excluded": int(excluded or 0),
            "pending_activation": int(pending_activation or 0),
        },
        "graph": {
            "confirmed_edges": int(confirmed_edges or 0),
            "pending_edges": int(pending_edges or 0),
            "recent_edges": [
                {
                    "leader_wallet": _row_get(r, "leader_wallet"),
                    "follower_wallet": _row_get(r, "follower_wallet"),
                    "follow_probability": _to_float(_row_get(r, "follow_probability", 0), 0.0),
                    "same_direction_rate": _to_float(_row_get(r, "same_direction_rate", 0), 0.0),
                    "co_occurrences": _to_int(_row_get(r, "co_occurrences", 0), 0),
                    "last_observed": _row_get(r, "last_observed").isoformat()
                    if _row_get(r, "last_observed")
                    else None,
                }
                for r in recent_edges
            ],
        },
        "batch_steps": [
            {"step": r["step"], "last_run": r["last_run"].isoformat() if r["last_run"] else None}
            for r in falcon_agents
        ],
        # Batch 2 fix #2 — canonical status block (UPPERCASE for the
        # SystemStatusChanged event contract; lowercase legacy aliases
        # exist on the bot block of the snapshot too).
        "bot_status": bot_status,
        "ws_status": ws_status,
        "ingestion": {
            "ws_last_message_age_s": (
                round(ws_last_message_age_s, 2)
                if ws_last_message_age_s is not None
                else None
            ),
            "observed_trades_24h": observed_trades_24h,
            "exec_trades_24h": exec_trades_24h,
        },
        # A12 — bootstrap maturity (see helper docstring for composition).
        # Surfaced on snapshot.bot.maturity by _build_bot_payload so the
        # dashboard banner can render without reaching across blocks.
        "maturity": maturity,
    }


async def _maybe_publish_system_status_change(
    redis_client,
    bot_status: str,
    ws_status: str,
    observed_trades_24h: int | None,
    exec_trades_24h: int | None,
    ws_last_message_age_s: float | None,
) -> None:
    """Publish a SystemStatusChanged event when the canonical
    (bot_status, ws_status) pair transitions vs the last broadcast.

    Best-effort, best-friend to A5: uses the schema + channel constants
    declared in src/events/schemas.py so the consumer side enforces
    pydantic-level validation. Failures are swallowed — this is purely
    a fan-out hint for the WS bridge / telegram alert path. The
    debounce is done via a Redis key (``system:status:last_emit``) so
    the watchdog doesn't spam consumers on every cache miss.
    """
    if redis_client is None:
        return
    try:
        from src.events.schemas import (
            SystemStatusChanged,
            CHANNEL_SYSTEM_STATUS,
        )
    except Exception:
        return
    debounce_key = "system:status:last_emit"
    try:
        last = await redis_client.get(debounce_key)
        signature = f"{bot_status}|{ws_status}"
        if isinstance(last, (bytes, bytearray)):
            last = last.decode("utf-8")
        if last == signature:
            return  # no transition — don't spam consumers
        # Note: killswitch is fetched at the pipeline endpoint; we
        # default to False here because the canonical event surface
        # treats killswitch as a separate broadcast. A producer with
        # access to the killswitch state should overwrite this.
        event = SystemStatusChanged(
            time=datetime.now(timezone.utc),
            bot=bot_status,
            ws=ws_status,
            ingest={
                "ws_last_message_age_s": ws_last_message_age_s,
                "observed_trades_24h": observed_trades_24h,
                "exec_trades_24h": exec_trades_24h,
            },
            killswitch=False,
        )
        await redis_client.publish(
            CHANNEL_SYSTEM_STATUS, event.model_dump_json()
        )
        # 30s debounce — long enough to collapse polling chatter, short
        # enough that the dashboard still sees the next genuine flip.
        await redis_client.set(debounce_key, signature, ex=30)
    except Exception as exc:
        logger.debug(f"system_status event publish skipped: {exc}")


# A12 — Bootstrap maturity targets. Each is the "fully mature" point;
# the corresponding percentage is min(actual / target, 1.0). Tuned to
# Polymarket bot mission scale (28% → 70% win-rate roadmap):
#   PROFILES — by the time we have 5k resolved positions across the
#     watchlist, the per-leader error model has the data it needs to
#     reach phase 3 (LightGBM) for the most active leaders.
#   SAMPLE   — 200 leaders × ≥20 trades each is roughly the point where
#     follow_min_trades (50) is hit for the median leader.
#   COVERAGE — 6 distinct market categories with ≥3 leaders each is the
#     point where category-conditional confidence (R8) has enough cells.
_MATURITY_TARGET_POSITIONS_RESOLVED = 5_000
_MATURITY_TARGET_LEADERS_WITH_TRADES = 200
_MATURITY_MIN_TRADES_PER_LEADER = 20
_MATURITY_TARGET_CATEGORIES = 6
_MATURITY_MIN_LEADERS_PER_CAT = 3


async def _compute_maturity(
    conn, *, decisions_24h_hint: int | None = None
) -> dict[str, Any]:
    """Aggregate bootstrap-progress signals into a single 0..1 score.

    Called inside ``system_status`` (cached upstream by ``api.main``'s
    snapshot 1s TTL — no need to add another cache here). Each probe is
    wrapped in its own try/except so a single missing table doesn't
    zero-out the rest of the breakdown.
    """
    profiles_pct = 0.0
    sample_eff_pct = 0.0
    cat_coverage_pct = 0.0
    decision_health_pct = 0.0

    # 1. profiles_pct — total resolved positions vs target.
    try:
        n_resolved = await conn.fetchval(
            "SELECT COUNT(*)::int FROM positions_reconstructed "
            "WHERE close_time IS NOT NULL"
        )
        n_resolved = int(n_resolved or 0)
        profiles_pct = min(n_resolved / _MATURITY_TARGET_POSITIONS_RESOLVED, 1.0)
    except Exception:
        n_resolved = 0

    # 2. sample_eff_pct — leaders with enough trades to be profileable.
    try:
        n_with_sample = await conn.fetchval(
            f"""
            SELECT COUNT(*)::int FROM leader_profiles
            WHERE trades_observed >= {_MATURITY_MIN_TRADES_PER_LEADER}
            """
        )
        n_with_sample = int(n_with_sample or 0)
        sample_eff_pct = min(n_with_sample / _MATURITY_TARGET_LEADERS_WITH_TRADES, 1.0)
    except Exception:
        n_with_sample = 0

    # 3. cat_coverage_pct — distinct categories with ≥N leaders each.
    # The category is stored in profile_json (Dirichlet posterior over
    # preferred_categories); we approximate "covered" by counting
    # distinct top-1 categories across profiles.
    try:
        n_categories = await conn.fetchval(
            f"""
            SELECT COUNT(*)::int FROM (
                SELECT COALESCE(m.category, 'unknown') AS cat
                FROM leader_profiles p
                LEFT JOIN markets m
                  ON m.market_id = (p.profile_json->'last_market'->>'market_id')
                WHERE p.trades_observed >= {_MATURITY_MIN_TRADES_PER_LEADER}
                GROUP BY cat
                HAVING COUNT(*) >= {_MATURITY_MIN_LEADERS_PER_CAT}
            ) sub
            """
        )
        n_categories = int(n_categories or 0)
        cat_coverage_pct = min(n_categories / _MATURITY_TARGET_CATEGORIES, 1.0)
    except Exception:
        n_categories = 0
        # Fall back to a simpler probe: just distinct categories in markets
        # that have at least one leader trade. Less accurate but never
        # zero on a healthy DB.
        try:
            fallback = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT m.category)::int
                FROM markets m
                JOIN trades_observed t ON t.market_id = m.market_id
                WHERE t.time >= NOW() - INTERVAL '30 days'
                  AND m.category IS NOT NULL
                """
            )
            n_categories = int(fallback or 0)
            cat_coverage_pct = min(n_categories / _MATURITY_TARGET_CATEGORIES, 1.0)
        except Exception:
            pass

    # 4. decision_health_pct — binary: any actionable decision in 24h.
    # If the caller already counted exec_trades_24h, reuse it as a hint
    # before issuing a separate query (the decision_log COUNT below is
    # the canonical source — exec_trades is just a fast bail-out).
    if isinstance(decisions_24h_hint, int) and decisions_24h_hint > 0:
        decision_health_pct = 1.0
    else:
        try:
            n_decisions = await conn.fetchval(
                "SELECT COUNT(*)::int FROM decision_log "
                "WHERE time >= NOW() - INTERVAL '24 hours'"
            )
            decision_health_pct = 1.0 if int(n_decisions or 0) > 0 else 0.0
        except Exception:
            decision_health_pct = 0.0

    overall_pct = (
        profiles_pct + sample_eff_pct + cat_coverage_pct + decision_health_pct
    ) / 4.0
    overall_pct = max(0.0, min(overall_pct, 1.0))

    if overall_pct < 0.3:
        tier = "bootstrap"
    elif overall_pct < 0.7:
        tier = "ramping_up"
    else:
        tier = "ready"

    return {
        "profiles_pct": round(profiles_pct, 4),
        "sample_eff_pct": round(sample_eff_pct, 4),
        "cat_coverage_pct": round(cat_coverage_pct, 4),
        "decision_health_pct": round(decision_health_pct, 4),
        "overall_pct": round(overall_pct, 4),
        "tier": tier,
        # Raw counts so the dashboard tooltip can show "823 / 5000 resolved"
        # instead of just "16%".
        "counts": {
            "positions_resolved": n_resolved,
            "leaders_with_sample": n_with_sample,
            "categories_covered": n_categories,
        },
        "targets": {
            "positions_resolved": _MATURITY_TARGET_POSITIONS_RESOLVED,
            "leaders_with_sample": _MATURITY_TARGET_LEADERS_WITH_TRADES,
            "categories_covered": _MATURITY_TARGET_CATEGORIES,
        },
    }


async def activation_queue(conn) -> list[dict]:
    import json as _json

    rows = await conn.fetch("""
        SELECT
            l.wallet_address, l.falcon_score, l.classification_json,
            COALESCE(p.trades_observed, 0)    AS trades_observed,
            COALESCE(p.positions_resolved, 0) AS positions_resolved,
            COALESCE(p.error_model_phase, 0)  AS error_model_phase,
            COUNT(e.id) FILTER (WHERE e.follow_probability > 0.6) AS confirmed_followers
        FROM leaders l
        LEFT JOIN leader_profiles p USING(wallet_address)
        LEFT JOIN follower_edges e ON e.leader_wallet = l.wallet_address
        WHERE l.on_watchlist=TRUE AND l.excluded=FALSE
        GROUP BY l.wallet_address, l.falcon_score, l.classification_json,
                 p.trades_observed, p.positions_resolved, p.error_model_phase
        HAVING NOT (
            (
                COALESCE(p.trades_observed,0) >= 50
                AND COUNT(e.id) FILTER (
                    WHERE e.follow_probability > 0.6 AND e.co_occurrences >= 5
                ) >= 5
            )
            OR COALESCE(p.positions_resolved,0) >= 50
        )
        ORDER BY COALESCE(p.trades_observed,0) DESC
        LIMIT 20
    """)
    result = []
    for r in rows:
        clf = r["classification_json"]
        if isinstance(clf, str):
            try:
                clf = _json.loads(clf)
            except Exception:
                clf = {}
        clf = clf or {}
        follow_pct = min(
            100,
            round(
                (
                    min(r["trades_observed"] / 50, 1) * 0.5
                    + min(r["confirmed_followers"] / 5, 1) * 0.3
                    + min(r["positions_resolved"] / 10, 1) * 0.2
                )
                * 100
            ),
        )
        result.append(
            {
                "wallet_address": r["wallet_address"],
                "falcon_score": float(r["falcon_score"] or 0),
                "strategy": clf.get("strategy", "—"),
                "trades_observed": int(r["trades_observed"]),
                "positions_resolved": int(r["positions_resolved"]),
                "confirmed_followers": int(r["confirmed_followers"]),
                "error_model_phase": int(r["error_model_phase"]),
                "follow_readiness_pct": follow_pct,
                "fade_readiness_pct": min(
                    100,
                    round(
                        (
                            min(r["positions_resolved"] / 50, 1) * 0.7
                            + min(r["error_model_phase"] / 2, 1) * 0.3
                        )
                        * 100
                    ),
                ),
            }
        )
    return result


async def open_positions_with_prices(conn, redis_client) -> list[dict]:
    """FIX 11: Open paper trades with live price from Redis and direction-aware unrealized PnL."""
    rows = await conn.fetch(
        f"""
        SELECT pt.id, pt.opened_at, pt.market_id, pt.token_id,
               pt.direction, pt.entry_price, pt.size_usdc,
               pt.strategy, pt.leader_wallet, pt.confidence,
               pt.fee_paid_usdc, pt.leader_context,
               EXTRACT(EPOCH FROM (NOW() - pt.opened_at))::int AS age_s,
               m.question, m.category, m.fee_rate_pct
        FROM paper_trades pt
        LEFT JOIN markets m USING (market_id)
        WHERE pt.status = 'open'
          AND {V1_PAPER_TRADE_PT_SQL}
        ORDER BY pt.opened_at DESC
        """
    )
    result = []
    for r in rows:
        market_id = r["market_id"]
        token_id = r["token_id"]
        entry_price = float(r["entry_price"] or 0)
        size_usdc = float(r["size_usdc"] or 0)
        direction = r["direction"]

        # Try Redis price cache first
        current_price = None
        if redis_client is not None:
            try:
                cached = await redis_client.get(f"price:{market_id}:{token_id}")
                if cached is not None:
                    current_price = float(cached)
            except Exception:
                pass
        # DB fallback
        if current_price is None:
            try:
                price_row = await conn.fetchrow(
                    "SELECT price FROM trades_observed "
                    "WHERE market_id=$1 AND token_id=$2 ORDER BY time DESC LIMIT 1",
                    market_id,
                    token_id,
                )
                if price_row:
                    current_price = float(price_row["price"])
            except Exception:
                pass

        # Direction-aware unrealized PnL
        if current_price is not None:
            if direction == "yes":
                unrealized_pnl = (current_price - entry_price) * size_usdc
            else:
                unrealized_pnl = (entry_price - current_price) * size_usdc
            fee = float(r["fee_paid_usdc"] or 0)
            unrealized_pnl = round(unrealized_pnl - fee, 2)
        else:
            unrealized_pnl = None

        result.append(
            {
                "id": r["id"],
                "opened_at": r["opened_at"].isoformat(),
                "market_id": market_id,
                "token_id": token_id,
                "question": r["question"] or f"Market {market_id[:30]}…",
                "category": r["category"],
                "direction": direction,
                "entry_price": entry_price,
                "current_price": current_price,
                "size_usdc": size_usdc,
                "unrealized_pnl": unrealized_pnl,
                "strategy": r["strategy"],
                "leader_wallet": r["leader_wallet"],
                "confidence": float(r["confidence"] or 0),
                "age_s": int(r["age_s"] or 0),
                "fee_rate_pct": float(r["fee_rate_pct"] or 0),
                "ml_snapshot": _extract_ml_snapshot(_row_get(r, "leader_context")),
            }
        )
    return result


async def recent_observed_trades(conn, limit: int = 50) -> list[dict]:
    # Perf fix (V1 audit Phase 3, dashboard rebuild hotpath):
    # The previous version used ROW_NUMBER() OVER (ORDER BY t.time DESC, ...)
    # to generate a global rank, which forces PostgreSQL to fully sort the
    # 580k-row trades_observed table on every call — defeating the
    # (time DESC) index. Frontend never consumes the rank; it only uses the
    # composed "id" string as a row key. Switching to t.id (BIGSERIAL PK
    # per CLAUDE.md § 6) gives a monotonically increasing identifier that's
    # already indexed and free to read.
    rows = await conn.fetch(
        """
        SELECT
            t.id AS seq,
            t.time,
            t.market_id,
            t.token_id,
            t.wallet_address,
            t.side,
            t.price,
            t.size_usdc,
            t.is_leader,
            m.question,
            m.category
        FROM trades_observed t
        LEFT JOIN markets m USING (market_id)
        WHERE t.time >= NOW() - INTERVAL '6 hours'
        ORDER BY t.time DESC
        LIMIT $1
        """,
        limit,
    )
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": (
                    f"obs:{_row_get(r, 'market_id')}:{_row_get(r, 'token_id')}:"
                    f"{_row_get(r, 'time').isoformat() if _row_get(r, 'time') else 'na'}:"
                    f"{_row_get(r, 'seq')}"
                ),
                "timestamp": _row_get(r, "time").isoformat() if _row_get(r, "time") else None,
                "market_id": _row_get(r, "market_id"),
                "market_title": _row_get(r, "question")
                or ((_row_get(r, "market_id", "")[:30] + "…") if _row_get(r, "market_id") else "—"),
                "market_category": _row_get(r, "category") or "unknown",
                "token_id": _row_get(r, "token_id"),
                "wallet_address": _row_get(r, "wallet_address"),
                "side": _row_get(r, "side"),
                "price": _to_float(_row_get(r, "price")),
                "notional": _to_float(_row_get(r, "size_usdc")),
                "execution_mode": "observed",
                "status": "observed",
                "is_leader": bool(_row_get(r, "is_leader", False)),
            }
        )
    return out


async def market_scanner_rows(conn, limit: int = 60) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = await conn.fetch(
        """
        WITH latest_books AS (
            SELECT DISTINCT ON (b.market_id, b.token_id)
                b.market_id,
                b.token_id,
                b.book_age_ms,
                b.spread_bps,
                b.mid_price,
                b.best_bid,
                b.best_ask,
                b.observed_at,
                b.source_timestamp
            FROM book_quality_snapshots b
            WHERE b.observed_at >= NOW() - INTERVAL '30 minutes'
            ORDER BY b.market_id, b.token_id, b.observed_at DESC
        ),
        trade_stats AS (
            SELECT
                t.market_id,
                COUNT(*) FILTER (WHERE t.time >= NOW() - INTERVAL '5 minutes')::int AS observations_5m,
                COUNT(*) FILTER (WHERE t.time >= NOW() - INTERVAL '1 minute')::int AS messages_last_minute,
                COUNT(*) FILTER (WHERE t.time >= NOW() - INTERVAL '30 minutes' AND t.is_leader)::int AS leader_trades_30m,
                MAX(t.time) AS last_trade_at
            FROM trades_observed t
            WHERE t.time >= NOW() - INTERVAL '30 minutes'
            GROUP BY t.market_id
        )
        SELECT
            lb.market_id,
            lb.token_id,
            lb.book_age_ms,
            lb.spread_bps,
            lb.mid_price,
            lb.best_bid,
            lb.best_ask,
            lb.observed_at,
            lb.source_timestamp,
            m.question,
            m.category,
            m.token_yes,
            m.token_no,
            COALESCE(ts.observations_5m, 0) AS observations_5m,
            COALESCE(ts.messages_last_minute, 0) AS messages_last_minute,
            COALESCE(ts.leader_trades_30m, 0) AS leader_trades_30m,
            ts.last_trade_at
        FROM latest_books lb
        LEFT JOIN markets m USING (market_id)
        LEFT JOIN trade_stats ts USING (market_id)
        ORDER BY
            COALESCE(ts.messages_last_minute, 0) DESC,
            COALESCE(ts.observations_5m, 0) DESC,
            lb.observed_at DESC
        LIMIT $1
        """,
        limit,
    )
    out: list[dict] = []
    for r in rows:
        observed_at = _parse_dt(_row_get(r, "observed_at"))
        source_ts = _parse_dt(_row_get(r, "source_timestamp"))
        freshness_ms = int(max(0.0, (now - observed_at).total_seconds() * 1000)) if observed_at else _to_int(_row_get(r, "book_age_ms"), 0)
        source_delay_ms = int(max(0.0, (observed_at - source_ts).total_seconds() * 1000)) if observed_at and source_ts else _to_int(_row_get(r, "book_age_ms"), 0)
        token_id = str(_row_get(r, "token_id") or "")
        token_yes = str(_row_get(r, "token_yes") or "")
        token_no = str(_row_get(r, "token_no") or "")
        if token_id and token_id == token_yes:
            direction = "YES"
        elif token_id and token_id == token_no:
            direction = "NO"
        else:
            direction = None
        spread_bps = _to_float(_row_get(r, "spread_bps"))
        out.append(
            {
                "market_id": _row_get(r, "market_id"),
                "token_id": token_id or None,
                "title": _row_get(r, "question")
                or ((_row_get(r, "market_id", "")[:30] + "…") if _row_get(r, "market_id") else "—"),
                "category": _row_get(r, "category") or "unknown",
                "market_type": _market_type_label(_row_get(r, "category"), _row_get(r, "question")),
                "direction": direction,
                "mid_price": _to_float(_row_get(r, "mid_price")),
                "spread_bps": spread_bps,
                "spread": round(spread_bps / 10000.0, 4) if spread_bps is not None else None,
                "best_bid": _to_float(_row_get(r, "best_bid")),
                "best_ask": _to_float(_row_get(r, "best_ask")),
                "freshness_ms": freshness_ms,
                "source_delay_ms": source_delay_ms,
                "observations": _to_int(_row_get(r, "observations_5m"), 0),
                "messages_last_minute": _to_int(_row_get(r, "messages_last_minute"), 0),
                "leader_trades_30m": _to_int(_row_get(r, "leader_trades_30m"), 0),
                "detected": _to_int(_row_get(r, "observations_5m"), 0) > 0,
                "quote_source": "book_quality_snapshots",
                "last_trade_at": _row_get(r, "last_trade_at").isoformat()
                if _row_get(r, "last_trade_at")
                else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Bundle 3 — observability queries
# ---------------------------------------------------------------------------
async def graph_top_edges(conn, limit: int = 30) -> dict:
    """Strongest Hawkes-confirmed follower edges.

    Ranks by hawkes_alpha_mu * follow_probability so a causal link with both
    strong excitation and high posterior probability floats to the top.
    """
    rows = await conn.fetch(
        """
        SELECT
            e.leader_wallet,
            e.follower_wallet,
            e.hawkes_alpha_mu,
            e.follow_probability,
            e.follow_beta_a,
            e.follow_beta_b,
            e.co_occurrences,
            e.avg_delay_s,
            e.same_direction_rate,
            e.trapped_rate,
            e.last_observed,
            e.first_observed
        FROM follower_edges e
        WHERE e.follow_probability > 0.6
          AND e.co_occurrences >= 5
        ORDER BY
            COALESCE(e.hawkes_alpha_mu, 0) * COALESCE(e.follow_probability, 0) DESC,
            e.co_occurrences DESC
        LIMIT $1
        """,
        limit,
    )
    edges = []
    nodes_set: dict[str, str] = {}  # wallet → role (leader|follower|both)
    for r in rows:
        hawkes = _to_float(_row_get(r, "hawkes_alpha_mu"), 0.0)
        prob = _to_float(_row_get(r, "follow_probability"), 0.0)
        cooc = _to_int(_row_get(r, "co_occurrences"), 0)
        # `is_confirmed` mirrors the R9 spec definition: the edge is a
        # confirmed follower link when the Hawkes excitation ratio
        # exceeds 1 (causal evidence) AND the Beta posterior crosses
        # the 60% follow_probability threshold with at least 5
        # co-occurrences (sample-size guard). The V2 graph view filters
        # by this flag — without it set, all edges were silently
        # dropped, leaving the graph empty.
        is_confirmed = (hawkes > 1.0) and (prob > 0.6) and (cooc >= 5)
        leader_w = _row_get(r, "leader_wallet")
        follower_w = _row_get(r, "follower_wallet")
        # Track all wallets appearing in any edge so the frontend can
        # render nodes without inferring them client-side.
        if leader_w:
            nodes_set[leader_w] = "both" if nodes_set.get(leader_w) == "follower" else "leader"
        if follower_w:
            nodes_set[follower_w] = "both" if nodes_set.get(follower_w) == "leader" else "follower"
        edges.append(
            {
                "leader_wallet": leader_w,
                "follower_wallet": follower_w,
                "hawkes_alpha_mu": hawkes,
                "follow_probability": prob,
                "beta_a": _to_float(_row_get(r, "follow_beta_a"), 0.0),
                "beta_b": _to_float(_row_get(r, "follow_beta_b"), 0.0),
                "co_occurrences": cooc,
                "avg_delay_s": _to_float(_row_get(r, "avg_delay_s"), 0.0),
                "same_direction_rate": _to_float(_row_get(r, "same_direction_rate"), 0.0),
                "trapped_rate": _to_float(_row_get(r, "trapped_rate"), 0.0),
                "is_confirmed": is_confirmed,
                "last_observed": _row_get(r, "last_observed").isoformat()
                if _row_get(r, "last_observed")
                else None,
                "first_observed": _row_get(r, "first_observed").isoformat()
                if _row_get(r, "first_observed")
                else None,
            }
        )
    nodes = [
        {"wallet_address": w, "role": role}
        for w, role in nodes_set.items()
    ]
    totals = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE follow_probability > 0.6 AND co_occurrences >= 5) AS confirmed,
            COUNT(*) FILTER (WHERE hawkes_alpha_mu > 1.0) AS hawkes_strong,
            COUNT(DISTINCT leader_wallet)
                FILTER (WHERE follow_probability > 0.6 AND co_occurrences >= 5) AS leaders_with_edges
        FROM follower_edges
        """
    )
    return {
        "edges": edges,
        "nodes": nodes,
        "totals": {
            "confirmed": _to_int(_row_get(totals, "confirmed"), 0),
            "hawkes_strong": _to_int(_row_get(totals, "hawkes_strong"), 0),
            "leaders_with_edges": _to_int(_row_get(totals, "leaders_with_edges"), 0),
        },
    }


async def profiler_health(conn) -> dict:
    """Error-model phase distribution, active drift alerts, and phase transition rate.

    Surfaces CUSUM drift signals (profile_json.error_model_runtime.drift_alert)
    and counts leaders per Beta / LogReg / LightGBM phase.
    """
    rows = await conn.fetch(
        f"""
        SELECT
            p.wallet_address,
            p.error_model_phase,
            p.positions_resolved,
            p.trades_observed,
            p.last_updated,
            p.profile_json
        FROM leader_profiles p
        WHERE {V1_PROFILE_P_SQL}
        """
    )
    phases = {"1": 0, "2": 0, "3": 0}
    drift_alerts: list[dict] = []
    recent_transitions = 0
    transitioned_24h = 0
    total_profiles = 0
    stale_profiles = 0
    now = datetime.now(timezone.utc)

    for r in rows:
        total_profiles += 1
        phase = _to_int(_row_get(r, "error_model_phase"), 1)
        key = "3" if phase >= 3 else ("2" if phase == 2 else "1")
        phases[key] += 1

        last_updated = _parse_dt(_row_get(r, "last_updated"))
        if last_updated is not None:
            age_s = (now - last_updated).total_seconds()
            if age_s > 2 * settings.FALCON_REFRESH_INTERVAL_S:
                stale_profiles += 1
            if age_s <= 86400:
                transitioned_24h += 1

        profile = _json_dict(_row_get(r, "profile_json"))
        runtime = profile.get("error_model_runtime") or {}
        if runtime.get("drift_alert"):
            triggered_at = runtime.get("drift_triggered_at") or runtime.get("last_drift_at")
            drift_alerts.append(
                {
                    "wallet_address": _row_get(r, "wallet_address"),
                    "phase": phase,
                    "drift_score": _to_float(runtime.get("cusum_score"), 0.0),
                    "error_rate": _to_float(runtime.get("error_rate"), 0.0),
                    "triggered_at": triggered_at,
                    "positions_resolved": _to_int(_row_get(r, "positions_resolved"), 0),
                }
            )
        if runtime.get("phase_transitioned_at"):
            tdt = _parse_dt(runtime.get("phase_transitioned_at"))
            if tdt and (now - tdt).total_seconds() <= 7 * 86400:
                recent_transitions += 1

    drift_alerts.sort(key=lambda d: d["drift_score"], reverse=True)
    return {
        "total_profiles": total_profiles,
        "phases": phases,
        "phase2_pct": round(phases["2"] / total_profiles * 100, 1) if total_profiles else 0.0,
        "phase3_pct": round(phases["3"] / total_profiles * 100, 1) if total_profiles else 0.0,
        "drift_alerts": drift_alerts[:20],
        "drift_alert_count": len(drift_alerts),
        "phase_transitions_7d": recent_transitions,
        "profiles_refreshed_24h": transitioned_24h,
        "stale_profiles": stale_profiles,
    }


async def data_quality(conn, redis_client=None) -> dict:
    """Silent-rot detector: unenriched markets, stale leaders, dead WS feed, orphan trades."""
    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {}

    # --- Market enrichment gaps ----------------------------------------------
    # unmapped_tokens only counts markets the bot actually cares about: those
    # with observed trades in the last 48h. Markets with NULL end_date and no
    # recent activity are typically resolved/dead markets that Polymarket Gamma
    # has purged — flagging them inflated the counter to ~1700 false positives
    # that never decreased no matter how many sync_markets cycles ran.
    mrow = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE (NULLIF(token_yes, '') IS NULL OR NULLIF(token_no, '') IS NULL)
                  AND (end_date IS NULL OR end_date > NOW() - INTERVAL '24 hours')
                  AND market_id IN (
                      SELECT DISTINCT market_id FROM trades_observed
                      WHERE time > NOW() - INTERVAL '48 hours'
                  )
            ) AS unmapped_tokens,
            COUNT(*) FILTER (WHERE end_date IS NOT NULL AND end_date < NOW()) AS expired_active,
            COUNT(*) FILTER (WHERE active = TRUE) AS active,
            COUNT(*) FILTER (
                WHERE (NULLIF(token_yes, '') IS NULL OR NULLIF(token_no, '') IS NULL)
                  AND end_date IS NOT NULL
                  AND end_date < NOW() - INTERVAL '24 hours'
            ) AS unmapped_expired_skipped
        FROM markets
        """
    )
    total_markets = _to_int(_row_get(mrow, "total"), 0)
    report["markets"] = {
        "total": total_markets,
        "active": _to_int(_row_get(mrow, "active"), 0),
        "unmapped_tokens": _to_int(_row_get(mrow, "unmapped_tokens"), 0),
        "expired_still_active": _to_int(_row_get(mrow, "expired_active"), 0),
        "unmapped_expired_skipped": _to_int(_row_get(mrow, "unmapped_expired_skipped"), 0),
        "token_map_coverage_pct": (
            round(
                (total_markets - _to_int(_row_get(mrow, "unmapped_tokens"), 0))
                / total_markets
                * 100,
                2,
            )
            if total_markets
            else None
        ),
    }

    # --- Orphan trades (trades whose market_id never got enriched) ----------
    orphan = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT t.market_id)
        FROM trades_observed t
        LEFT JOIN markets m USING(market_id)
        WHERE m.market_id IS NULL
          AND t.time >= NOW() - INTERVAL '7 days'
        """
    )
    report["markets"]["orphan_market_ids_7d"] = _to_int(orphan, 0)

    # --- Leader refresh staleness --------------------------------------------
    # Aligned with enrich_leaders' own 24h stale_cutoff (registry/leader_registry.py).
    # The previous threshold (FALCON_REFRESH_INTERVAL_S * 2 = ~1-2h) flagged
    # leaders as stale long before the registry's enrichment cycle would even
    # consider re-fetching them, causing 100% false-positive rates. The
    # registry CLAUDE.md notes that Falcon's leaderboard updates ~1x/day, so
    # 24h is the natural cadence for what counts as "stale" here.
    refresh_threshold = 86400
    lrow = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE last_refresh IS NULL
                   OR EXTRACT(EPOCH FROM (NOW() - last_refresh)) > $1
            ) AS stale
        FROM leaders
        WHERE on_watchlist = TRUE AND excluded = FALSE
        """,
        refresh_threshold,
    )
    report["leaders"] = {
        "active": _to_int(_row_get(lrow, "total"), 0),
        "stale_refresh": _to_int(_row_get(lrow, "stale"), 0),
        "stale_threshold_s": refresh_threshold,
    }

    # --- Profile staleness ---------------------------------------------------
    prow = await conn.fetchrow(
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE last_updated IS NULL
                   OR EXTRACT(EPOCH FROM (NOW() - last_updated)) > 86400
            ) AS stale
        FROM leader_profiles p
        WHERE {V1_PROFILE_P_SQL}
        """
    )
    report["profiles"] = {
        "total": _to_int(_row_get(prow, "total"), 0),
        "stale_over_24h": _to_int(_row_get(prow, "stale"), 0),
    }

    # --- Trade ingestion & WS feed (Redis) -----------------------------------
    last_trade_age_s = None
    try:
        last_trade_age_s = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(time))) FROM trades_observed WHERE time >= NOW() - INTERVAL '2 days'"
        )
    except Exception as exc:
        logger.warning(f"data_quality: last trade fetch failed: {exc}")

    ws_age_s = None
    price_cache_count = None
    if redis_client is not None:
        try:
            ts = await redis_client.get("ws:market:last_message_ts")
            if ts is not None:
                ws_age_s = max(0.0, now.timestamp() - float(ts))
        except Exception as exc:
            logger.warning(f"data_quality: ws ts fetch failed: {exc}")
        try:
            # Count cached prices (sample, capped)
            keys = []
            async for k in redis_client.scan_iter(match="price:*", count=500):
                keys.append(k)
                if len(keys) >= 5000:
                    break
            price_cache_count = len(keys)
        except Exception as exc:
            logger.warning(f"data_quality: price cache scan failed: {exc}")

    report["feed"] = {
        "last_trade_age_s": float(last_trade_age_s) if last_trade_age_s is not None else None,
        "ws_last_message_age_s": ws_age_s,
        "ws_healthy": ws_age_s is not None and ws_age_s <= 30.0,
        "price_cache_entries": price_cache_count,
    }

    # --- Overall health score -----------------------------------------------
    issues = 0
    if report["markets"]["unmapped_tokens"] > 0:
        issues += 1
    if report["markets"]["expired_still_active"] > 0:
        issues += 1
    if report["markets"]["orphan_market_ids_7d"] > 0:
        issues += 1
    if report["leaders"]["stale_refresh"] > 0:
        issues += 1
    if report["profiles"]["stale_over_24h"] > 0:
        issues += 1
    if not report["feed"]["ws_healthy"]:
        issues += 1
    report["issues_count"] = issues
    report["status"] = "healthy" if issues == 0 else ("degraded" if issues <= 2 else "unhealthy")
    return report


# ============================================================================
# UI v2 — Alpha Terminal extras (24h timeline + next-signal ETA)
# ============================================================================
async def alpha_extras(conn) -> dict:
    """
    Time-series data for the redesigned ALPHA TERMINAL hero panels.

    Returns:
        timeline       : 12 buckets of 2h each over the last 24h with
                         {trades, leader_trades, positions_resolved,
                          edges_observed, avg_maturity}
        readiness      : top leaders closest to triggering FOLLOW or FADE,
                         with what's missing and an ETA in hours.
        learning_totals: cumulative counts (current state) for headline KPIs.

    Resilience (B1v2 fix, 2026-05-19): the original timeline query used 12
    buckets × 4 correlated subqueries over `trades_observed` (~60M rows)
    = 48 range scans, and routinely timed out at 60s. The totals query
    used 9 sequential full-table COUNT(*) subqueries — same problem.

    Rewritten as single-pass GROUP BY using `date_bin()` (PG 14+,
    available in the prod 15 cluster). Each timeline source table is
    scanned ONCE with a `GROUP BY date_bin(...)`, then merged in Python
    by bucket. Totals is split into 3 independent fetches so a slow
    table doesn't block the others.

    We keep `SET LOCAL statement_timeout = '60s'` + try/except as
    defence-in-depth, but in practice each query now completes in
    <2s under the same load. Sequential on the same connection
    (asyncpg connections aren't concurrent-safe), but each is now an
    index-range scan instead of a correlated-subquery cartesian.
    """
    # Bump per-statement timeout for this call only. `SET LOCAL` is
    # transaction-scoped and avoids touching the shared pool config.
    try:
        await conn.execute("SET LOCAL statement_timeout = '60s'")
    except Exception as exc:
        logger.warning(f"alpha_extras: SET LOCAL statement_timeout failed: {exc}")

    # ---- 24h timeline (12 buckets of 2h) — single-pass per table -------- #
    # Strategy: scan each source table ONCE with `date_bin('2 hours', ...)`
    # + GROUP BY, then materialise the 12-bucket grid in Python and merge.
    # This drops the 48-range-scan cartesian from the legacy query down to
    # 3 index-range scans (trades_observed.time, positions.close_time,
    # follower_edges.last_observed).
    bucket_starts: list = []
    trades_by_bucket: dict = {}
    leader_trades_by_bucket: dict = {}
    positions_by_bucket: dict = {}
    edges_by_bucket: dict = {}

    # Q1 — trades + leader_trades from trades_observed (single scan)
    try:
        rows_trades = await conn.fetch(
            """
            SELECT
                date_bin('2 hours'::interval, time, NOW() - INTERVAL '24 hours') AS bucket_start,
                COUNT(*)::bigint AS trades,
                COUNT(*) FILTER (WHERE is_leader = TRUE)::bigint AS leader_trades
            FROM trades_observed
            WHERE time >= NOW() - INTERVAL '24 hours'
              AND time <  NOW()
            GROUP BY 1
            """
        )
        for r in rows_trades:
            bs = _row_get(r, "bucket_start")
            trades_by_bucket[bs] = _to_int(_row_get(r, "trades"), 0)
            leader_trades_by_bucket[bs] = _to_int(_row_get(r, "leader_trades"), 0)
    except Exception as exc:
        logger.warning(f"alpha_extras.timeline.trades failed: {exc}")

    # Q2 — positions_resolved from positions_reconstructed (single scan)
    try:
        rows_positions = await conn.fetch(
            """
            SELECT
                date_bin('2 hours'::interval, close_time, NOW() - INTERVAL '24 hours') AS bucket_start,
                COUNT(*)::bigint AS positions_resolved
            FROM positions_reconstructed
            WHERE close_time >= NOW() - INTERVAL '24 hours'
              AND close_time <  NOW()
            GROUP BY 1
            """
        )
        for r in rows_positions:
            bs = _row_get(r, "bucket_start")
            positions_by_bucket[bs] = _to_int(_row_get(r, "positions_resolved"), 0)
    except Exception as exc:
        logger.warning(f"alpha_extras.timeline.positions failed: {exc}")

    # Q3 — edges_active from follower_edges (single scan)
    try:
        rows_edges = await conn.fetch(
            """
            SELECT
                date_bin('2 hours'::interval, last_observed, NOW() - INTERVAL '24 hours') AS bucket_start,
                COUNT(*)::bigint AS edges_active
            FROM follower_edges
            WHERE last_observed >= NOW() - INTERVAL '24 hours'
              AND last_observed <  NOW()
            GROUP BY 1
            """
        )
        for r in rows_edges:
            bs = _row_get(r, "bucket_start")
            edges_by_bucket[bs] = _to_int(_row_get(r, "edges_active"), 0)
    except Exception as exc:
        logger.warning(f"alpha_extras.timeline.edges failed: {exc}")

    # Build the 12-bucket grid client-side so we always return 12 rows even
    # when a table had zero matching rows (preserves the legacy contract).
    try:
        grid_rows = await conn.fetch(
            """
            SELECT generate_series(
                date_bin('2 hours'::interval, NOW() - INTERVAL '24 hours', NOW() - INTERVAL '24 hours'),
                date_bin('2 hours'::interval, NOW() - INTERVAL '2 hours',  NOW() - INTERVAL '24 hours'),
                INTERVAL '2 hours'
            ) AS bucket_start
            """
        )
        bucket_starts = [_row_get(r, "bucket_start") for r in grid_rows]
    except Exception as exc:
        logger.warning(f"alpha_extras.timeline.grid failed: {exc}")
        bucket_starts = []

    timeline = [
        {
            "t": bs.isoformat() if bs else None,
            "trades": trades_by_bucket.get(bs, 0),
            "leader_trades": leader_trades_by_bucket.get(bs, 0),
            "positions_resolved": positions_by_bucket.get(bs, 0),
            "edges_active": edges_by_bucket.get(bs, 0),
        }
        for bs in bucket_starts
    ]

    # ---- Top leaders closest to FOLLOW readiness ------------------------ #
    # Thresholds from settings: FOLLOW needs 50 trades + 5 confirmed
    # followers + 10 resolved positions. We rank leaders by how few of
    # these gates remain.
    try:
        follow_rows = await conn.fetch(
            f"""
            WITH counts AS (
                SELECT
                    lp.wallet_address,
                    lp.trades_observed,
                    lp.positions_resolved,
                    lp.profile_maturity,
                    lp.error_model_phase,
                    COALESCE((
                        SELECT COUNT(*) FROM follower_edges e
                        WHERE e.leader_wallet = lp.wallet_address
                          AND e.co_occurrences >= 5
                          AND e.same_direction_rate >= 0.7
                    ), 0) AS confirmed_followers,
                    COALESCE((
                        SELECT COUNT(*) FROM trades_observed t
                        WHERE t.wallet_address = lp.wallet_address
                          AND t.time >= NOW() - INTERVAL '24 hours'
                    ), 0) AS trades_24h,
                    COALESCE(l.falcon_score, 0) AS falcon_score,
                    l.classification_json
                FROM leader_profiles lp
                JOIN leaders l USING (wallet_address)
                WHERE l.excluded = FALSE
                  AND l.on_watchlist = TRUE
            )
            SELECT
                wallet_address,
                trades_observed,
                positions_resolved,
                confirmed_followers,
                profile_maturity,
                error_model_phase,
                trades_24h,
                falcon_score,
                classification_json
            FROM counts
            ORDER BY
                -- Score: lower = closer to ready. Each gate contributes its
                -- gap (clamped to 0 once met). We weight followers more since
                -- they're the slowest to come online.
                (GREATEST(0, 50 - trades_observed)
                  + GREATEST(0, 10 - positions_resolved) * 2
                  + GREATEST(0, 5 - confirmed_followers) * 5) ASC,
                falcon_score DESC
            LIMIT 6
            """
        )
    except Exception as exc:
        logger.warning(f"alpha_extras.follow_rows failed: {exc}")
        follow_rows = []
    follow_ready: list[dict] = []
    for r in follow_rows:
        trades = _to_int(_row_get(r, "trades_observed"), 0)
        resolved = _to_int(_row_get(r, "positions_resolved"), 0)
        followers = _to_int(_row_get(r, "confirmed_followers"), 0)
        trades_24h = _to_int(_row_get(r, "trades_24h"), 0)
        rate_per_h = trades_24h / 24.0 if trades_24h else 0.0
        # ETA: hours to hit the binding gate (whichever is furthest).
        # Followers come from co-occurrences observed via graph_engine —
        # hard to estimate, so we approximate at 1 follower per 5 trades.
        gates = []
        if trades < 50:
            gates.append(("trades", 50 - trades, (50 - trades) / rate_per_h if rate_per_h else None))
        if resolved < 10:
            # Positions resolve at ~10% of trades observed (rough heuristic)
            est_h = ((10 - resolved) * 10) / rate_per_h if rate_per_h else None
            gates.append(("resolved", 10 - resolved, est_h))
        if followers < 5:
            est_h = ((5 - followers) * 5) / rate_per_h if rate_per_h else None
            gates.append(("followers", 5 - followers, est_h))
        eta_h = max((g[2] or 0) for g in gates) if gates else 0
        follow_ready.append(
            {
                "wallet_address": _row_get(r, "wallet_address"),
                "trades": trades,
                "trades_target": 50,
                "resolved": resolved,
                "resolved_target": 10,
                "followers": followers,
                "followers_target": 5,
                "phase": _to_int(_row_get(r, "error_model_phase"), 1),
                "maturity": _to_float(_row_get(r, "profile_maturity")),
                "rate_per_h": round(rate_per_h, 2),
                "missing": [{"gate": g[0], "gap": g[1], "eta_h": round(g[2], 1) if g[2] else None} for g in gates],
                "eta_h": round(eta_h, 1) if eta_h else None,
                "ready": len(gates) == 0,
            }
        )

    # ---- Learning totals (current state snapshot) ----------------------- #
    # B1v2: the legacy single-fetchrow with 9 sub-SELECTs forces all 9
    # COUNT(*) to share a single statement timeout. If `trades_observed`
    # COUNT(*) is slow, the entire totals payload is lost. We split into
    # 3 independent fetches grouped by table so a single slow scan only
    # zero-fills its own slice. (Still sequential on the same connection;
    # parallelising requires a pool refactor we don't want here.)
    trades_total = 0
    positions_resolved_total = 0
    edges_total = 0
    edges_confirmed = 0
    avg_maturity = 0.0
    profiles_total = 0
    phase1 = 0
    phase2 = 0
    phase3 = 0

    # T1 — trades_observed total (the hottest table)
    try:
        trades_total = _to_int(
            await conn.fetchval("SELECT COUNT(*) FROM trades_observed"),
            0,
        )
    except Exception as exc:
        logger.warning(f"alpha_extras.totals.trades failed: {exc}")

    # T2 — positions_reconstructed total + edges (two small tables)
    try:
        row_pos_edges = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM positions_reconstructed WHERE close_time IS NOT NULL)::bigint
                    AS positions_resolved_total,
                (SELECT COUNT(*) FROM follower_edges)::bigint AS edges_total,
                (SELECT COUNT(*) FROM follower_edges
                    WHERE co_occurrences >= 5 AND same_direction_rate >= 0.7)::bigint
                    AS edges_confirmed
            """
        )
        if row_pos_edges:
            positions_resolved_total = _to_int(_row_get(row_pos_edges, "positions_resolved_total"), 0)
            edges_total = _to_int(_row_get(row_pos_edges, "edges_total"), 0)
            edges_confirmed = _to_int(_row_get(row_pos_edges, "edges_confirmed"), 0)
    except Exception as exc:
        logger.warning(f"alpha_extras.totals.positions_edges failed: {exc}")

    # T3 — leader_profiles aggregates (single table, single scan)
    try:
        row_profiles = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::bigint                                        AS profiles_total,
                COUNT(*) FILTER (WHERE error_model_phase = 1)::bigint   AS phase1,
                COUNT(*) FILTER (WHERE error_model_phase = 2)::bigint   AS phase2,
                COUNT(*) FILTER (WHERE error_model_phase = 3)::bigint   AS phase3,
                COALESCE(AVG(profile_maturity), 0)::float8              AS avg_maturity
            FROM leader_profiles
            """
        )
        if row_profiles:
            profiles_total = _to_int(_row_get(row_profiles, "profiles_total"), 0)
            phase1 = _to_int(_row_get(row_profiles, "phase1"), 0)
            phase2 = _to_int(_row_get(row_profiles, "phase2"), 0)
            phase3 = _to_int(_row_get(row_profiles, "phase3"), 0)
            avg_maturity = _to_float(_row_get(row_profiles, "avg_maturity"), 0.0)
    except Exception as exc:
        logger.warning(f"alpha_extras.totals.profiles failed: {exc}")

    return {
        "timeline": timeline,
        "follow_ready": follow_ready,
        "totals": {
            "trades_total": trades_total,
            "positions_resolved_total": positions_resolved_total,
            "edges_total": edges_total,
            "edges_confirmed": edges_confirmed,
            "avg_maturity": round(avg_maturity, 4),
            "profiles_total": profiles_total,
            "phase1": phase1,
            "phase2": phase2,
            "phase3": phase3,
        },
    }


# ============================================================================
# UI v2 — Wallet Graph (force-directed friendly payload)
# ============================================================================
async def wallet_graph(conn, max_leaders: int = 3000) -> dict:
    """
    Returns node + edge lists ready for a force-directed visualisation.

    Cap notes (bumped 2026-05-17 for the Cosmograph WebGL viz, which targets
    rendering the full leader graph rather than a curated SVG slice):
      - max_leaders: 3000 covers all 2,628 watchlisted leaders with headroom.
      - edge LIMIT: 100000 covers the ~28k confirmed (co_occurrences>=2)
        edges with headroom. The `co_occurrences >= 2` floor filters
        single-coincidence noise (most are random co-occurrences).
      - Expect total payload ~3-6 MB JSON; the snapshot is cached for 30 s
        in api.main so this query runs at most twice per minute.

    nodes: [{id, label, role:'leader|follower', falcon_score, phase,
             maturity, total_trades, classification, x?, y?}]
    edges: [{source, target, p_follow, hawkes_alpha_mu, delay_s,
             same_dir, co_occurrences, confirmed}]
    """
    # Top leaders by maturity * falcon_score (so we surface the "alive" ones).
    # Also pulls win_rate (closed paper trades), 24h trade count and the
    # latest decision context so the front-end Wallet Scanner can show a
    # full leader-centric row without an extra round-trip.
    leader_rows = await conn.fetch(
        f"""
        WITH
        winrate AS (
            SELECT leader_wallet,
                   COUNT(*) FILTER (WHERE pnl_usdc IS NOT NULL)        AS closed_total,
                   COUNT(*) FILTER (WHERE pnl_usdc > 0)                AS wins,
                   COALESCE(SUM(pnl_usdc), 0)                          AS pnl_total
            FROM paper_trades
            WHERE status = 'closed'
              AND {V1_PAPER_TRADE_SQL}
            GROUP BY leader_wallet
        ),
        recent_act AS (
            SELECT wallet_address,
                   COUNT(*)                                            AS trades_24h,
                   MAX(time)                                           AS last_seen
            FROM trades_observed
            WHERE time >= NOW() - INTERVAL '24 hours'
              AND is_leader = TRUE
            GROUP BY wallet_address
        ),
        last_dec AS (
            SELECT DISTINCT ON (leader_wallet)
                   leader_wallet, action, confidence, time AS decided_at
            FROM decision_log
            WHERE time >= NOW() - INTERVAL '24 hours'
            ORDER BY leader_wallet, time DESC
        )
        SELECT lp.wallet_address,
               lp.profile_maturity,
               lp.error_model_phase,
               lp.trades_observed,
               lp.positions_resolved,
               COALESCE(l.falcon_score, 0)        AS falcon_score,
               l.classification_json,
               l.exclude_reason,
               COALESCE(wr.wins, 0)::float / NULLIF(wr.closed_total, 0) AS win_rate,
               COALESCE(wr.closed_total, 0)       AS closed_total,
               COALESCE(wr.pnl_total, 0)          AS pnl_total,
               COALESCE(ra.trades_24h, 0)         AS trades_24h,
               ra.last_seen,
               ld.action                           AS last_action,
               ld.confidence                       AS last_confidence,
               ld.decided_at                       AS last_decision_at
        FROM leader_profiles lp
        JOIN leaders l USING (wallet_address)
        LEFT JOIN winrate wr ON wr.leader_wallet = lp.wallet_address
        LEFT JOIN recent_act ra ON ra.wallet_address = lp.wallet_address
        LEFT JOIN last_dec ld ON ld.leader_wallet = lp.wallet_address
        WHERE l.excluded = FALSE
        ORDER BY (COALESCE(lp.profile_maturity, 0) * (COALESCE(l.falcon_score, 0) + 0.1)) DESC
        LIMIT $1
        """,
        max_leaders,
    )
    leader_wallets = {str(_row_get(r, "wallet_address")) for r in leader_rows}

    # Edges from those leaders. Tight cap to keep snapshot under 2s
    # — the snapshot already runs ~17 parallel queries. For the FULL
    # graph view, the dashboard should call /api/wallet_graph/full
    # which queries follower_edges directly (no other queries gathered).
    edge_rows = await conn.fetch(
        """
        SELECT leader_wallet, follower_wallet, follow_probability,
               hawkes_alpha_mu, avg_delay_s, same_direction_rate,
               co_occurrences, trapped_rate
        FROM follower_edges
        WHERE leader_wallet = ANY($1::text[])
          AND co_occurrences >= 5
        ORDER BY follow_probability DESC NULLS LAST,
                 co_occurrences DESC
        LIMIT 5000
        """,
        list(leader_wallets),
    )

    # Collect follower wallets we want to also surface as nodes.
    follower_wallets = {str(_row_get(r, "follower_wallet")) for r in edge_rows} - leader_wallets
    follower_meta_rows: list = []
    if follower_wallets:
        follower_meta_rows = await conn.fetch(
            """
            SELECT lp.wallet_address,
                   lp.profile_maturity,
                   lp.error_model_phase,
                   lp.trades_observed,
                   COALESCE(l.falcon_score, 0) AS falcon_score,
                   l.classification_json
            FROM leader_profiles lp
            LEFT JOIN leaders l USING (wallet_address)
            WHERE lp.wallet_address = ANY($1::text[])
            """,
            list(follower_wallets),
        )
    follower_meta = {str(_row_get(r, "wallet_address")): r for r in follower_meta_rows}

    def _classification_strategy(blob) -> str | None:
        try:
            parsed = blob if isinstance(blob, dict) else json.loads(blob) if blob else {}
            return parsed.get("strategy")
        except Exception:
            return None

    # Per-wallet top-3 categories over the last 30 days. Reads denormalized
    # trades_observed.category, so this stays correct even after the markets
    # table is pruned of resolved fossiles.
    top_cats_by_wallet: dict[str, list[dict]] = {}
    if leader_wallets:
        cat_rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT wallet_address,
                       COALESCE(NULLIF(category, ''), 'unknown') AS category,
                       COUNT(*)::int AS n,
                       SUM(COUNT(*)) OVER (PARTITION BY wallet_address) AS total,
                       ROW_NUMBER() OVER (
                           PARTITION BY wallet_address
                           ORDER BY COUNT(*) DESC
                       ) AS rk
                FROM trades_observed
                WHERE wallet_address = ANY($1::text[])
                  AND time >= NOW() - INTERVAL '30 days'
                  AND is_leader = TRUE
                GROUP BY wallet_address, COALESCE(NULLIF(category, ''), 'unknown')
            )
            SELECT wallet_address, category, n, total
            FROM ranked
            WHERE rk <= 3
            ORDER BY wallet_address, n DESC
            """,
            list(leader_wallets),
        )
        for row in cat_rows:
            w = str(_row_get(row, "wallet_address"))
            n = _to_int(_row_get(row, "n"), 0)
            total = _to_int(_row_get(row, "total"), 0) or 1
            top_cats_by_wallet.setdefault(w, []).append({
                "category": str(_row_get(row, "category") or "unknown"),
                "trades": n,
                "pct": round(n / total, 4),
            })

    nodes: list[dict] = []
    for r in leader_rows:
        wallet = str(_row_get(r, "wallet_address"))
        last_seen = _row_get(r, "last_seen")
        last_decision_at = _row_get(r, "last_decision_at")
        nodes.append(
            {
                "id": wallet,
                "label": wallet[:6] + "…" + wallet[-4:],
                "role": "leader",
                "falcon_score": _to_float(_row_get(r, "falcon_score"), 0.0),
                "phase": _to_int(_row_get(r, "error_model_phase"), 1),
                "maturity": _to_float(_row_get(r, "profile_maturity"), 0.0),
                "trades_observed": _to_int(_row_get(r, "trades_observed"), 0),
                "positions_resolved": _to_int(_row_get(r, "positions_resolved"), 0),
                "classification": _classification_strategy(_row_get(r, "classification_json")),
                "exclude_reason": _row_get(r, "exclude_reason"),
                # ── Wallet Scanner enrichments (replaces the old market-centric scanner) ──
                "win_rate": _to_float(_row_get(r, "win_rate")) if _row_get(r, "win_rate") is not None else None,
                "closed_total": _to_int(_row_get(r, "closed_total"), 0),
                "pnl_total": _to_float(_row_get(r, "pnl_total"), 0.0),
                "trades_24h": _to_int(_row_get(r, "trades_24h"), 0),
                "last_seen_iso": last_seen.isoformat() if last_seen else None,
                "last_action": _row_get(r, "last_action"),
                "last_confidence": _to_float(_row_get(r, "last_confidence")) if _row_get(r, "last_confidence") is not None else None,
                "last_decision_iso": last_decision_at.isoformat() if last_decision_at else None,
                "top_categories": top_cats_by_wallet.get(wallet, []),
            }
        )
    for wallet in follower_wallets:
        m = follower_meta.get(wallet)
        nodes.append(
            {
                "id": wallet,
                "label": wallet[:6] + "…" + wallet[-4:],
                "role": "follower",
                "falcon_score": _to_float(_row_get(m, "falcon_score"), 0.0) if m else 0.0,
                "phase": _to_int(_row_get(m, "error_model_phase"), 1) if m else 1,
                "maturity": _to_float(_row_get(m, "profile_maturity"), 0.0) if m else 0.0,
                "trades_observed": _to_int(_row_get(m, "trades_observed"), 0) if m else 0,
                "positions_resolved": 0,
                "classification": _classification_strategy(_row_get(m, "classification_json")) if m else None,
                "exclude_reason": None,
            }
        )

    edges: list[dict] = []
    for r in edge_rows:
        co_occ = _to_int(_row_get(r, "co_occurrences"), 0)
        same_dir = _to_float(_row_get(r, "same_direction_rate"), 0.0)
        confirmed = co_occ >= 5 and same_dir >= 0.7
        edges.append(
            {
                "source": str(_row_get(r, "leader_wallet")),
                "target": str(_row_get(r, "follower_wallet")),
                "p_follow": _to_float(_row_get(r, "follow_probability"), 0.0),
                "hawkes_alpha_mu": _to_float(_row_get(r, "hawkes_alpha_mu")),
                "delay_s": _to_float(_row_get(r, "avg_delay_s")),
                "same_dir": same_dir,
                "co_occurrences": co_occ,
                "trapped_rate": _to_float(_row_get(r, "trapped_rate")),
                "confirmed": confirmed,
            }
        )

    # Stats
    confirmed_count = sum(1 for e in edges if e["confirmed"])
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "leaders": len(leader_rows),
            "followers": len(follower_wallets),
            "edges_total": len(edges),
            "edges_confirmed": confirmed_count,
        },
    }


# ============================================================================
# UI v2 — Decision rejections breakdown (last hour)
# ============================================================================
async def decision_rejections_breakdown(conn, hours: int = 1) -> dict:
    """Aggregate SKIP reasons over the last `hours` for the dashboard."""
    rows = await conn.fetch(
        """
        SELECT
            COALESCE(NULLIF(reason, ''), 'unspecified') AS reason,
            COUNT(*) AS count,
            COUNT(DISTINCT leader_wallet) AS uniq_leaders,
            COUNT(DISTINCT market_id) AS uniq_markets,
            MAX(time) AS last_seen
        FROM decision_log
        WHERE action = 'skip'
          AND time >= NOW() - ($1 || ' hours')::interval
        GROUP BY 1
        ORDER BY count DESC
        LIMIT 12
        """,
        str(hours),
    )
    total = sum(_to_int(_row_get(r, "count"), 0) for r in rows)
    breakdown = []
    for r in rows:
        cnt = _to_int(_row_get(r, "count"), 0)
        breakdown.append(
            {
                "reason": _row_get(r, "reason"),
                "count": cnt,
                "pct": round(cnt / total * 100, 1) if total else 0,
                "uniq_leaders": _to_int(_row_get(r, "uniq_leaders"), 0),
                "uniq_markets": _to_int(_row_get(r, "uniq_markets"), 0),
                "last_seen": _row_get(r, "last_seen").isoformat() if _row_get(r, "last_seen") else None,
            }
        )
    return {"total": total, "window_hours": hours, "breakdown": breakdown}


# ============================================================================
# UI v2 — Equity curve for LIVE PORTFOLIO
# ============================================================================
async def inspector_snapshot(conn, redis_client=None, limit: int = 80) -> dict:
    """
    Pipeline observability snapshot for the INSPECTOR tab.

    PERFORMANCE: post V1-audit, the 5 sub-queries below used to run
    sequentially on one connection and took ~9s total. They are now
    issued in parallel via asyncio.gather() across separate pool
    connections. Latency drops to ~max(slowest) instead of sum().

    Surfaces the raw signals the bot is reacting to, so operators can
    diagnose attribution issues, source skew, latency drift, and
    decision-pipeline stalls without SSH'ing into the server.

    Sections returned:
      raw_trades   — last N trades_observed rows with full payload
      decisions    — last N decision_log rows with reason and confidence
      source_mix   — count by source over the last 5 min (ws / api_market / api_wallet)
      pipeline     — heartbeat / lag / pubsub backlog metrics from Redis
      counters     — DB-side counters (trades 1h, decisions 1h, etc.)
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {"generated_at": now.isoformat()}

    # Run the 4 DB sub-queries in parallel. `conn` is the caller's
    # connection — we need separate connections to actually run
    # in parallel. Import lazily to avoid circular dep at module load.
    from src.database.connection import get_db as _get_db

    async def _q_raw_trades():
        async with _get_db() as c:
            return await c.fetch(
                """
                SELECT t.id, t.time, t.market_id, t.token_id, t.wallet_address,
                       t.side, t.price, t.size_usdc, t.source, t.is_leader,
                       m.question AS market_question, m.category AS market_category
                FROM trades_observed t
                LEFT JOIN markets m ON m.market_id = t.market_id
                ORDER BY t.time DESC
                LIMIT $1
                """,
                limit,
            )

    async def _q_decisions():
        async with _get_db() as c:
            return await c.fetch(
                """
                SELECT time, leader_wallet, market_id, action, confidence,
                       kelly_fraction, thompson_follow, thompson_fade, reason, outcome
                FROM decision_log
                ORDER BY time DESC
                LIMIT $1
                """,
                min(limit, 50),
            )

    async def _q_source_mix():
        async with _get_db() as c:
            return await c.fetch(
                """
                SELECT COALESCE(source, 'unknown') AS source,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_leader) AS leader_count
                FROM trades_observed
                WHERE time > NOW() - INTERVAL '5 minutes'
                GROUP BY source
                ORDER BY total DESC
                """
            )

    async def _q_counters():
        async with _get_db() as c:
            return await c.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM trades_observed WHERE time > NOW() - INTERVAL '1 hour')      AS trades_1h,
                    (SELECT COUNT(*) FROM trades_observed
                      WHERE time > NOW() - INTERVAL '1 hour' AND is_leader = TRUE)                      AS leader_trades_1h,
                    (SELECT COUNT(*) FROM decision_log WHERE time > NOW() - INTERVAL '1 hour')          AS decisions_1h,
                    (SELECT COUNT(*) FROM decision_log
                      WHERE time > NOW() - INTERVAL '1 hour' AND action != 'skip')                      AS actionable_1h,
                    (SELECT COUNT(*) FROM positions_reconstructed
                      WHERE close_time > NOW() - INTERVAL '1 hour')                                     AS closes_1h
                """
            )

    raw_rows, dec_rows, src_rows, counters_row = await asyncio.gather(
        _q_raw_trades(),
        _q_decisions(),
        _q_source_mix(),
        _q_counters(),
    )
    payload["raw_trades"] = [
        {
            "id": _to_int(_row_get(r, "id"), 0),
            "time": _row_get(r, "time").isoformat() if _row_get(r, "time") else None,
            "market_id": _row_get(r, "market_id"),
            "market_question": _row_get(r, "market_question"),
            "market_category": _row_get(r, "market_category"),
            "token_id": _row_get(r, "token_id"),
            "wallet_address": _row_get(r, "wallet_address"),
            "side": _row_get(r, "side"),
            "price": _to_float(_row_get(r, "price"), 0.0),
            "size_usdc": _to_float(_row_get(r, "size_usdc"), 0.0),
            "source": _row_get(r, "source"),
            "is_leader": bool(_row_get(r, "is_leader")),
        }
        for r in raw_rows
    ]

    # ── Decision log (last N) — results from parallel gather above ─────────
    payload["decisions"] = [
        {
            "time": _row_get(r, "time").isoformat() if _row_get(r, "time") else None,
            "leader_wallet": _row_get(r, "leader_wallet"),
            "market_id": _row_get(r, "market_id"),
            "action": _row_get(r, "action"),
            "confidence": _to_float(_row_get(r, "confidence")),
            "kelly_fraction": _to_float(_row_get(r, "kelly_fraction")),
            "thompson_follow": _to_float(_row_get(r, "thompson_follow")),
            "thompson_fade": _to_float(_row_get(r, "thompson_fade")),
            "reason": _row_get(r, "reason"),
            "outcome": _row_get(r, "outcome"),
        }
        for r in dec_rows
    ]

    # ── Source mix (last 5 min) — parallel result ─────────────────────────
    payload["source_mix"] = [
        {
            "source": _row_get(r, "source"),
            "total": _to_int(_row_get(r, "total"), 0),
            "leader_count": _to_int(_row_get(r, "leader_count"), 0),
        }
        for r in src_rows
    ]

    # ── DB-side counters (last 1h) — parallel result ──────────────────────
    payload["counters"] = {
        "trades_1h": _to_int(_row_get(counters_row, "trades_1h"), 0),
        "leader_trades_1h": _to_int(_row_get(counters_row, "leader_trades_1h"), 0),
        "decisions_1h": _to_int(_row_get(counters_row, "decisions_1h"), 0),
        "actionable_1h": _to_int(_row_get(counters_row, "actionable_1h"), 0),
        "closes_1h": _to_int(_row_get(counters_row, "closes_1h"), 0),
    }

    # ── Redis pipeline metrics ───────────────────────────────────────────────
    pipeline: dict[str, Any] = {
        "ws_last_message_age_s": None,
        "ws_msgs_per_min": None,
        "trades_pubsub_backlog": None,
        "redis_reachable": False,
    }
    if redis_client is not None:
        try:
            ts = await redis_client.get("ws:market:last_message_ts")
            # FIX (post-deploy 2026-05-19): mark Redis reachable as soon as
            # the FIRST get succeeds — otherwise a later sub-fetch (PUBSUB
            # NUMSUB, etc.) raising an exception would keep redis_reachable
            # = False even though Redis is clearly alive. The dashboard's
            # "Redis reachable: DOWN" false-negative came from this.
            pipeline["redis_reachable"] = True
            if ts is not None:
                pipeline["ws_last_message_age_s"] = max(0.0, now.timestamp() - float(ts))
            # KEY MISMATCH FIX (Batch 2 / A7): the producer in
            # trade_observer._handle_ws_message writes per-minute buckets
            # under `ws:msgs:minute:<bucket>`, while this consumer was
            # reading the never-populated `ws:market:msgs_per_min`. The
            # current minute may be partial (we're mid-bucket), so we
            # SUM the previous minute + current minute; that gives a
            # rolling 1-2 min window that the dashboard treats as
            # "messages per minute" without divide-by-zero pitfalls.
            now_bucket = int(_time_module.time() // 60)
            try:
                values = await redis_client.mget(
                    f"ws:msgs:minute:{now_bucket - 1}",
                    f"ws:msgs:minute:{now_bucket}",
                )
                total = 0
                seen_any = False
                for v in values or []:
                    if v is None:
                        continue
                    seen_any = True
                    try:
                        total += int(v)
                    except (TypeError, ValueError):
                        # bytes from raw redis (no decode_responses) — try str path
                        try:
                            total += int(v.decode("utf-8")) if isinstance(v, (bytes, bytearray)) else 0
                        except Exception:
                            pass
                if seen_any:
                    pipeline["ws_msgs_per_min"] = float(total)
            except Exception:
                # Don't poison the rest of the pipeline payload on a
                # single redis hiccup — leave the field as None.
                pass
            # Channel pubsub channel stats (rough — pubsub doesn't have persistent backlog,
            # but we can read the count of sub-listeners as a sanity check).
            try:
                channels = await redis_client.execute_command("PUBSUB", "NUMSUB", "trades:observed")
                if isinstance(channels, list) and len(channels) >= 2:
                    pipeline["trades_pubsub_subscribers"] = int(channels[1])
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"inspector_snapshot redis fetch failed: {exc}")

    payload["pipeline"] = pipeline
    return payload


async def equity_curve(conn, limit: int = 200) -> dict:
    """Recent portfolio equity time-series + breakdown by leader/strategy."""
    series_rows = await conn.fetch(
        """
        SELECT time, capital, equity, unrealized_pnl, realized_pnl_cum, open_positions
        FROM portfolio_equity
        WHERE time >= NOW() - INTERVAL '7 days'
        ORDER BY time DESC
        LIMIT $1
        """,
        limit,
    )
    series = [
        {
            "t": _row_get(r, "time").isoformat() if _row_get(r, "time") else None,
            "capital": _to_float(_row_get(r, "capital"), 0.0),
            "equity": _to_float(_row_get(r, "equity"), 0.0),
            "unrealized_pnl": _to_float(_row_get(r, "unrealized_pnl"), 0.0),
            "realized_pnl_cum": _to_float(_row_get(r, "realized_pnl_cum"), 0.0),
            "open_positions": _to_int(_row_get(r, "open_positions"), 0),
        }
        for r in reversed(series_rows)  # chronological asc for sparklines
    ]

    by_leader_rows = await conn.fetch(
        """
        SELECT leader_wallet,
               COUNT(*) AS trades,
               COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins,
               COALESCE(SUM(pnl_usdc), 0) AS pnl,
               COALESCE(AVG(pnl_usdc), 0) AS avg_pnl
        FROM paper_trades
        WHERE status = 'closed'
          AND opened_at >= NOW() - INTERVAL '30 days'
        GROUP BY leader_wallet
        ORDER BY pnl DESC
        LIMIT 20
        """
    )
    by_leader = [
        {
            "wallet": _row_get(r, "leader_wallet"),
            "trades": _to_int(_row_get(r, "trades"), 0),
            "wins": _to_int(_row_get(r, "wins"), 0),
            "pnl": _to_float(_row_get(r, "pnl"), 0.0),
            "avg_pnl": _to_float(_row_get(r, "avg_pnl"), 0.0),
        }
        for r in by_leader_rows
    ]

    by_strategy_rows = await conn.fetch(
        """
        SELECT strategy,
               COUNT(*) AS trades,
               COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins,
               COALESCE(SUM(pnl_usdc), 0) AS pnl
        FROM paper_trades
        WHERE status = 'closed'
        GROUP BY strategy
        """
    )
    by_strategy = [
        {
            "strategy": _row_get(r, "strategy"),
            "trades": _to_int(_row_get(r, "trades"), 0),
            "wins": _to_int(_row_get(r, "wins"), 0),
            "pnl": _to_float(_row_get(r, "pnl"), 0.0),
        }
        for r in by_strategy_rows
    ]

    return {
        "series": series,
        "by_leader": by_leader,
        "by_strategy": by_strategy,
    }


# ─── WALLET PROFILE DRILL-DOWN ─────────────────────────────────────────────
async def wallet_profile(conn, wallet_address: str) -> dict | None:
    """Full per-wallet profile for the Wallet Graph drilldown.

    Surfaces the rich behavioural state we already compute server-side:
      - preferred_categories (Dirichlet posteriors → top 5)
      - entry_patterns (contrarian/momentum + time-of-day)
      - sizing (avg, EWMA-smoothed)
      - accuracy (overall + by_category Beta posteriors)
      - wallet360 highlights (Falcon Wallet360 selected metrics)
      - edge counts (incoming/outgoing in the social graph)
    """
    if not isinstance(wallet_address, str) or not wallet_address.startswith("0x") or len(wallet_address) != 42:
        return None

    row = await conn.fetchrow(
        """
        SELECT lp.wallet_address,
               lp.profile_json,
               lp.profile_maturity,
               lp.error_model_phase,
               lp.trades_observed,
               lp.positions_resolved,
               lp.last_updated,
               l.falcon_score,
               l.classification_json,
               l.wallet360_json,
               l.first_seen,
               l.last_refresh,
               l.excluded,
               l.exclude_reason,
               l.on_watchlist
        FROM leader_profiles lp
        LEFT JOIN leaders l USING (wallet_address)
        WHERE lp.wallet_address = $1
        """,
        wallet_address,
    )
    if not row:
        return None

    def _parse(blob):
        if blob is None: return {}
        if isinstance(blob, dict): return blob
        try: return json.loads(blob)
        except Exception: return {}

    profile = _parse(_row_get(row, "profile_json"))
    classification = _parse(_row_get(row, "classification_json"))
    w360 = _parse(_row_get(row, "wallet360_json"))

    # ── Preferred categories — Dirichlet → top 5 with normalized probabilities
    cat_block = profile.get("preferred_categories") or profile.get("category_counts") or {}
    cat_items: list[tuple[str, float]] = []
    if isinstance(cat_block, dict):
        # Each value can be a Dirichlet count OR a {alpha:..} dict.
        for k, v in cat_block.items():
            try:
                count = float(v) if not isinstance(v, dict) else float(v.get("alpha") or v.get("count") or v.get("posterior") or 0)
            except Exception:
                count = 0.0
            cat_items.append((str(k), count))
    cat_total = sum(c for _, c in cat_items) or 1.0
    preferred_categories = sorted(
        [{"category": k, "alpha": round(c, 3), "pct": round(c / cat_total, 4)} for k, c in cat_items],
        key=lambda x: x["pct"],
        reverse=True,
    )[:6]

    # ── Accuracy by category — Beta posteriors → mean win rate
    acc_block = profile.get("accuracy") or {}
    by_cat = acc_block.get("by_category") or {}
    accuracy_by_category = []
    for cat, stats in (by_cat.items() if isinstance(by_cat, dict) else []):
        if not isinstance(stats, dict): continue
        a = float(stats.get("beta_a", stats.get("alpha", 1)) or 1)
        b = float(stats.get("beta_b", stats.get("beta", 1)) or 1)
        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)
        n = wins + losses
        win_rate = a / (a + b) if (a + b) > 0 else None
        accuracy_by_category.append({
            "category": cat,
            "wins": wins,
            "losses": losses,
            "n": n,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "beta_a": a,
            "beta_b": b,
        })
    accuracy_by_category.sort(key=lambda x: x["n"], reverse=True)

    # ── Sizing
    sizing = profile.get("sizing") or {}
    sizing_out = {
        "avg_size_usdc": _to_float(sizing.get("avg_size") or sizing.get("avg_size_usdc")),
        "ewma_size_usdc": _to_float(sizing.get("ewma_size") or sizing.get("ewma_size_usdc")),
    }

    # ── Entry patterns
    entry = profile.get("entry_patterns") or {}
    entry_out = {
        "contrarian_rate": _to_float(entry.get("contrarian_rate")),
        "momentum_rate": _to_float(entry.get("momentum_rate")),
    }

    # ── Wallet360 highlights — pick the metrics that matter for an operator
    w360_keys = (
        "total_trades", "days_active", "total_pnl", "win_rate",
        "sharpe_ratio", "sortino_ratio", "calmar_ratio", "max_drawdown",
        "avg_trade_duration_s", "avg_holding_period_days",
        "markets_traded", "total_invested", "ulcer_index",
        "buy_trade_ratio", "timing_z_score", "timing_hit_rate",
        "best_market_pnl", "sybil_risk_flag", "risk_level",
    )
    w360_highlights = {k: w360.get(k) for k in w360_keys if k in w360}

    # ── Edge counts in the social graph
    edge_counts = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE leader_wallet = $1)::int AS as_leader,
            COUNT(*) FILTER (WHERE follower_wallet = $1)::int AS as_follower,
            COUNT(*) FILTER (
                WHERE leader_wallet = $1
                  AND co_occurrences >= 5
                  AND same_direction_rate >= 0.7
            )::int AS confirmed_followers
        FROM follower_edges
        """,
        wallet_address,
    )

    # ── Decisions activity on this leader
    dec_summary = await conn.fetchrow(
        """
        SELECT
            COUNT(*)::int AS total,
            COUNT(*) FILTER (WHERE action = 'follow')::int AS follow,
            COUNT(*) FILTER (WHERE action = 'fade')::int AS fade,
            COUNT(*) FILTER (WHERE action = 'skip')::int AS skip,
            MAX(time) AS last_decision_at
        FROM decision_log
        WHERE leader_wallet = $1
          AND time > NOW() - INTERVAL '30 days'
        """,
        wallet_address,
    )

    return {
        "wallet": wallet_address,
        "header": {
            "falcon_score": _to_float(_row_get(row, "falcon_score"), 0.0),
            "phase": _to_int(_row_get(row, "error_model_phase"), 1),
            "maturity": _to_float(_row_get(row, "profile_maturity"), 0.0),
            "trades_observed": _to_int(_row_get(row, "trades_observed"), 0),
            "positions_resolved": _to_int(_row_get(row, "positions_resolved"), 0),
            "first_seen_iso": _row_get(row, "first_seen").isoformat() if _row_get(row, "first_seen") else None,
            "last_refresh_iso": _row_get(row, "last_refresh").isoformat() if _row_get(row, "last_refresh") else None,
            "last_updated_iso": _row_get(row, "last_updated").isoformat() if _row_get(row, "last_updated") else None,
            "excluded": bool(_row_get(row, "excluded")),
            "exclude_reason": _row_get(row, "exclude_reason"),
            "on_watchlist": bool(_row_get(row, "on_watchlist")),
        },
        "classification": classification,
        "preferred_categories": preferred_categories,
        "accuracy": {
            "overall": _to_float(acc_block.get("overall")),
            "resolved_count": _to_int(acc_block.get("resolved_count"), 0),
            "by_category": accuracy_by_category[:8],
        },
        "sizing": sizing_out,
        "entry_patterns": entry_out,
        "wallet360": w360_highlights,
        "edges": {
            "as_leader": _to_int(_row_get(edge_counts, "as_leader"), 0),
            "as_follower": _to_int(_row_get(edge_counts, "as_follower"), 0),
            "confirmed_followers": _to_int(_row_get(edge_counts, "confirmed_followers"), 0),
        },
        "decisions_30d": {
            "total": _to_int(_row_get(dec_summary, "total"), 0),
            "follow": _to_int(_row_get(dec_summary, "follow"), 0),
            "fade": _to_int(_row_get(dec_summary, "fade"), 0),
            "skip": _to_int(_row_get(dec_summary, "skip"), 0),
            "last_at_iso": _row_get(dec_summary, "last_decision_at").isoformat() if _row_get(dec_summary, "last_decision_at") else None,
        },
    }


# ─── DECISION DRILL-DOWN ──────────────────────────────────────────────────
async def decision_detail(conn, decision_id: int) -> dict | None:
    """Full reasoning panel for a single decision_log row."""
    try:
        decision_id = int(decision_id)
    except (TypeError, ValueError):
        return None

    row = await conn.fetchrow(
        """
        SELECT d.id, d.time, d.leader_wallet, d.market_id, d.action,
               d.thompson_follow, d.thompson_fade, d.kelly_fraction,
               d.confidence, d.reason, d.outcome, d.strategy_track,
               d.economic_model_version, d.invalidated_at, d.invalidated_reason,
               d.signal_audit,
               m.question AS market_question, m.category AS market_category,
               m.end_date AS market_end_date, m.volume_24h AS market_volume_24h,
               l.falcon_score, l.classification_json,
               lp.profile_maturity, lp.error_model_phase,
               lp.trades_observed, lp.positions_resolved
        FROM decision_log d
        LEFT JOIN markets m ON m.market_id = d.market_id
        LEFT JOIN leaders l ON l.wallet_address = d.leader_wallet
        LEFT JOIN leader_profiles lp ON lp.wallet_address = d.leader_wallet
        WHERE d.id = $1
        """,
        decision_id,
    )
    if not row:
        return None

    audit = _row_get(row, "signal_audit") or {}
    if not isinstance(audit, dict):
        try: audit = json.loads(audit)
        except Exception: audit = {}

    # Sibling decisions on the same market within ±30 min — useful context.
    # Explicit ::timestamptz cast: asyncpg's parameter binding can leave the
    # type ambiguous (treated as INTERVAL in the BETWEEN clause without it).
    siblings = await conn.fetch(
        """
        SELECT id, time, leader_wallet, action, confidence
        FROM decision_log
        WHERE market_id = $1
          AND id != $2
          AND time >= ($3::timestamptz - INTERVAL '30 minutes')
          AND time <= ($3::timestamptz + INTERVAL '30 minutes')
        ORDER BY time DESC
        LIMIT 8
        """,
        _row_get(row, "market_id"),
        decision_id,
        _row_get(row, "time"),
    )

    classification = _row_get(row, "classification_json")
    if classification and not isinstance(classification, dict):
        try: classification = json.loads(classification)
        except Exception: classification = {}

    return {
        "id": _to_int(_row_get(row, "id"), 0),
        "time_iso": _row_get(row, "time").isoformat() if _row_get(row, "time") else None,
        "action": _row_get(row, "action"),
        "outcome": _row_get(row, "outcome"),
        "leader": {
            "wallet": _row_get(row, "leader_wallet"),
            "falcon_score": _to_float(_row_get(row, "falcon_score"), 0.0),
            "phase": _to_int(_row_get(row, "error_model_phase"), 1),
            "maturity": _to_float(_row_get(row, "profile_maturity"), 0.0),
            "trades_observed": _to_int(_row_get(row, "trades_observed"), 0),
            "positions_resolved": _to_int(_row_get(row, "positions_resolved"), 0),
            "classification": classification or {},
        },
        "market": {
            "id": _row_get(row, "market_id"),
            "question": _row_get(row, "market_question") or "—",
            "category": _row_get(row, "market_category") or "unknown",
            "end_date_iso": _row_get(row, "market_end_date").isoformat() if _row_get(row, "market_end_date") else None,
            "volume_24h": _to_float(_row_get(row, "market_volume_24h"), 0.0),
        },
        "scores": {
            "thompson_follow": _to_float(_row_get(row, "thompson_follow")),
            "thompson_fade": _to_float(_row_get(row, "thompson_fade")),
            "kelly_fraction": _to_float(_row_get(row, "kelly_fraction")),
            "confidence": _to_float(_row_get(row, "confidence")),
        },
        "reason": _row_get(row, "reason") or "",
        "strategy_track": _row_get(row, "strategy_track"),
        "economic_model_version": _row_get(row, "economic_model_version"),
        "invalidated_at_iso": _row_get(row, "invalidated_at").isoformat() if _row_get(row, "invalidated_at") else None,
        "invalidated_reason": _row_get(row, "invalidated_reason"),
        "signal_audit": audit,
        "siblings": [
            {
                "id": _to_int(_row_get(s, "id"), 0),
                "time_iso": _row_get(s, "time").isoformat() if _row_get(s, "time") else None,
                "leader_wallet": _row_get(s, "leader_wallet"),
                "action": _row_get(s, "action"),
                "confidence": _to_float(_row_get(s, "confidence")),
            }
            for s in siblings
        ],
    }


# ─── RISK CONFIG AUDIT LOG ────────────────────────────────────────────────
async def risk_history(conn, limit: int = 50) -> dict:
    """Recent runtime config changes for the Risk cockpit audit panel."""
    limit = max(1, min(int(limit or 50), 500))
    rows = await conn.fetch(
        """
        SELECT id, changed_at, key, old_value, new_value, actor, source
        FROM risk_config_history
        ORDER BY changed_at DESC
        LIMIT $1
        """,
        limit,
    )
    items = [
        {
            "id": _to_int(_row_get(r, "id"), 0),
            "changed_at_iso": _row_get(r, "changed_at").isoformat() if _row_get(r, "changed_at") else None,
            "key": _row_get(r, "key"),
            "old_value": _row_get(r, "old_value"),
            "new_value": _row_get(r, "new_value"),
            "actor": _row_get(r, "actor"),
            "source": _row_get(r, "source"),
        }
        for r in rows
    ]
    by_key_row = await conn.fetchrow(
        "SELECT COUNT(*)::int AS total, COUNT(DISTINCT key)::int AS distinct_keys FROM risk_config_history"
    )
    return {
        "items": items,
        "total": _to_int(_row_get(by_key_row, "total"), 0),
        "distinct_keys": _to_int(_row_get(by_key_row, "distinct_keys"), 0),
    }


async def log_risk_change(conn, key: str, old_value, new_value, actor: str | None, source: str = "dashboard") -> None:
    """Append a single key-change row to the audit log. Best-effort: any
    exception is swallowed so a logging failure never blocks a config update."""
    try:
        await conn.execute(
            """
            INSERT INTO risk_config_history (key, old_value, new_value, actor, source)
            VALUES ($1, $2, $3, $4, $5)
            """,
            str(key),
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            actor,
            source,
        )
    except Exception as exc:
        logger.debug(f"log_risk_change failed for {key}: {exc}")


# ─── DATA QUALITY DRILL-DOWN ───────────────────────────────────────────────
async def data_quality_markets(conn, issue: str, limit: int = 100) -> dict:
    """List the markets / leaders affected by a specific data-quality issue.

    Powers the Bot Health "click an issue → see affected items" drill-down.
    Each branch mirrors a counter in `data_quality()` so the totals match.
    """
    issue = (issue or "").strip().lower()
    out: dict = {"issue": issue, "markets": [], "hint": None, "total": 0}

    if issue == "unmapped_tokens":
        # Markets with NULL/empty token_yes or token_no AND recent observed trades.
        rows = await conn.fetch(
            """
            SELECT m.market_id, m.question, m.category, m.end_date,
                   (NULLIF(m.token_yes, '') IS NOT NULL) AS has_token_yes,
                   (NULLIF(m.token_no,  '') IS NOT NULL) AS has_token_no,
                   sub.trades_7d, sub.last_seen
            FROM markets m
            JOIN (
                SELECT market_id, COUNT(*)::int AS trades_7d, MAX(time) AS last_seen
                FROM trades_observed
                WHERE time > NOW() - INTERVAL '7 days'
                GROUP BY market_id
            ) sub USING (market_id)
            WHERE (NULLIF(m.token_yes, '') IS NULL OR NULLIF(m.token_no, '') IS NULL)
              AND (m.end_date IS NULL OR m.end_date > NOW() - INTERVAL '24 hours')
              AND sub.last_seen > NOW() - INTERVAL '48 hours'
            ORDER BY sub.trades_7d DESC, sub.last_seen DESC
            LIMIT $1
            """,
            limit,
        )
        out["markets"] = [
            {
                "market_id": str(_row_get(r, "market_id")),
                "question": _row_get(r, "question") or "—",
                "category": _row_get(r, "category") or "unknown",
                "end_date_iso": _row_get(r, "end_date").isoformat() if _row_get(r, "end_date") else None,
                "has_token_yes": bool(_row_get(r, "has_token_yes")),
                "has_token_no": bool(_row_get(r, "has_token_no")),
                "trades_7d": _to_int(_row_get(r, "trades_7d"), 0),
                "last_seen_iso": _row_get(r, "last_seen").isoformat() if _row_get(r, "last_seen") else None,
            }
            for r in rows
        ]
        out["total"] = await conn.fetchval(
            """
            SELECT COUNT(*) FROM markets m
            WHERE (NULLIF(m.token_yes, '') IS NULL OR NULLIF(m.token_no, '') IS NULL)
              AND (m.end_date IS NULL OR m.end_date > NOW() - INTERVAL '24 hours')
              AND m.market_id IN (
                  SELECT DISTINCT market_id FROM trades_observed
                  WHERE time > NOW() - INTERVAL '48 hours'
              )
            """
        ) or 0
        out["hint"] = "Registry sync_markets re-tries token enrichment every 30 min. Persistent unmapped markets are usually freshly resolved (Gamma API returned single-token payload)."

    elif issue == "expired_still_active":
        rows = await conn.fetch(
            """
            SELECT m.market_id, m.question, m.category, m.end_date,
                   (NULLIF(m.token_yes, '') IS NOT NULL) AS has_token_yes,
                   (NULLIF(m.token_no,  '') IS NOT NULL) AS has_token_no,
                   COALESCE(sub.trades_7d, 0) AS trades_7d, sub.last_seen
            FROM markets m
            LEFT JOIN (
                SELECT market_id, COUNT(*)::int AS trades_7d, MAX(time) AS last_seen
                FROM trades_observed
                WHERE time > NOW() - INTERVAL '7 days'
                GROUP BY market_id
            ) sub USING (market_id)
            WHERE m.active = TRUE
              AND m.end_date IS NOT NULL
              AND m.end_date < NOW()
            ORDER BY m.end_date DESC
            LIMIT $1
            """,
            limit,
        )
        out["markets"] = [
            {
                "market_id": str(_row_get(r, "market_id")),
                "question": _row_get(r, "question") or "—",
                "category": _row_get(r, "category") or "unknown",
                "end_date_iso": _row_get(r, "end_date").isoformat() if _row_get(r, "end_date") else None,
                "has_token_yes": bool(_row_get(r, "has_token_yes")),
                "has_token_no": bool(_row_get(r, "has_token_no")),
                "trades_7d": _to_int(_row_get(r, "trades_7d"), 0),
                "last_seen_iso": _row_get(r, "last_seen").isoformat() if _row_get(r, "last_seen") else None,
            }
            for r in rows
        ]
        out["hint"] = "These markets resolved but are still flagged active=TRUE. Should be cleaned by a registry sweep."

    elif issue == "orphan_market_ids":
        rows = await conn.fetch(
            """
            SELECT t.market_id,
                   '(no metadata)' AS question,
                   'unknown' AS category,
                   NULL::timestamptz AS end_date,
                   FALSE AS has_token_yes, FALSE AS has_token_no,
                   COUNT(*)::int AS trades_7d, MAX(t.time) AS last_seen
            FROM trades_observed t
            LEFT JOIN markets m USING(market_id)
            WHERE m.market_id IS NULL
              AND t.time >= NOW() - INTERVAL '7 days'
            GROUP BY t.market_id
            ORDER BY trades_7d DESC
            LIMIT $1
            """,
            limit,
        )
        out["markets"] = [
            {
                "market_id": str(_row_get(r, "market_id")),
                "question": "(no metadata — orphan)",
                "category": "unknown",
                "end_date_iso": None,
                "has_token_yes": False,
                "has_token_no": False,
                "trades_7d": _to_int(_row_get(r, "trades_7d"), 0),
                "last_seen_iso": _row_get(r, "last_seen").isoformat() if _row_get(r, "last_seen") else None,
            }
            for r in rows
        ]
        out["hint"] = "Trades exist for these market_ids but no row in `markets`. Observer's auto-stub should normally create one — investigate `_handle_trade` failures."

    elif issue == "stale_leaders":
        rows = await conn.fetch(
            """
            SELECT wallet_address,
                   first_seen, last_refresh, falcon_score,
                   EXTRACT(EPOCH FROM (NOW() - last_refresh))::int AS age_s,
                   wallet360_json IS NOT NULL AS has_w360
            FROM leaders
            WHERE on_watchlist = TRUE AND excluded = FALSE
              AND (last_refresh IS NULL OR last_refresh < NOW() - INTERVAL '24 hours')
            ORDER BY last_refresh ASC NULLS FIRST
            LIMIT $1
            """,
            limit,
        )
        out["markets"] = [
            {
                "market_id": str(_row_get(r, "wallet_address")),  # reuse the column for "id"
                "question": f"Leader · falcon_score {_to_float(_row_get(r, 'falcon_score'), 0):.2f}",
                "category": "leader",
                "end_date_iso": _row_get(r, "last_refresh").isoformat() if _row_get(r, "last_refresh") else None,
                "has_token_yes": bool(_row_get(r, "has_w360")),
                "has_token_no": False,
                "trades_7d": _to_int(_row_get(r, "age_s"), 0) // 3600,  # display: hours since refresh
                "last_seen_iso": _row_get(r, "last_refresh").isoformat() if _row_get(r, "last_refresh") else None,
            }
            for r in rows
        ]
        out["hint"] = "These leaders haven't been re-fetched from Falcon Wallet360 in 24h+. The 'Trades 7d' column above shows hours since last refresh."

    elif issue == "stale_profiles":
        rows = await conn.fetch(
            """
            SELECT lp.wallet_address, lp.last_updated, lp.trades_observed,
                   EXTRACT(EPOCH FROM (NOW() - lp.last_updated))::int AS age_s
            FROM leader_profiles lp
            WHERE lp.last_updated IS NULL
               OR EXTRACT(EPOCH FROM (NOW() - lp.last_updated)) > 86400
            ORDER BY lp.last_updated ASC NULLS FIRST
            LIMIT $1
            """,
            limit,
        )
        out["markets"] = [
            {
                "market_id": str(_row_get(r, "wallet_address")),
                "question": f"Profile · {_to_int(_row_get(r, 'trades_observed'), 0)} trades observed",
                "category": "profile",
                "end_date_iso": _row_get(r, "last_updated").isoformat() if _row_get(r, "last_updated") else None,
                "has_token_yes": False,
                "has_token_no": False,
                "trades_7d": _to_int(_row_get(r, "age_s"), 0) // 3600,
                "last_seen_iso": _row_get(r, "last_updated").isoformat() if _row_get(r, "last_updated") else None,
            }
            for r in rows
        ]
        out["hint"] = "Leaders that stopped being processed by the profiler. Usually they stopped trading; consider excluding them from the watchlist."

    else:
        out["hint"] = f"Unknown issue key: {issue!r}"

    return out


# ─── ML DIAGNOSTICS ────────────────────────────────────────────────────────
async def ml_diagnostics(conn) -> dict:
    """High-signal indicators for tracking the ML pipeline's health.

    Composed of cheap aggregations that surface bottlenecks the existing
    KPI strip can't reveal:
      - close_methods : sell vs merge vs resolution distribution
      - sample_efficiency : trades_observed → positions_resolved ratio
      - holding_period : median seconds, by phase
      - category_coverage : % of trades with non-unknown category
      - phase_progression : days_to_p2 / p3 estimates per leader
      - decisions_24h : follow / fade / skip distribution
      - falcon_enrichment_lag : seconds from leader_first_seen → wallet360 populated
    """
    out: dict = {}

    # 1. Close-method distribution (validates merge detection per CLAUDE.md §14)
    # Resilience: each ml_diagnostics SQL query is independently wrapped so
    # one slow aggregate (statement timeout) doesn't 500 the whole endpoint.
    # Empty list = section absent from response; downstream iterators handle it.
    try:
        cm_rows = await conn.fetch(
            """
            SELECT COALESCE(close_method, 'open') AS method,
                   COUNT(*)::int AS n,
                   AVG(holding_period_s)::int AS avg_holding_s,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY holding_period_s)::int AS median_holding_s
            FROM positions_reconstructed
            WHERE open_time > NOW() - INTERVAL '30 days'
            GROUP BY 1
            ORDER BY n DESC
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.close_methods failed: {exc}")
        cm_rows = []
    total_pos = sum(_to_int(_row_get(r, "n"), 0) for r in cm_rows) or 1
    out["close_methods"] = [
        {
            "method": str(_row_get(r, "method")),
            "count": _to_int(_row_get(r, "n"), 0),
            "pct": round(_to_int(_row_get(r, "n"), 0) / total_pos, 4),
            "avg_holding_s": _to_int(_row_get(r, "avg_holding_s"), 0),
            "median_holding_s": _to_int(_row_get(r, "median_holding_s"), 0),
        }
        for r in cm_rows
    ]

    # 2. Sample efficiency: positions_resolved / trades_observed per leader,
    #    aggregated. Low ratio = lots of activity but few reconstructable cycles.
    try:
        eff_row = await conn.fetchrow(
            """
            SELECT
                SUM(trades_observed)::int   AS sum_trades,
                SUM(positions_resolved)::int AS sum_resolved,
                COUNT(*) FILTER (WHERE trades_observed > 0)::int AS active_profiles
            FROM leader_profiles
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.sample_efficiency failed: {exc}")
        eff_row = None
    sum_trades = _to_int(_row_get(eff_row, "sum_trades"), 0) or 1
    sum_resolved = _to_int(_row_get(eff_row, "sum_resolved"), 0)
    out["sample_efficiency"] = {
        "trades_observed_total": sum_trades,
        "positions_resolved_total": sum_resolved,
        "ratio": round(sum_resolved / sum_trades, 4),
        "active_profiles": _to_int(_row_get(eff_row, "active_profiles"), 0),
    }

    # 3. Holding period by phase (when did the position open).
    try:
        hp_rows = await conn.fetch(
            """
            SELECT lp.error_model_phase AS phase,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p.holding_period_s)::int AS median_s,
                   PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY p.holding_period_s)::int AS p90_s,
                   COUNT(*)::int AS n
            FROM positions_reconstructed p
            JOIN leader_profiles lp ON lp.wallet_address = p.wallet_address
            WHERE p.holding_period_s IS NOT NULL AND p.holding_period_s > 0
              AND p.open_time > NOW() - INTERVAL '30 days'
            GROUP BY 1
            ORDER BY 1
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.holding_by_phase failed: {exc}")
        hp_rows = []
    out["holding_by_phase"] = [
        {
            "phase": _to_int(_row_get(r, "phase"), 1),
            "median_s": _to_int(_row_get(r, "median_s"), 0),
            "p90_s": _to_int(_row_get(r, "p90_s"), 0),
            "count": _to_int(_row_get(r, "n"), 0),
        }
        for r in hp_rows
    ]

    # 4. Category coverage trend — % of trades with a non-unknown category,
    #    bucketed per day for the last 14 days.
    # NOTE: this scans 14 days of trades_observed (60M+ rows). Statement
    # timeout is expected on cold partitions; wrap so a slow scan doesn't
    # nuke the entire ML diagnostics endpoint. Card "CATEGORY COVERAGE · 14D"
    # will render empty when this section fails.
    try:
        cov_rows = await conn.fetch(
            """
            SELECT DATE_TRUNC('day', time)::date AS day,
                   COUNT(*)::int AS total,
                   COUNT(*) FILTER (
                       WHERE category IS NOT NULL
                         AND category NOT IN ('', 'unknown', 'none', 'null')
                   )::int AS known
            FROM trades_observed
            WHERE time > NOW() - INTERVAL '14 days'
            GROUP BY 1
            ORDER BY 1
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.category_coverage failed: {exc}")
        cov_rows = []
    out["category_coverage"] = [
        {
            "day": _row_get(r, "day").isoformat() if _row_get(r, "day") else None,
            "total": _to_int(_row_get(r, "total"), 0),
            "known": _to_int(_row_get(r, "known"), 0),
            "pct": round(
                _to_int(_row_get(r, "known"), 0) / _to_int(_row_get(r, "total"), 0),
                4,
            ) if _to_int(_row_get(r, "total"), 0) else 0.0,
        }
        for r in cov_rows
    ]

    # 5. Decision flow last 24h: follow / fade / skip
    # Batch 2 fix #4: accept BOTH legacy lowercase action values
    # ('follow','fade','skip','volume_anticipation') and the canonical
    # upper-case set ('OPEN','CLOSE','REDUCE','SKIP') so the counter
    # converges with reconciliation regardless of which producer wrote
    # the row. Without this the dashboard's "DECISIONS 24H" card
    # showed 0 when the engine was emitting only legacy actions.
    try:
        dec_rows = await conn.fetch(
            """
            SELECT LOWER(action) AS action, COUNT(*)::int AS n
            FROM decision_log
            WHERE time > NOW() - INTERVAL '24 hours'
              AND action IS NOT NULL
            GROUP BY LOWER(action)
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.decisions_24h failed: {exc}")
        dec_rows = []
    total_dec_raw = sum(_to_int(_row_get(r, "n"), 0) for r in dec_rows)
    total_dec = total_dec_raw or 1  # guard against /0 in pct calc
    out["decisions_24h"] = {
        "total": total_dec_raw,
        "by_action": [
            {
                "action": str(_row_get(r, "action")),
                "count": _to_int(_row_get(r, "n"), 0),
                "pct": round(_to_int(_row_get(r, "n"), 0) / total_dec, 4),
            }
            for r in dec_rows
        ],
    }

    # 6. Falcon enrichment lag — for leaders who got a wallet360, how long
    #    did it take from first_seen to last_refresh.
    try:
        lag_row = await conn.fetchrow(
            """
            SELECT
                PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (last_refresh - first_seen))
                )::int AS median_s,
                PERCENTILE_CONT(0.9) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (last_refresh - first_seen))
                )::int AS p90_s,
                COUNT(*) FILTER (WHERE wallet360_json IS NOT NULL)::int AS enriched,
                COUNT(*) FILTER (
                    WHERE wallet360_json IS NULL AND excluded = FALSE
                )::int AS pending
            FROM leaders
            WHERE last_refresh IS NOT NULL AND first_seen IS NOT NULL
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.falcon_enrichment_lag failed: {exc}")
        lag_row = None
    out["falcon_enrichment_lag"] = {
        "median_s": _to_int(_row_get(lag_row, "median_s"), 0),
        "p90_s": _to_int(_row_get(lag_row, "p90_s"), 0),
        "enriched": _to_int(_row_get(lag_row, "enriched"), 0),
        "pending": _to_int(_row_get(lag_row, "pending"), 0),
    }

    # 7. Phase progression ETA — how many days to reach P2 / P3 at current
    #    velocity. Estimated as (target - current) / (current_per_day).
    try:
        phase_rows = await conn.fetch(
            """
            WITH velocity AS (
                SELECT lp.wallet_address,
                       lp.error_model_phase,
                       lp.positions_resolved,
                       COUNT(p.id) FILTER (
                           WHERE p.close_time > NOW() - INTERVAL '7 days'
                       )::float AS resolved_7d
                FROM leader_profiles lp
                LEFT JOIN positions_reconstructed p USING (wallet_address)
                WHERE lp.error_model_phase < 3
                GROUP BY lp.wallet_address, lp.error_model_phase, lp.positions_resolved
            )
            SELECT wallet_address,
                   error_model_phase,
                   positions_resolved,
                   resolved_7d
            FROM velocity
            ORDER BY resolved_7d DESC NULLS LAST
            LIMIT 6
            """
        )
    except Exception as exc:
        logger.warning(f"ml_diagnostics.phase_eta failed: {exc}")
        phase_rows = []
    eta_list = []
    for r in phase_rows:
        phase = _to_int(_row_get(r, "error_model_phase"), 1)
        resolved = _to_int(_row_get(r, "positions_resolved"), 0)
        per_day = _to_float(_row_get(r, "resolved_7d"), 0.0) / 7.0
        target = 100 if phase == 1 else 500 if phase == 2 else None
        eta_days = (target - resolved) / per_day if (target and per_day > 0) else None
        eta_list.append({
            "wallet": str(_row_get(r, "wallet_address")),
            "label": str(_row_get(r, "wallet_address"))[:6] + "…" + str(_row_get(r, "wallet_address"))[-4:],
            "current_phase": phase,
            "resolved": resolved,
            "target": target,
            "resolved_per_day": round(per_day, 2),
            "eta_days": round(eta_days, 1) if eta_days is not None else None,
        })
    out["phase_eta_top"] = eta_list

    return out


# ─── WALLET DRILLDOWN ──────────────────────────────────────────────────────
async def wallet_markets(conn, wallet_address: str, window_days: int = 30, limit: int = 20) -> dict:
    """Per-wallet market drilldown for the Wallet Graph inspector.

    Aggregates trades_observed by market over the window, joining to markets
    for the human-readable question and to positions_reconstructed for PnL.
    Returns the top `limit` markets ordered by trade count, plus a category
    breakdown summary.
    """
    # Validate the address shape: cheap defense against SQL/path traversal.
    # Polymarket proxy wallets are 0x + 40 hex chars; anything else gets a
    # safe empty result rather than an error.
    if not isinstance(wallet_address, str) or not wallet_address.startswith("0x") or len(wallet_address) != 42:
        return {"wallet": wallet_address, "window_days": window_days, "markets": [], "category_breakdown": [], "total_trades": 0, "distinct_markets": 0}
    window_days = max(1, min(int(window_days or 30), 365))
    limit = max(1, min(int(limit or 20), 100))

    market_rows = await conn.fetch(
        f"""
        WITH trades_in_window AS (
            SELECT t.market_id,
                   COUNT(*)::int AS n_trades,
                   COUNT(*) FILTER (WHERE UPPER(t.side) = 'BUY')::int AS n_buys,
                   COUNT(*) FILTER (WHERE UPPER(t.side) = 'SELL')::int AS n_sells,
                   COALESCE(SUM(t.size_usdc), 0) AS volume_usdc,
                   MIN(t.time) AS first_seen,
                   MAX(t.time) AS last_seen,
                   COALESCE(NULLIF(MAX(t.category), ''), 'unknown') AS category
            FROM trades_observed t
            WHERE t.wallet_address = $1
              AND t.time >= NOW() - INTERVAL '{window_days} days'
            GROUP BY t.market_id
        ),
        pnl_in_window AS (
            SELECT p.market_id,
                   COALESCE(SUM(p.pnl_usdc), 0) AS pnl_usdc,
                   COUNT(*) FILTER (WHERE p.pnl_usdc IS NOT NULL)::int AS resolved
            FROM positions_reconstructed p
            WHERE p.wallet_address = $1
              AND p.open_time >= NOW() - INTERVAL '{window_days} days'
            GROUP BY p.market_id
        )
        SELECT tw.market_id,
               COALESCE(m.question, 'Market ' || LEFT(tw.market_id, 10) || '…') AS question,
               COALESCE(NULLIF(m.category, ''), tw.category) AS category,
               m.end_date,
               m.active,
               tw.n_trades, tw.n_buys, tw.n_sells, tw.volume_usdc,
               tw.first_seen, tw.last_seen,
               COALESCE(pw.pnl_usdc, 0) AS pnl_usdc,
               COALESCE(pw.resolved, 0) AS resolved_positions
        FROM trades_in_window tw
        LEFT JOIN markets m ON m.market_id = tw.market_id
        LEFT JOIN pnl_in_window pw ON pw.market_id = tw.market_id
        ORDER BY tw.n_trades DESC, tw.last_seen DESC
        LIMIT $2
        """,
        wallet_address, limit,
    )

    # Aggregate breakdown over ALL trades in the window (not just top N).
    breakdown_rows = await conn.fetch(
        f"""
        SELECT COALESCE(NULLIF(category, ''), 'unknown') AS category,
               COUNT(*)::int AS n,
               COALESCE(SUM(size_usdc), 0) AS volume_usdc
        FROM trades_observed
        WHERE wallet_address = $1
          AND time >= NOW() - INTERVAL '{window_days} days'
        GROUP BY COALESCE(NULLIF(category, ''), 'unknown')
        ORDER BY n DESC
        """,
        wallet_address,
    )
    total_trades = sum(_to_int(_row_get(r, "n"), 0) for r in breakdown_rows)
    category_breakdown = [
        {
            "category": str(_row_get(r, "category") or "unknown"),
            "trades": _to_int(_row_get(r, "n"), 0),
            "volume_usdc": _to_float(_row_get(r, "volume_usdc"), 0.0),
            "pct": round(_to_int(_row_get(r, "n"), 0) / total_trades, 4) if total_trades else 0.0,
        }
        for r in breakdown_rows
    ]
    markets = [
        {
            "market_id": str(_row_get(r, "market_id")),
            "question": str(_row_get(r, "question") or ""),
            "category": str(_row_get(r, "category") or "unknown"),
            "end_date_iso": _row_get(r, "end_date").isoformat() if _row_get(r, "end_date") else None,
            "active": bool(_row_get(r, "active")) if _row_get(r, "active") is not None else None,
            "n_trades": _to_int(_row_get(r, "n_trades"), 0),
            "n_buys": _to_int(_row_get(r, "n_buys"), 0),
            "n_sells": _to_int(_row_get(r, "n_sells"), 0),
            "volume_usdc": _to_float(_row_get(r, "volume_usdc"), 0.0),
            "first_seen_iso": _row_get(r, "first_seen").isoformat() if _row_get(r, "first_seen") else None,
            "last_seen_iso": _row_get(r, "last_seen").isoformat() if _row_get(r, "last_seen") else None,
            "pnl_usdc": _to_float(_row_get(r, "pnl_usdc"), 0.0),
            "resolved_positions": _to_int(_row_get(r, "resolved_positions"), 0),
        }
        for r in market_rows
    ]
    distinct_markets = await conn.fetchval(
        f"SELECT COUNT(DISTINCT market_id) FROM trades_observed WHERE wallet_address = $1 AND time >= NOW() - INTERVAL '{window_days} days'",
        wallet_address,
    )
    return {
        "wallet": wallet_address,
        "window_days": window_days,
        "markets": markets,
        "category_breakdown": category_breakdown,
        "total_trades": total_trades,
        "distinct_markets": _to_int(distinct_markets, 0),
    }


# ─── LIVE PORTFOLIO DASHBOARD (May 17, 2026) ────────────────────────────────
# Backend for the redesigned terminal-style Live Portfolio view (see
# docs/autonomous_session_2026_05_17_strategy/03_UI_REDESIGN_PROFESSIONAL.md).
#
# Five builders, each shaped for one frontend panel:
#   portfolio_timeseries  → OHLCV-style equity buckets (TradingView lightweight-charts)
#   portfolio_trades      → trade list (color-coded by status/PnL, joined with markets)
#   portfolio_allocation  → current capital allocation (horizontal stacked bar)
#   portfolio_kpis        → top tile row (single payload, polled every 5s)
#   portfolio_pipeline_status → bot/WS/ingestion health bar
#
# Performance budget: each builder must return JSON in <500ms at current
# scale (≤ 30 open paper_trades, ~10k portfolio_equity samples / 7 days,
# ~580k trades_observed rows partitioned by day). Latency benchmarks from
# prod EXPLAIN ANALYZE are annotated per-function.


# Allowed bucket sizes (frontend timeframe buttons → PostgreSQL date_trunc unit
# or a numeric multiplier for the sub-hour aggregations not natively supported
# by date_trunc). We restrict to a whitelist to prevent SQL injection via the
# `timeframe` query parameter.
_TIMEFRAME_TO_BUCKET_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}

# Default lookback range per timeframe, chosen to (a) give the chart ~50–200
# bars on first paint and (b) stay under 500ms by keeping rows below ~10k.
# Override via `from=` / `to=` query params. Note: the 1h/7d cell warms to
# ~400ms on prod (cold 1.2s, see EXPLAIN log in this commit).
_TIMEFRAME_DEFAULT_LOOKBACK_HOURS = {
    "1m": 6,
    "5m": 24,
    "15m": 48,
    "1h": 168,    # 7d
    "4h": 336,    # 14d
    "1d": 1440,   # 60d
    "1w": 8760,   # 365d
}


def _normalize_timeframe(timeframe: str | None) -> str:
    """Return a whitelisted timeframe label or 'NoneType' raises ValueError."""
    tf = (timeframe or "1h").strip().lower()
    if tf not in _TIMEFRAME_TO_BUCKET_SECONDS:
        raise ValueError(
            f"invalid timeframe '{tf}'; "
            f"must be one of {sorted(_TIMEFRAME_TO_BUCKET_SECONDS)}"
        )
    return tf


async def portfolio_timeseries(
    conn,
    timeframe: str = "1h",
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> dict:
    """OHLCV-style equity buckets for a given timeframe.

    Reads `portfolio_equity` (snapshotted by the paper_trader mark-to-market
    loop), bucketed via floor(epoch / N) so that any whitelisted timeframe
    (including 5m/15m/4h/1w which are not native date_trunc units) gets a
    correct grid.

    For each bucket we emit:
      - time:   ISO timestamp (bucket start)
      - open:   first `equity` sample (earliest time in bucket)
      - high:   max `equity`
      - low:    min `equity`
      - close:  last `equity` sample
      - pnl_realized: sum of `pnl_usdc` from paper_trades that CLOSED in the bucket

    PERFORMANCE (prod EXPLAIN ANALYZE 2026-05-17, portfolio_equity = 10k rows):
      1m / 6h    : p50 ~ 50 ms (index scan)
      5m / 24h   : p50 ~ 150 ms (index scan)
      15m / 2d   : p50 ~ 250 ms (index scan)
      1h / 7d    : p50 ~ 400 ms (seq scan, all rows fit in cache after first hit)
      1d / 60d   : p50 ~ 500 ms (seq scan)  ← borderline; rebuild PE keeps warm
      1w / 365d  : p50 ~ 500 ms (seq scan)  ← table still tiny (~700 kB)
    """
    tf = _normalize_timeframe(timeframe)
    bucket_seconds = _TIMEFRAME_TO_BUCKET_SECONDS[tf]
    # Default window: timeframe-specific lookback (see annotation above).
    if to_ts is None:
        to_ts = datetime.now(timezone.utc)
    if from_ts is None:
        lookback_hours = _TIMEFRAME_DEFAULT_LOOKBACK_HOURS[tf]
        from datetime import timedelta
        from_ts = to_ts - timedelta(hours=lookback_hours)

    # Bucket via floor(epoch / N) → epoch * N to_timestamp() — works for
    # ANY whitelisted timeframe including 5m / 15m / 4h / 1w which
    # date_trunc cannot do natively. Index on time stays usable thanks to
    # the WHERE clause (PG plans index scan when range is small enough).
    bar_rows = await conn.fetch(
        """
        SELECT
            to_timestamp(
                floor(EXTRACT(EPOCH FROM time) / $3::numeric) * $3::numeric
            )                                            AS bucket,
            MIN(equity)::float                           AS low,
            MAX(equity)::float                           AS high,
            (array_agg(equity ORDER BY time ASC))[1]     AS open,
            (array_agg(equity ORDER BY time DESC))[1]    AS close,
            COUNT(*)::int                                AS n_samples
        FROM portfolio_equity
        WHERE time >= $1 AND time < $2
        GROUP BY 1
        ORDER BY 1 ASC
        """,
        from_ts, to_ts, int(bucket_seconds),
    )

    # Realized PnL per bucket — closed paper_trades grouped by the same
    # floor() pattern. Joined client-side rather than via SQL JOIN to avoid
    # forcing a CROSS JOIN on portfolio_equity × paper_trades (which would
    # blow up the planner cost on hot-path 5s polling).
    pnl_rows = await conn.fetch(
        """
        SELECT
            to_timestamp(
                floor(EXTRACT(EPOCH FROM closed_at) / $3::numeric) * $3::numeric
            )                                AS bucket,
            COALESCE(SUM(pnl_usdc), 0)::float AS pnl_realized,
            COUNT(*)::int                     AS trades_closed
        FROM paper_trades
        WHERE status = 'closed'
          AND closed_at IS NOT NULL
          AND closed_at >= $1
          AND closed_at < $2
        GROUP BY 1
        ORDER BY 1 ASC
        """,
        from_ts, to_ts, int(bucket_seconds),
    )
    pnl_by_bucket: dict = {}
    for r in pnl_rows:
        ts = _row_get(r, "bucket")
        if ts is None:
            continue
        pnl_by_bucket[ts] = {
            "pnl_realized": _to_float(_row_get(r, "pnl_realized"), 0.0),
            "trades_closed": _to_int(_row_get(r, "trades_closed"), 0),
        }

    bars: list[dict] = []
    for r in bar_rows:
        ts = _row_get(r, "bucket")
        if ts is None:
            continue
        pnl_entry = pnl_by_bucket.get(ts) or {"pnl_realized": 0.0, "trades_closed": 0}
        bars.append({
            "time": ts.isoformat() if ts else None,
            "open": _to_float(_row_get(r, "open"), 0.0),
            "high": _to_float(_row_get(r, "high"), 0.0),
            "low": _to_float(_row_get(r, "low"), 0.0),
            "close": _to_float(_row_get(r, "close"), 0.0),
            "pnl_realized": pnl_entry["pnl_realized"],
            "trades_closed": pnl_entry["trades_closed"],
            "n_samples": _to_int(_row_get(r, "n_samples"), 0),
        })

    return {
        "timeframe": tf,
        "bucket_seconds": bucket_seconds,
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "bars": bars,
    }


async def portfolio_trades(
    conn,
    redis_client=None,
    limit: int = 50,
    order: str = "closed_desc",
    status: str = "all",
) -> list[dict]:
    """Recent paper_trades joined with `markets` for the trade list panel.

    Parameters are whitelisted:
      - limit  : 1..500 (caller enforces; SQL caps at 500 defensively)
      - order  : closed_desc | closed_asc | opened_desc | opened_asc | pnl_desc | pnl_asc
      - status : closed | open | all

    For each trade, attaches the live best_bid / best_ask from the Redis
    `book:last:{market_id}:{token_id}` cache (populated by the trade
    observer maintenance loop). Falls back to null on missing keys —
    operator can still see the entry/exit prices.

    PERFORMANCE (prod EXPLAIN ANALYZE 2026-05-17):
      limit=50, status='closed' : 68 ms (LEFT JOIN markets via PK + sort)
      limit=50, status='open'   : 25 ms (partial index `idx_paper_open`)
      limit=200                 : 95 ms
    """
    status_clause = ""
    if status == "open":
        status_clause = "AND pt.status = 'open'"
    elif status == "closed":
        status_clause = "AND pt.status = 'closed'"
    # status == "all" → no filter

    order_map = {
        "closed_desc":   "ORDER BY pt.closed_at DESC NULLS LAST",
        "closed_asc":    "ORDER BY pt.closed_at ASC NULLS LAST",
        "opened_desc":   "ORDER BY pt.opened_at DESC",
        "opened_asc":    "ORDER BY pt.opened_at ASC",
        "pnl_desc":      "ORDER BY pt.pnl_usdc DESC NULLS LAST",
        "pnl_asc":       "ORDER BY pt.pnl_usdc ASC NULLS LAST",
    }
    order_clause = order_map.get(order, order_map["closed_desc"])

    # SQL whitelist enforcement: status_clause and order_clause are NEVER
    # built from user input directly. They come from a fixed dict lookup.
    safe_limit = max(1, min(int(limit or 50), 500))

    rows = await conn.fetch(
        f"""
        SELECT
            pt.id,
            pt.strategy,
            pt.leader_wallet,
            pt.market_id,
            pt.token_id,
            pt.direction,
            pt.entry_price,
            pt.exit_price,
            pt.size_usdc,
            pt.opened_at,
            pt.closed_at,
            pt.close_reason,
            pt.pnl_usdc,
            pt.status,
            pt.confidence,
            pt.fee_paid_usdc,
            EXTRACT(EPOCH FROM (COALESCE(pt.closed_at, NOW()) - pt.opened_at))::int
                AS holding_period_s,
            m.question,
            m.category
        FROM paper_trades pt
        LEFT JOIN markets m USING (market_id)
        WHERE {V1_PAPER_TRADE_PT_SQL}
          {status_clause}
        {order_clause}
        LIMIT {safe_limit}
        """
    )

    result: list[dict] = []
    for r in rows:
        market_id = _row_get(r, "market_id")
        token_id = _row_get(r, "token_id")
        question = _row_get(r, "question")
        # Truncate question to keep payload light.
        if question and len(question) > 80:
            question_short = question[:77] + "…"
        else:
            question_short = question or f"Market {str(market_id)[:30]}…"

        # Live book read — best-effort, null on cache miss / parse failure.
        best_bid: float | None = None
        best_ask: float | None = None
        if redis_client is not None and market_id and token_id:
            try:
                raw = await redis_client.get(f"book:last:{market_id}:{token_id}")
                if raw is not None:
                    book = json.loads(raw)
                    if book.get("best_bid") is not None:
                        best_bid = float(book["best_bid"])
                    if book.get("best_ask") is not None:
                        best_ask = float(book["best_ask"])
            except Exception:
                pass

        entry_price = _to_float(_row_get(r, "entry_price"), 0.0)
        exit_price = _row_get(r, "exit_price")
        size_usdc = _to_float(_row_get(r, "size_usdc"), 0.0)
        pnl_usdc = _row_get(r, "pnl_usdc")
        pnl_pct = None
        if pnl_usdc is not None and size_usdc > 0:
            pnl_pct = round(float(pnl_usdc) / size_usdc, 4)

        leader = _row_get(r, "leader_wallet")
        leader_short = (
            f"{leader[:6]}…{leader[-4:]}"
            if leader and len(leader) >= 12
            else leader
        )

        result.append({
            "id": _to_int(_row_get(r, "id"), 0),
            "strategy": _row_get(r, "strategy") or "—",
            "leader_wallet": leader,
            "leader_short": leader_short,
            "market_id": market_id,
            "token_id": token_id,
            "market_question": question_short,
            "category": _row_get(r, "category") or "unknown",
            "direction": _row_get(r, "direction"),
            "entry_price": entry_price,
            "exit_price": _to_float(exit_price) if exit_price is not None else None,
            "size_usdc": size_usdc,
            "opened_at": _row_get(r, "opened_at").isoformat()
                if _row_get(r, "opened_at") else None,
            "closed_at": _row_get(r, "closed_at").isoformat()
                if _row_get(r, "closed_at") else None,
            "holding_period_s": _to_int(_row_get(r, "holding_period_s"), 0),
            "close_reason": _row_get(r, "close_reason"),
            "status": _row_get(r, "status"),
            "pnl_usdc": _to_float(pnl_usdc) if pnl_usdc is not None else None,
            "pnl_pct": pnl_pct,
            "confidence": _to_float(_row_get(r, "confidence"), 0.0),
            "fee_paid_usdc": _to_float(_row_get(r, "fee_paid_usdc"), 0.0),
            "bid": best_bid,
            "ask": best_ask,
        })
    return result


async def portfolio_allocation(
    conn,
    as_of: datetime | None = None,
) -> dict:
    """Current capital allocation, grouped by category / leader / strategy.

    Sums `size_usdc` over open paper_trades. The `as_of` parameter is
    accepted for API symmetry but currently ignored — open positions are
    by definition "as of now" (we do not snapshot historical open-state).
    Future: query positions_reconstructed for a point-in-time allocation.

    PERFORMANCE (prod EXPLAIN ANALYZE 2026-05-17, 27 open trades):
      total : <5 ms (3 small aggregates on partial index `idx_paper_open`)
    """
    # as_of unused for now; kept in signature for forward-compat
    _ = as_of

    # Totals + by-category in one query (LEFT JOIN markets to get category).
    by_category_rows = await conn.fetch(
        """
        SELECT
            COALESCE(m.category, 'unknown') AS label,
            COUNT(*)::int                   AS count,
            COALESCE(SUM(pt.size_usdc), 0)::float AS capital_usdc
        FROM paper_trades pt
        LEFT JOIN markets m USING (market_id)
        WHERE pt.status = 'open'
        GROUP BY 1
        ORDER BY 3 DESC
        """
    )

    # By leader (top 5 + "other" aggregation)
    by_leader_rows = await conn.fetch(
        """
        SELECT
            COALESCE(leader_wallet, 'unknown') AS label,
            COUNT(*)::int                      AS count,
            COALESCE(SUM(size_usdc), 0)::float AS capital_usdc
        FROM paper_trades
        WHERE status = 'open'
        GROUP BY 1
        ORDER BY 3 DESC
        """
    )

    # By strategy (follow / fade)
    by_strategy_rows = await conn.fetch(
        """
        SELECT
            strategy                            AS label,
            COUNT(*)::int                       AS count,
            COALESCE(SUM(size_usdc), 0)::float  AS capital_usdc
        FROM paper_trades
        WHERE status = 'open'
        GROUP BY 1
        ORDER BY 3 DESC
        """
    )

    # Total capital from portfolio_state (snapshot) — the bot's current bankroll.
    portfolio = await _fetch_portfolio_snapshot(conn)
    total_capital = portfolio["capital"]
    total_open_capital = sum(
        _to_float(_row_get(r, "capital_usdc"), 0.0) for r in by_category_rows
    )

    def _pct(value: float) -> float:
        return round(value / total_capital, 4) if total_capital > 0 else 0.0

    by_category = [
        {
            "label": _row_get(r, "label") or "unknown",
            "count": _to_int(_row_get(r, "count"), 0),
            "capital_usdc": _to_float(_row_get(r, "capital_usdc"), 0.0),
            "pct_of_total": _pct(_to_float(_row_get(r, "capital_usdc"), 0.0)),
        }
        for r in by_category_rows
    ]

    # Bucket leaders beyond top-5 into "other".
    by_leader: list[dict] = []
    top_leaders = list(by_leader_rows[:5])
    other_leaders = list(by_leader_rows[5:])
    for r in top_leaders:
        leader = _row_get(r, "label") or "unknown"
        cap = _to_float(_row_get(r, "capital_usdc"), 0.0)
        leader_short = (
            f"{leader[:6]}…{leader[-4:]}"
            if leader and len(leader) >= 12
            else leader
        )
        by_leader.append({
            "label": leader_short,
            "wallet": leader,
            "count": _to_int(_row_get(r, "count"), 0),
            "capital_usdc": cap,
            "pct_of_total": _pct(cap),
        })
    if other_leaders:
        other_cap = sum(
            _to_float(_row_get(r, "capital_usdc"), 0.0) for r in other_leaders
        )
        other_n = sum(_to_int(_row_get(r, "count"), 0) for r in other_leaders)
        by_leader.append({
            "label": "other",
            "wallet": None,
            "count": other_n,
            "capital_usdc": other_cap,
            "pct_of_total": _pct(other_cap),
        })

    by_strategy = [
        {
            "label": _row_get(r, "label") or "unknown",
            "count": _to_int(_row_get(r, "count"), 0),
            "capital_usdc": _to_float(_row_get(r, "capital_usdc"), 0.0),
            "pct_of_total": _pct(_to_float(_row_get(r, "capital_usdc"), 0.0)),
        }
        for r in by_strategy_rows
    ]

    return {
        "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
        "total_capital": total_capital,
        "total_open_capital": round(total_open_capital, 2),
        "open_pct_of_total": _pct(total_open_capital),
        "by_category": by_category,
        "by_leader": by_leader,
        "by_strategy": by_strategy,
    }


def _compute_win_streak(pnls: list[float]) -> tuple[int, int]:
    """Given a list of pnl values ordered MOST RECENT FIRST, return
    (current_streak, best_streak). A streak is a run of consecutive
    positive-PnL trades. Negative or zero PnL breaks the streak.
    """
    current = 0
    best = 0
    run = 0
    # Reverse to iterate oldest → newest so we can track running and best.
    for pnl in reversed(pnls):
        if pnl is None:
            continue
        if pnl > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    # The current streak is the suffix of consecutive positives at the
    # head of the original list (newest end).
    for pnl in pnls:
        if pnl is None:
            continue
        if pnl > 0:
            current += 1
        else:
            break
    return current, best


async def portfolio_kpis(conn, redis_client=None) -> dict:
    """Top-line KPIs for the dashboard tile row.

    Designed for a 5-second poll → < 100ms p50. All counters come from
    `paper_trades` and `portfolio_state`, both of which are tiny (paper_trades
    fits entirely in the buffer cache; portfolio_state is a single row).

    PERFORMANCE (prod EXPLAIN ANALYZE 2026-05-17):
      aggregate query             : 94 ms (FILTER on 27 rows seq scan)
      portfolio_state fetchrow    : <1 ms (PK lookup)
      30-day pnl pull for streak  : <1 ms (filter on closed_at + status)
      latency probe (Redis)       : ~1 ms
      Total p50                   : ~100 ms (well under budget)
    """
    # One aggregate read for almost everything daily / weekly / 30d.
    agg = await conn.fetchrow(
        f"""
        SELECT
            COUNT(*) FILTER (WHERE status='open')::int AS open_count,
            COALESCE(SUM(size_usdc) FILTER (WHERE status='open'), 0)::float
                AS open_capital,
            COALESCE(SUM(pnl_usdc) FILTER (
                WHERE status='closed' AND closed_at >= date_trunc('day', NOW())
            ), 0)::float                                                AS daily_pnl,
            COUNT(*) FILTER (
                WHERE status='closed' AND closed_at >= date_trunc('day', NOW())
                  AND pnl_usdc > 0
            )::int                                                       AS daily_wins,
            COUNT(*) FILTER (
                WHERE status='closed' AND closed_at >= date_trunc('day', NOW())
                  AND pnl_usdc <= 0
            )::int                                                       AS daily_losses,
            COALESCE(SUM(pnl_usdc) FILTER (
                WHERE status='closed' AND closed_at >= NOW() - INTERVAL '7 days'
            ), 0)::float                                                AS weekly_pnl,
            COUNT(*) FILTER (
                WHERE status='closed' AND closed_at >= NOW() - INTERVAL '30 days'
            )::int                                                       AS m_total,
            COUNT(*) FILTER (
                WHERE status='closed' AND closed_at >= NOW() - INTERVAL '30 days'
                  AND pnl_usdc > 0
            )::int                                                       AS m_wins
        FROM paper_trades
        WHERE {V1_PAPER_TRADE_SQL}
        """
    )

    portfolio = await _fetch_portfolio_snapshot(conn)

    # Win streak — small pull of last 50 closed trades for streak calc.
    streak_rows = await conn.fetch(
        f"""
        SELECT pnl_usdc::float AS pnl
        FROM paper_trades
        WHERE status='closed' AND {V1_PAPER_TRADE_SQL}
        ORDER BY closed_at DESC
        LIMIT 100
        """
    )
    streak_pnls = [_to_float(_row_get(r, "pnl"), 0.0) for r in streak_rows]
    win_streak_current, win_streak_best = _compute_win_streak(streak_pnls)

    # Latency p50 — read from Redis if present. Set null if not measured.
    latency_p50_ms: float | None = None
    if redis_client is not None:
        try:
            raw = await redis_client.get("metrics:decision_latency_p50_ms")
            if raw is not None:
                latency_p50_ms = float(raw)
        except Exception:
            latency_p50_ms = None

    m_total = _to_int(_row_get(agg, "m_total"), 0) if agg else 0
    m_wins = _to_int(_row_get(agg, "m_wins"), 0) if agg else 0
    win_rate_30d = round(m_wins / m_total, 4) if m_total > 0 else 0.0

    return {
        "capital": portfolio["capital"],
        "peak_capital": portfolio["peak_capital"],
        "drawdown_pct": portfolio["drawdown_pct"],
        "daily_pnl": _to_float(_row_get(agg, "daily_pnl"), 0.0) if agg else 0.0,
        "daily_win_count": _to_int(_row_get(agg, "daily_wins"), 0) if agg else 0,
        "daily_loss_count": _to_int(_row_get(agg, "daily_losses"), 0) if agg else 0,
        "weekly_pnl": _to_float(_row_get(agg, "weekly_pnl"), 0.0) if agg else 0.0,
        "win_rate_30d": win_rate_30d,
        "win_streak_current": int(win_streak_current),
        "win_streak_best": int(win_streak_best),
        "latency_p50_ms": latency_p50_ms,
        "open_positions_count": _to_int(_row_get(agg, "open_count"), 0) if agg else 0,
        "open_capital_usdc": _to_float(_row_get(agg, "open_capital"), 0.0) if agg else 0.0,
    }


async def portfolio_pipeline_status(conn, redis_client=None) -> dict:
    """Bot pipeline health bar — single payload for the header strip.

    Composes 6 inputs:
      - Redis ping (redis_ok)
      - Postgres ping (db_ok, implicit via successful conn read)
      - WS last-message timestamp (`ws:market:last_message_ts` in Redis)
      - trades_observed MAX(time) for ingestion freshness
      - paper_trades MAX(opened_at) for engine activity
      - killswitch state + settings.PAPER_TRADING for exec_mode

    Graceful degradation: if Redis is unreachable, returns degraded but
    DOES NOT raise — the caller can still see the DB-derived signals.

    PERFORMANCE (prod EXPLAIN ANALYZE 2026-05-17):
      MAX(time) trades_observed  : ~5 ms (uses idx on time DESC)
      MAX(opened_at) paper_trades: ~1 ms
      Redis ping + GET           : ~2 ms
      Killswitch get_state       : <1 ms (Redis cache hit, 2s TTL)
      Total                      : ~10 ms (well under budget)
    """
    # DB side — wrap each in try/except so a single failure does not
    # crash the response; the operator still gets the other signals.
    db_ok = True
    last_trade_at: datetime | None = None
    last_decision_at: datetime | None = None
    ingestion_lag_s: float | None = None
    ingestion_count_24h: int | None = None
    try:
        # WHERE prunes partitions (trades_observed is partitioned by time).
        # Without it, postgres scans every partition for MAX().
        last_obs_row = await conn.fetchrow(
            "SELECT MAX(time) AS t FROM trades_observed WHERE time >= NOW() - INTERVAL '2 days'"
        )
        if last_obs_row and last_obs_row["t"] is not None:
            last_t = last_obs_row["t"]
            ingestion_lag_s = (
                datetime.now(timezone.utc) - last_t
            ).total_seconds()
        # 24h count of observed trades — pulls from the per-day partitions.
        last_decision_at = await conn.fetchval(
            f"""
            SELECT MAX(opened_at) FROM paper_trades
            WHERE {V1_PAPER_TRADE_SQL}
            """
        )
        # paper_trades MAX(closed_at)  → last trade
        last_trade_at = await conn.fetchval(
            f"""
            SELECT MAX(COALESCE(closed_at, opened_at)) FROM paper_trades
            WHERE {V1_PAPER_TRADE_SQL}
            """
        )
        # NOTE: 2026-05-17 — was a COUNT(*) over 24h which scans every
        # partition (~9s on prod, blocked pipeline_status endpoint).
        # Replaced by a Redis-cached counter maintained by the observer.
        ingestion_count_24h = None
        if redis_client is not None:
            try:
                v = await asyncio.wait_for(
                    redis_client.get("metrics:trades_observed_24h"),
                    timeout=0.3,
                )
                if v is not None:
                    ingestion_count_24h = int(v)
            except Exception:
                ingestion_count_24h = None
    except Exception as exc:
        logger.warning(f"pipeline_status DB read failed: {exc}")
        db_ok = False

    # Redis side — graceful degradation on failure.
    redis_ok = False
    ws_status = "unknown"
    ws_last_message_age_s: float | None = None
    if redis_client is not None:
        try:
            await redis_client.ping()
            redis_ok = True
            ws_ts = await redis_client.get("ws:market:last_message_ts")
            if ws_ts is not None:
                ws_last_message_age_s = max(
                    0.0,
                    float(datetime.now(timezone.utc).timestamp()) - float(ws_ts),
                )
                if ws_last_message_age_s <= 30:
                    ws_status = "connected"
                elif ws_last_message_age_s <= 120:
                    ws_status = "stale"
                else:
                    ws_status = "disconnected"
            else:
                ws_status = "no_data"
        except Exception as exc:
            logger.warning(f"pipeline_status redis read failed: {exc}")
            redis_ok = False
            ws_status = "redis_unreachable"

    # Killswitch — already Redis-cached (2s TTL via KillswitchService).
    killswitch_active = False
    exec_mode = "paper"
    paused_reason: str | None = None
    try:
        from src.control.killswitch import get_killswitch
        state = await get_killswitch().get_state()
        # `execution_enabled` is the master switch. When False the bot is paused.
        killswitch_active = not bool(state.execution_enabled)
        paused_reason = state.paused_reason
        if state.real_execution_enabled and state.execution_enabled:
            exec_mode = "live"
        elif state.execution_enabled:
            exec_mode = "paper" if settings.PAPER_TRADING else "live"
        else:
            exec_mode = "paused"
    except Exception as exc:
        logger.warning(f"pipeline_status killswitch read failed: {exc}")

    # Overall bot status — green if both DB+Redis ok AND engine activity
    # in the last hour AND WS connected. Otherwise degraded / down.
    if not db_ok or not redis_ok:
        bot_status = "down"
    elif killswitch_active:
        bot_status = "paused"
    elif ws_status in ("disconnected", "redis_unreachable"):
        bot_status = "degraded"
    elif ingestion_lag_s is not None and ingestion_lag_s > 300:
        bot_status = "degraded"
    else:
        bot_status = "healthy"

    # Batch 2 fix #2 — also expose the canonical UPPERCASE form so the
    # /api/portfolio/pipeline_status and /api/v1/live-summary endpoints
    # converge on identical bot_status / ws_status values. The legacy
    # lower-case keys above stay untouched for existing JSX consumers.
    if bot_status == "healthy":
        bot_status_canonical = "RUNNING"
    elif bot_status == "paused":
        bot_status_canonical = "STOPPED"
    elif bot_status in ("down", "degraded"):
        bot_status_canonical = "DEGRADED"
    else:
        bot_status_canonical = "RUNNING"

    if ws_status == "connected":
        ws_status_canonical = "LIVE"
    elif ws_status == "stale":
        ws_status_canonical = "DEGRADED"
    elif ws_status in ("disconnected", "redis_unreachable", "no_data"):
        ws_status_canonical = "DOWN"
    else:
        ws_status_canonical = "DOWN"

    return {
        "bot_status": bot_status,
        "ws_status": ws_status,
        # Canonical UPPERCASE aliases — see Batch 2 fix #2 comment.
        "bot_status_canonical": bot_status_canonical,
        "ws_status_canonical": ws_status_canonical,
        "ws_last_message_age_s": (
            round(ws_last_message_age_s, 2) if ws_last_message_age_s is not None
            else None
        ),
        "ingestion_lag_s": round(ingestion_lag_s, 2) if ingestion_lag_s is not None else None,
        "ingestion_count_24h": ingestion_count_24h,
        # Distinct counters (Batch 2 fix #3). The pipeline endpoint
        # exposed only the observed count — now it returns both so the
        # dashboard can stop reading the live-summary and the pipeline
        # endpoint with different keys.
        "observed_trades_24h": ingestion_count_24h,
        "exec_mode": exec_mode,
        "killswitch_active": killswitch_active,
        "paused_reason": paused_reason,
        "redis_ok": redis_ok,
        "db_ok": db_ok,
        "last_trade_at": last_trade_at.isoformat() if last_trade_at else None,
        "last_decision_at": last_decision_at.isoformat() if last_decision_at else None,
    }
