"""Health gauge for the 5 paper-trading pillars (Bot Health tab).

Per memory `project_paper_trading_pillars.md`, the 5 pillars are:
  1. PriceOracle      — src/control/price_oracle.py
  2. Reconciliation   — scripts/reconciliation.py + paper_close_divergences
  3. Backfill         — markets.resolved_outcome populated by recon backfill
  4. Spread gates     — close_audit_log.oracle_source='fail' = rejected close
  5. Close audit log  — close_audit_log table (every close recorded)

Each pillar exposes a boolean `ok` + a short detail string. Failing
pillar → operator sees red on Bot Health and knows the paper trading
numbers are not trustworthy.

CONVENTIONS
  * Pure async, asyncpg connection injected. No global state.
  * Tolerates missing tables (returns ok=False with detail='table missing').
  * Cheap to cache 30 s — all queries are tiny.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _age_seconds(value: Any) -> int | None:
    if not isinstance(value, datetime):
        return None
    delta = datetime.now(timezone.utc) - value.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds()))


def _fmt_age(sec: int | None) -> str:
    if sec is None:
        return "never"
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


async def _check_oracle(conn) -> dict[str, Any]:
    """PriceOracle health: are we producing book/gamma quotes?"""
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE oracle_source IN ('book', 'gamma')
                      AND closed_at > NOW() - INTERVAL '24 hours'
                )::int                              AS quotes_24h,
                MAX(closed_at) FILTER (
                    WHERE oracle_source IN ('book', 'gamma')
                )                                    AS last_quote_at
            FROM close_audit_log
            """
        )
    except Exception as e:
        return {"ok": False, "detail": f"table missing: {e.__class__.__name__}", "quotes_24h": 0}
    quotes_24h = int(row["quotes_24h"] or 0)
    last_age = _age_seconds(row["last_quote_at"])
    ok = quotes_24h > 0
    if ok:
        detail = f"{quotes_24h} quotes/24h, last {_fmt_age(last_age)}"
    else:
        detail = "no quotes in 24h"
    return {
        "ok": ok,
        "detail": detail,
        "last_quote_age_s": last_age,
        "quotes_24h": quotes_24h,
    }


async def _check_reconciliation(conn) -> dict[str, Any]:
    """Reconciliation health: did the recon job run recently?"""
    try:
        last_run_at = await conn.fetchval(
            "SELECT MAX(detected_at) FROM paper_close_divergences"
        )
        divergences_24h = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM paper_close_divergences
            WHERE detected_at > NOW() - INTERVAL '24 hours'
            """
        )
        closed_paper_24h = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM paper_trades
            WHERE status = 'closed' AND closed_at > NOW() - INTERVAL '24 hours'
            """
        )
    except Exception as e:
        return {"ok": False, "detail": f"table missing: {e.__class__.__name__}", "divergences_24h": 0}

    age_s = _age_seconds(last_run_at)
    divergences_24h = int(divergences_24h or 0)
    closed_paper_24h = int(closed_paper_24h or 0)

    # ok if recon has ever run AND ran in the last 24h.
    # Exception: if 0 closed trades in 24h there's nothing to reconcile
    # → still ok.
    if last_run_at is None:
        ok = closed_paper_24h == 0
        detail = "never run" if not ok else "no closes 24h"
    else:
        ok = age_s is not None and age_s < 86400
        detail = f"ran {_fmt_age(age_s)}, {divergences_24h} divergences" if ok else f"stale ({_fmt_age(age_s)})"

    return {
        "ok": ok,
        "detail": detail,
        "last_run_at_iso": _safe_iso(last_run_at),
        "last_run_age_s": age_s,
        "divergences_24h": divergences_24h,
    }


