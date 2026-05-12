"""Historical trades backfill from Polymarket data-api (Sprint 1 Day 1.2).

EXECUTION_PLAN_2026_05_12.md § 6 — gives the bot instant maturity on top
leaders without waiting weeks for the live observer to accumulate
positions_resolved.

Source: ``https://data-api.polymarket.com/trades?user={wallet}`` — public
REST endpoint, no auth, **not** Falcon. Falcon agent 556 quota is a
shared resource and we want this one-shot script to be replayable
without burning it.

Strategy
--------

1. Pull top-N wallets from ``wallet_universe`` (descending volume).
2. For each wallet, paginate ``?user=W&limit=500&offset=...`` newest-first
   until the oldest row in the page is older than ``--days-back``.
3. Bulk-INSERT into ``trades_observed`` with ``source='backfill_data_api'``
   and the existing ``ON CONFLICT (wallet, market, time, side, price,
   size) DO NOTHING`` clause as the dedup safety net (any trades the
   live observer already captured will be silently skipped).
4. ``wallet_universe`` aggregates are NOT touched here — the live
   observer's Day 2.2 upsert is now the canonical writer; on a fresh
   backfill those rows already exist (we read them in step 1).

Run example
-----------

.. code-block:: bash

    docker exec polymarket_observer python -m scripts.backfill_polymarket_trades \\
        --top-n 500 --days-back 90 --concurrency 8

Idempotent: re-running on the same window is a no-op (dedup at DB layer).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp
import asyncpg
from loguru import logger

DATA_API_URL = "https://data-api.polymarket.com/trades"
PAGE_LIMIT = 500
HTTP_TIMEOUT_S = 15
INSERT_BATCH = 200


async def fetch_top_wallets(conn: asyncpg.Connection, top_n: int) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT wallet_address
        FROM wallet_universe
        ORDER BY total_volume_usdc_ever DESC NULLS LAST
        LIMIT $1
        """,
        top_n,
    )
    return [r["wallet_address"] for r in rows]


def _parse_ts(raw) -> datetime | None:
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    # > 1e12 → milliseconds, else seconds
    return datetime.fromtimestamp(
        ts / 1000 if ts > 1_000_000_000_000 else ts, tz=timezone.utc
    )


def _normalize_trade(raw: dict) -> tuple | None:
    """Project a data-api trade dict to the trades_observed tuple, or
    return None if mandatory fields are missing.
    """
    try:
        wallet = (raw.get("proxyWallet") or "").strip()
        market_id = (raw.get("conditionId") or "").strip()
        token_id = (raw.get("asset") or "").strip()
        side = (raw.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            return None
        if not (wallet and market_id and token_id):
            return None
        price = Decimal(str(raw.get("price") or 0))
        size_shares = float(raw.get("size") or 0)
        if price <= 0 or size_shares <= 0:
            return None
        size_usdc = Decimal(str(round(size_shares * float(price), 2)))
        if size_usdc <= 0:
            return None
        trade_time = _parse_ts(raw.get("timestamp"))
        if trade_time is None:
            return None
    except (ValueError, TypeError):
        return None
    return (
        trade_time,
        market_id,
        token_id,
        wallet,
        side,
        price,
        size_usdc,
        "backfill",  # 8 chars — fits trades_observed.source VARCHAR(10)
        False,  # is_leader — let the live pipeline re-flag on the next refresh
        "unknown",  # category — markets row may not exist; left to refiner
    )


async def fetch_wallet_history(
    session: aiohttp.ClientSession,
    wallet: str,
    cutoff: datetime,
    max_pages: int = 200,
) -> list[tuple]:
    """Paginate wallet trades back to ``cutoff``. Returns normalized rows."""
    out: list[tuple] = []
    offset = 0
    for _ in range(max_pages):
        url = (
            f"{DATA_API_URL}?user={wallet}&limit={PAGE_LIMIT}&offset={offset}"
        )
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            ) as resp:
                if resp.status == 429:
                    logger.warning(f"429 on {wallet} — backing off 10 s")
                    await asyncio.sleep(10)
                    continue
                if resp.status != 200:
                    logger.debug(f"{wallet}: HTTP {resp.status} stop")
                    break
                page = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.debug(f"{wallet}: fetch error {e}")
            break
        if not isinstance(page, list) or not page:
            break
        page_rows: list[tuple] = []
        oldest_in_page: datetime | None = None
        for raw in page:
            row = _normalize_trade(raw)
            if row is None:
                continue
            ts = row[0]
            if ts < cutoff:
                continue
            if oldest_in_page is None or ts < oldest_in_page:
                oldest_in_page = ts
            page_rows.append(row)
        out.extend(page_rows)
        # Stop once the response naturally drifted past cutoff. Use the
        # raw (un-filtered) oldest timestamp because page_rows can be
        # empty after filtering.
        raw_oldest: datetime | None = None
        for raw in page:
            ts = _parse_ts(raw.get("timestamp"))
            if ts is not None and (raw_oldest is None or ts < raw_oldest):
                raw_oldest = ts
        if raw_oldest is not None and raw_oldest < cutoff:
            break
        if len(page) < PAGE_LIMIT:
            break  # short page → no more history
        offset += PAGE_LIMIT
        await asyncio.sleep(0.05)  # gentle pacing
    return out


