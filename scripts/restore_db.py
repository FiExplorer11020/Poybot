"""
Restore a Postgres dump from R2.

Usage:
    # List available backups (newest first):
    python scripts/restore_db.py --list

    # Download a specific key to /tmp:
    python scripts/restore_db.py --key postgres/2026/04/2026-04-29T05-00-00Z.dump --download-only

    # Download and pg_restore it into a target DSN (DESTRUCTIVE if --clean):
    python scripts/restore_db.py --key postgres/2026/04/...dump \
        --target-dsn postgresql://user:pass@localhost:5432/restore_test --clean

Notes:
    * `--clean` issues `--clean --if-exists` to pg_restore: drop the
      existing schema before recreating. Do NOT point at production
      without thinking twice.
    * Default behavior is download-only — safe by default.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import urlparse

from loguru import logger

from src.backups.r2_client import R2Client
from src.config import settings


def _make_r2() -> R2Client:
    return R2Client(
        endpoint_url=settings.R2_ENDPOINT_URL,
        access_key_id=settings.R2_ACCESS_KEY_ID,
        secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET,
    )


def _list_backups(r2: R2Client) -> int:
    objects = sorted(
        r2.list_objects(prefix=settings.R2_KEY_PREFIX),
        key=lambda obj: obj.last_modified,
        reverse=True,
    )
    if not objects:
        print(f"(no objects under {settings.R2_KEY_PREFIX!r})")
        return 0
    print(f"{'KEY':<60} {'BYTES':>12}  {'LAST_MODIFIED':>20}")
    for obj in objects:
        print(f"{obj.key:<60} {obj.size:>12}  {obj.last_modified.isoformat():>20}")
    return 0


def _download(r2: R2Client, *, key: str, dest: Path) -> Path:
    client = r2._ensure_client()  # noqa: SLF001 — internal but stable
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"downloading {key} → {dest}")
    client.download_file(Bucket=r2.bucket, Key=key, Filename=str(dest))
    return dest


def _pg_restore(*, target_dsn: str, dump_path: Path, clean: bool) -> int:
    binary = shutil.which("pg_restore")
    if not binary:
        logger.error("pg_restore not on PATH — install postgresql-client")
        return 1
    parsed = urlparse(target_dsn)
    args = [
        binary,
        "--host", parsed.hostname or "localhost",
        "--port", str(parsed.port or 5432),
        "--username", parsed.username or "",
        "--dbname", (parsed.path.lstrip("/") or ""),
        "--no-owner",
        "--no-acl",
    ]
    if clean:
        args += ["--clean", "--if-exists"]
    args.append(str(dump_path))

    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    logger.info(f"pg_restore: {' '.join(args[:-1])} {dump_path}")
    completed = subprocess.run(args, env=env, capture_output=True, text=True)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Postgres from R2 backup")
    parser.add_argument("--list", action="store_true", help="List available backups")
    parser.add_argument("--key", help="Object key to download/restore")
    parser.add_argument(
        "--dest",
        default="/tmp/polymarket-restore.dump",
        help="Local path for the downloaded dump",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Just download — don't run pg_restore",
    )
    parser.add_argument("--target-dsn", help="postgresql://... DSN to restore into")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Pass --clean --if-exists to pg_restore (destructive)",
    )
    args = parser.parse_args()

    r2 = _make_r2()
    if args.list:
        return _list_backups(r2)

    if not args.key:
        parser.error("--key is required (use --list to discover)")

    dest = Path(args.dest)
    _download(r2, key=args.key, dest=dest)
    print(f"downloaded {args.key} → {dest} ({dest.stat().st_size} B)")

    if args.download_only:
        return 0
    if not args.target_dsn:
        parser.error("--target-dsn required for restore (or use --download-only)")
    return _pg_restore(target_dsn=args.target_dsn, dump_path=dest, clean=args.clean)


if __name__ == "__main__":
    sys.exit(main())
