"""Autonomous maintenance loop — keeps the trade pipeline operational.

Runs forever, executing these jobs on a schedule:
  - every 60 min: bootstrap fee_snapshots from markets + Gamma refresh
  - every 60 min: refresh markets.end_date + volume_24h from Gamma
  - every 10 min: leader_profiles.trades_observed reconciliation
  - every 10 min: close orphan open positions for resolved markets
  - every 30 min: backfill markets.resolved_outcome from Gamma
  - every 6 hours: rebuild follower_edges (full graph)
  - every 6 hours: auto-promote follower-rich leaders to on_watchlist
  - every 30 min: book:last cache refresh for top liquid markets

Designed as a long-running container/daemon. Stop with SIGTERM.

This script is the SAFETY NET for known stale-data failure modes:
  - markets.end_date stays current (was NULL silently for all rows)
  - fee_snapshots stays fresh (gate requires < 24h)
  - follower_edges stays populated (was being wiped on engine restart)
  - position_tracker_state never holds open rows for markets that
    actually resolved (would otherwise lock per-direction terminal
    PnL out of positions_reconstructed)
  - high-influence leaders flip to ``on_watchlist`` once the follower
    graph confirms them, even if Falcon never picked them up

Idempotent and safe to run alongside the live engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone

import aiohttp
import asyncpg
import redis.asyncio as redis_async

from src.config import settings

# Optional dependency: snapshot_builder is delivered by Agent A in
# parallel. Guard the import so the rest of the maintenance loop keeps
# running even before that module lands. The live_summary job is gated
# on _HAS_SNAPSHOT_BUILDER and silently skipped when the import fails.
try:
    from src.api.snapshot_builder import build_terminal_snapshot
    _HAS_SNAPSHOT_BUILDER = True
except ImportError:
    _HAS_SNAPSHOT_BUILDER = False

DB_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "polymarket-leader-bot-maintenance/1.0"

# Job intervals (seconds)
FEE_BOOTSTRAP_INTERVAL_S = 3600           # 1 h
GAMMA_REFRESH_INTERVAL_S = 3600           # 1 h
PROFILES_RECONCILE_INTERVAL_S = 600       # 10 min
GRAPH_REBUILD_INTERVAL_S = 21600          # 6 h
BOOK_CACHE_REFRESH_INTERVAL_S = 120       # 2 min — must be < BOOK_CACHE_TTL_S
BOOK_CACHE_TTL_S = 240                    # 4 min TTL — paper_trader has its own
                                          # 60s staleness gate; this is defense
                                          # in depth so a market dropped from
                                          # the refresh query (resolved or
                                          # low-volume) ages out 5x faster.
STREAM_TRIM_INTERVAL_S = 300              # 5 min
RESOLUTION_BACKFILL_INTERVAL_S = 1800     # 30 min — backfill markets.resolved_outcome
                                          # from Gamma closed-market endpoint so
                                          # paper_trader can close resolved
                                          # positions at terminal value instead
                                          # of deferring indefinitely.
ORPHAN_CLOSE_INTERVAL_S = 600             # 10 min — sweep position_tracker_state
                                          # for opens on markets Gamma reports
                                          # closed and publish market_resolved
                                          # envelopes so the in-process tracker
                                          # closes them at terminal value.
PROMOTE_WATCHLIST_INTERVAL_S = 21600      # 6 h — auto-promote leaders with ≥5
                                          # confirmed follower edges to
                                          # on_watchlist so the observer's
                                          # bootstrap UNION picks them up.
FULL_BACKFILL_INTERVAL_S = 21600          # 6 h — Lever A aggressive sweep. The
                                          # 30-min ORPHAN_CLOSE job only publishes
                                          # Redis envelopes; if the observer was
                                          # down at the time, those publishes are
                                          # dropped. This job does the
                                          # INSERT/DELETE in-band against the DB
                                          # so a long observer outage doesn't
                                          # strand thousands of opens.
FULL_BACKFILL_DAYS = 14                   # Cover the last 14 days incrementally
                                          # on the recurring schedule; the
                                          # one-shot 2026-05-17 backfill seeds
                                          # the 90-day history.
REFRESH_EVENT_TIMES_INTERVAL_S = 1800     # 30 min — refresh markets.is_live_match
                                          # so a sport market that became live in
                                          # the last 30m flips True and the
                                          # confidence engine's `live_match_blocked`
                                          # gate kicks in immediately. Tier 1 fix #1
                                          # of docs/autonomous_session_2026_05_17_strategy
                                          # /02_STRUCTURAL_FIX_PLAN.md (the bug that
                                          # cost 9 paper trades at -97% on 2026-05-17).
SWEEP_EXPIRED_INTERVAL_S = 1800           # 30 min — flip ``active=FALSE`` on every
                                          # market whose ``end_date`` slipped past
                                          # NOW() - 1 day but Gamma's closed-list
                                          # endpoint never picked up. Lever F of the
                                          # 2026-05-17 backfill plan, run as a
                                          # standalone cadence so the 6h
                                          # full_backfill no longer owns it alone.
LIVE_SUMMARY_INTERVAL_S = 30.0            # 30 s — rebuild the /api/v1/live-summary
                                          # snapshot and write it to Redis. Replaces
                                          # the API's in-process rebuilder so the
                                          # 17 SQL queries no longer saturate the
                                          # pool under parallel dashboard load.
                                          # See docs/autonomous_session_2026_05_17_
                                          # strategy/04_PRECOMPUTED_SNAPSHOT_
                                          # ARCHITECTURE.md.

REDIS_MARKET_RESOLVED_CHANNEL = "market:resolved"
REDIS_BACKFILL_LAG_ALERT_CHANNEL = "engine:backfill:lag_alert"

# Hard cap on consecutive HTTP 429 hits on the SAME endpoint before we
# bail out and log ERROR. Prevents a degraded Gamma from monopolising
# the maintenance loop's 30-min slot. Note: this is consecutive — a
# single success resets the counter.
BACKFILL_MAX_CONSECUTIVE_429 = 5

# Jitter band on the computed backoff. Picked at ±20% so worst-case the
# next attempt fires at 1.2× the nominal sleep — well under cap.
BACKFILL_RETRY_JITTER = 0.20

_running = True


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} | {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────
# Job: fee_snapshots bootstrap
# ──────────────────────────────────────────────────────────────────────

async def bootstrap_fee_snapshots(pool: asyncpg.Pool) -> int:
    """Seed fresh fee_snapshots for every active market token reachable by the engine.

    Uses markets.fee_rate_pct (refreshed by gamma) as the source.

    Plan 2026-05-19 P0-1: widened market eligibility. The legacy filter
    ``volume_24h > 500`` excluded every market where Gamma's volume column
    was stale or 0 — exactly the set of fresh markets where leader trades
    most frequently land. The new predicate accepts ANY market with either
    moderate Gamma-reported volume (``> 100``) OR observed trade activity
    in the last 24h (``EXISTS trades_observed``). This restores
    fee_snapshots coverage on the markets the signal_audit gate actually
    needs to greenlight a paper trade. Stale rows still age out via the
    7-day DELETE below.
    """
    async with pool.acquire() as conn:
        # YES side
        a = await conn.execute(
            """
            INSERT INTO fee_snapshots (market_id, token_id, fee_enabled, fee_rate,
                                       source, captured_at)
            SELECT m.market_id, m.token_yes, TRUE,
                   COALESCE(m.fee_rate_pct, 0.01)::numeric,
                   'maintenance_loop', NOW()
            FROM markets m
            WHERE m.active=TRUE AND m.end_date > NOW()
              AND m.token_yes IS NOT NULL
              AND (
                m.volume_24h > 100
                OR EXISTS (
                    SELECT 1 FROM trades_observed t
                    WHERE t.market_id = m.market_id
                      AND t.time > NOW() - INTERVAL '24 hours'
                    LIMIT 1
                )
              )
            ON CONFLICT (market_id, token_id, captured_at, source) DO NOTHING
            """
        )
        # NO side
        b = await conn.execute(
            """
            INSERT INTO fee_snapshots (market_id, token_id, fee_enabled, fee_rate,
                                       source, captured_at)
            SELECT m.market_id, m.token_no, TRUE,
                   COALESCE(m.fee_rate_pct, 0.01)::numeric,
                   'maintenance_loop', NOW()
            FROM markets m
            WHERE m.active=TRUE AND m.end_date > NOW()
              AND m.token_no IS NOT NULL
              AND (
                m.volume_24h > 100
                OR EXISTS (
                    SELECT 1 FROM trades_observed t
                    WHERE t.market_id = m.market_id
                      AND t.time > NOW() - INTERVAL '24 hours'
                    LIMIT 1
                )
              )
            ON CONFLICT (market_id, token_id, captured_at, source) DO NOTHING
            """
        )
        # Trim old: keep last 7 days only
        await conn.execute(
            "DELETE FROM fee_snapshots WHERE captured_at < NOW() - INTERVAL '7 days'"
        )

    n1 = int(a.split()[-1]) if a else 0
    n2 = int(b.split()[-1]) if b else 0
    return n1 + n2


# ──────────────────────────────────────────────────────────────────────
# Job: Gamma markets refresh (top-N by volume)
# ──────────────────────────────────────────────────────────────────────

def _parse_dt(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


async def _fetch_gamma_page(session, offset, limit=100):
    params = {
        "limit": limit, "offset": offset,
        "active": "true", "closed": "false",
        "order": "volume24hr", "ascending": "false",
    }
    async with session.get(GAMMA_URL, params=params, timeout=30) as resp:
        if resp.status != 200:
            return []
        return await resp.json()


async def refresh_gamma_markets(pool: asyncpg.Pool, *, max_pages: int = 30) -> tuple[int, int]:
    """Pull top markets by volume from Gamma and refresh end_date + volume."""
    updated = 0
    inserted = 0
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        for page in range(max_pages):
            markets = await _fetch_gamma_page(session, offset=page * 100, limit=100)
            if not markets:
                break
            async with pool.acquire() as conn:
                for m in markets:
                    cid = m.get("conditionId")
                    if not cid:
                        continue
                    end_date = _parse_dt(m.get("endDate"))
                    vol_raw = (m.get("volume24hr") or m.get("volume24h")
                               or m.get("liquidity") or 0)
                    try:
                        vol = float(vol_raw)
                    except (TypeError, ValueError):
                        vol = 0.0
                    tokens = m.get("clobTokenIds") or m.get("tokens") or []
                    token_yes = None
                    token_no = None
                    if isinstance(tokens, str):
                        with suppress(Exception):
                            tokens = json.loads(tokens)
                    if isinstance(tokens, list) and len(tokens) >= 2:
                        token_yes = str(tokens[0])
                        token_no = str(tokens[1])
                    # Derive active from Gamma's `closed` flag. When
                    # closed=TRUE we MUST flip active=FALSE so the
                    # sweep + downstream gates respect the resolution.
                    # When closed is falsy we preserve the existing
                    # active value — the previous behaviour of forcing
                    # TRUE was re-flipping markets the 30-min sweep had
                    # just deactivated.
                    closed = bool(m.get("closed"))
                    active_preserve = not closed
                    res = await conn.execute(
                        """
                        UPDATE markets SET
                            end_date = COALESCE($2::timestamptz, end_date),
                            volume_24h = $3::numeric,
                            active = CASE WHEN $6::boolean THEN markets.active ELSE FALSE END,
                            token_yes = COALESCE($4::varchar, token_yes),
                            token_no = COALESCE($5::varchar, token_no),
                            updated_at = NOW()
                        WHERE market_id = $1::varchar
                        """,
                        cid, end_date, vol, token_yes, token_no, active_preserve,
                    )
                    if res.startswith("UPDATE 0"):
                        with suppress(Exception):
                            await conn.execute(
                                """
                                INSERT INTO markets
                                    (market_id, question, end_date, volume_24h,
                                     active, token_yes, token_no, updated_at)
                                VALUES ($1::varchar, $2::text, $3::timestamptz, $4::numeric,
                                        $7::boolean, $5::varchar, $6::varchar, NOW())
                                ON CONFLICT (market_id) DO NOTHING
                                """,
                                cid, m.get("question", "")[:1000],
                                end_date, vol, token_yes, token_no, active_preserve,
                            )
                            inserted += 1
                    else:
                        updated += 1
            if len(markets) < 100:
                break
    return updated, inserted


# ──────────────────────────────────────────────────────────────────────
# Job: markets.resolved_outcome backfill
# ──────────────────────────────────────────────────────────────────────

async def _fetch_gamma_closed_page(session, offset, limit=500):
    """Paginate Gamma closed markets. Returns [] on any failure so the
    caller can log + skip without crashing the loop.

    Kept for back-compat with ``close_orphan_resolved_positions`` which
    is happy with a swallow-all-errors paginator. The robust backfill
    path uses ``_fetch_gamma_closed_page_robust`` for retry semantics.
    """
    params = {
        "limit": limit, "offset": offset,
        "closed": "true", "active": "false",
    }
    try:
        async with session.get(
            GAMMA_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []


def _compute_backoff(attempt: int, *, initial: float, cap: float) -> float:
    """Exponential backoff with ±BACKFILL_RETRY_JITTER jitter.

    ``attempt`` is 0-indexed for the first failure. The base delay is
    ``initial × 2**attempt`` clamped to ``cap``. Jitter is applied
    multiplicatively (1 - j .. 1 + j) so the spread is symmetric and
    never produces negative sleeps even for tiny initial values.
    """
    base = min(cap, initial * (2 ** max(0, attempt)))
    j = BACKFILL_RETRY_JITTER
    factor = 1.0 + random.uniform(-j, j)
    return max(0.0, min(cap, base * factor))


async def _fetch_gamma_closed_page_robust(
    session: aiohttp.ClientSession,
    *,
    offset: int,
    limit: int,
    initial_backoff_s: float,
    max_backoff_s: float,
) -> tuple[list[dict], int]:
    """Paginate Gamma closed markets WITH retry semantics on HTTP 429.

    Order matters for the backfill: we ask Gamma for the oldest-resolved
    first (``order=endDate&ascending=true``) so stable resolutions get
    populated before fresh ones. The endpoint occasionally rate-limits;
    we obey ``Retry-After`` when present and otherwise fall back to
    exponential backoff with jitter.

    Returns ``(markets, retries_consumed)``. If the same endpoint hits
    HTTP 429 more than ``BACKFILL_MAX_CONSECUTIVE_429`` times we give
    up and return ``([], retries_consumed)`` so the caller can log ERROR
    and move on.
    """
    params = {
        "limit": str(limit), "offset": str(offset),
        "closed": "true", "active": "false",
        "order": "endDate", "ascending": "true",
    }
    consecutive_429 = 0
    retries = 0
    attempt = 0

    while consecutive_429 < BACKFILL_MAX_CONSECUTIVE_429:
        try:
            async with session.get(
                GAMMA_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    consecutive_429 += 1
                    retries += 1
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s: float
                    if retry_after:
                        try:
                            sleep_s = max(0.0, min(max_backoff_s, float(retry_after)))
                        except (TypeError, ValueError):
                            sleep_s = _compute_backoff(
                                attempt, initial=initial_backoff_s, cap=max_backoff_s,
                            )
                    else:
                        sleep_s = _compute_backoff(
                            attempt, initial=initial_backoff_s, cap=max_backoff_s,
                        )
                    _log(
                        f"[backfill_resolved] 429 offset={offset} "
                        f"attempt={attempt + 1}/{BACKFILL_MAX_CONSECUTIVE_429} "
                        f"sleep={sleep_s:.1f}s (retry_after={retry_after!r})"
                    )
                    attempt += 1
                    await asyncio.sleep(sleep_s)
                    continue
                if resp.status != 200:
                    _log(
                        f"[backfill_resolved] non-200 status={resp.status} "
                        f"offset={offset}; skipping page"
                    )
                    return [], retries
                payload = await resp.json()
                if not isinstance(payload, list):
                    _log(
                        f"[backfill_resolved] unexpected payload type "
                        f"{type(payload).__name__} at offset={offset}"
                    )
                    return [], retries
                return payload, retries
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Treat network errors like a 429 (transient) so a single
            # blip doesn't abort the run. Same consecutive counter.
            consecutive_429 += 1
            retries += 1
            sleep_s = _compute_backoff(
                attempt, initial=initial_backoff_s, cap=max_backoff_s,
            )
            _log(
                f"[backfill_resolved] network error offset={offset} "
                f"attempt={attempt + 1}: {type(exc).__name__}: {exc}; "
                f"sleep={sleep_s:.1f}s"
            )
            attempt += 1
            await asyncio.sleep(sleep_s)
            continue

    _log(
        f"[backfill_resolved] giving up on offset={offset} after "
        f"{BACKFILL_MAX_CONSECUTIVE_429} consecutive 429s/errors"
    )
    return [], retries


def _parse_resolved_outcome(market: dict) -> str | None:
    """Robustly extract the resolved outcome from a Gamma market payload.

    Returns "yes" / "no" or None when the payload is malformed (missing
    field, unparseable JSON, non-numeric prices). Callers should log
    a warning + skip the row on None — never crash the run.

    Convention (Polymarket binary markets): ``outcomes[0]`` is YES,
    ``outcomes[1]`` is NO. ``outcomePrices[0] > 0.5`` ⇒ YES winner.
    """
    prices = market.get("outcomePrices")
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


async def backfill_resolved_outcomes(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    *,
    redis_client=None,
    batch_size: int | None = None,
    lag_alert_threshold: int | None = None,
    initial_backoff_s: float | None = None,
    max_backoff_s: float | None = None,
) -> dict:
    """Populate markets.resolved_outcome from Gamma closed-market data.

    Without this, paper_trader's resolved-market close path defers
    indefinitely (up to a 30-day timeout) because it can't read a
    terminal value for the YES/NO outcome. We mirror Gamma's
    ``outcomePrices[0]`` → "yes" if > 0.5 else "no".

    Robust rewrite (2026-05-17): paginated fetch, exponential backoff
    with jitter on HTTP 429, ``Retry-After`` honoured, idempotent UPDATE
    (``WHERE resolved_outcome IS NULL``), parse-or-skip on malformed
    payloads, lag alert via Redis when the post-run remaining count
    exceeds ``BACKFILL_LAG_ALERT_THRESHOLD``.

    Returns a metrics dict ``{scanned, fetched, populated,
    skipped_malformed, retried_429, run_duration_s, missing_after,
    lag_alert_fired}``. Kept for parity with the rest of the
    maintenance jobs which all surface their counters via the log line
    emitted by ``run_with_recovery``.
    """
    started_monotonic = time.monotonic()
    cfg_batch = int(batch_size if batch_size is not None else settings.BACKFILL_BATCH_SIZE)
    cfg_thr = int(
        lag_alert_threshold if lag_alert_threshold is not None
        else settings.BACKFILL_LAG_ALERT_THRESHOLD
    )
    cfg_init = float(
        initial_backoff_s if initial_backoff_s is not None
        else settings.BACKFILL_RETRY_INITIAL_S
    )
    cfg_max = float(
        max_backoff_s if max_backoff_s is not None
        else settings.BACKFILL_RETRY_MAX_S
    )

    page_size = 100  # Gamma's stable page size for closed-market scans
    offset = 0
    scanned = 0
    fetched = 0
    populated = 0
    skipped_malformed = 0
    retried_429 = 0

    while scanned < cfg_batch:
        # Cap the last page so we never request more than the remaining
        # batch budget — Gamma will happily return 100 rows we'd then
        # have to throw away.
        page_limit = min(page_size, cfg_batch - scanned)
        markets, retries = await _fetch_gamma_closed_page_robust(
            session,
            offset=offset,
            limit=page_limit,
            initial_backoff_s=cfg_init,
            max_backoff_s=cfg_max,
        )
        retried_429 += retries
        if not markets:
            break
        scanned += len(markets)
        fetched += len(markets)

        async with pool.acquire() as conn:
            for m in markets:
                if not m.get("closed"):
                    # Defensive: filter on the caller side too in case
                    # Gamma starts returning mixed pages.
                    continue
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid:
                    skipped_malformed += 1
                    _log(
                        f"[backfill_resolved] skipped malformed: "
                        f"missing conditionId"
                    )
                    continue
                outcome = _parse_resolved_outcome(m)
                if outcome is None:
                    skipped_malformed += 1
                    _log(
                        f"[backfill_resolved] skipped malformed market="
                        f"{cid}: outcomePrices unparseable"
                    )
                    continue
                res = await conn.execute(
                    """
                    UPDATE markets
                    SET resolved_outcome = $2::varchar,
                        updated_at = NOW()
                    WHERE market_id = $1::varchar
                      AND resolved_outcome IS NULL
                    """,
                    cid, outcome,
                )
                # asyncpg returns "UPDATE <n>"; only count actual writes.
                if res and not res.endswith(" 0"):
                    populated += 1

        if len(markets) < page_limit:
            # Tail page — Gamma had nothing more for our filter window.
            break
        offset += len(markets)

    # Lag accounting: how many rows still need a resolved_outcome?
    # The SQL is cheap (indexed scan on a partial filter); we always run
    # it so the operator can see "we're catching up" in the log even on
    # quiet runs where no alert fires.
    missing_after = 0
    try:
        async with pool.acquire() as conn:
            missing_after = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM markets
                    WHERE active = FALSE AND resolved_outcome IS NULL
                    """
                ) or 0
            )
    except Exception as exc:
        _log(f"[backfill_resolved] missing-count probe failed: {exc}")

    lag_alert_fired = False
    if missing_after > cfg_thr and redis_client is not None:
        envelope = {
            "type": "backfill_resolved_outcomes_lag",
            "missing_count": missing_after,
            "threshold": cfg_thr,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await redis_client.publish(
                REDIS_BACKFILL_LAG_ALERT_CHANNEL, json.dumps(envelope)
            )
            lag_alert_fired = True
            _log(
                f"[backfill_resolved] LAG ALERT — missing_after="
                f"{missing_after} threshold={cfg_thr}"
            )
        except Exception as exc:
            _log(f"[backfill_resolved] lag-alert publish failed: {exc}")

    duration_s = round(time.monotonic() - started_monotonic, 2)
    _log(
        f"[backfill_resolved] scanned={scanned} fetched={fetched} "
        f"populated={populated} skipped_malformed={skipped_malformed} "
        f"retried_429={retried_429} missing_after={missing_after} "
        f"lag_alert={lag_alert_fired} run_duration_s={duration_s}"
    )

    return {
        "scanned": scanned,
        "fetched": fetched,
        "populated": populated,
        "skipped_malformed": skipped_malformed,
        "retried_429": retried_429,
        "run_duration_s": duration_s,
        "missing_after": missing_after,
        "lag_alert_fired": lag_alert_fired,
    }


# ──────────────────────────────────────────────────────────────────────
# Job: sweep expired-but-still-active markets (Lever F, recurring)
# ──────────────────────────────────────────────────────────────────────


async def sweep_expired_active_markets(pool: asyncpg.Pool) -> int:
    """Flip ``active=FALSE`` for every market whose ``end_date`` slipped
    past ``NOW() - 1 day`` but the flag was never updated.

    Mirrors ``scripts/backfill_gamma_resolutions_2026_05_17.sweep_expired_active_markets``
    on a 30-min cadence so a long observer/engine outage no longer keeps
    thousands of dead markets in the active set. Idempotent.
    """
    async with pool.acquire() as conn:
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


# ──────────────────────────────────────────────────────────────────────
# Job: orphan-resolved-position sweep
# ──────────────────────────────────────────────────────────────────────


async def close_orphan_resolved_positions(
    pool: asyncpg.Pool,
    redis_client,
    session: aiohttp.ClientSession,
) -> tuple[int, int]:
    """Close per-market opens in ``position_tracker_state`` whose markets
    Gamma already reports as ``closed=true``.

    The websocket ``market_resolved`` dispatch is the primary path
    (observer/main.py publishes envelopes; PositionTracker subscribes
    and closes). This job is the SAFETY NET for two failure modes:

      1. The observer container restarted between the resolution and
         the next backfill — the WS frame is gone and nobody publishes.
      2. Polymarket's WS never shipped a ``market_resolved`` frame for
         this market (it happens occasionally on edge resolutions).

    We query Gamma's closed-market endpoint, intersect with rows in
    ``position_tracker_state``, and republish a normalised envelope on
    ``REDIS_MARKET_RESOLVED_CHANNEL`` for each orphan. The PositionTracker
    handler is idempotent — closing a market with no open positions is
    a no-op, so a redundant publish from this sweep is harmless.

    Returns ``(markets_checked, envelopes_published)``.
    """
    # Step 1: list distinct (market_id) with open state rows.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT market_id
            FROM position_tracker_state
            WHERE shares_remaining > 0
            """
        )
    open_markets = {str(r["market_id"]) for r in rows if r["market_id"]}
    if not open_markets:
        return (0, 0)

    # Step 2: walk Gamma closed markets; only act on intersecting IDs.
    # Reuses the same paginator as backfill_resolved_outcomes for cache
    # affinity (Gamma is OK with this volume).
    offset = 0
    limit = 500
    max_pages = 50
    published = 0
    seen = set()

    for _ in range(max_pages):
        markets = await _fetch_gamma_closed_page(session, offset=offset, limit=limit)
        if not markets:
            break
        for m in markets:
            if not m.get("closed"):
                continue
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid or cid not in open_markets or cid in seen:
                continue
            seen.add(cid)
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                with suppress(Exception):
                    prices = json.loads(prices)
            if not isinstance(prices, list) or not prices:
                continue
            try:
                yes_terminal = float(prices[0])
            except (TypeError, ValueError):
                continue
            outcome = "yes" if yes_terminal > 0.5 else "no"
            envelope = json.dumps(
                {
                    "market_id": cid,
                    "outcome": outcome,
                    "source": "maintenance_orphan_sweep",
                }
            )
            try:
                await redis_client.publish(
                    REDIS_MARKET_RESOLVED_CHANNEL, envelope
                )
                published += 1
            except Exception as exc:
                _log(
                    f"[orphan_close] publish failed for market={cid}: {exc}"
                )
        if len(markets) < limit:
            break
        offset += limit

    return (len(open_markets), published)


# ──────────────────────────────────────────────────────────────────────
# Job: full backfill of Gamma resolutions (Lever A — recurring sweep)
# ──────────────────────────────────────────────────────────────────────


async def full_backfill_resolutions(
    pool: asyncpg.Pool,
    redis_client,
    session: aiohttp.ClientSession,
    *,
    days: int = FULL_BACKFILL_DAYS,
) -> dict:
    """Recurring incremental wrapper around the one-shot
    ``scripts/backfill_gamma_resolutions_2026_05_17`` script.

    The 30-min ``RESOLUTION_BACKFILL_INTERVAL_S`` job populates
    ``markets.resolved_outcome`` only. The 10-min ``ORPHAN_CLOSE_INTERVAL_S``
    job publishes Redis envelopes for the in-process tracker to close
    open state rows, but those publishes only land if the observer is
    running at that moment. When the observer was down for any window
    (deploy, OOM, restart) the open rows accumulate.

    This 6-h job runs the full DB-side close path (INSERT
    positions_reconstructed + DELETE position_tracker_state + publish
    ``positions:closed``) for every market Gamma reports closed in the
    last ``days`` window. Idempotent — already-closed positions are
    filtered by ``shares_remaining > 0`` and the markets UPDATE skips
    rows already settled.
    """
    # Local import keeps the maintenance loop's startup time small —
    # the backfill script pulls in argparse + logger config that we
    # don't need until the first 6-h tick.
    from scripts import backfill_gamma_resolutions_2026_05_17 as backfill_script

    summary = await backfill_script.run_backfill(
        pool=pool,
        redis_client=redis_client,
        session=session,
        days=days,
        batch_size=100,
        dry_run=False,
    )
    return summary.as_dict()


# ──────────────────────────────────────────────────────────────────────
# Job: refresh markets.event_start_time / is_live_match (Tier 1 fix #1)
# ──────────────────────────────────────────────────────────────────────


async def refresh_event_times(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
) -> dict:
    """Re-pull Gamma `gameStartTime` for every active sport market and
    recompute the `is_live_match` flag.

    `is_live_match` is a wall-clock derivative (TRUE iff event_start
    within ±2h of NOW) so even an unchanged event_start_time row
    flips True / False over the day. Running this every 30 min keeps
    the confidence engine's hot-path gate accurate without forcing
    it to compare timestamps inline.

    The actual enrichment logic lives in the import script — we just
    invoke its top-level orchestrator. Idempotent and hot-deploy
    safe; if the import script is mid-run when this fires, the second
    run no-ops on already-correct rows.

    Returns the summary dict so the scheduler can log it.
    """
    # Local import keeps the maintenance loop startup small — the
    # import script pulls in pydantic, which is a non-trivial cost
    # we don't need until the first 30-min tick.
    from scripts import import_gamma_event_times_2026_05_17 as event_times_script

    summary = await event_times_script.run_import(
        pool=pool,
        session=session,
        category="sports",
        dry_run=False,
    )
    return summary.as_dict()


# ──────────────────────────────────────────────────────────────────────
# Job: auto-promote follower-rich leaders to on_watchlist
# ──────────────────────────────────────────────────────────────────────


async def auto_promote_to_watchlist(pool: asyncpg.Pool) -> int:
    """Flip ``on_watchlist=TRUE`` for leaders the follower graph already
    confirmed but the observer bootstrap might skip.

    Selection criterion (matches the bootstrap UNION query in
    ``src/observer/main.py``): a wallet with ≥5 follower_edges entries
    where ``co_occurrences >= 5``. Once promoted, the next observer
    bootstrap UNION picks the wallet up in the falcon-score branch
    (because ``excluded=FALSE AND on_watchlist=TRUE`` already qualifies).

    Excluded wallets are NOT promoted — exclusion is a deliberate signal
    from leader_registry (bot/structural detection) and must be respected.
    Returns the number of wallets newly promoted.
    """
    async with pool.acquire() as conn:
        # We use UPDATE … WHERE on_watchlist=FALSE so the row count
        # reflects "newly promoted" rather than "already on_watchlist".
        res = await conn.execute(
            """
            UPDATE leaders
            SET on_watchlist = TRUE
            WHERE excluded = FALSE
              AND on_watchlist = FALSE
              AND wallet_address IN (
                  SELECT leader_wallet
                  FROM follower_edges
                  WHERE co_occurrences >= 5
                  GROUP BY leader_wallet
                  HAVING COUNT(*) >= 5
              )
            """
        )
    try:
        promoted = int(res.split()[-1])
    except (IndexError, ValueError):
        promoted = 0
    return promoted


# ──────────────────────────────────────────────────────────────────────
# Job: leader_profiles.trades_observed reconciliation
# ──────────────────────────────────────────────────────────────────────

async def reconcile_profiles(pool: asyncpg.Pool) -> int:
    """Sync leader_profiles.trades_observed with the live count.

    The behavior_profiler is supposed to maintain this, but it lags or
    misses when daemons restart. We backfill from the source of truth.

    The ``sub.cnt > COALESCE(lp.trades_observed, 0)`` guard was removed
    on 2026-05-17 — it prevented ``last_updated`` from refreshing for
    leaders whose 90d trade count was unchanged, stranding 702 stale
    profiles that the dashboard then surfaced as "no recent activity".
    The set ``trades_observed = sub.cnt`` is idempotent when the count
    matches so dropping the guard is safe.
    """
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE leader_profiles lp SET
                trades_observed = sub.cnt,
                last_updated = NOW()
            FROM (
                SELECT wallet_address, COUNT(*) AS cnt
                FROM trades_observed
                WHERE time >= NOW() - INTERVAL '90 days'
                GROUP BY wallet_address
            ) sub
            WHERE lp.wallet_address = sub.wallet_address
            """
        )
    return int(res.split()[-1]) if res else 0


