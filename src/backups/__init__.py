"""
Backups module (S4.12).

Daily Postgres backup → Cloudflare R2 with GFS retention.

The flow (run_backup_once → make_backup_job for APScheduler) is:
    1. pg_dump --format=custom --compress=9 → /tmp/<ts>.dump
    2. boto3 PUT → s3://<bucket>/<prefix>YYYY/MM/<ts>.dump
    3. List existing keys, apply GFS policy, delete what's beyond
       7 daily + 4 weekly + 3 monthly.
    4. Remove the local scratch file.

Each step is its own module so retention can be unit-tested without
spinning up boto3 or pg_dump.
"""

from __future__ import annotations

from src.backups.dumper import PgDumpError, run_pg_dump
from src.backups.job import make_backup_job, run_backup_once
from src.backups.r2_client import R2Client, R2Error
from src.backups.retention import RetentionDecision, classify_keys

__all__ = [
    "PgDumpError",
    "R2Client",
    "R2Error",
    "RetentionDecision",
    "classify_keys",
    "make_backup_job",
    "run_backup_once",
    "run_pg_dump",
]
