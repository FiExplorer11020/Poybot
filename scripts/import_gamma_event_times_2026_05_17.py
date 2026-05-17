"""Enrich `markets` with the Gamma `gameStartTime` field.

Strategy plan reference:
``docs/autonomous_session_2026_05_17_strategy/02_STRUCTURAL_FIX_PLAN.md``
Tier 1 fix #1 — Gamma event_start_time enrichment.

WHY
---
`markets.end_date` is the *dispute window* expiration, not the moment
the underlying event resolves. For sport markets the dispute window
runs 7+ days AFTER the actual match. The 6h MIN_HOURS_TO_RESOLUTION
gate passes "Punjab Kings vs RCB" (end_date = 2026-05-24, +169h) even
though the match starts within minutes — and the bot loses -97% in 3h.

WHAT
----
For every `markets WHERE active=TRUE AND category='sports'`, fetch the
Gamma metadata, pull the `gameStartTime` field (top-level, the truth
for live matches), parse to UTC, populate the three new columns from
migration 047:

  event_start_time   ← `gameStartTime` (preferred) or `events[0].startDate`
                       when gameStartTime is NULL but the event row
                       still carries a usable timestamp (cold futures
                       like "Carolina Hurricanes win Stanley Cup" keep
                       NULL — their event start is unknowable and they
                       are NOT live-match candidates anyway).
  event_end_time     ← gameStartTime + 4h projected wall (covers the
                       longest binary sport markets — cricket T20,
                       basketball, hockey, soccer). Used as the
                       contract `resolves-by` time, NOT the dispute
                       window.
  is_live_match      ← TRUE iff event_start_time within ±2h of NOW.
                       This is the hot-path gate the confidence engine
                       reads via the partial index from migration 047.
  event_metadata_source ← 'gamma:gameStartTime' / 'gamma:event.startDate'
                       / 'gamma:absent' so we can audit which Gamma
                       field populated each row.

HOW (operational contract)
--------------------------
* Async, asyncpg, aiohttp, pydantic for the Gamma payload validator
* Parameterised SQL only (CLAUDE.md §10)
* Bounded concurrency on the Gamma fetch (15 in-flight) so we don't
  flood the Polymarket API with 2.5k concurrent requests
* Idempotent: a re-run replays the same UPDATE — the `is_live_match`
  flag recomputes from the live wall-clock so a row that was True
  yesterday flips False today automatically
* Single-shot CLI: ``--dry-run`` reports what would change without
  writing.

EXIT
----
    {
      "markets_scanned": N,
      "rows_populated": N,
      "live_match_count": N,
      "gamma_misses": N,
      "errors": N
    }

CLI
---

.. code-block:: bash

    # Dry run
    python scripts/import_gamma_event_times_2026_05_17.py --dry-run

    # Prod
    docker exec polymarket_engine python /app/scripts/import_gamma_event_times_2026_05_17.py
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
from typing import Any

import aiohttp
import asyncpg
from pydantic import BaseModel, ConfigDict, Field

try:
    from loguru import logger
except ImportError:  # pragma: no cover — tests stub
    import logging

    logger = logging.getLogger("import_gamma_event_times")


GAMMA_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "polymarket-bot-event-times-2026-05-17/1.0"
HTTP_TIMEOUT_S = 15
# Hard cap on concurrent Gamma requests. The endpoint tolerates more
# but we share quota with the maintenance loop's refresh + the
# backfill script — 15 is the documented co-existence ceiling.
GAMMA_CONCURRENCY = 15
# A "live match" is a market whose event starts within ±this window
# of wall-clock NOW. ±2h captures matches that just started AND
# matches scheduled in the next 2 hours (we don't want to FOLLOW
# either: too late for the first, too short a window for the second).
LIVE_MATCH_WINDOW = timedelta(hours=2)
# Projected event duration for the synthetic event_end_time. Cricket
# T20 ≈ 3.5h, basketball/hockey ≈ 2.5h, soccer ≈ 2h. 4h gives us a
# safe upper bound for binary sport markets.
EVENT_DURATION = timedelta(hours=4)


# --------------------------------------------------------------------------- #
# Pydantic models — Gamma payload (CLAUDE.md §10: validate at boundaries)     #
# --------------------------------------------------------------------------- #


class GammaEvent(BaseModel):
    """Inner event object inside Gamma's `events[]` array.

    We only pull the timestamps; everything else is ignored so the
    validator stays cheap and forward-compatible with Gamma adding
    new keys.
    """

    model_config = ConfigDict(extra="ignore")

    startDate: str | None = None
    endDate: str | None = None


class GammaMarket(BaseModel):
    """Top-level Gamma market entity, narrow shape for this script."""

    model_config = ConfigDict(extra="ignore")

    conditionId: str | None = None
    condition_id: str | None = None  # legacy snake_case fallback
    gameStartTime: str | None = None
    startDate: str | None = None
    endDate: str | None = None
    events: list[GammaEvent] = Field(default_factory=list)

    def market_id(self) -> str | None:
        cid = self.conditionId or self.condition_id
        if not cid:
            return None
        cid = cid.strip()
        return cid or None


# --------------------------------------------------------------------------- #
# Result container                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class ImportSummary:
    markets_scanned: int = 0
    rows_populated: int = 0
    live_match_count: int = 0
    gamma_misses: int = 0
    errors: list[str] = field(default_factory=list)
    # Source breakdown — operator visibility on which field actually
    # carried the time for each enriched row. Helps spot Gamma schema
    # drift (e.g. gameStartTime suddenly going NULL on cricket).
    source_breakdown: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets_scanned": self.markets_scanned,
            "rows_populated": self.rows_populated,
            "live_match_count": self.live_match_count,
            "gamma_misses": self.gamma_misses,
            "errors_count": len(self.errors),
            "errors": list(self.errors[:10]),  # cap to keep log lines bounded
            "source_breakdown": dict(self.source_breakdown),
        }


# --------------------------------------------------------------------------- #
# Parsing helpers                                                              #
# --------------------------------------------------------------------------- #


def _parse_iso_ts(raw: Any) -> datetime | None:
    """Tolerant ISO-8601 / `YYYY-MM-DD HH:MM:SS+00` parser.

    Gamma's `gameStartTime` ships in two shapes seen in the wild:
      * ``"2026-05-17 10:00:00+00"`` (space separator, +00 offset)
      * ``"2026-05-17T10:00:00Z"`` (standard ISO)

    Returns None on any failure so the caller can mark the row as a
    Gamma miss without crashing.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    # Space → 'T' so fromisoformat accepts it on 3.10 (3.11 is more
    # lenient but we keep the normalisation for back-compat).
    s = s.replace(" ", "T", 1)
    # 'Z' → '+00:00' (fromisoformat needs offset form).
    s = s.replace("Z", "+00:00")
    # Gamma's "+00" without minutes — append ':00' so fromisoformat
    # is happy. Only do this when the trailing component is exactly
    # ±HH (no minutes) to avoid mangling already-valid offsets.
    if len(s) >= 3 and s[-3] in ("+", "-") and s[-2:].isdigit():
        s = s + ":00"
    try:
        ts = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def extract_event_start(
    market: GammaMarket,
) -> tuple[datetime | None, str]:
    """Pull the best available event-start timestamp from a Gamma row.

    Precedence (matches the docstring contract):
      1. Top-level ``gameStartTime`` — the truth for live matches.
      2. ``events[0].startDate`` — fallback for sport markets where
         Gamma populated the event row but not the top-level field.
         Skipped when it's clearly the market-creation timestamp
         (i.e. > 7 days before the market's own endDate — sport
         events are scheduled <= a few weeks ahead).
      3. None — the market is a long-dated future. Caller stamps
         ``'gamma:absent'`` and leaves event_start_time NULL.

    Returns (timestamp_or_None, source_tag).
    """
    ts = _parse_iso_ts(market.gameStartTime)
    if ts is not None:
        return ts, "gamma:gameStartTime"

    # Fallback: events[0].startDate. We only trust it if the event end
    # is close to the start — otherwise it's a season-long futures
    # event ("2026 NHL Stanley Cup", events[0].startDate = 2025-06-23,
    # events[0].endDate = 2026-06-30) which is NOT a live match.
    if market.events:
        ev = market.events[0]
        ev_start = _parse_iso_ts(ev.startDate)
        ev_end = _parse_iso_ts(ev.endDate)
        if ev_start is not None and ev_end is not None:
            duration = ev_end - ev_start
            # A 'real' match event has duration < 14 days (covers
            # multi-day tournaments). Anything wider is a futures
            # series and stays NULL — the confidence engine treats
            # NULL as "non-live" which is correct for futures.
            if duration < timedelta(days=14):
                return ev_start, "gamma:event.startDate"

    return None, "gamma:absent"


