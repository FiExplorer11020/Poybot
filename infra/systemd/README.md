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

## Install (production box, as root)

```bash
# 1. Create the service account once.
useradd --system --home /opt/polymarket-bot --shell /usr/sbin/nologin polymarket

# 2. Copy the units into systemd's search path.
cp infra/systemd/polymarket-*.service /etc/systemd/system/

# 3. Reload, then enable each unit so it survives reboot.
systemctl daemon-reload
systemctl enable polymarket-engine.service
systemctl enable polymarket-observer.service
systemctl enable polymarket-onchain.service
systemctl enable polymarket-crawler.service
systemctl enable polymarket-falcon-refresher.service
systemctl enable polymarket-api.service

# 4. Start them.
systemctl start polymarket-engine.service
systemctl start polymarket-observer.service
systemctl start polymarket-onchain.service
systemctl start polymarket-crawler.service
systemctl start polymarket-falcon-refresher.service
systemctl start polymarket-api.service

# 5. Verify.
systemctl status polymarket-*.service
journalctl -u polymarket-onchain.service -f
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
