# Polymarket Leader Intelligence Engine

Bot de trading orienté intelligence de wallets Polymarket. Le système observe les leaders, reconstruit leurs positions, cartographie leurs followers, profile leurs comportements, modélise leurs erreurs, puis prend des décisions paper trading `FOLLOW` / `FADE` / `SKIP`.

Le bot est déployé en production sur Hetzner Helsinki (CX23, Ubuntu 22.04, 2 vCPU / 4 GB) et tourne 24/7 en mode paper trading. Le live trading est gated par flags (`LIVE_TRADING_DRY_RUN=true` par défaut).

## Capabilities at a glance

- **Leader registry** Falcon API (10 agents : leaderboard, Wallet360, trades, PnL, market insights, etc.)
- **Dual-source ingestion** WebSocket CLOB + REST polling data-api, dedup Redis + DB-level UNIQUE INDEX
- **Position reconstruction** : OPEN -> CLOSE cycles via PositionTracker (sell, merge, resolution)
- **Social graph** leader -> follower via Beta-Binomial hot path + Hawkes MLE batch nightly
- **Behavioral profiling** par wallet : Dirichlet (catégories, size-weighted), EWMA sizing, KDE timing
- **Error modelling** progressif 3 phases : Beta-Binomial -> Bayesian LogReg -> LightGBM + Platt
- **Decision engine** : Thompson Sampling + Bayesian Kelly avec shrinkage
- **Paper trader** + RiskManager (drawdown, consecutive losses, market exposure, mutable runtime config)
- **Dashboard FastAPI 8 onglets** + WebSocket live (Alpha Terminal, ML Progression, Wallet Graph, Live Portfolio, Decision Engine, Inspector, Risk & Config, Bot Health)
- **Cockpit risk runtime-mutable** : POST `/api/risk/update` valide + persiste les overrides en Redis, RiskManager les lit dans les 30 s
- **Inspector** : flux raw temps réel des trades observés + source mix + pipeline health + dernières décisions avec reason

## Repository structure

```
polymarket-bot/
├── src/
│   ├── api/             # FastAPI dashboard + WebSocket bridge + endpoints
│   ├── backups/         # Postgres -> R2 nightly (idle si BACKUPS_ENABLED=false)
│   ├── control/         # Killswitch + RuntimeConfig (mutable risk knobs)
│   ├── database/        # asyncpg pool, schema, queries
│   ├── economics/       # Versioning des modèles éco
│   ├── engine/          # ConfidenceEngine, PaperTrader, LiveTrader, RiskManager,
│   │                    # Scheduler (APScheduler), Watchdog, NeuralReadiness
│   ├── execution/       # py-clob-client wrapper (live trading)
│   ├── graph/           # GraphEngine + HawkesFitter
│   ├── monitoring/      # Health checks, metrics
│   ├── observer/        # WebSocket + REST polling + dedup + PositionTracker
│   ├── profiler/        # BehaviorProfiler + ErrorModel cascade
│   ├── registry/        # Falcon client + LeaderRegistry + sync_markets
│   └── telegram_bot/    # Notifier (sortant) + Bot (commandes /status etc.)
├── docs/                # Architecture + ops docs (voir Documentation ci-dessous)
├── scripts/             # setup_db, batch_runner, healthcheck, cleanup, etc.
├── static/dashboard/    # 3 fichiers JSX transformés par Babel-on-the-fly
├── templates/           # dashboard.html
├── tests/               # Unit + integration
├── docker-compose.yml         # Base (postgres, redis, observer, engine, registry, api, backups)
├── docker-compose.prod.yml    # Memory caps + restart policy + log rotation
└── Dockerfile                 # Multi-stage shared par tous les services Python
```

## Local run

```bash
python -m pip install -e ".[dev]"
cp .env.example .env
# Add FALCON_API_KEY (and optionally TELEGRAM_*) to .env

# Start infra (postgres + redis only)
docker compose up -d postgres redis

# Apply schema
python scripts/setup_db.py

# Bot processes (separate terminals or via supervisor)
python -m src.registry.main
python -m src.observer.main
python -m src.engine.main

# Dashboard API
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Dashboard: `http://127.0.0.1:8000`

