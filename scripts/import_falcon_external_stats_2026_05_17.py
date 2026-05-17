"""
Strategy upgrade 2026-05-17 round 2 — Lever B (Falcon prior integration).

One-shot CLI that walks `leaders.wallet360_json`, extracts the
Falcon Wallet 360 track-record fields, and persists them into the
new `leader_profiles.external_*` columns (added by migration 046).

Falcon Wallet 360 schema discovery (DB sample, 5,265 rows):
- ``wallet360_json->>'total_trades'``   — total trades over window
- ``wallet360_json->>'winning_trades'`` — resolved wins (subset of total)
- ``wallet360_json->>'losing_trades'``  — resolved losses (subset of total)
- ``wallet360_json->>'win_rate'``       — winning / (winning + losing), in [0, 1]
- ``wallet360_json->>'total_pnl'``      — realized PnL (USDC)

We use ``winning_trades`` + ``losing_trades`` as ``external_wins +
external_losses`` and ``external_resolved_count = wins + losses``
because:
  * ``total_trades`` includes open positions (Falcon agent 581 surfaces
    a 224k-trade wallet with only 4,496 + 4,694 = 9,190 resolved — the
    rest are still open).
  * The confidence-engine gate is fundamentally Bayesian on RESOLVED
    outcomes; counting open positions would inflate the prior.

The script:
  * Uses ``INSERT ... ON CONFLICT (wallet_address) UPDATE`` so it's
    idempotent and safe to re-run after Falcon refresh.
  * Skips rows where Falcon never returned wallet360 (the
    ``WHERE wallet360_json IS NOT NULL AND wallet360_json::text != 'null'``
    filter on the SELECT).
  * Defaults missing/non-numeric fields to 0 so a partial Falcon
    payload doesn't poison the prior.
  * Reports ``rows_processed`` (SELECT count), ``rows_updated``
    (UPSERT count), ``leaders_unlocked`` (those that just crossed the
    Tier-C ``min_resolved`` floor of 30 via the new external counter).

Usage (local):
    python scripts/import_falcon_external_stats_2026_05_17.py

Usage (prod, after rsync + migration 046):
    docker exec polymarket_engine \\
        python /app/scripts/import_falcon_external_stats_2026_05_17.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool


# Tier-C floor (matches src.config.TIER_C_MIN_RESOLVED default). We
# count a leader as "unlocked" when their post-import
# external_resolved_count alone clears this bar — those are the
# wallets the confidence-engine gate was silently rejecting.
TIER_C_RESOLVED_FLOOR = int(getattr(settings, "TIER_C_MIN_RESOLVED", 30))


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion. NULLs, strings, floats, and bogus
    JSON values all degrade gracefully to 0 so the import never aborts
    on a single malformed row."""
    if value is None:
        return 0
    try:
        # Handle string-encoded ints/floats from JSON.
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def run_import(batch_size: int = 250) -> dict[str, int]:
    """Walk `leaders.wallet360_json` → upsert `leader_profiles.external_*`.

    Returns a summary dict ready for logging:
        {"rows_processed": ..., "rows_updated": ..., "leaders_unlocked": ...}

    Batched by ``batch_size`` so the connection pool's 30 s
    command_timeout doesn't trip on the full 5k+ JSONB scan. We
    project the JSON fields server-side (->>'winning_trades' etc.)
    so each row over the wire is a handful of strings, not the full
    60-key payload. We also use keyset pagination on `wallet_address`
    (PK) rather than `OFFSET` — `OFFSET 4500` forces Postgres to
    re-scan + skip 4500 rows on each batch, which is what was timing
    out. Keyset pagination is O(1) per batch.
    """
    rows_processed = 0
    rows_updated = 0
    leaders_unlocked = 0
    last_wallet = ""  # cursor — wallets are ordered alphabetically

    async with get_db() as conn:
        while True:
            # Keyset pagination on the PK. We deliberately DO NOT
            # filter on `wallet360_json IS NOT NULL` in SQL — that
            # predicate has no supporting index in production and the
            # planner falls back to a seq scan + the JSONB ::text
            # cast is expensive enough on 5k rows to blow the 30 s
            # command_timeout. Instead we filter in Python (cheap)
            # and let the cursor scan the whole `leaders` PK in O(n).
            rows = await conn.fetch(
                """
                SELECT
                    wallet_address,
                    (wallet360_json->>'winning_trades') AS winning_trades_str,
                    (wallet360_json->>'losing_trades') AS losing_trades_str
                FROM leaders
                WHERE wallet_address > $1
                ORDER BY wallet_address
                LIMIT $2
                """,
                last_wallet,
                batch_size,
            )
            if not rows:
                break
            last_wallet = rows[-1]["wallet_address"]
            logger.info(
                f"Processing batch ending at wallet={last_wallet[:10]}... "
                f"size={len(rows)} processed_so_far={rows_processed}"
            )
            for row in rows:
                rows_processed += 1
                wallet = row["wallet_address"]

                wins = _coerce_int(row["winning_trades_str"])
                losses = _coerce_int(row["losing_trades_str"])
                resolved = wins + losses

                # Skip rows with no resolved-trade signal — they'd just
                # write zeros and waste a transaction. We DO still count
                # them in rows_processed so the report is faithful.
                if resolved <= 0:
                    continue

                try:
                    # UPSERT into leader_profiles. The INSERT path covers
                    # leaders that don't yet have a profile row (rare —
                    # the profiler creates one on first observed trade,
                    # but Falcon may have data on wallets we haven't
                    # observed yet). The UPDATE path is the common case.
                    result = await conn.execute(
                        """
                        INSERT INTO leader_profiles (
                            wallet_address,
                            profile_json,
                            external_resolved_count,
                            external_wins,
                            external_losses,
                            external_source,
                            external_last_updated
                        ) VALUES (
                            $1,
                            '{}'::jsonb,
                            $2,
                            $3,
                            $4,
                            'falcon_wallet360',
                            NOW()
                        )
                        ON CONFLICT (wallet_address) DO UPDATE
                        SET external_resolved_count = EXCLUDED.external_resolved_count,
                            external_wins = EXCLUDED.external_wins,
                            external_losses = EXCLUDED.external_losses,
                            external_source = EXCLUDED.external_source,
                            external_last_updated = EXCLUDED.external_last_updated
                        """,
                        wallet,
                        resolved,
                        wins,
                        losses,
                    )
                    rows_updated += 1
                    if resolved >= TIER_C_RESOLVED_FLOOR:
                        leaders_unlocked += 1
                    # asyncpg's `execute` returns the command tag string
                    # ("INSERT 0 1" or "UPDATE 1") — useful in debug logs
                    # but we don't parse it (the rows_updated counter is
                    # the source of truth for this report).
                    _ = result
                except Exception as exc:
                    logger.warning(
                        f"Falcon stats UPSERT failed for {wallet}: {exc}"
                    )

            if len(rows) < batch_size:
                break

    return {
        "rows_processed": rows_processed,
        "rows_updated": rows_updated,
        "leaders_unlocked": leaders_unlocked,
    }


async def main(dry_run: bool = False) -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    try:
        if dry_run:
            # Dry-run only walks the SELECT path so the operator can
            # see how many rows would be touched without mutating
            # leader_profiles. Useful before the first prod run.
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS n
                    FROM leaders
                    WHERE wallet360_json IS NOT NULL
                      AND wallet360_json::text != 'null'
                    """
                )
                logger.info(
                    f"[dry-run] {row['n']} leaders carry wallet360_json — "
                    "would attempt UPSERT on each."
                )
            return

        summary = await run_import()
        logger.info(
            "Falcon external stats import complete: "
            f"rows_processed={summary['rows_processed']} "
            f"rows_updated={summary['rows_updated']} "
            f"leaders_unlocked={summary['leaders_unlocked']} "
            f"(unlocked = those crossing tier_c_min_resolved={TIER_C_RESOLVED_FLOOR})"
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Import Falcon Wallet 360 trade-history into "
            "leader_profiles.external_* columns (migration 046)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count target rows; do not mutate the DB.",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
