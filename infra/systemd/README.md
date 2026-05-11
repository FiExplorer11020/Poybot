# Polymarket bot — systemd units

This directory holds the systemd unit files for the Round 6 split-daemon
topology (see `docs/ROUND_6_THE_SPINE.md` § 3.5).

| Unit                                   | Module                          | Memory budget |
|----------------------------------------|---------------------------------|---------------|
| `polymarket-engine.service`            | `src.engine.main`               | 800 MB        |
| `polymarket-observer.service`          | `src.observer.main`             | 400 MB        |
| `polymarket-onchain.service`           | `src.onchain.main` *(Wave 2)*   | 400 MB        |
| `polymarket-crawler.service`           | `src.crawler.main` *(Wave 2)*   | 200 MB        |
| `polymarket-falcon-refresher.service`  | `src.registry.refresher_main` *(Wave 2)* | 200 MB |
| `polymarket-api.service`               | `src.api.main` (uvicorn)        | 300 MB        |

Total budget: ~2.3 GB (CX23 has 4 GB; leaves headroom for Postgres + Redis).

## Pre-flight

Confirm the prerequisites are in place on the box (`polymarket-prod`,
`/opt/polymarket-bot/`):

```bash
# 1. The service account exists and owns the deploy tree.
id polymarket || sudo useradd --system --home /opt/polymarket-bot --shell /usr/sbin/nologin polymarket
sudo chown -R polymarket:polymarket /opt/polymarket-bot

# 2. The venv exists and the deploy is current (rsync from local).
ls /opt/polymarket-bot/.venv/bin/python
ls /opt/polymarket-bot/.env

# 3. Postgres + Redis are reachable.
sudo -u polymarket /opt/polymarket-bot/.venv/bin/python -c \
  "from src.database.connection import get_db; import asyncio; asyncio.run(get_db().__aenter__())"
```

If any of those fail, fix them BEFORE installing the units — a unit
that crashes on first start because `.env` is missing is a confusing
first-impression for an operator new to the box.

## Install (production box, as root)

```bash
# 1. Copy the units into systemd's search path.
sudo cp infra/systemd/polymarket-*.service /etc/systemd/system/

# 2. Reload systemd so it picks up the new units.
sudo systemctl daemon-reload

# 3. Enable + start each unit in one shot (--now is idempotent and
#    survives reboot).
sudo systemctl enable --now polymarket-engine.service
sudo systemctl enable --now polymarket-observer.service
sudo systemctl enable --now polymarket-onchain.service
sudo systemctl enable --now polymarket-crawler.service
sudo systemctl enable --now polymarket-falcon-refresher.service
sudo systemctl enable --now polymarket-api.service

# 4. Verify everything is active (running).
systemctl status polymarket-*.service

# 5. Tail a unit's journal to watch startup.
journalctl -u polymarket-onchain.service -f
```

If a unit shows `failed` or `activating (auto-restart)`, the journal
is the source of truth — `journalctl -u <unit> -n 200 --no-pager`
typically tells you whether it's a missing dependency, a Python import
error, or a `.env` issue.

## Migration from the pre-Round-6 monolith

Until Round 6, ingestion lived inside `polymarket-engine.service`. The
new unit set splits ingestion across separate processes. Switch over
in this order so there's never a window with zero ingestion:

```bash
# 1. Drop the legacy unit's ingestion responsibilities. The engine
#    will keep running but stop spawning its in-process WS / crawler
#    coroutines (controlled by INGESTION_IN_PROCESS=false in .env).
sudo sed -i 's/^INGESTION_IN_PROCESS=.*/INGESTION_IN_PROCESS=false/' /opt/polymarket-bot/.env

# 2. Restart engine so it picks up the new env.
sudo systemctl restart polymarket-engine.service

# 3. Bring the new daemons online (already enabled from the install
#    step above).
sudo systemctl start polymarket-onchain.service \
                     polymarket-crawler.service \
                     polymarket-falcon-refresher.service

# 4. Watch chain_sync_state.last_processed_block + wallet_universe row
#    count tick forward over the next few minutes. If they don't, fall
#    back via the rollback section below.
```

## Operational notes

- Every unit declares `EnvironmentFile=/opt/polymarket-bot/.env`. The
  file must exist and be readable by the `polymarket` user; otherwise
  the unit start fails fast.
- `Restart=always, RestartSec=5s` — a daemon that crashes hard will be
  back online in ~5 s. `NRestarts` is exposed via
  `polybot_ingestion_daemon_restarts_total` (see
  `src/ingestion_daemon/supervisor.py`).
- `MemoryMax=` is the hard ceiling. Hitting it kills the unit via
  cgroup OOM; the `Restart=always` policy then brings it back. The
  intent is "fail fast on a leak, never wedge the box".
- `polymarket-crawler.service` declares `After=polymarket-onchain.service`
  because the crawler's hot path piggybacks on events the listener
  publishes to `chain:trades:stream`.

## Verifying a clean shutdown

```bash
systemctl stop polymarket-onchain.service
# In another terminal, watch chain_sync_state.last_processed_block.
# It should advance by a small final batch and then stop — the listener
# flushes its in-flight events on SIGTERM (see
# CLOBChainListener.stop()).
```

## Rollback

```bash
# Stop and disable every Round-6 daemon.
systemctl stop polymarket-onchain.service polymarket-crawler.service \
               polymarket-falcon-refresher.service
systemctl disable polymarket-onchain.service polymarket-crawler.service \
                  polymarket-falcon-refresher.service

# Engine/observer/api fall back to their pre-Round-6 behaviour
# (observer keeps REST/WS polling; engine still trades from REST data).
```
