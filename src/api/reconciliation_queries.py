"""Paper-trade reconciliation queries for the dashboard Inspector tab.

Reads `paper_close_divergences` (Pillar 2 — populated by
`scripts/reconciliation.py` nightly + every 5 min cron pre-warm) and
joins `paper_trades` + `markets` to surface the displayed-vs-truth
gap to the operator.

Without these queries the +39 784 USDC phantom-BTC PnL from the
2026-05-17 audit would stay buried under aggregated dashboard totals.

CONVENTIONS
  * Pure async, asyncpg connection injected.
  * No global state.
  * Tolerates an empty divergence table by reporting verdict='unknown'.
  * Verdict thresholds (per ADR-PMK-014.3):
      ok       : |delta_abs| <  25
      warn     : |delta_abs| in [25, 250)
      critical : |delta_abs| >= 250
      unknown  : no closed paper trades in window OR no reconciliation run yet
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.economics.versioning import valid_paper_trade_filter

V1_PAPER_TRADE_SQL = valid_paper_trade_filter()
V1_PAPER_TRADE_PT_SQL = valid_paper_trade_filter("pt")


# Verdict thresholds (USDC). Centralised here so the UI badge code never
# duplicates the boundary logic — single source of truth per ADR-014.10.
VERDICT_OK_BELOW_USDC: float = 25.0
VERDICT_WARN_BELOW_USDC: float = 250.0

# Flag taxonomy that maps each divergence row to one of the four UI
# classifications. Mirror the comments in
# docs/migrations/051_paper_close_divergences.sql.
_PHANTOM_FLAGS = {"fake_win", "fake_loss"}
_PREMATURE_FLAGS = {"still_open_in_reality", "premature_close"}


def _classify_flag(flag: str | None) -> str:
    if flag in _PHANTOM_FLAGS:
        return "phantom"
    if flag in _PREMATURE_FLAGS:
        return "premature"
    return "drift"


def _verdict_for_delta(delta_abs: float | None, trades_evaluated: int) -> str:
    if trades_evaluated == 0 or delta_abs is None:
        return "unknown"
    a = abs(delta_abs)
    if a < VERDICT_OK_BELOW_USDC:
        return "ok"
    if a < VERDICT_WARN_BELOW_USDC:
        return "warn"
    return "critical"


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


async def reconciliation_summary(conn, window_days: int = 30) -> dict[str, Any]:
    """Aggregate divergence summary for the dashboard recon panel.

    Reads `paper_close_divergences` joined to `paper_trades` for trades
    closed within the last ``window_days``. Returns the JSON payload
    consumed by /api/inspector/reconciliation.
    """
    # Hot-path: a single aggregate read covers most of the payload.
    row = await conn.fetchrow(
        f"""
        WITH closed_in_window AS (
            SELECT pt.id, pt.pnl_usdc, pt.closed_at
            FROM paper_trades pt
            WHERE pt.status = 'closed'
              AND pt.closed_at >= NOW() - ($1 || ' days')::INTERVAL
              AND {V1_PAPER_TRADE_PT_SQL}
        ),
        divergences_in_window AS (
            SELECT pcd.paper_trade_id, pcd.delta_usdc, pcd.flag, pcd.detected_at
            FROM paper_close_divergences pcd
            JOIN closed_in_window ciw ON ciw.id = pcd.paper_trade_id
        )
        SELECT
            (SELECT COUNT(*) FROM closed_in_window)::int                              AS trades_evaluated,
            (SELECT COUNT(*) FROM divergences_in_window)::int                         AS trades_drift_count,
            COALESCE((SELECT SUM(pnl_usdc) FROM closed_in_window), 0)::float          AS pnl_displayed_sum,
            COALESCE((SELECT SUM(delta_usdc) FROM divergences_in_window), 0)::float   AS sum_delta_usdc,
            (SELECT COUNT(*) FROM divergences_in_window WHERE flag = ANY($2))::int    AS phantom_count,
            (SELECT COUNT(*) FROM divergences_in_window WHERE flag = ANY($3))::int    AS premature_count,
            (SELECT MAX(detected_at) FROM divergences_in_window)                      AS latest_divergence_at
        """,
        str(window_days),
        list(_PHANTOM_FLAGS),
        list(_PREMATURE_FLAGS),
    )

    trades_evaluated = int(row["trades_evaluated"] or 0)
    trades_drift_count = int(row["trades_drift_count"] or 0)
    pnl_displayed_sum = float(row["pnl_displayed_sum"] or 0.0)
    # Divergence delta convention (per 051 migration comment):
    #   delta_usdc = db_pnl - truth_pnl  →  truth = displayed - delta
    sum_delta = float(row["sum_delta_usdc"] or 0.0)
    pnl_oracle_sum = pnl_displayed_sum - sum_delta if trades_drift_count > 0 else pnl_displayed_sum
    pnl_delta_abs = pnl_displayed_sum - pnl_oracle_sum
    pnl_delta_pct = (pnl_delta_abs / abs(pnl_displayed_sum)) if pnl_displayed_sum else 0.0

    # Run timestamp + age. We take the latest detected_at across ALL
    # divergence rows (not just the window) so an empty window still
    # shows the last time the recon job ran globally.
    if trades_drift_count > 0 and row["latest_divergence_at"] is not None:
        run_at = row["latest_divergence_at"]
    else:
        run_at = await conn.fetchval(
            "SELECT MAX(detected_at) FROM paper_close_divergences"
        )

    run_at_iso = _safe_iso(run_at)
    age_s: int | None = None
    if isinstance(run_at, datetime):
        age_s = max(0, int((datetime.now(timezone.utc) - run_at.astimezone(timezone.utc)).total_seconds()))

    # Sparkline: bucket divergence detection by hour, take last 5 buckets.
    spark_rows = await conn.fetch(
        """
        SELECT date_trunc('hour', detected_at) AS bucket_at,
               SUM(ABS(delta_usdc))::float    AS delta_abs_sum
        FROM paper_close_divergences
        WHERE detected_at >= NOW() - INTERVAL '24 hours'
        GROUP BY bucket_at
        ORDER BY bucket_at DESC
        LIMIT 5
        """
    )
    last_5_runs = list(
        reversed([
            {
                "run_at_iso": _safe_iso(r["bucket_at"]),
                "pnl_delta_abs": float(r["delta_abs_sum"] or 0.0),
            }
            for r in spark_rows
        ])
    )

    verdict = _verdict_for_delta(pnl_delta_abs, trades_evaluated)

    return {
        "window_days": int(window_days),
        "run_at_iso": run_at_iso,
        "age_s": age_s,
        "trades_evaluated": trades_evaluated,
        "trades_drift_count": trades_drift_count,
        "pnl_displayed_sum": round(pnl_displayed_sum, 2),
        "pnl_oracle_sum": round(pnl_oracle_sum, 2),
        "pnl_delta_abs": round(pnl_delta_abs, 2),
        "pnl_delta_pct": round(pnl_delta_pct, 6),
        "phantom_count": int(row["phantom_count"] or 0),
        "premature_count": int(row["premature_count"] or 0),
        "verdict": verdict,
        "last_5_runs": last_5_runs,
    }


async def reconciliation_drift_trades(
    conn,
    classification: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-trade drift list for the Inspector drill-down modal.

    ``classification`` filters: ok | drift | phantom | premature | None.
    'ok' = trades with NO divergence row (joined LEFT and filtered).
    """
    if classification == "ok":
        rows = await conn.fetch(
            f"""
            SELECT pt.id AS paper_trade_id,
                   pt.market_id, pt.direction, pt.closed_at, pt.pnl_usdc,
                   m.question AS market_question, m.category
            FROM paper_trades pt
            LEFT JOIN markets m       ON m.market_id = pt.market_id
            LEFT JOIN paper_close_divergences pcd ON pcd.paper_trade_id = pt.id
            WHERE pt.status = 'closed'
              AND pcd.id IS NULL
              AND {V1_PAPER_TRADE_PT_SQL}
            ORDER BY pt.closed_at DESC
            LIMIT $1
            """,
            int(limit),
        )
        return [
            {
                "paper_trade_id": int(r["paper_trade_id"]),
                "market_id": r["market_id"],
                "market_question": r["market_question"],
                "category": r["category"],
                "closed_at_iso": _safe_iso(r["closed_at"]),
                "direction": r["direction"],
                "pnl_displayed": float(r["pnl_usdc"] or 0.0),
                "pnl_oracle": float(r["pnl_usdc"] or 0.0),
                "delta_abs": 0.0,
                "delta_pct": 0.0,
                "classification": "ok",
                "flag": None,
            }
            for r in rows
        ]

    where_extra = ""
    args: list[Any] = []
    if classification == "phantom":
        where_extra = "AND pcd.flag = ANY($2)"
        args.append(list(_PHANTOM_FLAGS))
    elif classification == "premature":
        where_extra = "AND pcd.flag = ANY($2)"
        args.append(list(_PREMATURE_FLAGS))
    elif classification == "drift":
        where_extra = "AND pcd.flag NOT IN ('fake_win','fake_loss','still_open_in_reality','premature_close')"
    # else: any classification (ok handled above, None = all)

    sql = f"""
        SELECT pcd.paper_trade_id, pcd.delta_usdc, pcd.flag,
               pcd.db_pnl_usdc, pcd.truth_pnl_usdc, pcd.closed_at,
               pcd.direction, pcd.market_id,
               m.question AS market_question, m.category
        FROM paper_close_divergences pcd
        LEFT JOIN markets m ON m.market_id = pcd.market_id
        WHERE 1=1 {where_extra}
        ORDER BY ABS(pcd.delta_usdc) DESC
        LIMIT $1
    """
    rows = await conn.fetch(sql, int(limit), *args)
    out: list[dict[str, Any]] = []
    for r in rows:
        displayed = float(r["db_pnl_usdc"] or 0.0)
        oracle = float(r["truth_pnl_usdc"] or 0.0)
        delta_abs = float(r["delta_usdc"] or 0.0)
        delta_pct = (delta_abs / abs(displayed)) if displayed else 0.0
        out.append(
            {
                "paper_trade_id": int(r["paper_trade_id"]),
                "market_id": r["market_id"],
                "market_question": r["market_question"],
                "category": r["category"],
                "closed_at_iso": _safe_iso(r["closed_at"]),
                "direction": r["direction"],
                "pnl_displayed": round(displayed, 2),
                "pnl_oracle": round(oracle, 2),
                "delta_abs": round(delta_abs, 2),
                "delta_pct": round(delta_pct, 6),
                "classification": _classify_flag(r["flag"]),
                "flag": r["flag"],
            }
        )
    return out


async def reconciliation_trigger_run(
    conn,
    redis_client,
    window_days: int = 30,
) -> dict[str, Any]:
    """Operator-triggered reconciliation. Non-blocking.

    Sets a Redis key the engine container polls. The actual recon work
    is performed by `scripts/reconciliation.py` invoked from the
    scheduler — we just signal that a fresh pass is wanted.
    """
    queued_at = datetime.now(timezone.utc)
    payload = json.dumps(
        {
            "window_days": int(window_days),
            "queued_at": queued_at.isoformat(),
        }
    )
    redis_key = "recon:trigger:queued"
    if redis_client is not None:
        try:
            await redis_client.set(redis_key, payload, ex=300)
        except Exception as e:  # pragma: no cover — best-effort
            logger.warning(f"reconciliation_trigger_run: redis SET failed: {e}")
            return {
                "scheduled": False,
                "queued_at_iso": queued_at.isoformat(),
                "key": redis_key,
                "error": str(e),
            }
    return {
        "scheduled": redis_client is not None,
        "queued_at_iso": queued_at.isoformat(),
        "key": redis_key,
        "window_days": int(window_days),
    }
