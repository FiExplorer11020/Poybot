"""Autonomous maintenance loop — keeps the trade pipeline operational.

Runs forever, executing these jobs on a schedule:
  - every 60 min: bootstrap fee_snapshots from markets + Gamma refresh
  - every 60 min: refresh markets.end_date + volume_24h from Gamma
  - every 10 min: leader_profiles.trades_observed reconciliation
  - every 6 hours: rebuild follower_edges (full graph)
  - every 30 min: book:last cache refresh for top liquid markets

Designed as a long-running container/daemon. Stop with SIGTERM.

This script is the SAFETY NET for known stale-data failure modes:
  - markets.end_date stays current (was NULL silently for all rows)
  - fee_snapshots stays fresh (gate requires < 24h)
  - follower_edges stays populated (was being wiped on engine restart)

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
BOOK_CACHE_REFRESH_INTERVAL_S = 1800      # 30 min

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
                    res = await conn.execute(
                        """
                        UPDATE markets SET
                            end_date = COALESCE($2::timestamptz, end_date),
                            volume_24h = $3::numeric,
                            active = TRUE,
                            token_yes = COALESCE($4::varchar, token_yes),
                            token_no = COALESCE($5::varchar, token_no),
                            updated_at = NOW()
                        WHERE market_id = $1::varchar
                        """,
                        cid, end_date, vol, token_yes, token_no,
                    )
                    if res.startswith("UPDATE 0"):
                        with suppress(Exception):
                            await conn.execute(
                                """
                                INSERT INTO markets
                                    (market_id, question, end_date, volume_24h,
                                     active, token_yes, token_no, updated_at)
                                VALUES ($1::varchar, $2::text, $3::timestamptz, $4::numeric,
                                        TRUE, $5::varchar, $6::varchar, NOW())
                                ON CONFLICT (market_id) DO NOTHING
                                """,
                                cid, m.get("question", "")[:1000],
                                end_date, vol, token_yes, token_no,
                            )
                            inserted += 1
                    else:
                        updated += 1
            if len(markets) < 100:
                break
    return updated, inserted


# ──────────────────────────────────────────────────────────────────────
# Job: leader_profiles.trades_observed reconciliation
# ──────────────────────────────────────────────────────────────────────

async def reconcile_profiles(pool: asyncpg.Pool) -> int:
    """Sync leader_profiles.trades_observed with the live count.

    The behavior_profiler is supposed to maintain this, but it lags or
    misses when daemons restart. We backfill from the source of truth.
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
              AND sub.cnt > COALESCE(lp.trades_observed, 0)
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

async def refresh_book_cache(redis_client) -> int:
    """For markets where book:last is missing/stale, fetch fresh quotes
    from Polymarket CLOB orderbook endpoint and SET them with a fresh
    observed_ts. Caps at top 200 markets by volume to keep load low.
    """
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2, command_timeout=30)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT market_id, token_yes, token_no, volume_24h
                FROM markets
                WHERE active = TRUE AND end_date > NOW()
                  AND volume_24h > 1000
                  AND token_yes IS NOT NULL AND token_no IS NOT NULL
                ORDER BY volume_24h DESC
                LIMIT 200
                """
            )
    finally:
        await pool.close()

    refreshed = 0
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        for row in rows:
            for token in (row["token_yes"], row["token_no"]):
                try:
                    # Polymarket CLOB orderbook endpoint
                    url = f"https://clob.polymarket.com/book?token_id={token}"
                    async with session.get(url, timeout=8) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                except Exception:
                    continue
                bids = data.get("bids") or []
                asks = data.get("asks") or []
                if not bids or not asks:
                    continue
                try:
                    best_bid = str(bids[0].get("price"))
                    best_ask = str(asks[0].get("price"))
                except Exception:
                    continue
                if not best_bid or not best_ask:
                    continue
                now_ts = time.time()
                payload = json.dumps({
                    "market_id": row["market_id"],
                    "token_id": str(token),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "observed_ts": now_ts,
                    "captured_at": now_ts,
                    "source": "maintenance_loop",
                })
                key = f"book:last:{row['market_id']}:{token}"
                try:
                    await redis_client.set(key, payload, ex=60)
                    refreshed += 1
                except Exception:
                    pass
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

    _log("maintenance_loop: started")

    # initial pass — run everything once on startup
    fee_n = await run_with_recovery("fees", bootstrap_fee_snapshots, pool) or 0
    _log(f"[startup] fee_snapshots inserted={fee_n}")

    gamma_u, gamma_i = (
        await run_with_recovery("gamma", refresh_gamma_markets, pool, max_pages=30)
        or (0, 0)
    )
    _log(f"[startup] gamma updated={gamma_u} inserted={gamma_i}")

    prof_n = await run_with_recovery("profiles", reconcile_profiles, pool) or 0
    _log(f"[startup] profiles updated={prof_n}")

    graph_t, graph_c = (
        await run_with_recovery("graph", rebuild_follower_graph, pool, days=7)
        or (0, 0)
    )
    _log(f"[startup] follower_edges total={graph_t} confirmed={graph_c}")

    book_n = await run_with_recovery("book", refresh_book_cache, redis_client) or 0
    _log(f"[startup] book:last refreshed={book_n}")

    # background schedule
    last_run = {
        "fees": time.monotonic(),
        "gamma": time.monotonic(),
        "profiles": time.monotonic(),
        "graph": time.monotonic(),
        "book": time.monotonic(),
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
            t, c = (
                await run_with_recovery("graph", rebuild_follower_graph, pool, days=7)
                or (0, 0)
            )
            _log(f"graph: total={t} confirmed={c}")
            last_run["graph"] = now

        if now - last_run["book"] > BOOK_CACHE_REFRESH_INTERVAL_S:
            n = await run_with_recovery("book", refresh_book_cache, redis_client) or 0
            _log(f"book: refreshed={n}")
            last_run["book"] = now

    _log("maintenance_loop: shutting down")
    await pool.close()
    await redis_client.aclose()


def _sig(*_):
    global _running
    _running = False


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    asyncio.run(main())
