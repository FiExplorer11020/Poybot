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
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone

import aiohttp
import asyncpg
import redis.asyncio as redis_async

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

REDIS_MARKET_RESOLVED_CHANNEL = "market:resolved"

_running = True


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} | {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────
# Job: fee_snapshots bootstrap
# ──────────────────────────────────────────────────────────────────────

async def bootstrap_fee_snapshots(pool: asyncpg.Pool) -> int:
    """Seed fresh fee_snapshots for every active+liquid market token.

    Uses markets.fee_rate_pct (refreshed by gamma) as the source.
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
              AND m.volume_24h > 500 AND m.token_yes IS NOT NULL
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
              AND m.volume_24h > 500 AND m.token_no IS NOT NULL
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
    caller can log + skip without crashing the loop."""
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


async def backfill_resolved_outcomes(
    pool: asyncpg.Pool, session: aiohttp.ClientSession,
) -> tuple[int, int]:
    """Populate markets.resolved_outcome from Gamma closed-market data.

    Without this, paper_trader's resolved-market close path defers
    indefinitely (up to a 30-day timeout) because it can't read a
    terminal value for the YES/NO outcome. We mirror Gamma's
    `outcomePrices[0]` → "yes" if > 0.5 else "no".

    Only UPSERT where resolved_outcome IS NULL — preserves manual
    operator overrides. Idempotent and hot-deploy safe.
    """
    fetched = 0
    resolved = 0
    offset = 0
    limit = 500
    # Hard cap on pages so a Gamma misbehavior can't stall the loop.
    max_pages = 50

    for _ in range(max_pages):
        markets = await _fetch_gamma_closed_page(session, offset=offset, limit=limit)
        if not markets:
            break
        fetched += len(markets)

        async with pool.acquire() as conn:
            for m in markets:
                if not m.get("closed"):
                    continue
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid:
                    continue
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    with suppress(Exception):
                        prices = json.loads(prices)
                if not isinstance(prices, list) or len(prices) < 1:
                    continue
                try:
                    yes_terminal = float(prices[0])
                except (TypeError, ValueError):
                    continue
                outcome = "yes" if yes_terminal > 0.5 else "no"
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
                if res and not res.endswith("0"):
                    resolved += 1

        if len(markets) < limit:
            break
        offset += limit

    return fetched, resolved


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
    res_fetched, res_resolved = (
        await run_with_recovery(
            "resolutions", backfill_resolved_outcomes, pool, http_session,
        )
        or (0, 0)
    )
    _log(
        f"[startup] resolutions fetched={res_fetched} populated={res_resolved}"
    )

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
            f, r = (
                await run_with_recovery(
                    "resolutions", backfill_resolved_outcomes,
                    pool, http_session,
                )
                or (0, 0)
            )
            _log(f"resolutions: fetched={f} populated={r}")
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
