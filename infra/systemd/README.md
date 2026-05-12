# Polymarket bot — systemd units

This directory holds the systemd unit files for the Round 6 split-daemon
topology (see `docs/ROUND_6_THE_SPINE.md` § 3.5).

| Unit                                       | Module                          | Memory budget |
|--------------------------------------------|---------------------------------|---------------|
| `polymarket-engine.service`                | `src.engine.main`               | 800 MB        |
| `polymarket-observer.service`              | `src.observer.main`             | 400 MB        |
| `polymarket-onchain.service`               | `src.onchain.main` *(Wave 2)*   | 400 MB        |
| `polymarket-crawler.service`               | `src.crawler.main` *(Wave 2)*   | 200 MB        |
| `polymarket-falcon-refresher.service`      | `src.registry.refresher_main` *(Wave 2)* | 200 MB |
| `polymarket-mempool.service`               | `src.mempool` *(Round 7)*       | 300 MB        |
| `polymarket-strategy-classifier.service`   | `src.strategy_classifier` *(Round 8)* | 400 MB  |
| `polymarket-follower-volume.service`       | `src.follower_volume` *(Round 9)* | 400 MB        |
| `polymarket-causal.service`                | `src.causal` *(Round 10)*       | 500 MB        |
| `polymarket-book-l3.service`               | `src.observer.clob_book_main` *(Round 11)* | 500 MB |
| `polymarket-microstructure.service`        | `src.microstructure` *(Round 11)* | 400 MB        |
| `polymarket-social.service`                | `src.social` *(Round 12)*       | 300 MB        |
| `polymarket-crossmarket.service`           | `src.cross_market` *(Round 12)* | 300 MB        |
| `polymarket-api.service`                   | `src.api.main` (uvicorn)        | 300 MB        |

Total budget: ~4.7 GB (CX23 has 4 GB; Round 12 adds two more daemons —
operators may need to provision a CX33 or move ingest to a dedicated
box. The L3 firehose + microstructure deriver pair remains the dominant
cost; Round 12's social + cross-market daemons together fit in 600 MB.
The R11 daemons MUST run alongside a 500 GB Hetzner volume mounted at
the Postgres data directory — see R11 § 2.3 for the storage rationale.

**Round 12 operator gates (NOT shipped in code, deliverable separately):**

* X API basic-tier subscription (~$100/mo) — `X_API_KEY` in `.env`.
* NLP classifier model file at `NLP_CLASSIFIER_MODEL_PATH`. Until the
  operator delivers a trained sklearn pipeline, the daemon runs the
  built-in `HeuristicTweetClassifier` (rule-based; ~50µs per call).
* Kalshi API key (free, rate-limited) — `KALSHI_API_KEY`.
* Telegram bot token + public-channel list — `TELEGRAM_BOT_TOKEN_READ`
  and `TELEGRAM_PUBLIC_CHANNELS`.
* Discord bot token + public-channel list — `DISCORD_BOT_TOKEN_READ`
  and `DISCORD_PUBLIC_CHANNELS`.
* ~100 manual wallet-resolution seeds via `WalletResolver.seed_manual`
  (operator script). Per spec § 7 acceptance: ≥ 10 cross-market
  operators resolved before R8 retrain.

The `polymarket-causal.service` unit hosts the Round 10 nightly 2SLS
estimator. It runs once per day (default 04:00 UTC) and exits cleanly
between passes; the systemd unit keeps the process alive across the
sleep window. The engine's APScheduler also registers a `causal_nightly`
cron job at the same hour — operators can deploy EITHER the systemd
unit OR rely on the engine cron, both write the same `causal_estimates`
table.

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
sudo systemctl enable --now polymarket-mempool.service
sudo systemctl enable --now polymarket-book-l3.service
sudo systemctl enable --now polymarket-microstructure.service
sudo systemctl enable --now polymarket-social.service
sudo systemctl enable --now polymarket-crossmarket.service
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
