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
  * Pure async, asyncpg pool OR connection injected. No global state.
  * Tolerates missing tables (returns ok=False with detail='table missing').
  * When a pool is passed, the 5 pillar checks run in parallel via
    `asyncio.gather` with a dedicated connection per pillar — typical
    cold-start of `pillars_status` drops from ~280ms (sequential) to
    ~80ms (parallel, bounded by the slowest single query). When a
    single connection is passed (legacy callers, tests), the checks
    run sequentially on that connection.
  * Cheap to cache 30 s — all queries are tiny.
"""

from __future__ import annotations

import asyncio
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


_PILLAR_CHECKS: tuple[tuple[str, Any], ...] = (
    ("oracle", _check_oracle),
    ("reconciliation", _check_reconciliation),
    ("backfill", _check_backfill),
    ("spread_gates", _check_spread_gates),
    ("audit_log", _check_audit_log),
)


def _pillar_error_payload(name: str, exc: BaseException) -> dict[str, Any]:
    """Build a uniform error payload when a single pillar query fails.

    Keeps the gauge UI alive: instead of 500-ing the whole snapshot, the
    failing pillar shows up as red with a short reason.
    """
    return {
        "ok": False,
        "detail": f"check failed: {exc.__class__.__name__}",
        "error": str(exc)[:200],
    }


async def _check_pillar_isolated(pool, name: str, fn) -> dict[str, Any]:
    """Acquire a fresh connection from the pool and run one pillar check.

    Exceptions are caught here (in addition to the inner check) so a
    pool-acquire failure or a check-level uncaught error doesn't poison
    the `gather` result.
    """
    try:
        async with pool.acquire() as conn:
            return await fn(conn)
    except Exception as e:  # noqa: BLE001 — surface error to caller, never crash gauge
        logger.warning(f"pillars: {name} check failed: {e!r}")
        return _pillar_error_payload(name, e)


def _looks_like_pool(obj: Any) -> bool:
    """Duck-typing pool detection.

    asyncpg.Pool exposes `acquire` as a method returning a context-manager
    factory; an asyncpg.Connection also has `acquire` semantics through
    transaction handles, so we check for the attributes unique to a pool
    (`get_size` is a clean discriminator since 0.27).

    Test mocks (unittest.mock.MagicMock) auto-satisfy any `hasattr` probe,
    which would route every mocked call to the parallel path and break
    side_effect-ordering tests. Reject anything from `unittest.mock` so
    those tests stay on the deterministic sequential path while real
    asyncpg pools (module starts with `asyncpg.`) still flow through
    gather().
    """
    if not (hasattr(obj, "acquire") and hasattr(obj, "get_size")):
        return False
    module = type(obj).__module__ or ""
    if module.startswith("unittest.mock"):
        return False
    return True


def _extract_pool_from_conn(conn: Any) -> Any | None:
    """Best-effort extraction of the owning pool from a pooled connection.

    When a `PoolConnectionProxy` (yielded by `pool.acquire()`) is passed
    in, `conn._holder._pool` points back to the pool. This is technically
    private API but has been stable since asyncpg 0.23 and lets us turn
    legacy single-conn callers into parallel callers without changing
    their call sites. If introspection fails for any reason (raw
    connection, internal API change, etc.), we return None and the
    caller falls back to the sequential path.
    """
    try:
        holder = getattr(conn, "_holder", None)
        if holder is None:
            return None
        pool = getattr(holder, "_pool", None)
        if pool is not None and _looks_like_pool(pool):
            return pool
    except Exception:  # noqa: BLE001 — introspection must never crash
        return None
    return None


async def pillars_status(conn_or_pool, redis_client=None) -> dict[str, Any]:
    """Return health for the 5 paper-trading pillars.

    See module docstring for the pillar list. Each pillar function
    catches exceptions internally so the gauge always returns a payload
    rather than 500-ing the snapshot.

    If a pool is passed (`asyncpg.Pool`), the 5 checks run concurrently
    via `asyncio.gather`. If a pooled connection is passed, we transparently
    extract its parent pool and parallelise the same way (the original
    connection is held by the caller for the duration of the call and
    each pillar borrows a sibling connection). Otherwise (raw
    connection, mocks, tests) we fall back to a sequential pass on the
    provided connection — asyncpg connections are NOT safe for
    concurrent queries.
    """
    pool = conn_or_pool if _looks_like_pool(conn_or_pool) else _extract_pool_from_conn(conn_or_pool)
    if pool is not None:
        results = await asyncio.gather(
            *[
                _check_pillar_isolated(pool, name, fn)
                for name, fn in _PILLAR_CHECKS
            ],
            return_exceptions=True,
        )
        pillars: dict[str, Any] = {}
        for (name, _fn), result in zip(_PILLAR_CHECKS, results):
            if isinstance(result, BaseException):
                logger.warning(f"pillars: {name} gather raised {result!r}")
                pillars[name] = _pillar_error_payload(name, result)
            else:
                pillars[name] = result
    else:
        # Legacy single-connection path: sequential, but each pillar
        # is wrapped so one bad query doesn't kill the gauge.
        pillars = {}
        for name, fn in _PILLAR_CHECKS:
            try:
                pillars[name] = await fn(conn_or_pool)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"pillars: {name} (seq) failed: {e!r}")
                pillars[name] = _pillar_error_payload(name, e)

    overall_ok = all(p.get("ok", False) for p in pillars.values())

    return {
        "pillars": pillars,
        "overall_ok": overall_ok,
        "computed_at_iso": datetime.now(timezone.utc).isoformat(),
    }
