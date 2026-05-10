"""
Rolling-forward partition creator for `trades_observed`.

Companion to migration 013 (docs/migrations/013_trades_observed_partition.sql).
Generates the next N monthly partitions on demand. Idempotent: re-running
is a no-op (uses `CREATE TABLE IF NOT EXISTS`).

Designed for monthly cron — typically on the 1st of each month at 00:30 UTC:

    30 0 1 * * cd /opt/polymarket-bot && python -m scripts.maintenance.create_trades_partitions

Usage:
    python -m scripts.maintenance.create_trades_partitions              # default: next 3 months
    python -m scripts.maintenance.create_trades_partitions --months 6   # next 6 months
    python -m scripts.maintenance.create_trades_partitions --dry-run    # print DDL, do not execute

Why a Python script and not pg_partman?
- The bot already requires asyncpg; no new system dependency.
- pg_partman adds an extension install step on Hetzner + Oracle hosts.
- We only need monthly RANGE partitions on a single table — pg_partman's
  feature surface is overkill.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg
from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config import settings  # noqa: E402

PARENT_TABLE = "trades_observed"


def _month_floor(dt: datetime) -> datetime:
    """Return the first instant of `dt`'s month, in UTC."""
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=timezone.utc)


def _next_month(dt: datetime) -> datetime:
    """Return the first instant of the month after `dt`'s month."""
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return datetime(dt.year, dt.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _partition_name(month_start: datetime) -> str:
    """Naming convention from migration 013: trades_observed_YYYYMM."""
    return f"{PARENT_TABLE}_{month_start.year:04d}{month_start.month:02d}"


def generate_partition_ddl(
    start_month: datetime,
    months_ahead: int,
) -> list[tuple[str, str]]:
    """
    Build the (partition_name, DDL) pairs for the next `months_ahead` months
    starting at `start_month` (inclusive).

    The returned DDL uses `CREATE TABLE IF NOT EXISTS … PARTITION OF
    trades_observed FOR VALUES FROM (a) TO (b)`. Idempotent.

    Args:
        start_month: any datetime; will be snapped to the first of its month.
        months_ahead: how many monthly partitions to create.

    Returns:
        List of (table_name, sql) tuples, in chronological order.
    """
    if months_ahead < 1:
        raise ValueError(f"months_ahead must be >= 1, got {months_ahead}")

    base = _month_floor(start_month)
    out: list[tuple[str, str]] = []
    cursor = base
    for _ in range(months_ahead):
        next_cursor = _next_month(cursor)
        name = _partition_name(cursor)
        sql = (
            f"CREATE TABLE IF NOT EXISTS {name}\n"
            f"    PARTITION OF {PARENT_TABLE}\n"
            f"    FOR VALUES FROM ('{cursor.isoformat()}') "
            f"TO ('{next_cursor.isoformat()}');"
        )
        out.append((name, sql))
        cursor = next_cursor
    return out


async def _table_exists(conn: asyncpg.Connection, name: str) -> bool:
    """Defensive — used only by the verification logger, not as a guard
    (the DDL itself is `IF NOT EXISTS`)."""
    row = await conn.fetchval(
        "SELECT 1 FROM pg_class WHERE relname = $1 AND relkind = 'r'",
        name,
    )
    return row is not None


async def _verify_parent_is_partitioned(conn: asyncpg.Connection) -> None:
    """Bail loudly if migration 013 has not been applied yet."""
    relkind = await conn.fetchval(
        "SELECT relkind FROM pg_class WHERE relname = $1",
        PARENT_TABLE,
    )
    if relkind is None:
        raise RuntimeError(
            f"{PARENT_TABLE} does not exist. Apply migration 013 first."
        )
    if relkind != "p":
        raise RuntimeError(
            f"{PARENT_TABLE} exists but is not partitioned (relkind={relkind!r}). "
            f"Apply migration 013 first."
        )


async def apply_partitions(
    dsn: str,
    start_month: datetime,
    months_ahead: int,
    *,
    dry_run: bool = False,
) -> list[str]:
    """
    Connect to the DB and apply (or print) partition DDL.

    Returns the list of partition names that were targeted (created or
    already-present — they're indistinguishable from outside the runner
    because CREATE TABLE IF NOT EXISTS does not raise).
    """
    ddl_pairs = generate_partition_ddl(start_month, months_ahead)

    if dry_run:
        logger.info(f"DRY-RUN — would apply {len(ddl_pairs)} partition(s):")
        for name, sql in ddl_pairs:
            logger.info(f"  {name}:\n{sql}")
        return [name for name, _ in ddl_pairs]

    conn = await asyncpg.connect(dsn)
    try:
        await _verify_parent_is_partitioned(conn)
        applied: list[str] = []
        for name, sql in ddl_pairs:
            pre_existed = await _table_exists(conn, name)
            await conn.execute(sql)
            applied.append(name)
            if pre_existed:
                logger.info(f"  {name}: already existed (no-op)")
            else:
                logger.info(f"  {name}: created")
        return applied
    finally:
        await conn.close()


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Roll forward monthly trades_observed partitions."
    )
    parser.add_argument(
        "--months",
        type=int,
        default=3,
        help="Number of future months to ensure exist (default: 3).",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help=(
            "ISO date for the first partition (default: current month UTC). "
            "Anything within the desired month works; gets snapped to the 1st."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print DDL without connecting to the database.",
    )
    parser.add_argument(
        "--dsn",
        type=str,
        default=None,
        help="Override DATABASE_URL.",
    )
    return parser.parse_args(argv)


def _resolve_start(arg: str | None) -> datetime:
    if arg is None:
        return datetime.now(tz=timezone.utc)
    try:
        # Allow plain "YYYY-MM-DD" and full ISO timestamps.
        return datetime.fromisoformat(arg).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise SystemExit(f"Bad --start value {arg!r}: {e}")


async def main_async(argv: list[str] | None = None) -> None:
    args = _parse_cli_args(argv)
    start = _resolve_start(args.start)
    dsn = args.dsn or settings.DATABASE_URL
    applied = await apply_partitions(
        dsn=dsn,
        start_month=start,
        months_ahead=args.months,
        dry_run=args.dry_run,
    )
    logger.info(
        f"Partition roll-forward complete — targeted {len(applied)} month(s) "
        f"starting {_month_floor(start).date()}."
    )


if __name__ == "__main__":
    asyncio.run(main_async())
