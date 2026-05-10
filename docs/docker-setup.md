# Docker setup

Build, run, and ship the bot via Docker. Everything below assumes you
have `docker` and `docker compose` v2 on the host.

## Image

The repo ships a single multi-stage `Dockerfile`:

- **builder stage** — `python:3.11-slim` + `build-essential`. Installs
  the package and the `[backtest]` extra into `/opt/venv`. Pulls
  pre-built wheels for jax / lightgbm / scipy / numpyro to keep the
  build under ~3 min.
- **runtime stage** — `python:3.11-slim` (no compiler, no headers).
  Copies `/opt/venv` from the builder, drops to a non-root user
  (`polymarket`, uid 1000), `tini` as PID 1, `HEALTHCHECK` pointing at
  `scripts/docker_healthcheck.py`. Also bundles `docs/migrations/` so
  `scripts/setup_db.py` can apply pending schema changes at boot.

A single image `polymarket-bot:latest` is reused by every application
service. Each compose service overrides `command:` to pick the entry
point.

## Local dev

```bash
# Backends only (Postgres + Redis), then run python from your shell.
docker compose up -d postgres redis

# Or full stack with the dashboard exposed on :8080.
docker compose up -d
docker compose logs -f engine
```

Inside the compose network, `DATABASE_URL` and `REDIS_URL` are
overridden in `docker-compose.yml` to use service DNS
(`postgres:5432`, `redis:6379`) regardless of what's in `.env`. Your
`.env` keeps localhost values for native (non-Docker) runs.

## Production (Hetzner Helsinki — `/opt/polymarket-bot/`)

```bash
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The deployment workflow on the Mac side is rsync-based (the VM does
**not** track a git remote in `/opt/polymarket-bot/`):

```bash
rsync -avz --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '.venv/' \
  --exclude '.env' --exclude '*.log' --exclude 'data_cache/' \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/
```

The prod overlay adds:

- `restart: unless-stopped` on every service.
- Memory caps that fit the 4 GB Hetzner CX23 with comfortable headroom:
  postgres 300 MB, redis 64 MB, observer 300 MB, engine 600 MB,
  registry 200 MB, api 200 MB, backups 200 MB. Total 1.66 GB hard-cap.
- Postgres tuning flags (shared_buffers, work_mem, etc.).
- `json-file` log rotation capped at `10m × 5 files` per service.
- Removes the host-port mapping for postgres + redis so they only
  listen on the compose bridge.
- Keeps `:8080` exposed on the api service (the public dashboard).

## Healthchecks

Every app service runs `scripts/docker_healthcheck.py` every 30 s
(start-period 60 s for observer / engine). The probe pings Redis +
Postgres and exits non-zero if either is unreachable.

The api service uses its own `/healthz` endpoint instead — that
catches dashboard-specific regressions (template path, static dir,
etc.) that the generic probe wouldn't.

## Tests

`tests/test_docker.py` locks the structural invariants of the image
(multi-stage, non-root, tini, healthcheck), the compose services
(commands, dependencies, healthchecks, network URLs), and the prod
overlay (memory limits, restart policy, log rotation). It runs without
Docker installed — purely YAML and text inspection — so it's safe in
CI.

```bash
python -m pytest tests/test_docker.py -v
```

## What this does NOT cover (yet)

- **Image registry push.** Build is local on the Hetzner VM after rsync;
  there's no `docker push` step. Switching to a remote registry (GHCR,
  ECR) would let us push from CI and pull from the VM, which is the
  natural next step once builds outgrow the VM's CPU.
- **Secrets.** `.env` is bind-mounted via `env_file:`. Move to docker
  secrets / sops when we go fully live.
- **Backups.** Postgres backups → Cloudflare R2 are wired but the
  service is idle (`BACKUPS_ENABLED=false`) until R2 credentials are
  populated. See [backups.md](backups.md).
