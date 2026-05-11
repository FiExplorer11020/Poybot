"""
One-shot operator-gated backfill for market_features_history.

Phase 3 Round 2 Agent Y — audit MG-3 §3.1.

For every row in `markets`, insert a single SEED row into
`market_features_history` dated `liquidity_score_updated_at`
(preferred — captures the exact moment the live liquidity_score was
stamped, per migration 012) or `updated_at` (fallback for pre-Task-C
rows). This gives `error_model._fetch_training_data` SOME historical
reference for legacy positions whose `open_time` predates the
dual-write rollout — even if the seed value is coarse (one snapshot
across the row's full history) it's better than the AS-OF-NOW
leakage the audit raised.

Why this is NOT auto-run:
  * The seed is a single row per market, dated to the most-recent
    refresh — it can't capture intra-market dynamics that happened
    before that timestamp. If your training window includes
    positions opened months before that refresh, the seed will
    over-fit the late state of the market.
  * The dual-write in `sync_markets` (Phase 3 Round 2) starts
    accumulating proper history from the deploy onward. Operators
    who can wait the lookback window (90 days for phase 2,
    longer for phase 3) need not run the backfill at all.
  * If the backfill is desired (e.g. to bootstrap evaluation on
    historical positions immediately), run it ONCE on a
    maintenance window so the COUNTs are visible in the log.

Usage:
    # Preview impact — no writes.
    python scripts/backfill_market_features_history.py --dry-run

    # Apply (operator must pass --yes to confirm).
    python scripts/backfill_market_features_history.py --yes

    # Limit the scan for testing.
    python scripts/backfill_market_features_history.py --dry-run --limit 50
"""

import argparse
import asyncio
import os
import sys

from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool


SCAN_SQL = """
SELECT
    market_id,
    liquidity_score,
    volume_24h,
    category,
    fee_rate_pct,
    liquidity_score_source,
    -- Prefer the explicit liquidity_score_updated_at (added in migration
    -- 012); fall back to the whole-row updated_at for pre-Task-C rows.
    COALESCE(liquidity_score_updated_at, updated_at) AS captured_at
FROM markets
WHERE COALESCE(liquidity_score_updated_at, updated_at) IS NOT NULL
ORDER BY market_id
"""


INSERT_SQL = """
INSERT INTO market_features_history
    (market_id, captured_at, liquidity_score, volume_24h,
     category, fee_rate_pct, source)
VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, 'manual'))
"""


async def _run(dry_run: bool, limit: int | None) -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    try:
        sql = SCAN_SQL
        if limit is not None and limit > 0:
            sql = sql + f"\nLIMIT {int(limit)}"

        async with get_db() as conn:
            rows = await conn.fetch(sql)

        logger.info(f"Backfill scan: {len(rows)} markets eligible")

        if dry_run:
            for row in rows[:20]:
                logger.info(
                    f"DRY-RUN seed: market_id={row['market_id']} "
                    f"captured_at={row['captured_at']} "
                    f"liquidity_score={row['liquidity_score']} "
                    f"source={row['liquidity_score_source'] or 'manual'}"
                )
            if len(rows) > 20:
                logger.info(f"... and {len(rows) - 20} more (suppressed)")
            logger.info(
                "DRY-RUN complete — no rows written. Re-run with --yes to apply."
            )
            return

        # Apply path. We insert in one batch via executemany so the
        # backfill is fast even on large `markets` tables. The history
        # table is APPEND-ONLY — duplicates are theoretically fine
        # (no UNIQUE constraint by design) but we still guard against
        # accidentally running this twice by warning the operator if
        # the table is non-empty BEFORE we start.
        async with get_db() as conn:
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM market_features_history"
            )
        if int(existing or 0) > 0:
            logger.warning(
                f"market_features_history already has {existing} row(s). "
                "Proceeding will create duplicate seed rows for any market_id "
                "already present. Pass --force to suppress this warning, "
                "or Ctrl-C now to abort."
            )

        params = [
            (
                row["market_id"],
                row["captured_at"],
                row["liquidity_score"],
                row["volume_24h"],
                row["category"],
                row["fee_rate_pct"],
                row["liquidity_score_source"],
            )
            for row in rows
        ]
        async with get_db() as conn:
            await conn.executemany(INSERT_SQL, params)
        logger.info(
            f"Backfill complete: {len(params)} seed row(s) inserted into "
            "market_features_history (source defaults to 'manual' when "
            "the originating markets row had no liquidity_score_source)."
        )
    finally:
        await close_pool()


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed market_features_history from the live markets table.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and print what WOULD be inserted, no writes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required to actually run the backfill (in non-dry-run mode).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Suppress the 'history table non-empty' warning.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional row limit for testing.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_cli_args()
    if not args.dry_run and not args.yes:
        logger.error(
            "Refusing to run without --yes. Pass --dry-run to preview, or "
            "--yes to actually apply the backfill."
        )
        sys.exit(2)
    asyncio.run(_run(dry_run=args.dry_run, limit=args.limit))