Or run the full stack in Docker with the dashboard exposed on `:8080`:

```bash
docker compose up -d --build
docker compose logs -f engine
```

## Production deployment (Hetzner Helsinki)

Le VM `polymarket-prod` (89.167.23.215) tourne `/opt/polymarket-bot/` avec docker-compose. Le déploiement actuel est un workflow rsync + rebuild :

```bash
# Sur le Mac local
rsync -avz --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '.venv/' \
  --exclude '.env' --exclude '*.log' --exclude 'data_cache/' \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/

# Sur le VM
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate
```

Détails infra dans [docs/INFRA.md](docs/INFRA.md).

## Useful commands

```bash
python scripts/health_check.py                # local health probe
python scripts/batch_runner.py                # run nightly batch on demand
python scripts/backfill_decision_learning.py  # rebuild learning state
python scripts/bootstrap_leaders.py           # cold-boot the leaders table
pytest -q                                     # unit + integration tests

# Run cleanup script (e.g. after migration)
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  < scripts/cleanup_falcon_no_data_leaders.sql
```

## Documentation

- [CLAUDE.md](CLAUDE.md) : master architecture context (lis avant de coder)
- [docs/INFRA.md](docs/INFRA.md) : infra Hetzner + Docker + memory budgets
- [docs/docker-setup.md](docs/docker-setup.md) : Dockerfile / compose / healthchecks
- [docs/backups.md](docs/backups.md) : Postgres -> Cloudflare R2 (currently idle)
- [docs/live-trading-setup.md](docs/live-trading-setup.md) : procédure de bascule live
- [docs/PHASE_A_BACKTESTER_DESIGN.md](docs/PHASE_A_BACKTESTER_DESIGN.md) : design du backtester
- [docs/SESSION_2026-05-10_RUNBOOK.md](docs/SESSION_2026-05-10_RUNBOOK.md) : dernière session de patches (DQ + risk cockpit + wallet scanner + inspector + size-weighted profile)
- `src/*/CLAUDE.md` : architecture notes par module

## Runtime map

```text
                ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
                │ Polymarket WS   │  │ Polymarket REST │  │ Falcon API      │
                │ (price_change,  │  │ (data-api,      │  │ (10 agents)     │
                │  book events)   │  │  trades/wallet) │  │                 │
                └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                         │                    │                    │
                         ▼                    ▼                    ▼
                 ┌──────────────────────────────────────────────────────┐
                 │ Observer  (dual-source + dedup Redis + DB UNIQUE)    │
                 └────────┬─────────────────────────────────────────────┘
                          │  publish trades:observed (Redis pub/sub)
        ┌─────────────────┼──────────────────────┬──────────────────┐
        ▼                 ▼                      ▼                  ▼
  ┌──────────┐      ┌──────────────┐      ┌──────────────┐    ┌─────────────┐
  │ Graph    │      │ Profiler     │      │ Confidence   │    │ Inspector   │
  │ Engine   │      │ (Dirichlet,  │      │ Engine       │    │ /api/...    │
  │ + Hawkes │      │  EWMA, KDE,  │      │ (Thompson +  │    │ snapshot    │
  │ batch    │      │  ErrorModel) │      │  Kelly)      │    │ + WS bridge │
  └──────────┘      └──────────────┘      └──────┬───────┘    └─────────────┘
                                                 │
                                                 ▼
                                         ┌─────────────────┐
                                         │ DecisionRouter  │
                                         └────────┬────────┘
                                                  ▼
                              ┌──────────────────────────────────┐
                              │ PaperTrader (or LiveTrader)      │
                              │ + RiskManager (mutable runtime   │
                              │  config via Redis pub/sub)       │
                              └──────────────────────────────────┘
```

PostgreSQL 15 backs everything; Redis 7.2 carries dedup keys, killswitch state, runtime config overrides, pub/sub channels (`trades:observed`, `runtime_config:changed`), and price cache.
