"""
Backup job factory + one-shot runner (S4.12).

`run_backup_once()` does the full pipeline: dump → upload → retention
sweep → cleanup. `make_backup_job()` wraps it as an APScheduler-
friendly coroutine factory (mirroring src/engine/jobs/).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from loguru import logger

from src.backups.dumper import PgDumpError, run_pg_dump
from src.backups.r2_client import R2Client, R2Error, R2Object
from src.backups.retention import RetentionDecision, classify_keys


# Object keys live under `<prefix>YYYY/MM/<utc-iso>.dump`.
# We embed the timestamp in the key name so retention can recover it
# without an extra HEAD request.
_KEY_TS_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)\.dump$"
)


@dataclass(frozen=True)
class BackupResult:
    """Outcome of a single run_backup_once invocation. Surfaced to
    logs and (eventually) Telegram alerts."""

    uploaded_key: Optional[str]
    uploaded_bytes: int
    deleted_keys: list[str]
    kept_keys: list[str]
    skipped: bool = False
    skipped_reason: str = ""


def _build_object_key(*, prefix: str, ts: datetime) -> str:
    # Cloudflare R2 dashboards group neatly by year/month folders.
    iso = ts.strftime("%Y-%m-%dT%H-%M-%SZ")
    folder = ts.strftime("%Y/%m")
    safe_prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
    return f"{safe_prefix}{folder}/{iso}.dump"


def _parse_ts_from_key(key: str) -> Optional[datetime]:
    match = _KEY_TS_PATTERN.search(key)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("ts"), "%Y-%m-%dT%H-%M-%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _decide_retention(
    objects: list[R2Object],
    *,
    daily: int,
    weekly: int,
    monthly: int,
    weekly_dow: int,
) -> RetentionDecision:
    """Build the (key, ts) list that retention.classify_keys expects.

    We prefer the timestamp embedded in the key name (cheap, accurate)
    and fall back to the object's LastModified — which is fine for
    objects we just uploaded but slightly drifty for older ones."""
    tagged: list[tuple[str, datetime]] = []
    for obj in objects:
        ts = _parse_ts_from_key(obj.key) or obj.last_modified
        # Normalize to UTC so weekday()/day comparisons in classify_keys
        # are consistent.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        tagged.append((obj.key, ts.astimezone(timezone.utc)))
    return classify_keys(
        tagged,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        weekly_dow=weekly_dow,
    )


# --------------------------------------------------------------------------- #
# One-shot runner                                                              #
# --------------------------------------------------------------------------- #


def run_backup_once(
    *,
    dsn: str,
    r2_client: R2Client,
    prefix: str,
    scratch_dir: Path | str,
    daily: int,
    weekly: int,
    monthly: int,
    weekly_dow: int,
    pg_dump_timeout_s: int,
    now: Optional[datetime] = None,
) -> BackupResult:
    """Run a full dump+upload+retention pass synchronously.

    Pure-ish — boto3 + pg_dump are the only side effects. The `now`
    arg makes time-travel tests trivial.
    """
    ts = now or datetime.now(timezone.utc)
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    iso = ts.strftime("%Y-%m-%dT%H-%M-%SZ")
    local_path = scratch_dir / f"polymarket-{iso}.dump"

    # 1. Dump.
    logger.info(f"backup: pg_dump → {local_path}")
    try:
        run_pg_dump(
            dsn=dsn,
            output_path=local_path,
            timeout_s=pg_dump_timeout_s,
        )
    except PgDumpError as exc:
        logger.error(f"backup: pg_dump failed — {exc}")
        raise

    # 2. Upload.
    key = _build_object_key(prefix=prefix, ts=ts)
    logger.info(f"backup: uploading {local_path} ({local_path.stat().st_size} B) → {key}")
    try:
        uploaded = r2_client.put_file(local_path=local_path, key=key)
    except R2Error as exc:
        logger.error(f"backup: upload failed — {exc}")
        # Keep the local dump so the next run can retry — don't unlink.
        raise

    # 3. Retention sweep.
    objects = list(r2_client.list_objects(prefix=prefix))
    decision = _decide_retention(
        objects,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        weekly_dow=weekly_dow,
    )
    deleted: list[str] = []
    if decision.delete:
        try:
            r2_client.delete_objects(keys=decision.delete)
            deleted = list(decision.delete)
            logger.info(
                f"backup: retention pruned {len(deleted)} object(s); "
                f"kept {len(decision.keep)}"
            )
        except R2Error as exc:
            # Don't fail the whole run because of a retention hiccup —
            # the new backup is already up.
            logger.warning(f"backup: retention sweep failed — {exc}")
    else:
        logger.info(f"backup: retention kept all {len(decision.keep)} object(s)")

    # 4. Local cleanup. Keep on failure (handled in the upload branch
    # above); only unlink on the happy path.
    try:
        local_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning(f"backup: failed to unlink scratch file {local_path}: {exc}")

    return BackupResult(
        uploaded_key=key,
        uploaded_bytes=uploaded,
        deleted_keys=deleted,
        kept_keys=list(decision.keep),
    )


# --------------------------------------------------------------------------- #
# APScheduler factory                                                          #
# --------------------------------------------------------------------------- #


def make_backup_job(
    *,
    dsn: str,
    r2_client: R2Client,
    prefix: str,
    scratch_dir: str,
    daily: int,
    weekly: int,
    monthly: int,
    weekly_dow: int,
    pg_dump_timeout_s: int,
) -> Callable[[], Awaitable[None]]:
    """Return an awaitable wrapper around run_backup_once. Mirrors
    the factory pattern used by src.engine.jobs.* so the backup loop
    looks like every other scheduled job in the codebase."""

    async def _job() -> None:
        try:
            # run_backup_once is sync (pg_dump + boto3 are blocking)
            # but each step is short — we still hop to a thread to
            # keep the event loop responsive while pg_dump runs.
            result = await asyncio.to_thread(
                run_backup_once,
                dsn=dsn,
                r2_client=r2_client,
                prefix=prefix,
                scratch_dir=scratch_dir,
                daily=daily,
                weekly=weekly,
                monthly=monthly,
                weekly_dow=weekly_dow,
                pg_dump_timeout_s=pg_dump_timeout_s,
            )
            logger.info(
                f"backup: completed key={result.uploaded_key} "
                f"bytes={result.uploaded_bytes} "
                f"deleted={len(result.deleted_keys)} "
                f"kept={len(result.kept_keys)}"
            )
        except Exception:
            # Swallow — APScheduler will fire again on the next cron
            # tick. The watchdog/Telegram alert path is wired via
            # logs, not exceptions here.
            logger.exception("backup: run_backup_once raised — will retry next cycle")

    return _job
