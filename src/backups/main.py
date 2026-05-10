"""
Entry point for the backups container (S4.12).

Runs forever, scheduling the daily Postgres → R2 backup. Mirrors
src/{observer,engine,registry}/main.py.
"""

from __future__ import annotations

import asyncio
import signal

from loguru import logger

from src.backups.job import make_backup_job
from src.backups.r2_client import R2Client
from src.config import settings
from src.engine.scheduler import Scheduler
from src.logging_setup import configure_logging


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting Backups service (log_level={level})")

    if not settings.BACKUPS_ENABLED:
        logger.warning(
            "BACKUPS_ENABLED=false — service will idle. Flip to true on the "
            "production VM after R2 creds are populated."
        )
        # Still spin a stop-event loop so `docker stop` is clean and
        # the container exits 0 instead of crash-looping.
        stop = asyncio.Event()

        def _handle(*_):
            stop.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle)
        await stop.wait()
        return

    # --- credentials check (loud) -------------------------------------- #
    missing = [
        name
        for name, value in [
            ("R2_ENDPOINT_URL", settings.R2_ENDPOINT_URL),
            ("R2_ACCESS_KEY_ID", settings.R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", settings.R2_SECRET_ACCESS_KEY),
            ("R2_BUCKET", settings.R2_BUCKET),
        ]
        if not value
    ]
    if missing:
        logger.error(
            f"Backups service: missing required env vars {missing}. "
            f"Set them in .env or disable BACKUPS_ENABLED."
        )
        # Exit non-zero so docker compose surfaces the failure.
        raise SystemExit(2)

    r2 = R2Client(
        endpoint_url=settings.R2_ENDPOINT_URL,
        access_key_id=settings.R2_ACCESS_KEY_ID,
        secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET,
    )

    scheduler = Scheduler()
    scheduler.add_cron(
        "postgres_backup",
        make_backup_job(
            dsn=settings.DATABASE_URL,
            r2_client=r2,
            prefix=settings.R2_KEY_PREFIX,
            scratch_dir=settings.BACKUP_LOCAL_SCRATCH_DIR,
            daily=settings.BACKUP_RETENTION_DAILY,
            weekly=settings.BACKUP_RETENTION_WEEKLY,
            monthly=settings.BACKUP_RETENTION_MONTHLY,
            weekly_dow=settings.BACKUP_WEEKLY_DOW,
            pg_dump_timeout_s=settings.BACKUP_PG_DUMP_TIMEOUT_S,
        ),
        hour=settings.BACKUP_HOUR_UTC,
        minute=0,
    )
    await scheduler.start()
    logger.info(
        f"Backups service: cron @ {settings.BACKUP_HOUR_UTC:02d}:00 UTC, "
        f"bucket={settings.R2_BUCKET}, prefix={settings.R2_KEY_PREFIX}, "
        f"retention={settings.BACKUP_RETENTION_DAILY}d/"
        f"{settings.BACKUP_RETENTION_WEEKLY}w/"
        f"{settings.BACKUP_RETENTION_MONTHLY}m"
    )

    stop = asyncio.Event()

    def _handle(*_):
        logger.info("Backups service: shutdown signal received")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle)

    try:
        await stop.wait()
    finally:
        await scheduler.stop()
        logger.info("Backups service stopped")


if __name__ == "__main__":
    asyncio.run(main())