# ──────────────────────────────────────────────────────────────────────
# Job: follower_edges rebuild
# ──────────────────────────────────────────────────────────────────────

async def rebuild_follower_graph(pool: asyncpg.Pool, *, days: int = 7) -> tuple[int, int]:
    """Recompute follower_edges from a window of trades.

    This is the core leader-follower discovery pass. We GREATEST the
    co_occurrences so we don't regress existing edges that have grown
    via the live hot path.
    """
    sql = f"""
        SET LOCAL statement_timeout = '600000';
        INSERT INTO follower_edges (
            leader_wallet, follower_wallet, co_occurrences,
            follow_probability, follow_beta_a, follow_beta_b,
            avg_delay_s, same_direction_rate, first_observed, last_observed
        )
        SELECT
            l.wallet_address, f.wallet_address, COUNT(*),
            (SUM(CASE WHEN l.side = f.side THEN 1 ELSE 0 END) + 1.0)
                / (COUNT(*) + 2.0),
            (SUM(CASE WHEN l.side = f.side THEN 1 ELSE 0 END) + 1.0),
            (SUM(CASE WHEN l.side <> f.side THEN 1 ELSE 0 END) + 1.0),
            AVG(EXTRACT(EPOCH FROM (f.time - l.time)))::numeric,
            (SUM(CASE WHEN l.side = f.side THEN 1 ELSE 0 END)::numeric
                / NULLIF(COUNT(*), 0)),
            MIN(l.time), MAX(l.time)
        FROM trades_observed l
        JOIN leaders ld ON ld.wallet_address = l.wallet_address AND ld.excluded = FALSE
        JOIN trades_observed f
          ON f.market_id = l.market_id
         AND f.wallet_address <> l.wallet_address
         AND f.time > l.time
         AND f.time <= l.time + INTERVAL '300 seconds'
        WHERE l.time >= NOW() - INTERVAL '{days} days'
          AND f.time >= NOW() - INTERVAL '{days} days'
        GROUP BY l.wallet_address, f.wallet_address
        HAVING COUNT(*) >= 2
        ON CONFLICT (leader_wallet, follower_wallet) DO UPDATE SET
            co_occurrences = GREATEST(follower_edges.co_occurrences, EXCLUDED.co_occurrences),
            follow_probability = EXCLUDED.follow_probability,
            follow_beta_a = EXCLUDED.follow_beta_a,
            follow_beta_b = EXCLUDED.follow_beta_b,
            avg_delay_s = EXCLUDED.avg_delay_s,
            same_direction_rate = EXCLUDED.same_direction_rate,
            first_observed = LEAST(follower_edges.first_observed, EXCLUDED.first_observed),
            last_observed = GREATEST(follower_edges.last_observed, EXCLUDED.last_observed)
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.execute(sql)
        n_total = await conn.fetchval("SELECT COUNT(*) FROM follower_edges")
        n_confirmed = await conn.fetchval(
            """SELECT COUNT(*) FROM follower_edges
               WHERE co_occurrences >= 5 AND same_direction_rate >= 0.7"""
        )
    return int(n_total), int(n_confirmed)


# ──────────────────────────────────────────────────────────────────────
# Job: book:last cache refresh
# ──────────────────────────────────────────────────────────────────────

async def trim_runaway_streams(redis_client) -> dict:
    """Safety net: trim known unbounded Redis streams. The producer-side
    cap is the primary defense (CLOB_BOOK_STREAM_MAXLEN) but a stale
    consumer or a producer with an old MAXLEN config can still let
    book:events:stream balloon and OOM Redis.
    """
    caps = {
        "book:events:stream": 100_000,
        "trades:stream": 50_000,
        "mempool:leader_intent": 10_000,
    }
    trimmed = {}
    for stream, cap in caps.items():
        try:
            n = await redis_client.xtrim(stream, maxlen=cap, approximate=True)
            trimmed[stream] = int(n) if n is not None else 0
        except Exception:
            pass
    return trimmed


async def refresh_book_cache(redis_client) -> int:
    """For all liquid markets, fetch fresh quotes from CLOB orderbook
    endpoint and write to book:last cache.

    Parallelized with bounded concurrency so 1500 markets × 2 tokens =
    3000 HTTP calls complete in tens of seconds, not minutes.
    """
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2, command_timeout=30)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT market_id, token_yes, token_no, volume_24h
                FROM markets
                WHERE active = TRUE AND end_date > NOW()
                  AND volume_24h > 500
                  AND token_yes IS NOT NULL AND token_no IS NOT NULL
                ORDER BY volume_24h DESC
                LIMIT 1500
                """
            )
    finally:
        await pool.close()

    # Build flat list of (market_id, token_id) pairs.
    targets = []
    for row in rows:
        for token in (row["token_yes"], row["token_no"]):
            if token:
                targets.append((row["market_id"], str(token)))

    sem = asyncio.Semaphore(30)  # 30 concurrent requests max
    refreshed = 0
    refreshed_lock = asyncio.Lock()

    async def fetch_one(session, market_id, token_id):
        nonlocal refreshed
        async with sem:
            try:
                url = f"https://clob.polymarket.com/book?token_id={token_id}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
            except Exception:
                return
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            if not bids or not asks:
                return
            try:
                best_bid = str(bids[0].get("price"))
                best_ask = str(asks[0].get("price"))
            except Exception:
                return
            if not best_bid or not best_ask:
                return
            now_ts = time.time()
            payload = json.dumps({
                "market_id": market_id,
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "observed_ts": now_ts,
                "captured_at": now_ts,
                "source": "maintenance_loop",
            })
            key = f"book:last:{market_id}:{token_id}"
            try:
                await redis_client.set(key, payload, ex=BOOK_CACHE_TTL_S)
                async with refreshed_lock:
                    refreshed += 1
            except Exception:
                pass

    connector = aiohttp.TCPConnector(limit=60, limit_per_host=30, force_close=False)
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        connector=connector,
    ) as session:
        tasks = [fetch_one(session, mid, tid) for mid, tid in targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    return refreshed


# ──────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────

async def run_with_recovery(name: str, fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:
        _log(f"[{name}] FAILED: {type(exc).__name__}: {exc}")
        return None


async def main():
    if not DB_URL:
        _log("DATABASE_URL not set, exiting.")
        sys.exit(1)

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4, command_timeout=600)
    redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)
    # Shared HTTP session for jobs that hit Gamma/CLOB on the cadence
    # path (refresh_gamma_markets builds its own internally for back-compat
    # — only backfill_resolved_outcomes uses this one for now).
    http_session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})

    _log("maintenance_loop: started")

    # initial pass — run everything once on startup
    trim_res = await run_with_recovery("stream_trim", trim_runaway_streams, redis_client) or {}
    _log(f"[startup] stream_trim={trim_res}")

    fee_n = await run_with_recovery("fees", bootstrap_fee_snapshots, pool) or 0
    _log(f"[startup] fee_snapshots inserted={fee_n}")

    gamma_u, gamma_i = (
        await run_with_recovery("gamma", refresh_gamma_markets, pool, max_pages=30)
        or (0, 0)
    )
    _log(f"[startup] gamma updated={gamma_u} inserted={gamma_i}")

    # Sweep AFTER the gamma refresh so any end_date we just learned gets
    # honoured on the first tick instead of waiting 30 min.
    sweep_n = await run_with_recovery(
        "sweep_expired", sweep_expired_active_markets, pool,
    ) or 0
    _log(f"[startup] sweep_expired flipped={sweep_n}")

    # First-tick backfill so historical resolutions get populated quickly
    # — paper_trader's resolved-close path needs this to avoid the 30d
    # defer-until-timeout failure mode.
    res_summary = (
        await run_with_recovery(
            "resolutions", backfill_resolved_outcomes,
            pool, http_session, redis_client=redis_client,
        )
        or {}
    )
    _log(f"[startup] resolutions {res_summary}")

    prof_n = await run_with_recovery("profiles", reconcile_profiles, pool) or 0
    _log(f"[startup] profiles updated={prof_n}")

    # Startup pass for the orphan-close sweep — covers any markets that
    # resolved while the observer container was down.
    orphan_checked, orphan_pub = (
        await run_with_recovery(
            "orphan_close",
            close_orphan_resolved_positions,
            pool, redis_client, http_session,
        )
        or (0, 0)
    )
    _log(
        f"[startup] orphan_close open_markets={orphan_checked} "
        f"published={orphan_pub}"
    )

    promoted = (
        await run_with_recovery(
            "promote_watchlist", auto_promote_to_watchlist, pool,
        )
        or 0
    )
    _log(f"[startup] promote_watchlist newly_promoted={promoted}")

    # Skip graph rebuild on startup — it can take 5-10 min and stalls
    # the loop. Existing edges in DB are fine; the periodic 6h rebuild
    # picks up new ones. Run book/fee refresh first so paper trades can
    # fire immediately.
    book_n = await run_with_recovery("book", refresh_book_cache, redis_client) or 0
    _log(f"[startup] book:last refreshed={book_n}")

    # Graph rebuild in background — don't block startup, but log eventual result.
    async def _bg_graph():
        t, c = (await run_with_recovery("graph", rebuild_follower_graph, pool, days=7)
                or (0, 0))
        _log(f"[bg startup] follower_edges total={t} confirmed={c}")
    asyncio.create_task(_bg_graph())

    # background schedule
    last_run = {
        "fees": time.monotonic(),
        "gamma": time.monotonic(),
        "profiles": time.monotonic(),
        "graph": time.monotonic(),
        "book": time.monotonic(),
        "trim": time.monotonic(),
        "resolutions": time.monotonic(),
        "orphan_close": time.monotonic(),
        "promote_watchlist": time.monotonic(),
        # Init at 0 so the 6h full_backfill (which contains
        # sweep_expired_active_markets) fires within 30s of daemon
        # restart — without this, 5000+ expired markets stay
        # active=TRUE for up to 6 hours after every deploy.
        "full_backfill": 0.0,
        # Initialise at 0 so the first tick happens within 30s of
        # daemon start — sport markets that became live during a
        # restart get flagged immediately.
        "event_times": 0.0,
        # Init at 0 so the first tick fires within 30s — the startup
        # call has already run, but if startup is short-circuited (e.g.
        # SIGTERM mid-startup) we still want the very next loop pass
        # to flip stale rows instead of waiting 30 min.
        "sweep_expired": 0.0,
        # Init at 0 so the first snapshot build fires on the very next
        # loop tick after daemon start — the API endpoint returns 503
        # until the first build lands, so we don't want to wait 30s.
        "live_summary": 0.0,
    }

    while _running:
        await asyncio.sleep(30)
        now = time.monotonic()

        if now - last_run["fees"] > FEE_BOOTSTRAP_INTERVAL_S:
            n = await run_with_recovery("fees", bootstrap_fee_snapshots, pool) or 0
            _log(f"fees: inserted={n}")
            last_run["fees"] = now

        if now - last_run["gamma"] > GAMMA_REFRESH_INTERVAL_S:
            u, i = (
                await run_with_recovery("gamma", refresh_gamma_markets, pool, max_pages=15)
                or (0, 0)
            )
            _log(f"gamma: updated={u} inserted={i}")
            last_run["gamma"] = now

        if now - last_run["profiles"] > PROFILES_RECONCILE_INTERVAL_S:
            n = await run_with_recovery("profiles", reconcile_profiles, pool) or 0
            _log(f"profiles: updated={n}")
            last_run["profiles"] = now

        if now - last_run["graph"] > GRAPH_REBUILD_INTERVAL_S:
            # Run in a separate task so it doesn't block the maintenance
            # loop if the rebuild SQL stalls (10+ min on a busy DB).
            async def _bg_graph():
                t, c = (
                    await run_with_recovery("graph", rebuild_follower_graph, pool, days=7)
                    or (0, 0)
                )
                _log(f"graph: total={t} confirmed={c}")
            asyncio.create_task(_bg_graph())
            last_run["graph"] = now

        if now - last_run["book"] > BOOK_CACHE_REFRESH_INTERVAL_S:
            n = await run_with_recovery("book", refresh_book_cache, redis_client) or 0
            _log(f"book: refreshed={n}")
            last_run["book"] = now

        if now - last_run["trim"] > STREAM_TRIM_INTERVAL_S:
            res = await run_with_recovery("stream_trim", trim_runaway_streams, redis_client) or {}
            _log(f"stream_trim: {res}")
            last_run["trim"] = now

        if now - last_run["resolutions"] > RESOLUTION_BACKFILL_INTERVAL_S:
            summary = (
                await run_with_recovery(
                    "resolutions", backfill_resolved_outcomes,
                    pool, http_session, redis_client=redis_client,
                )
                or {}
            )
            _log(f"resolutions: {summary}")
            last_run["resolutions"] = now

        if now - last_run["orphan_close"] > ORPHAN_CLOSE_INTERVAL_S:
            checked, published = (
                await run_with_recovery(
                    "orphan_close",
                    close_orphan_resolved_positions,
                    pool, redis_client, http_session,
                )
                or (0, 0)
            )
            _log(f"orphan_close: open_markets={checked} published={published}")
            last_run["orphan_close"] = now

        if now - last_run["promote_watchlist"] > PROMOTE_WATCHLIST_INTERVAL_S:
            promoted = (
                await run_with_recovery(
                    "promote_watchlist", auto_promote_to_watchlist, pool,
                )
                or 0
            )
            _log(f"promote_watchlist: newly_promoted={promoted}")
            last_run["promote_watchlist"] = now

        if now - last_run["event_times"] > REFRESH_EVENT_TIMES_INTERVAL_S:
            # Tier 1 fix #1: keep is_live_match accurate vs wall-clock.
            # Run in a background task so a slow Gamma sweep doesn't
            # stall the maintenance loop (2.5k sport markets × ~15
            # concurrency → ~30s typical, ~3min worst-case).
            async def _bg_event_times():
                summary = (
                    await run_with_recovery(
                        "event_times",
                        refresh_event_times,
                        pool, http_session,
                    )
                    or {}
                )
                _log(f"event_times: {summary}")
            asyncio.create_task(_bg_event_times())
            last_run["event_times"] = now

        if now - last_run["sweep_expired"] > SWEEP_EXPIRED_INTERVAL_S:
            n = await run_with_recovery(
                "sweep_expired", sweep_expired_active_markets, pool,
            ) or 0
            _log(f"sweep_expired: flipped={n}")
            last_run["sweep_expired"] = now

        if now - last_run["full_backfill"] > FULL_BACKFILL_INTERVAL_S:
            # Don't block the maintenance loop on a multi-minute scan
            # of 14 days of Gamma history — defer to a background task.
            async def _bg_full_backfill():
                summary = (
                    await run_with_recovery(
                        "full_backfill",
                        full_backfill_resolutions,
                        pool, redis_client, http_session,
                    )
                    or {}
                )
                _log(f"full_backfill: {summary}")
            asyncio.create_task(_bg_full_backfill())
            last_run["full_backfill"] = now

        # ──────────────────────────────────────────────────────────────
        # Job: build live-summary snapshot (every 30s)
        # ──────────────────────────────────────────────────────────────
        # Replaces the API's in-process `_snapshot_rebuilder_loop`. Runs
        # the 17 dashboard SQL queries here (single writer, no pool
        # contention), serialises the result, writes Redis, publishes a
        # pubsub event. The /api/v1/live-summary endpoint reads from
        # Redis and returns in <10ms. Gated on _HAS_SNAPSHOT_BUILDER so
        # we keep working before Agent A's module lands.
        if (
            _HAS_SNAPSHOT_BUILDER
            and (time.monotonic() - last_run["live_summary"]) >= LIVE_SUMMARY_INTERVAL_S
        ):
            t0 = time.monotonic()
            try:
                await build_terminal_snapshot(pool, redis_client)
                dur = time.monotonic() - t0
                _log(f"maintenance_loop: live_summary built in {dur:.2f}s")
            except Exception as exc:
                _log(
                    f"maintenance_loop: live_summary build failed "
                    f"{type(exc).__name__}: {exc}"
                )
            last_run["live_summary"] = time.monotonic()

    _log("maintenance_loop: shutting down")
    await http_session.close()
    await pool.close()
    await redis_client.aclose()


def _sig(*_):
    global _running
    _running = False


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    asyncio.run(main())