async def _check_backfill(conn) -> dict[str, Any]:
    """Backfill health: more resolved markets than pending past-end-date."""
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE resolved_outcome IS NOT NULL)::int      AS resolved,
                COUNT(*) FILTER (
                    WHERE resolved_outcome IS NULL AND end_date < NOW()
                )::int                                                          AS pending
            FROM markets
            """
        )
    except Exception as e:
        return {"ok": False, "detail": f"table missing: {e.__class__.__name__}", "markets_resolved": 0, "markets_pending": 0}
    resolved = int(row["resolved"] or 0)
    pending = int(row["pending"] or 0)
    # ok when we have any resolved markets AND pending isn't dominating.
    # The threshold (pending < resolved) catches the case where the
    # backfill job has fallen behind and most past-end markets aren't
    # marked resolved.
    ok = resolved > 0 and pending < max(1, resolved)
    return {
        "ok": ok,
        "detail": f"{resolved} resolved / {pending} pending",
        "markets_resolved": resolved,
        "markets_pending": pending,
    }


async def _check_spread_gates(conn) -> dict[str, Any]:
    """Spread gates health: reject rate < 50% of closes."""
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE oracle_source = 'fail')::int   AS rejects_24h,
                COUNT(*)::int                                          AS total_24h
            FROM close_audit_log
            WHERE closed_at > NOW() - INTERVAL '24 hours'
            """
        )
    except Exception as e:
        return {"ok": False, "detail": f"table missing: {e.__class__.__name__}", "rejects_24h": 0}
    rejects = int(row["rejects_24h"] or 0)
    total = int(row["total_24h"] or 0)
    if total == 0:
        return {"ok": True, "detail": "no activity 24h", "rejects_24h": 0, "total_24h": 0}
    reject_pct = rejects / total
    ok = reject_pct < 0.5
    return {
        "ok": ok,
        "detail": f"{rejects}/{total} rejects ({reject_pct * 100:.0f}%)",
        "rejects_24h": rejects,
        "total_24h": total,
    }


async def _check_audit_log(conn) -> dict[str, Any]:
    """Close audit log health: closes are being recorded."""
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int                                            AS rows_24h,
                COUNT(*) FILTER (WHERE oracle_source = 'fallback')::int  AS fallback_24h,
                COUNT(*) FILTER (WHERE oracle_source = 'fail')::int      AS fail_24h
            FROM close_audit_log
            WHERE closed_at > NOW() - INTERVAL '24 hours'
            """
        )
        closed_paper_24h = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM paper_trades
            WHERE status = 'closed' AND closed_at > NOW() - INTERVAL '24 hours'
            """
        )
    except Exception as e:
        return {"ok": False, "detail": f"table missing: {e.__class__.__name__}", "rows_24h": 0}
    rows_24h = int(row["rows_24h"] or 0)
    fallback_24h = int(row["fallback_24h"] or 0)
    closed_paper_24h = int(closed_paper_24h or 0)
    # ok when audit rows >= closed trades (every close should be audited),
    # OR no trades closed in 24h.
    ok = closed_paper_24h == 0 or rows_24h >= closed_paper_24h
    detail = f"{rows_24h} rows · {fallback_24h} fallbacks"
    if not ok:
        detail = f"{rows_24h} rows for {closed_paper_24h} closes (audit gap)"
    return {
        "ok": ok,
        "detail": detail,
        "rows_24h": rows_24h,
        "phantom_count_24h": fallback_24h,
        "fallback_count_24h": fallback_24h,
    }


async def pillars_status(conn, redis_client=None) -> dict[str, Any]:
    """Return health for the 5 paper-trading pillars.

    See module docstring for the pillar list. Each pillar function
    catches exceptions internally so the gauge always returns a payload
    rather than 500-ing the snapshot.
    """
    oracle = await _check_oracle(conn)
    recon = await _check_reconciliation(conn)
    backfill = await _check_backfill(conn)
    spread = await _check_spread_gates(conn)
    audit = await _check_audit_log(conn)

    pillars = {
        "oracle": oracle,
        "reconciliation": recon,
        "backfill": backfill,
        "spread_gates": spread,
        "audit_log": audit,
    }
    overall_ok = all(p.get("ok", False) for p in pillars.values())

    return {
        "pillars": pillars,
        "overall_ok": overall_ok,
        "computed_at_iso": datetime.now(timezone.utc).isoformat(),
    }