async def bulk_insert(conn: asyncpg.Connection, rows: list[tuple]) -> int:
    """Insert rows in chunks of INSERT_BATCH. Returns total inserted."""
    if not rows:
        return 0
    inserted = 0
    for start in range(0, len(rows), INSERT_BATCH):
        chunk = rows[start : start + INSERT_BATCH]
        params: list = []
        placeholders: list[str] = []
        for i, row in enumerate(chunk):
            base = i * 10
            placeholders.append(
                f"(${base + 1}, ${base + 2}, ${base + 3}, ${base + 4}, "
                f"${base + 5}, ${base + 6}, ${base + 7}, ${base + 8}, "
                f"${base + 9}, ${base + 10})"
            )
            params.extend(row)
        sql = (
            "INSERT INTO trades_observed "
            "(time, market_id, token_id, wallet_address, side, price, "
            "size_usdc, source, is_leader, category) VALUES "
            + ", ".join(placeholders)
            + " ON CONFLICT (wallet_address, market_id, time, side, "
            "price, size_usdc) DO NOTHING"
        )
        result = await conn.execute(sql, *params)
        # asyncpg returns "INSERT 0 N" — parse trailing int
        try:
            inserted += int(result.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            inserted += len(chunk)  # best-effort fallback
    return inserted


async def run(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        return 2
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=args.days_back)
    logger.info(
        f"Backfilling top {args.top_n} wallets, cutoff={cutoff.isoformat()}, "
        f"concurrency={args.concurrency}"
    )
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            wallets = await fetch_top_wallets(conn, args.top_n)
        if not wallets:
            logger.warning("No wallets in wallet_universe — nothing to do")
            return 0
        logger.info(f"Got {len(wallets)} wallets to backfill")

        sem = asyncio.Semaphore(args.concurrency)
        total_fetched = 0
        total_inserted = 0
        started = time.monotonic()

        async with aiohttp.ClientSession() as session:
            async def one(idx: int, wallet: str) -> None:
                nonlocal total_fetched, total_inserted
                async with sem:
                    rows = await fetch_wallet_history(session, wallet, cutoff)
                    if not rows:
                        return
                    async with pool.acquire() as conn:
                        inserted = await bulk_insert(conn, rows)
                    total_fetched += len(rows)
                    total_inserted += inserted
                    if idx % 25 == 0 or args.verbose:
                        logger.info(
                            f"[{idx + 1}/{len(wallets)}] {wallet[:10]}…: "
                            f"{len(rows)} fetched, {inserted} inserted "
                            f"(running totals {total_fetched}/{total_inserted})"
                        )

            await asyncio.gather(
                *(one(i, w) for i, w in enumerate(wallets)),
                return_exceptions=False,
            )

        elapsed = time.monotonic() - started
        logger.info(
            f"DONE: {len(wallets)} wallets in {elapsed:.0f}s — "
            f"{total_fetched} fetched, {total_inserted} inserted "
            f"({total_fetched - total_inserted} duplicates skipped)"
        )
    finally:
        await pool.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=500)
    parser.add_argument("--days-back", type=int, default=90)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
