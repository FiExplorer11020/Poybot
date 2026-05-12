"""
Rolling-forward partition creator + retention sweeper for `clob_book_events`.

Companion to migration 032 (docs/migrations/032_clob_book_events.sql).
Generates the next N hourly partitions on demand AND drops partitions
older than `settings.CLOB_BOOK_RETENTION_DAYS`. Idempotent: re-running
is a no-op (uses `CREATE TABLE IF NOT EXISTS` + a precise
`DROP TABLE IF EXISTS` filter).

Designed for hourly cron — typically 30 minutes past the hour:

    30 * * * * cd /opt/polymarket-bot && \
      python -m scripts.maintenance.create_book_events_partitions

Usage:
    python -m scripts.maintenance.create_book_events_partitions
    python -m scripts.maintenance.create_book_events_partitions --hours 48
    python -m scripts.maintenance.create_book_events_partitions --dry-run
    python -m scripts.maintenance.create_book_events_partitions --no-drop

Why a Python script and not pg_partman?
- The R6 trades_observed roller (`create_trades_partitions.py`) sets the
  precedent of plain-asyncpg + idempotent DDL.
- Volume: ~13 GB/day raw means partition pruning is load-bearing; we
  want to OWN that knob, not delegate to a generic tool.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config import settings  # noqa: E402

PARENT_TABLE = "clob_book_events"


def _hour_floor(dt: datetime) -> datetime:
    """Return the first instant of `dt`'s hour, in UTC."""
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _next_hour(dt: datetime) -> datetime:
    return _hour_floor(dt) + timedelta(hours=1)


def _partition_name(hour_start: datetime) -> str:
    """Naming convention from migration 032: clob_book_events_YYYYMMDD_HH."""
    hs = _hour_floor(hour_start)
    return f"{PARENT_TABLE}_{hs.year:04d}{hs.month:02d}{hs.day:02d}_{hs.hour:02d}"


def generate_partition_ddl(
    start_hour: datetime,
    hours_ahead: int,
) -> list[tuple[str, str]]:
    """Build (partition_name, DDL) pairs for the next `hours_ahead`
    hours starting at `start_hour` (inclusive)."""
    if hours_ahead < 1:
        raise ValueError(f"hours_ahead must be >= 1, got {hours_ahead}")
    base = _hour_floor(start_hour)
    out: list[tuple[str, str]] = []
    cursor = base
    for _ in range(hours_ahead):
        nxt = _next_hour(cursor)
        name = _partition_name(cursor)
        sql = (
            f"CREATE TABLE IF NOT EXISTS {name}\n"
            f"    PARTITION OF {PARENT_TABLE}\n"
            f"    FOR VALUES FROM ('{cursor.isoformat()}') "
            f"TO ('{nxt.isoformat()}');"
        )
        out.append((name, sql))
        cursor = nxt
    return out


async def _verify_parent_is_partitioned(conn: asyncpg.Connection) -> None:
    relkind = await conn.fetchval(
        "SELECT relkind FROM pg_class WHERE relname = $1",
        PARENT_TABLE,
    )
    if relkind is None:
        raise RuntimeError(
            f"{PARENT_TABLE} does not exist. Apply migration 032 first."
        )
    if relkind != "p":
        raise RuntimeError(
            f"{PARENT_TABLE} exists but is not partitioned (relkind={relkind!r}). "
            f"Apply migration 032 first."
        )


async def _list_existing_partitions(conn: asyncpg.Connection) -> list[str]:
    """Return the names of every child partition of clob_book_events."""
    rows = await conn.fetch(
        """
        SELECT c.relname AS name
        FROM pg_inherits i
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE p.relname = $1
        ORDER BY c.relname
        """,
        PARENT_TABLE,
    )
    return [r["name"] for r in rows]