def compute_event_end(
    event_start: datetime | None,
    market: GammaMarket,
) -> datetime | None:
    """Project event_end_time from event_start.

    For live matches we use `event_start + EVENT_DURATION` (4h) as a
    safe upper bound for binary sport markets. We deliberately do NOT
    use `markets.end_date` — that's the dispute window expiration
    which is the very thing this script exists to bypass.

    Falls back to `events[0].endDate` only when the duration looks
    reasonable (< 14 days from event_start, same heuristic as the
    futures detector in extract_event_start).
    """
    if event_start is None:
        return None
    # Prefer the Gamma-provided event end IF it's a tight match
    # window — for sport markets this matches reality better than
    # the synthetic +4h.
    if market.events:
        ev_end = _parse_iso_ts(market.events[0].endDate)
        if ev_end is not None and ev_end > event_start:
            if ev_end - event_start < timedelta(days=14):
                # Use the tighter of (Gamma event end, +4h projection)
                # — Gamma is sometimes generous for daily-match
                # contracts (24h window for a 3h match).
                projected = event_start + EVENT_DURATION
                return min(ev_end, projected) if ev_end > projected else ev_end
    return event_start + EVENT_DURATION


def compute_is_live_match(
    event_start: datetime | None,
    now: datetime,
    window: timedelta = LIVE_MATCH_WINDOW,
) -> bool:
    """`is_live_match` = event_start within ±`window` of `now`.

    Symmetric window: a match that started 90 minutes ago is still
    in-play; a match scheduled in 90 minutes is too close to FOLLOW
    safely (paper trader latency + leader scalp timing). Both flip
    to True.

    NULL `event_start` → False (the confidence engine treats unknown
    as non-live — safe by default for futures markets).
    """
    if event_start is None:
        return False
    delta = abs(event_start - now)
    return delta <= window


