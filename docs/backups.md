# Postgres → Cloudflare R2 backups

> **Status (May 2026): the `backups` service is wired but idle.**
> `BACKUPS_ENABLED=false` in the prod `.env` until the Cloudflare R2
> bucket and credentials are provisioned. The container starts, the
> APScheduler cron is registered, but `make_backup_job` returns early
> because the master flag is off. Switch the flag to `true` AFTER
> populating `R2_*` env vars (see "Configuration" below). No data is
> being archived off-site at the moment — the prod VM's 40 GB SSD is
> the only persistence layer right now.

Nightly `pg_dump` archives uploaded to a Cloudflare R2 bucket, with
GFS-style retention (7 daily + 4 weekly + 3 monthly). Lives in its
own `backups` Docker service so a stuck pg_dump can never block the
trading path.

---

## What runs where

| Component             | Where                                                 |
|-----------------------|-------------------------------------------------------|
| `pg_dump` invocation  | `src/backups/dumper.py` → subprocess to `pg_dump`     |
| R2 client (boto3)     | `src/backups/r2_client.py`                            |
| Retention logic (GFS) | `src/backups/retention.py` — pure, fully unit-tested  |
| Cron loop             | `src/backups/main.py` (APScheduler, 1 cron job/night) |
| Container             | `backups` service in `docker-compose.yml`             |

The runtime image carries `postgresql-client` (Debian bookworm ships
v15) so `pg_dump`/`pg_restore` are present without a sidecar.

## Configuration

All knobs live in `.env` and are loaded by `src/config.py`:

```env
# Master switch — leave OFF until R2 creds are populated.
BACKUPS_ENABLED=false

# Cron — UTC, 24h, hour-only granularity.
BACKUP_HOUR_UTC=5

# Cloudflare R2 — get the access key + secret from the
# "R2 → Manage API Tokens" page in the Cloudflare dashboard.
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=polymarket-backups
R2_KEY_PREFIX=postgres/

# GFS retention. Bound = daily + weekly + monthly = 14 max.
BACKUP_RETENTION_DAILY=7
BACKUP_RETENTION_WEEKLY=4
BACKUP_RETENTION_MONTHLY=3
BACKUP_WEEKLY_DOW=6                # 0=Mon … 6=Sun

# Safety nets.
BACKUP_PG_DUMP_TIMEOUT_S=1800
BACKUP_LOCAL_SCRATCH_DIR=/tmp
```

`BACKUPS_ENABLED=false` makes the `backups` container idle — useful
when you want the rest of the stack up but the R2 bucket isn't
provisioned yet.

## Object layout

Keys are written as

```
postgres/2026/04/2026-04-27T05-00-00Z.dump
^^^^^^^^^         ^^^^^^^^^^^^^^^^^^^^^
prefix            UTC ISO timestamp
        ^^^^^^^^
        YYYY/MM folder for dashboard browsing
```

Format is `pg_dump --format=custom --compress=9` so a single object
restores cleanly with `pg_restore`.

## Running by hand

One-off dump (no upload, no retention) — handy when you want a
local snapshot before a risky migration:

```bash
docker compose run --rm backups python scripts/backup_db.py --output /tmp/snap.dump
```

Force a "real" run that uploads to R2 right now:

```bash
docker compose run --rm backups python -c "
import asyncio
from src.config import settings
from src.backups.r2_client import R2Client
from src.backups.job import run_backup_once
client = R2Client(
    endpoint_url=settings.R2_ENDPOINT_URL,
    access_key_id=settings.R2_ACCESS_KEY_ID,
    secret_access_key=settings.R2_SECRET_ACCESS_KEY,
    bucket=settings.R2_BUCKET,
)
print(run_backup_once(
    dsn=settings.DATABASE_URL,
    r2_client=client,
    prefix=settings.R2_KEY_PREFIX,
    scratch_dir=settings.BACKUP_LOCAL_SCRATCH_DIR,
    daily=settings.BACKUP_RETENTION_DAILY,
    weekly=settings.BACKUP_RETENTION_WEEKLY,
    monthly=settings.BACKUP_RETENTION_MONTHLY,
    weekly_dow=settings.BACKUP_WEEKLY_DOW,
    pg_dump_timeout_s=settings.BACKUP_PG_DUMP_TIMEOUT_S,
))
"
```

## Restoring

`scripts/restore_db.py` is the entry point. Steps:

1. List what's available in R2:

   ```bash
   docker compose run --rm backups python scripts/restore_db.py --list
   ```

2. Download a specific object:

   ```bash
   docker compose run --rm backups \
     python scripts/restore_db.py --key postgres/2026/04/2026-04-27T05-00-00Z.dump \
     --dest /tmp/restore.dump --download-only
   ```

3. Restore into a fresh database (DANGEROUS — wipes the target):

   ```bash
   docker compose run --rm backups \
     python scripts/restore_db.py --key postgres/2026/04/2026-04-27T05-00-00Z.dump \
     --target-dsn postgresql://polymarket:<pwd>@postgres:5432/polymarket_restore \
     --clean
   ```

`--clean` passes `--clean --if-exists` to `pg_restore` so existing
objects are dropped before being recreated.

## How retention behaves

`classify_keys` (in `retention.py`) marks each backup with the
buckets it satisfies. Multiple reasons stack — a Sunday-1st-of-month
backup is tagged both `weekly` and `monthly`, but only kept once.
Worst case: `daily + weekly + monthly = 14` objects. Typical day:
8–10 (overlap eats slots).

If `delete_objects` fails mid-sweep, `run_backup_once` does **not**
re-raise — the new backup is already up, and the next cron tick
re-runs the sweep.

## Failure modes & alerts

| Failure                                | Behaviour                                     |
|----------------------------------------|-----------------------------------------------|
| `pg_dump` exits non-zero               | local file deleted, `PgDumpError` logged      |
| `pg_dump` hangs past timeout           | killed via `subprocess.TimeoutExpired`        |
| Upload fails                           | local dump kept on disk for next run          |
| Retention sweep fails after upload     | warning logged, next run retries              |
| Empty dump file                        | `PgDumpError("missing or empty")`             |

The async wrapper (`make_backup_job`) swallows exceptions so the
APScheduler cron keeps firing. Watchdog/Telegram alerts are wired
through Loguru, not via raised exceptions.

## Sanity-check before going live

```bash
# Inside the backups container
pg_dump --version                            # confirms binary path
python -c "import boto3; print(boto3.__version__)"  # 1.34.69
python -m src.backups.main --help            # currently no flags, but
                                             # imports must succeed
```

The compose healthcheck is just `pg_dump --version` — a stuck
scheduler won't fail it, but a missing binary or a busted Python
environment will.
