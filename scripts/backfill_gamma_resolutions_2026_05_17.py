"""Bulk backfill of Gamma-resolved markets + orphan position closures.

Strategy plan reference:
``docs/autonomous_session_2026_05_17_strategy/01_DATA_OPTIMIZATION_PLAN.md``
Levers A (data harvest) and F (markets hygiene).

The 30-minute maintenance-loop sweeps (``backfill_resolved_outcomes`` and
``close_orphan_resolved_positions``) handle the incremental case but
were never asked to backfill 90 days of history in one go, and the
orphan sweep depends on a running observer (it publishes to Redis and
the in-process PositionTracker subscribes). When the observer was
restarted between WS resolution and the next backfill, the open rows
in ``position_tracker_state`` were stranded — that's the 6,897 rows
this script frees.

What it does (idempotent, parameterised, async asyncpg + aiohttp):

  Lever A — Resolutions
    1. Paginate ``gamma-api.polymarket.com/markets?closed=true&active=false``
       for the last ``--days`` (default 90), ascending by end_date so a
       crash mid-run resumes cleanly the next time.
    2. For each Gamma market: derive outcome ("yes" if outcomePrices[0]>0.5
       else "no"); UPDATE ``markets`` only when the row still claims
       ``active=TRUE`` OR ``resolved_outcome IS NULL`` (idempotency).
    3. For every market we just updated, find rows in
       ``position_tracker_state`` and CLOSE each open position by:
         - INSERT into ``positions_reconstructed`` with
           ``close_method='resolution'`` and per-direction terminal price
           (1.0 if winning direction, 0.0 otherwise);
         - DELETE the matching ``position_tracker_state`` row (atomic with
           the INSERT inside one transaction);
         - PUBLISH the resulting envelope on Redis ``positions:closed``
           so the behavior profiler picks the update up in real time
           and ``leader_profiles.positions_resolved`` advances.

  Lever F — Markets hygiene
    4. ``UPDATE markets SET active=FALSE WHERE end_date < NOW() - INTERVAL
       '1 day' AND active=TRUE`` — sweep stragglers Gamma never reports
       (deleted markets, edge resolutions) so liquidity queries stop
       picking them up.

CLI
---

.. code-block:: bash

    # Local dry-run (no writes, just counts)
    python scripts/backfill_gamma_resolutions_2026_05_17.py --dry-run --days 90

    # Production full-history run
    docker exec polymarket_engine python /app/scripts/backfill_gamma_resolutions_2026_05_17.py --days 90

Idempotent
----------

Re-running on the same window is a no-op: the markets UPDATE filters
out rows that already have ``active=FALSE AND resolved_outcome IS NOT
NULL``; the position close path uses a hot ``shares_remaining > 0``
filter so already-closed rows do not produce duplicate
``positions_reconstructed`` inserts. The Redis publish is best-effort
(failure logs a warning but does not roll back the write).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiohttp
import asyncpg

try:
    # Loguru is the project's structured-logger of choice (CLAUDE.md §10).
    from loguru import logger
except ImportError:  # pragma: no cover — tests stub the project venv
    import logging

    logger = logging.getLogger("backfill_gamma_resolutions")


GAMMA_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "polymarket-bot-backfill-2026-05-17/1.0"
ECONOMIC_MODEL_VERSION = "v1.0.0"
REDIS_POSITIONS_CHANNEL = "positions:closed"
DEFAULT_DAYS = 90
DEFAULT_BATCH_SIZE = 100
HTTP_TIMEOUT_S = 30
# Hard cap on Gamma pages per run — at limit=100 that's 50k markets,
# more than the full Polymarket history. Stops a misbehaving paginator
# from looping forever.
MAX_PAGES = 1000


# --------------------------------------------------------------------------- #
# Result containers                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class BackfillSummary:
    """Accumulator returned from the top-level run.

    Every counter is incremented under the orchestration loop so the
    dry-run path and the real path produce structurally identical
    summaries — the difference is only in whether the underlying
    UPDATE/INSERT/DELETE ran.
    """

    markets_seen: int = 0
    markets_updated: int = 0
    positions_closed: int = 0
    leaders_affected: set[str] = field(default_factory=set)
    publish_failures: int = 0
    expired_marked_inactive: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets_seen": self.markets_seen,
            "markets_updated": self.markets_updated,
            "positions_closed": self.positions_closed,
            "leaders_affected": len(self.leaders_affected),
            "publish_failures": self.publish_failures,
            "expired_marked_inactive": self.expired_marked_inactive,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# Gamma parsing helpers                                                        #
# --------------------------------------------------------------------------- #


def derive_outcome(market: dict[str, Any]) -> str | None:
    """Return ``"yes"`` / ``"no"`` for a Gamma market dict, or ``None``
    when the payload doesn't carry usable terminal prices.

    Gamma sometimes ships ``outcomePrices`` as a JSON-encoded string
    (legacy) rather than a list (current). Both shapes are accepted.
    Index 0 is YES by Polymarket convention; ``> 0.5`` is the
    binary-resolution threshold (a fractional terminal value never
    happens on binary markets but the gate is defensive against fuzz).
    """
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        with suppress(Exception):
            prices = json.loads(prices)
    if not isinstance(prices, (list, tuple)) or len(prices) < 1:
        return None
    try:
        yes_terminal = float(prices[0])
    except (TypeError, ValueError):
        return None
    return "yes" if yes_terminal > 0.5 else "no"


def market_condition_id(market: dict[str, Any]) -> str | None:
    """Read the ``condition_id`` (our ``market_id``) from a Gamma row.

    Gamma uses ``conditionId`` (camelCase) but some legacy responses
    still ship ``condition_id``; we accept both for forward-compat.
    """
    cid = market.get("conditionId") or market.get("condition_id")
    if cid is None:
        return None
    cid = str(cid).strip()
    return cid or None


async def _fetch_gamma_page(
    session: aiohttp.ClientSession,
    *,
    offset: int,
    limit: int,
    days: int,
) -> list[dict[str, Any]]:
    """Single Gamma page; returns ``[]`` on any HTTP / parse failure so
    the orchestrator can log + skip without aborting the whole run.

    ``order=endDate&ascending=false`` returns the newest end_date first
    so the orchestrator can stop paginating once an entire page falls
    outside the ``--days`` cutoff. (Gamma uses camelCase ``endDate``;
    ``end_date`` is silently accepted-but-ignored and reverts to the
    default ID order — verified 2026-05-17 by curl.)
    """
    params = {
        "limit": limit,
        "offset": offset,
        "closed": "true",
        "active": "false",
        "order": "endDate",
        "ascending": "false",
    }
    try:
        async with session.get(
            GAMMA_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    f"Gamma page offset={offset} returned HTTP {resp.status}"
                )
                return []
            payload = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning(f"Gamma page offset={offset} failed: {exc}")
        return []
    if not isinstance(payload, list):
        return []
    return payload


# --------------------------------------------------------------------------- #
# DB write helpers                                                             #
# --------------------------------------------------------------------------- #


async def update_market_resolution(
    conn: asyncpg.Connection,
    *,
    market_id: str,
    outcome: str,
) -> bool:
    """Atomic, idempotent UPDATE on ``markets``. Returns True when the
    UPDATE actually changed a row (i.e. the market was active OR had no
    resolved_outcome yet), False otherwise.

    This filter is the cornerstone of idempotency — a second pass over
    the same Gamma window will hit ``UPDATE 0`` for every market that
    was already settled, and the orchestrator will skip the position
    close path for those markets.
    """
    res = await conn.execute(
        """
        UPDATE markets
        SET active = FALSE,
            resolved_outcome = $2::varchar,
            updated_at = NOW()
        WHERE market_id = $1::varchar
          AND (active = TRUE OR resolved_outcome IS NULL)
        """,
        market_id,
        outcome,
    )
    # asyncpg returns "UPDATE N" — N==0 means no row touched.
    try:
        return int(res.split()[-1]) > 0
    except (IndexError, ValueError):
        return False


async def fetch_open_positions(
    conn: asyncpg.Connection, market_id: str,
) -> list[asyncpg.Record]:
    """Return rows in ``position_tracker_state`` still open on this market.

    We deliberately SELECT only what the close path needs (no
    ``state_json``, no ``updated_at``) so the I/O cost is proportional
    to the leader set, not the table width.
    """
    return await conn.fetch(
        """
        SELECT wallet_address, token_id, direction, open_time,
               entry_price, size_usdc, size_shares, shares_remaining,
               fee_rate_pct
        FROM position_tracker_state
        WHERE market_id = $1::varchar
          AND shares_remaining > 0
        """,
        market_id,
    )


async def _market_category(
    conn: asyncpg.Connection, market_id: str,
) -> str:
    """Denormalised category for the positions_reconstructed insert.

    We mirror what ``observer.position_tracker._close_position`` writes
    so backfilled rows are indistinguishable from live-closed ones.
    A missing market or NULL category falls back to ``"unknown"`` —
    matching the live tracker.
    """
    row = await conn.fetchrow(
        "SELECT category FROM markets WHERE market_id = $1::varchar",
        market_id,
    )
    if row and row["category"]:
        return str(row["category"])
    return "unknown"


def compute_pnl(
    *,
    direction: str,
    outcome: str,
    entry_price: Decimal,
    size_usdc: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate (exit_price, pnl_usdc, pnl_pct) for a resolution close.

    Polymarket binary-token math (the simple form the strategy plan
    asks for: ``shares = size_usdc / entry_price``, ``pnl =
    shares * (exit - entry)``). We DO NOT model fees in this script —
    Gamma-resolved positions are months old, and the strategy plan
    explicitly trades fee fidelity for backfill throughput. Fee-aware
    accounting remains the live-tracker contract; backfill rows are
    flagged via ``close_method='resolution'`` so the profiler treats
    them the same way.
    """
    exit_price = Decimal("1.0") if direction == outcome else Decimal("0.0")
    if entry_price <= 0:
        # Defensive: division by zero would crash the orchestrator on a
        # malformed state row. The pnl in that case can't be reconstructed
        # so we return 0 and let the audit pick it up.
        return exit_price, Decimal("0"), Decimal("0")
    shares = size_usdc / entry_price
    pnl_usdc = shares * (exit_price - entry_price)
    # pnl_pct relative to the entry notional ([-1, 1] range for binary).
    pnl_pct = (exit_price - entry_price) / entry_price
    return exit_price, pnl_usdc, pnl_pct


