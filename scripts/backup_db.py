"""
One-shot backup runner — invoke manually or from CI to force a dump.

Usage:
    python scripts/backup_db.py              # dump + upload to R2
    python scripts/backup_db.py --dry-run    # dump locally, skip upload

Reads DATABASE_URL + R2_* from settings (i.e. .env). The compose
`backups` service uses src/backups/main.py instead, which adds the
APScheduler loop on top. This script is for ops one-offs.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.backups.dumper import run_pg_dump
from src.backups.job import run_backup_once
from src.backups.r2_client import R2Client
from src.config import settings


def _make_r2() -> R2Client:
    return R2Client(
        endpoint_url=settings.R2_ENDPOINT_URL,
        access_key_id=settings.R2_ACCESS_KEY_ID,
        secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="On-demand Postgres backup → R2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dump locally only, skip upload + retention sweep.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="(--dry-run) Path to write the dump. Defaults to /tmp/<ts>.dump",
    )
    args = parser.parse_args()

    if args.dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        path = Path(args.output or f"/tmp/polymarket-{ts}.dump")
        logger.info(f"dry-run: pg_dump → {path}")
        run_pg_dump(
            dsn=settings.DATABASE_URL,
            output_path=path,
            timeout_s=settings.BACKUP_PG_DUMP_TIMEOUT_S,
        )
        logger.info(f"dry-run: wrote {path} ({path.stat().st_size} B)")
        return 0

    result = run_backup_once(
        dsn=settings.DATABASE_URL,
        r2_client=_make_r2(),
        prefix=settings.R2_KEY_PREFIX,
        scratch_dir=settings.BACKUP_LOCAL_SCRATCH_DIR,
        daily=settings.BACKUP_RETENTION_DAILY,
        weekly=settings.BACKUP_RETENTION_WEEKLY,
        monthly=settings.BACKUP_RETENTION_MONTHLY,
        weekly_dow=settings.BACKUP_WEEKLY_DOW,
        pg_dump_timeout_s=settings.BACKUP_PG_DUMP_TIMEOUT_S,
    )
    print(
        f"OK uploaded={result.uploaded_key} bytes={result.uploaded_bytes} "
        f"deleted={len(result.deleted_keys)} kept={len(result.kept_keys)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