# --------------------------------------------------------------------------- #
# Gamma fetch helpers                                                          #
# --------------------------------------------------------------------------- #


async def fetch_gamma_for_market(
    session: aiohttp.ClientSession,
    condition_id: str,
) -> GammaMarket | None:
    """Fetch the Gamma row for a single market_id (a.k.a. conditionId).

    Returns None on any failure — the orchestrator counts a None as a
    `gamma_miss` and keeps going. We deliberately do NOT raise: one
    failed market should never block the other 2,499 in the sweep.
    """
    params = {"condition_ids": condition_id}
    try:
        async with session.get(
            GAMMA_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    if not isinstance(payload, list) or not payload:
        return None
    try:
        return GammaMarket.model_validate(payload[0])
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# DB write helpers                                                             #
# --------------------------------------------------------------------------- #


async def fetch_target_markets(
    conn: asyncpg.Connection,
    *,
    category: str = "sports",
) -> list[asyncpg.Record]:
    """Return the active sport markets that need enrichment.

    Restricted to category='sports' per the strategy plan (sports are
    the cohort that lost -97% on 2026-05-17). The orchestrator can be
    re-invoked with `--category=esports` etc. once the sport rollout
    is verified, but this script's default is sports-only.

    We deliberately include rows that already have event_start_time
    populated — the `is_live_match` flag must be re-computed every
    run against the current wall-clock (a match that was live an
    hour ago isn't anymore), so an UPDATE is always cheaper than the
    branching skip-already-populated logic.
    """
    return await conn.fetch(
        """
        SELECT market_id, question, end_date
        FROM markets
        WHERE active = TRUE
          AND category = $1::varchar
        ORDER BY end_date NULLS LAST
        """,
        category,
    )


async def update_event_times(
    conn: asyncpg.Connection,
    *,
    market_id: str,
    event_start: datetime | None,
    event_end: datetime | None,
    is_live: bool,
    source: str,
) -> bool:
    """Atomic UPDATE — returns True iff a row was actually changed.

    Always writes is_live_match + event_metadata_source. event_start
    and event_end are COALESCEd against the existing values so a
    re-run with a Gamma miss doesn't blow away a prior valid time.
    """
    res = await conn.execute(
        """
        UPDATE markets
        SET event_start_time      = COALESCE($2::timestamptz, event_start_time),
            event_end_time        = COALESCE($3::timestamptz, event_end_time),
            is_live_match         = $4::boolean,
            event_metadata_source = $5::varchar,
            updated_at            = NOW()
        WHERE market_id = $1::varchar
        """,
        market_id,
        event_start,
        event_end,
        is_live,
        source,
    )
    try:
        return int(res.split()[-1]) > 0
    except (IndexError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Orchestrator                                                                 #
# --------------------------------------------------------------------------- #


async def _enrich_one(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    *,
    market_id: str,
    now: datetime,
    summary: ImportSummary,
    dry_run: bool,
) -> None:
    """Single-market enrichment path. Bounded by `sem` so the sweep
    of 2.5k markets doesn't open 2.5k sockets at once."""
    async with sem:
        try:
            gamma = await fetch_gamma_for_market(session, market_id)
        except Exception as exc:
            summary.errors.append(f"{market_id}: gamma fetch failed: {exc}")
            return

    if gamma is None:
        summary.gamma_misses += 1
        return

    event_start, source = extract_event_start(gamma)
    event_end = compute_event_end(event_start, gamma)
    is_live = compute_is_live_match(event_start, now)

    summary.source_breakdown[source] = summary.source_breakdown.get(source, 0) + 1
    if is_live:
        summary.live_match_count += 1

    if dry_run:
        # Count as if we would have written; the dry-run is what the
        # operator runs before the prod sweep so the numbers must
        # line up with the real-run output.
        summary.rows_populated += 1
        return

    try:
        async with pool.acquire() as conn:
            changed = await update_event_times(
                conn,
                market_id=market_id,
                event_start=event_start,
                event_end=event_end,
                is_live=is_live,
                source=source,
            )
            if changed:
                summary.rows_populated += 1
    except Exception as exc:
        summary.errors.append(f"{market_id}: update failed: {exc}")


async def run_import(
    *,
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    category: str = "sports",
    dry_run: bool = False,
    now: datetime | None = None,
) -> ImportSummary:
    """End-to-end orchestrator. Tests drive this directly with stubs."""
    summary = ImportSummary()
    if now is None:
        now = datetime.now(tz=timezone.utc)

    async with pool.acquire() as conn:
        targets = await fetch_target_markets(conn, category=category)

    summary.markets_scanned = len(targets)
    if not targets:
        logger.info(f"import_gamma_event_times: 0 markets in category={category}")
        return summary

    logger.info(
        f"import_gamma_event_times: enriching {summary.markets_scanned} "
        f"category={category} markets (dry_run={dry_run})"
    )

    sem = asyncio.Semaphore(GAMMA_CONCURRENCY)
    tasks = [
        _enrich_one(
            pool, session, sem,
            market_id=str(row["market_id"]),
            now=now,
            summary=summary,
            dry_run=dry_run,
        )
        for row in targets
    ]
    # Use gather with return_exceptions so a single coroutine raising
    # an unexpected error doesn't cancel the rest of the sweep.
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(f"import_gamma_event_times: summary={summary.as_dict()}")
    return summary


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich markets.event_start_time / event_end_time / "
            "is_live_match from the Gamma `gameStartTime` field. "
            "See module docstring."
        )
    )
    parser.add_argument(
        "--category", default="sports",
        help="markets.category to sweep (default: sports)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count would-be writes without modifying any rows.",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var not set; aborting.")
        return 2

    pool = await asyncpg.create_pool(
        db_url, min_size=1, max_size=4, command_timeout=60,
    )
    session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    try:
        summary = await run_import(
            pool=pool,
            session=session,
            category=args.category,
            dry_run=args.dry_run,
        )
    finally:
        await session.close()
        await pool.close()

    print(json.dumps({"summary": summary.as_dict()}, default=str), flush=True)
    return 0 if not summary.errors else 1


def main() -> None:
    args = _build_arg_parser().parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