async def close_position(
    conn: asyncpg.Connection,
    *,
    market_id: str,
    row: asyncpg.Record,
    outcome: str,
    category: str,
    close_time: datetime,
) -> Decimal:
    """Atomic INSERT-positions_reconstructed + DELETE-state.

    Wrapped in a single transaction inside the caller (we don't open
    one here so the caller can compose multiple closures for the same
    market into one logical unit — matches the live tracker's
    transaction boundary in ``_close_position``).

    Returns the realized ``pnl_usdc`` (Decimal) so the orchestrator can
    populate the Redis publish envelope without re-deriving it.
    """
    direction = str(row["direction"])
    entry_price = Decimal(str(row["entry_price"]))
    size_usdc = Decimal(str(row["size_usdc"]))
    shares_remaining = Decimal(str(row["shares_remaining"]))
    exit_price, pnl_usdc, pnl_pct = compute_pnl(
        direction=direction,
        outcome=outcome,
        entry_price=entry_price,
        size_usdc=size_usdc,
    )
    open_time = row["open_time"]
    holding_s = max(0, int((close_time - open_time).total_seconds()))

    await conn.execute(
        """
        INSERT INTO positions_reconstructed
            (wallet_address, market_id, token_id, direction,
             open_time, close_time, entry_price, exit_price,
             size_usdc, pnl_usdc, pnl_pct, holding_period_s,
             close_method, size_shares,
             entry_fee_usdc, exit_fee_usdc,
             gross_pnl_usdc, net_pnl_usdc,
             economic_model_version, category)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                $15,$16,$17,$18,$19,$20)
        """,
        row["wallet_address"],
        market_id,
        row["token_id"],
        direction,
        open_time,
        close_time,
        entry_price,
        exit_price,
        size_usdc,
        round(pnl_usdc, 2),
        round(pnl_pct, 4),
        holding_s,
        "resolution",
        shares_remaining,
        Decimal("0"),
        Decimal("0"),
        round(pnl_usdc, 2),  # gross == net when fees=0
        round(pnl_usdc, 2),
        ECONOMIC_MODEL_VERSION,
        category,
    )
    await conn.execute(
        """
        DELETE FROM position_tracker_state
        WHERE wallet_address = $1::varchar
          AND market_id      = $2::varchar
          AND token_id       = $3::varchar
          AND direction      = $4::varchar
        """,
        row["wallet_address"],
        market_id,
        row["token_id"],
        direction,
    )
    return pnl_usdc


