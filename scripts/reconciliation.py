"""Pillar 2 — Gamma reconciliation nightly job.

Walks every paper_trades.status='closed' row within the last
``lookback_days`` and confronts its inscribed PnL against the truth
Polymarket would settle: either the DB ``markets.resolved_outcome``
(populated by the Pillar 3 backfill) or, when that column is still
NULL, a direct Gamma /markets fetch with exponential-backoff retry on
HTTP 429.

Each divergence ``|db_pnl - truth_pnl| > tolerance_usdc`` UPSERTs into
``paper_close_divergences`` (one row per trade — re-runs refresh the
existing row). When at least one new divergence is inserted, a single
``paper:audit:divergence`` Redis event is published carrying the
top-3-worst summary so the Telegram operator sees the discrepancy
without scrolling through SQL.

Without this job the +39,784 USDC of phantom BTC PnL from the
2026-05-17 audit would have stayed buried until manual inspection.

CONVENTIONS
  * Pure async, asyncpg pool injected.
  * Owns no global state — the caller passes the http session so the
    same job can run from the engine container (long-lived aiohttp
    session) or from a one-shot ``python -m scripts.reconciliation``.
  * Never raises out of the metrics gather — Gamma fetch failures
    increment ``gamma_unreachable`` and skip the trade.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

import aiohttp
import asyncpg
from loguru import logger


# --------------------------------------------------------------------------- #
# Channel + constants                                                          #
# --------------------------------------------------------------------------- #
# Declared here so the producer module owns the channel string — the
# notifier picks it up via ALL_CHANNELS.

CHANNEL_PAPER_AUDIT_DIVERGENCE = "paper:audit:divergence"

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_TIMEOUT_S = 15.0

# Reconciliation-specific retry envelope. Smaller cap than the
# backfill_resolved_outcomes path because we want this job to finish
# even if Gamma is shedding load — partial coverage is better than a
# hung job at 04:00 UTC.
RECONCILE_MAX_CONSECUTIVE_429 = 3
RECONCILE_RETRY_INITIAL_S = 5.0
RECONCILE_RETRY_MAX_S = 20.0
RECONCILE_RETRY_JITTER = 0.20

# Threshold above which the global discrepancy logs WARNING and the
# Redis envelope advertises an "alarming" flag for the Telegram
# formatter to amplify.
ALARMING_DISCREPANCY_USDC = 100.0


# --------------------------------------------------------------------------- #
# Helpers — Gamma fetch with retry                                             #
# --------------------------------------------------------------------------- #


def _compute_backoff(attempt: int, *, initial: float, cap: float) -> float:
    """Exponential backoff with symmetric jitter.

    Local copy (not imported from ``scripts.maintenance_loop``) to
    avoid pulling that 1300-line module's import chain when this job
    runs from the engine container. Behaviour matches the one used by
    the resolved-outcome backfill so an operator reading both logs
    sees consistent timing.
    """
    base = min(cap, initial * (2 ** max(0, attempt)))
    j = RECONCILE_RETRY_JITTER
    factor = 1.0 + random.uniform(-j, j)
    return max(0.0, min(cap, base * factor))


async def _fetch_gamma_market(
    session: aiohttp.ClientSession,
    market_id: str,
    *,
    initial_backoff_s: float | None = None,
    max_backoff_s: float | None = None,
) -> dict | None:
    """Return a single Gamma /markets row by condition_id, or None.

    Returns None on persistent 429s, non-200 status, malformed payload,
    or network failures — the caller treats that as "Gamma unreachable
    for this trade" and bumps the metric instead of crashing.

    Defaults are read from the module-level constants at call time so
    tests can monkeypatch ``RECONCILE_RETRY_INITIAL_S`` / ``_MAX_S`` to
    keep retry sleeps near zero.
    """
    if initial_backoff_s is None:
        initial_backoff_s = RECONCILE_RETRY_INITIAL_S
    if max_backoff_s is None:
        max_backoff_s = RECONCILE_RETRY_MAX_S
    params = {"condition_ids": market_id}
    consecutive_429 = 0
    attempt = 0

    while consecutive_429 < RECONCILE_MAX_CONSECUTIVE_429:
        try:
            async with session.get(
                GAMMA_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=GAMMA_TIMEOUT_S),
            ) as resp:
                if resp.status == 429:
                    consecutive_429 += 1
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            sleep_s = max(
                                0.0, min(max_backoff_s, float(retry_after))
                            )
                        except (TypeError, ValueError):
                            sleep_s = _compute_backoff(
                                attempt,
                                initial=initial_backoff_s,
                                cap=max_backoff_s,
                            )
                    else:
                        sleep_s = _compute_backoff(
                            attempt,
                            initial=initial_backoff_s,
                            cap=max_backoff_s,
                        )
                    logger.debug(
                        f"reconciliation: gamma 429 market={market_id!r} "
                        f"attempt={attempt + 1}/{RECONCILE_MAX_CONSECUTIVE_429} "
                        f"sleep={sleep_s:.1f}s"
                    )
                    attempt += 1
                    await asyncio.sleep(sleep_s)
                    continue
                if resp.status != 200:
                    logger.debug(
                        f"reconciliation: gamma status={resp.status} "
                        f"market={market_id!r}; skipping"
                    )
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            consecutive_429 += 1
            sleep_s = _compute_backoff(
                attempt, initial=initial_backoff_s, cap=max_backoff_s
            )
            logger.debug(
                f"reconciliation: gamma network error market={market_id!r} "
                f"attempt={attempt + 1}: {type(exc).__name__}; sleep={sleep_s:.1f}s"
            )
            attempt += 1
            await asyncio.sleep(sleep_s)
            continue

        # Normalise wrapper shapes.
        if isinstance(payload, dict) and "data" in payload:
            rows = payload.get("data") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        if not isinstance(row, dict):
            return None
        return row

    return None  # gave up after consecutive 429s


def _parse_gamma_outcome(payload: dict) -> str | None:
    """Return 'yes' / 'no' or None for an unresolved/malformed Gamma row.

    Mirrors ``scripts.maintenance_loop._parse_resolved_outcome`` so the
    two pipelines never disagree on what "winning side" means.
    """
    prices = payload.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (ValueError, TypeError):
            return None
    if not isinstance(prices, list) or len(prices) < 1:
        return None
    try:
        yes_terminal = float(prices[0])
    except (TypeError, ValueError):
        return None
    return "yes" if yes_terminal > 0.5 else "no"


def _gamma_is_closed(payload: dict) -> bool:
    """Return True iff Gamma flags the market as terminal (closed=true)."""
    val = payload.get("closed")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


# --------------------------------------------------------------------------- #
# Truth-PnL computation                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrueOutcome:
    """Resolved-truth bundle for one closed paper trade.

    ``gamma_outcome`` is None when the truth came from the DB
    ``markets.resolved_outcome`` column AND we never queried Gamma
    (still recorded in ``gamma_snapshot=None``). ``still_open`` is True
    when Gamma confirms closed=false at reconciliation time — the
    sentinel for the "we closed prematurely" case.
    """

    truth_exit_price: float | None
    truth_pnl_usdc: float | None
    gamma_outcome: str | None
    gamma_snapshot: dict | None
    still_open: bool
    source: str  # 'db_resolved' | 'gamma_resolved' | 'gamma_open' | 'unknown'


def _theoretical_pnl(
    *,
    direction: str,
    size_usdc: float,
    entry_price: float,
    fee_paid_usdc: float,
    winning_side: str,
) -> tuple[float, float]:
    """Return (truth_exit_price, truth_pnl_net) for a resolved market.

    The held token wins → exit_price=1.0; the held token loses → 0.0.
    Mirror the PnL formula paper_trader uses at close time so any
    deviation is genuinely a data bug, not a formula drift.
    """
    truth_exit_price = 1.0 if direction == winning_side else 0.0
    shares = size_usdc / max(entry_price, 1e-9)
    truth_pnl_gross = shares * truth_exit_price - size_usdc
    truth_pnl_net = truth_pnl_gross - float(fee_paid_usdc or 0.0)
    return truth_exit_price, truth_pnl_net


async def _resolve_truth(
    conn: asyncpg.Connection,
    session: aiohttp.ClientSession,
    trade: dict,
    *,
    gamma_cache: dict[str, dict | None],
) -> tuple[TrueOutcome, bool]:
    """Compute the (truth_exit_price, truth_pnl) for a single trade.

    Returns (TrueOutcome, gamma_was_called). ``gamma_cache`` is mutated
    so multiple trades on the same market_id share a single HTTP
    round-trip across one ``reconcile_closed_trades`` invocation.
    """
    market_id = trade["market_id"]
    direction = trade["direction"]
    size_usdc = float(trade["size_usdc"])
    entry_price = float(trade["entry_price"])
    fee_paid_usdc = float(trade["fee_paid_usdc"] or 0.0)

    # Step 1 — DB resolved_outcome (cheapest source of truth).
    row = await conn.fetchrow(
        "SELECT resolved_outcome FROM markets WHERE market_id=$1",
        market_id,
    )
    db_outcome = row["resolved_outcome"] if row else None
    if db_outcome is not None:
        outcome_str = str(db_outcome).strip().lower()
        if outcome_str in ("yes", "no"):
            truth_exit, truth_pnl = _theoretical_pnl(
                direction=direction,
                size_usdc=size_usdc,
                entry_price=entry_price,
                fee_paid_usdc=fee_paid_usdc,
                winning_side=outcome_str,
            )
            return (
                TrueOutcome(
                    truth_exit_price=truth_exit,
                    truth_pnl_usdc=truth_pnl,
                    gamma_outcome=outcome_str,
                    gamma_snapshot=None,
                    still_open=False,
                    source="db_resolved",
                ),
                False,
            )

    # Step 2 — Gamma fallback. Cache per market_id within this run.
    if market_id in gamma_cache:
        payload = gamma_cache[market_id]
    else:
        payload = await _fetch_gamma_market(session, market_id)
        gamma_cache[market_id] = payload
    if payload is None:
        return (
            TrueOutcome(
                truth_exit_price=None,
                truth_pnl_usdc=None,
                gamma_outcome=None,
                gamma_snapshot=None,
                still_open=False,
                source="unknown",
            ),
            True,
        )

    closed = _gamma_is_closed(payload)
    if not closed:
        return (
            TrueOutcome(
                truth_exit_price=None,
                truth_pnl_usdc=None,
                gamma_outcome=None,
                gamma_snapshot=payload,
                still_open=True,
                source="gamma_open",
            ),
            True,
        )

    outcome = _parse_gamma_outcome(payload)
    if outcome is None:
        return (
            TrueOutcome(
                truth_exit_price=None,
                truth_pnl_usdc=None,
                gamma_outcome=None,
                gamma_snapshot=payload,
                still_open=False,
                source="unknown",
            ),
            True,
        )
    truth_exit, truth_pnl = _theoretical_pnl(
        direction=direction,
        size_usdc=size_usdc,
        entry_price=entry_price,
        fee_paid_usdc=fee_paid_usdc,
        winning_side=outcome,
    )
    return (
        TrueOutcome(
            truth_exit_price=truth_exit,
            truth_pnl_usdc=truth_pnl,
            gamma_outcome=outcome,
            gamma_snapshot=payload,
            still_open=False,
            source="gamma_resolved",
        ),
        True,
    )


# --------------------------------------------------------------------------- #
# Flag classification                                                          #
# --------------------------------------------------------------------------- #


def _classify(
    *,
    db_pnl: float,
    truth: TrueOutcome,
    tolerance: float,
    closed_at: datetime,
    detected_at: datetime,
) -> tuple[str | None, str | None]:
    """Return (flag, notes) — or (None, None) when the row matches
    within tolerance and must NOT be inserted.

    ``still_open_in_reality`` takes priority over fake_win/fake_loss
    because it carries a more actionable diagnostic (we closed before
    the market resolved). The plain ``premature_close`` flag is
    reserved for the rare case where Gamma is *unreachable* AND
    closed_at is in the past — recorded by the caller when truth is
    unknown but db_pnl differs from zero by more than tolerance.
    """
    if truth.still_open:
        # We have a paper close in the past but Gamma says the market
        # is still trading → the close was premature and any PnL we
        # booked is unrealised in reality.
        return (
            "still_open_in_reality",
            f"closed_at={closed_at.isoformat()} but Gamma reports closed=false at "
            f"{detected_at.isoformat()}",
        )

    if truth.truth_pnl_usdc is None:
        # Gamma unreachable + DB resolved_outcome NULL. We can't
        # decide; the caller will treat this as gamma_unreachable and
        # skip insertion unless db_pnl looks materially off.
        return (None, None)

    delta = db_pnl - truth.truth_pnl_usdc
    if abs(delta) <= tolerance:
        return (None, None)

    if db_pnl > 0 and truth.truth_pnl_usdc < db_pnl - tolerance:
        return ("fake_win", None)
    if db_pnl < -tolerance and truth.truth_pnl_usdc > -tolerance:
        return ("fake_loss", None)
    # Any other material disagreement (e.g. both positive but very
    # different magnitudes) is treated as a premature_close marker.
    return ("premature_close", None)


# --------------------------------------------------------------------------- #
# Persistence — UPSERT into paper_close_divergences                            #
# --------------------------------------------------------------------------- #


_UPSERT_SQL = """
INSERT INTO paper_close_divergences (
    paper_trade_id, detected_at, closed_at, market_id, direction,
    db_pnl_usdc, truth_pnl_usdc, delta_usdc, db_exit_price,
    truth_exit_price, gamma_outcome, gamma_snapshot, flag, notes
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14
)
ON CONFLICT (paper_trade_id) DO UPDATE SET
    detected_at = EXCLUDED.detected_at,
    db_pnl_usdc = EXCLUDED.db_pnl_usdc,
    truth_pnl_usdc = EXCLUDED.truth_pnl_usdc,
    delta_usdc = EXCLUDED.delta_usdc,
    truth_exit_price = EXCLUDED.truth_exit_price,
    gamma_outcome = EXCLUDED.gamma_outcome,
    gamma_snapshot = EXCLUDED.gamma_snapshot,
    flag = EXCLUDED.flag,
    notes = EXCLUDED.notes