def _hour_from_partition_name(name: str) -> datetime | None:
    """Parse YYYYMMDD_HH out of a `clob_book_events_<…>` name, returning
    a timezone-aware datetime in UTC. Returns None if the name doesn't
    match the expected shape (e.g. the DEFAULT partition).
    """
    prefix = f"{PARENT_TABLE}_"
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix):]
    parts = suffix.split("_")
    if len(parts) != 2 or len(parts[0]) != 8 or len(parts[1]) != 2:
        return None
    try:
        y = int(parts[0][:4])
        m = int(parts[0][4:6])
        d = int(parts[0][6:8])
        h = int(parts[1])
    except ValueError:
        return None
    return datetime(y, m, d, h, 0, 0, tzinfo=timezone.utc)


async def apply_partitions(
    dsn: str,
    start_hour: datetime,
    hours_ahead: int,
    *,
    retention_days: int,
    do_drop: bool = True,
    dry_run: bool = False,
) -> dict:
    """Connect to the DB, apply forward partitions, optionally drop
    retention-aged partitions. Returns a summary dict.
    """
    ddl_pairs = generate_partition_ddl(start_hour, hours_ahead)
    cutoff = _hour_floor(start_hour) - timedelta(days=max(0, int(retention_days)))

    if dry_run:
        logger.info(f"DRY-RUN — would apply {len(ddl_pairs)} partition(s):")
        for name, sql in ddl_pairs:
            logger.info(f"  {name}:\n{sql}")
        return {
            "created": [name for name, _ in ddl_pairs],
            "dropped": [],
            "cutoff": cutoff.isoformat(),
            "dry_run": True,
        }

    created: list[str] = []
    dropped: list[str] = []
    conn = await asyncpg.connect(dsn)
    try:
        await _verify_parent_is_partitioned(conn)
        for name, sql in ddl_pairs:
            await conn.execute(sql)
            created.append(name)
            logger.info(f"  {name}: ensured")
        if do_drop:
            partitions = await _list_existing_partitions(conn)
            for name in partitions:
                if name.endswith("_default"):
                    continue
                hour = _hour_from_partition_name(name)
                if hour is None:
                    continue
                if hour < cutoff:
                    await conn.execute(f"DROP TABLE IF EXISTS {name}")
                    dropped.append(name)
                    logger.info(f"  {name}: DROPPED (before {cutoff.isoformat()})")
    finally:
        await conn.close()
    return {
        "created": created,
        "dropped": dropped,
        "cutoff": cutoff.isoformat(),
        "dry_run": False,
    }


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Roll forward hourly clob_book_events partitions + drop old ones."
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Number of future hours to ensure exist (default: 24).",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help=(
            "Drop partitions older than this many days. "
            "Default: settings.CLOB_BOOK_RETENTION_DAYS."
        ),
    )
    parser.add_argument(
        "--no-drop",
        action="store_true",
        help="Skip the retention DROP pass.",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="ISO datetime for the first partition (default: now UTC).",
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
        return datetime.fromisoformat(arg).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise SystemExit(f"Bad --start value {arg!r}: {e}")


async def main_async(argv: list[str] | None = None) -> None:
    args = _parse_cli_args(argv)
    start = _resolve_start(args.start)
    retention = (
        int(args.retention_days)
        if args.retention_days is not None
        else int(getattr(settings, "CLOB_BOOK_RETENTION_DAYS", 30))
    )
    dsn = args.dsn or settings.DATABASE_URL
    summary = await apply_partitions(
        dsn=dsn,
        start_hour=start,
        hours_ahead=args.hours,
        retention_days=retention,
        do_drop=not args.no_drop,
        dry_run=args.dry_run,
    )
    logger.info(
        f"clob_book_events partition maintenance complete — "
        f"created/ensured: {len(summary['created'])}, "
        f"dropped: {len(summary['dropped'])}, "
        f"cutoff: {summary['cutoff']}"
    )


if __name__ == "__main__":
    asyncio.run(main_async())