async def publish_close_event(
    redis_client,
    *,
    market_id: str,
    row: asyncpg.Record,
    pnl_usdc: Decimal,
    outcome: str,
    category: str,
    close_time: datetime,
) -> bool:
    """Mirror the live tracker's ``positions:closed`` envelope shape.

    The behavior_profiler subscribes here and updates the per-leader
    Beta posterior + the ``leader_profiles.positions_resolved`` counter
    — the whole point of this script is to bump that counter, so a
    publish failure is loud (returns False; the orchestrator counts it)
    but does NOT roll back the DB write (we'd rather over-write the
    posterior on a re-run than lose the close).
    """
    direction = str(row["direction"])
    entry_price = Decimal(str(row["entry_price"]))
    size_usdc = Decimal(str(row["size_usdc"]))
    shares_remaining = Decimal(str(row["shares_remaining"]))
    exit_price = Decimal("1.0") if direction == outcome else Decimal("0.0")
    open_time = row["open_time"]
    holding_s = max(0, int((close_time - open_time).total_seconds()))
    event = {
        "wallet_address": row["wallet_address"],
        "market_id": market_id,
        "token_id": row["token_id"],
        "direction": direction,
        "open_time": open_time.isoformat() if hasattr(open_time, "isoformat")
                     else str(open_time),
        "close_time": close_time.isoformat(),
        "pnl_usdc": str(round(pnl_usdc, 2)),
        "gross_pnl_usdc": str(round(pnl_usdc, 2)),
        "category": category,
        "size_usdc": str(size_usdc),
        "size_shares": str(shares_remaining),
        "entry_price": str(entry_price),
        "exit_price": str(exit_price),
        "economic_model_version": ECONOMIC_MODEL_VERSION,
        "holding_period_s": holding_s,
        "is_contrarian": False,
        "close_method": "resolution",
        "source": "backfill_gamma_resolutions_2026_05_17",
    }
    try:
        await redis_client.publish(REDIS_POSITIONS_CHANNEL, json.dumps(event))
        return True
    except Exception as exc:
        logger.warning(
            f"Redis publish failed for wallet={row['wallet_address']} "
            f"market={market_id}: {exc}"
        )
        return False