RETURNING (xmax = 0) AS inserted
"""


async def _upsert_divergence(
    conn: asyncpg.Connection,
    *,
    trade: dict,
    detected_at: datetime,
    truth: TrueOutcome,
    delta: float,
    flag: str,
    notes: str | None,
) -> bool:
    """Return True if a brand-new row was inserted, False on UPDATE."""
    snapshot_json = (
        json.dumps(truth.gamma_snapshot, default=str)
        if truth.gamma_snapshot is not None
        else None
    )
    truth_pnl_for_db = (
        float(truth.truth_pnl_usdc) if truth.truth_pnl_usdc is not None else 0.0
    )
    row = await conn.fetchrow(
        _UPSERT_SQL,
        int(trade["id"]),
        detected_at,
        trade["closed_at"],
        trade["market_id"],
        trade["direction"],
        float(trade["pnl_usdc"] or 0.0),
        truth_pnl_for_db,
        float(delta),
        float(trade["exit_price"] or 0.0),
        float(truth.truth_exit_price) if truth.truth_exit_price is not None else None,
        truth.gamma_outcome,
        snapshot_json,
        flag,
        notes,
    )
    return bool(row["inserted"]) if row is not None else False


# --------------------------------------------------------------------------- #
# Main entry point                                                             #
# --------------------------------------------------------------------------- #


async def reconcile_closed_trades(
    pool: asyncpg.Pool,
    redis_client,
    http_session: aiohttp.ClientSession,
    *,
    lookback_days: int = 30,
    tolerance_usdc: float = 2.0,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Reconcile closed paper trades against Gamma truth.

    See module docstring for the full design rationale. Returns a
    metrics dict carrying the same shape we publish on Redis.
    """
    started_monotonic = asyncio.get_event_loop().time()
    detected_at = datetime.now(timezone.utc)
    run_id = str(uuid4())

    metrics: dict[str, Any] = {
        "scanned": 0,
        "matched": 0,
        "divergences_inserted": 0,
        "divergences_updated": 0,
        "still_open_in_reality": 0,
        "gamma_unreachable": 0,
        "by_flag": {},
        "total_db_pnl": 0.0,
        "total_truth_pnl": 0.0,
        "run_duration_s": 0.0,
    }

    # Top-3-worst tracker — keyed by absolute delta.
    top_worst: list[tuple[float, dict]] = []

    async with pool.acquire() as conn:
        trades: Iterable[asyncpg.Record] = await conn.fetch(
            """
            SELECT id, market_id, token_id, direction, entry_price, exit_price,
                   size_usdc, pnl_usdc, fee_paid_usdc, closed_at, close_reason
            FROM paper_trades
            WHERE status = 'closed'
              AND closed_at >= NOW() - ($1::int * INTERVAL '1 day')
            ORDER BY closed_at DESC
            LIMIT $2
            """,
            int(lookback_days),
            int(batch_size),
        )

        gamma_cache: dict[str, dict | None] = {}
        for record in trades:
            trade = dict(record)
            metrics["scanned"] += 1
            db_pnl = float(trade["pnl_usdc"] or 0.0)
            metrics["total_db_pnl"] += db_pnl

            try:
                truth, gamma_called = await _resolve_truth(
                    conn, http_session, trade, gamma_cache=gamma_cache
                )
            except Exception as exc:
                logger.warning(
                    f"reconciliation: truth resolution crashed for trade "
                    f"#{trade['id']}: {exc}"
                )
                metrics["gamma_unreachable"] += 1
                continue

            if truth.source == "unknown":
                # Gamma unreachable AND DB outcome NULL → can't decide.
                metrics["gamma_unreachable"] += 1
                continue

            if truth.truth_pnl_usdc is not None:
                metrics["total_truth_pnl"] += truth.truth_pnl_usdc

            flag, notes = _classify(
                db_pnl=db_pnl,
                truth=truth,
                tolerance=float(tolerance_usdc),
                closed_at=trade["closed_at"],
                detected_at=detected_at,
            )
            if flag is None:
                metrics["matched"] += 1
                continue

            delta = db_pnl - (truth.truth_pnl_usdc if truth.truth_pnl_usdc is not None else 0.0)
            try:
                inserted = await _upsert_divergence(
                    conn,
                    trade=trade,
                    detected_at=detected_at,
                    truth=truth,
                    delta=delta,
                    flag=flag,
                    notes=notes,
                )
            except Exception as exc:
                logger.warning(
                    f"reconciliation: UPSERT failed for trade #{trade['id']}: {exc}"
                )
                continue
            if inserted:
                metrics["divergences_inserted"] += 1
            else:
                metrics["divergences_updated"] += 1
            metrics["by_flag"][flag] = metrics["by_flag"].get(flag, 0) + 1
            if flag == "still_open_in_reality":
                metrics["still_open_in_reality"] += 1

            top_entry = {
                "trade_id": int(trade["id"]),
                "flag": flag,
                "delta": float(delta),
                "market_id": trade["market_id"],
                "direction": trade["direction"],
            }
            top_worst.append((abs(float(delta)), top_entry))

    metrics["run_duration_s"] = round(
        asyncio.get_event_loop().time() - started_monotonic, 2
    )

    # Sort + truncate top-3-worst.
    top_worst.sort(key=lambda x: x[0], reverse=True)
    top_3_payload = [entry for _, entry in top_worst[:3]]

    discrepancy = metrics["total_db_pnl"] - metrics["total_truth_pnl"]
    log_fn = (
        logger.warning
        if abs(discrepancy) > ALARMING_DISCREPANCY_USDC
        else logger.info
    )
    log_fn(
        f"reconciliation: run_id={run_id} scanned={metrics['scanned']} "
        f"matched={metrics['matched']} divergences={len(top_worst)} "
        f"(inserted={metrics['divergences_inserted']}, "
        f"updated={metrics['divergences_updated']}) "
        f"gamma_unreachable={metrics['gamma_unreachable']} "
        f"total_db_pnl={metrics['total_db_pnl']:.2f} "
        f"total_truth_pnl={metrics['total_truth_pnl']:.2f} "
        f"discrepancy={discrepancy:+.2f} "
        f"by_flag={metrics['by_flag']} "
        f"duration={metrics['run_duration_s']}s"
    )

    if (
        redis_client is not None
        and metrics["divergences_inserted"] > 0
    ):
        envelope = {
            "type": "reconciliation_nightly",
            "run_id": run_id,
            "scanned": metrics["scanned"],
            "divergences": metrics["by_flag"],
            "total_db_pnl": round(metrics["total_db_pnl"], 2),
            "total_truth_pnl": round(metrics["total_truth_pnl"], 2),
            "discrepancy": round(discrepancy, 2),
            "alarming": abs(discrepancy) > ALARMING_DISCREPANCY_USDC,
            "top_3_worst": top_3_payload,
            "ts": detected_at.isoformat(),
        }
        try:
            await redis_client.publish(
                CHANNEL_PAPER_AUDIT_DIVERGENCE, json.dumps(envelope, default=str)
            )
        except Exception as exc:
            logger.warning(
                f"reconciliation: publish to {CHANNEL_PAPER_AUDIT_DIVERGENCE} "
                f"failed: {exc}"
            )

    return metrics
