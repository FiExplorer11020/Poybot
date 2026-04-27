"""
All database queries for the API layer.
Each function accepts an asyncpg connection and returns plain dicts/lists.
No SQL lives outside this module.
"""

import json
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
    total_pnl = await conn.fetchval(
        f"""
        SELECT COALESCE(SUM(pnl_usdc), 0)
        FROM paper_trades
        WHERE status='closed'
          AND {V1_PAPER_TRADE_SQL}
        """
    )
    win_rate_row = await conn.fetchrow(
        f"""
        SELECT
            COUNT(*) FILTER (WHERE pnl_usdc > 0)::float /
            NULLIF(COUNT(*), 0) AS win_rate
        FROM paper_trades
        WHERE status='closed'
          AND {V1_PAPER_TRADE_SQL}
        """
    )
    active_leaders = await conn.fetchval(
        "SELECT COUNT(*) FROM leaders WHERE on_watchlist = TRUE AND excluded = FALSE"
    )
    open_positions = await conn.fetchval(
        f"""
        SELECT COUNT(*)
        FROM paper_trades
        WHERE status='open'
          AND {V1_PAPER_TRADE_SQL}
        """
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
    last_trade_row = await conn.fetchrow("SELECT MAX(time) AS last_trade FROM trades_observed")
    total_trades = await conn.fetchval("SELECT COUNT(*) FROM trades_observed")

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


async def leaders(conn) -> list[dict]:
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


async def system_status(conn) -> dict:
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
    rows = await conn.fetch(
        """
        SELECT
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
        ORDER BY t.time DESC
        LIMIT $1
        """,
        limit,
    )
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": f"obs:{_row_get(r, 'market_id')}:{_row_get(r, 'token_id')}:{_row_get(r, 'time').isoformat() if _row_get(r, 'time') else 'na'}",
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
    for r in rows:
        edges.append(
            {
                "leader_wallet": _row_get(r, "leader_wallet"),
                "follower_wallet": _row_get(r, "follower_wallet"),
                "hawkes_alpha_mu": _to_float(_row_get(r, "hawkes_alpha_mu"), 0.0),
                "follow_probability": _to_float(_row_get(r, "follow_probability"), 0.0),
                "beta_a": _to_float(_row_get(r, "follow_beta_a"), 0.0),
                "beta_b": _to_float(_row_get(r, "follow_beta_b"), 0.0),
                "co_occurrences": _to_int(_row_get(r, "co_occurrences"), 0),
                "avg_delay_s": _to_float(_row_get(r, "avg_delay_s"), 0.0),
                "same_direction_rate": _to_float(_row_get(r, "same_direction_rate"), 0.0),
                "trapped_rate": _to_float(_row_get(r, "trapped_rate"), 0.0),
                "last_observed": _row_get(r, "last_observed").isoformat()
                if _row_get(r, "last_observed")
                else None,
                "first_observed": _row_get(r, "first_observed").isoformat()
                if _row_get(r, "first_observed")
                else None,
            }
        )
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
    mrow = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE NULLIF(token_yes, '') IS NULL OR NULLIF(token_no, '') IS NULL
            ) AS unmapped_tokens,
            COUNT(*) FILTER (WHERE end_date IS NOT NULL AND end_date < NOW()) AS expired_active,
            COUNT(*) FILTER (WHERE active = TRUE) AS active
        FROM markets
        """
    )
    total_markets = _to_int(_row_get(mrow, "total"), 0)
    report["markets"] = {
        "total": total_markets,
        "active": _to_int(_row_get(mrow, "active"), 0),
        "unmapped_tokens": _to_int(_row_get(mrow, "unmapped_tokens"), 0),
        "expired_still_active": _to_int(_row_get(mrow, "expired_active"), 0),
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
    refresh_threshold = settings.FALCON_REFRESH_INTERVAL_S * 2
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
            "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(time))) FROM trades_observed"
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