async def sweep_expired_active_markets(
    conn: asyncpg.Connection,
) -> int:
    """Lever F: mark expired markets inactive.

    Targets the 4,518-row pool of markets whose ``end_date`` slipped
    past ``NOW() - 1 day`` but never had their ``active`` flag flipped
    (Gamma sometimes drops resolved markets before they show up in the
    closed-list endpoint). Returns the number of rows updated.
    """
    res = await conn.execute(
        """
        UPDATE markets
        SET active = FALSE,
            updated_at = NOW()
        WHERE end_date < NOW() - INTERVAL '1 day'
          AND active = TRUE
        """
    )
    try:
        return int(res.split()[-1])
    except (IndexError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Orchestrator                                                                 #
# --------------------------------------------------------------------------- #


async def _process_market(
    pool: asyncpg.Pool,
    redis_client,
    *,
    market: dict[str, Any],
    summary: BackfillSummary,
    dry_run: bool,
) -> None:
    """One Gamma row: derive outcome, update markets, close orphans.

    Per-market exceptions are caught + recorded so a single malformed
    row never blocks the rest of the batch — the script processes
    thousands of markets per run and a stray TypeError on one of them
    is normal noise, not a fatal error.
    """
    cid = market_condition_id(market)
    if not cid:
        return
    outcome = derive_outcome(market)
    if outcome is None:
        return
    summary.markets_seen += 1

    if dry_run:
        # Count what we would do without writing.
        async with pool.acquire() as conn:
            already_settled = await conn.fetchval(
                """
                SELECT 1 FROM markets
                WHERE market_id = $1::varchar
                  AND active = FALSE
                  AND resolved_outcome IS NOT NULL
                LIMIT 1
                """,
                cid,
            )
            if not already_settled:
                summary.markets_updated += 1
            opens = await fetch_open_positions(conn, cid)
            for row in opens:
                summary.positions_closed += 1
                summary.leaders_affected.add(str(row["wallet_address"]))
        return

    close_time = datetime.now(tz=timezone.utc)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                changed = await update_market_resolution(
                    conn, market_id=cid, outcome=outcome
                )
                if not changed:
                    # Idempotent fast-exit: the market was already
                    # settled in a prior run. Skip the close path
                    # too — its row in position_tracker_state has
                    # already been DELETEd.
                    return
                summary.markets_updated += 1
                category = await _market_category(conn, cid)
                opens = await fetch_open_positions(conn, cid)

                publish_payloads: list[tuple[asyncpg.Record, Decimal]] = []
                for row in opens:
                    pnl = await close_position(
                        conn,
                        market_id=cid,
                        row=row,
                        outcome=outcome,
                        category=category,
                        close_time=close_time,
                    )
                    summary.positions_closed += 1
                    summary.leaders_affected.add(str(row["wallet_address"]))
                    publish_payloads.append((row, pnl))
    except Exception as exc:
        summary.errors.append(f"{cid}: {type(exc).__name__}: {exc}")
        logger.error(f"market={cid} processing failed: {exc}")
        return

    # Redis publish AFTER the DB transaction commits — the profiler
    # subscriber re-reads from DB on receipt, so an early publish on
    # a doomed transaction would race against the rollback. Failure
    # to publish doesn't roll back the close (the DB state is still
    # correct; the profiler picks the update up at the next
    # ``reconcile_profiles`` maintenance sweep).
    for row, pnl in publish_payloads:
        ok = await publish_close_event(
            redis_client,
            market_id=cid,
            row=row,
            pnl_usdc=pnl,
            outcome=outcome,
            category=str(market.get("category") or "unknown"),
            close_time=close_time,
        )
        if not ok:
            summary.publish_failures += 1


async def run_backfill(
    *,
    pool: asyncpg.Pool,
    redis_client,
    session: aiohttp.ClientSession,
    days: int = DEFAULT_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> BackfillSummary:
    """End-to-end orchestrator (split out so tests can drive it with
    fake pool/redis/session and assert summary counters without
    spinning up real infra)."""
    summary = BackfillSummary()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    logger.info(
        f"backfill_gamma_resolutions: days={days} batch_size={batch_size} "
        f"dry_run={dry_run} cutoff={cutoff.isoformat()}"
    )

    offset = 0
    for page_idx in range(MAX_PAGES):
        markets = await _fetch_gamma_page(
            session, offset=offset, limit=batch_size, days=days,
        )
        if not markets:
            break
        # Walk markets in this page; the orchestrator filters per-row
        # so a single off-window market doesn't truncate the page.
        in_window_count = 0
        for market in markets:
            end_date_raw = market.get("endDate") or market.get("end_date")
            end_date = _parse_iso_ts(end_date_raw)
            # Strict cutoff filter: only act on markets that ended in
            # the requested window. Older markets are skipped silently;
            # the loop continues so a window straddle doesn't cut off
            # a still-relevant in-window row later in the page.
            if end_date is not None and end_date < cutoff:
                continue
            in_window_count += 1
            await _process_market(
                pool, redis_client,
                market=market, summary=summary, dry_run=dry_run,
            )
        logger.info(
            f"page={page_idx} offset={offset} "
            f"received={len(markets)} in_window={in_window_count} "
            f"markets_updated={summary.markets_updated} "
            f"positions_closed={summary.positions_closed}"
        )
        # Pagination terminator: a short page means we've hit the end
        # of the closed-market list. We also bail if the entire page
        # was off-window — the ordering guarantees nothing in-window
        # follows in pagination order.
        if len(markets) < batch_size or in_window_count == 0:
            break
        offset += batch_size

    # Lever F runs regardless of dry-run flag — in dry-run we report
    # the would-be count via SELECT only.
    if dry_run:
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM markets
                WHERE end_date < NOW() - INTERVAL '1 day'
                  AND active = TRUE
                """
            )
        summary.expired_marked_inactive = int(count or 0)
    else:
        async with pool.acquire() as conn:
            summary.expired_marked_inactive = await sweep_expired_active_markets(conn)

    logger.info(f"backfill_gamma_resolutions: summary={summary.as_dict()}")
    return summary


def _parse_iso_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-backfill Gamma-resolved markets and close orphan "
            "position_tracker_state rows. See module docstring."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help="Lookback window in days (default: 90)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help="Gamma page size (default: 100)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count would-be writes without modifying any tables.",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var not set; aborting.")
        return 2
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

    # Local import so a stripped-down test environment without redis
    # installed can still import the module (tests stub redis directly).
    import redis.asyncio as redis_async

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, command_timeout=120)
    redis_client = redis_async.from_url(redis_url, decode_responses=True)
    session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    try:
        summary = await run_backfill(
            pool=pool,
            redis_client=redis_client,
            session=session,
            days=args.days,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    finally:
        await session.close()
        await redis_client.aclose()
        await pool.close()

    # Final summary line in a stable, machine-readable shape so ops
    # can grep it out of container logs.
    print(json.dumps({"summary": summary.as_dict()}, default=str), flush=True)
    return 0 if not summary.errors else 1


def main() -> None:
    args = _build_arg_parser().parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
