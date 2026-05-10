# Infrastructure Guide

## Phase 1 — Local development

**Tout tourne en local via Docker Compose.**

```
Ta machine (Mac/Windows/Linux)
├── Docker Desktop
│   ├── PostgreSQL 15 container (port 5432)
│   ├── Redis 7.2 container  (port 6379)
│   └── (full stack) observer / engine / registry / api / backups
└── Python (si tu lances en natif)
    ├── src/registry/main.py     (refresh Falcon + sync_markets)
    ├── src/observer/main.py     (WebSocket + REST polling)
    ├── src/engine/main.py       (decisions + paper trading + scheduler + watchdog)
    └── src/api/main.py          (FastAPI dashboard)
```

**Requis** : Docker Desktop, 4 Go RAM libres, connexion internet stable, clé API Falcon.

**Note** : Pas de TimescaleDB. Le volume (~10-15 GB/an de trades + reconstructions) ne justifie pas les hypertables. PostgreSQL standard suffit.

---

## Phase 2 — Production sur Hetzner Cloud

**VM actuelle** : `polymarket-prod`, datacenter Helsinki (HEL1), Finlande.

### Pourquoi Helsinki, pas Falkenstein ni Oracle

Polymarket bloque par géolocalisation IP. État au moment de la rédaction :

| Pays         | Statut                                   |
|--------------|------------------------------------------|
| France       | full block (deposit + trading + access)  |
| Allemagne    | trading-restricted (peut hold, pas open) |
| USA          | full block (CFTC)                        |
| Singapour    | full block                               |
| Finlande     | OK ✅                                     |

Le free-tier Oracle Cloud (Ampere A1 eu-paris-1) avait été testé pendant 5 jours mais saturait constamment. Hetzner Helsinki est passé devant pour deux raisons : pas de bloc Polymarket et performances stables.

### VM specs

```
Provider       Hetzner Cloud
Plan           CX23 (renamed from CX22)
CPU            2 vCPU AMD
RAM            4 GB
Disk           40 GB SSD
Bandwidth      20 TB/mo
Region         eu-helsinki-1 (HEL1)
OS             Ubuntu 22.04 LTS x86
Public IP      89.167.23.215 (fixed IPv4)
Cost           €4.79 server + €0.96 backups + €0.60 IPv4 = €6.35/mo TTC
```

### Hardening

```
UFW            default deny, allow SSH:22 from 81.250.173.80 only,
               allow 0.0.0.0 on :8080 (public dashboard)
fail2ban       active, bans IPs after 5 SSH fails
SSH            password auth disabled, root login disabled,
               ed25519 key only (~/.ssh/hetzner_polymarket on local)
unattended-upgrades   security patches auto, reboot 04:00 UTC if kernel
User           non-root `polymarket` (uid 1000), sudo NOPASSWD
```

---

## Docker stack (7 services)

Le `docker-compose.yml` + `docker-compose.prod.yml` orchestrent :

| Service               | Image                | Mem cap | Role                                              |
|-----------------------|----------------------|---------|---------------------------------------------------|
| `polymarket_db`       | `postgres:15`        | 300 MB  | Main DB                                           |
| `polymarket_redis`    | `redis:7.2-alpine`   |  64 MB  | Cache + pub/sub                                   |
| `polymarket_observer` | `polymarket-bot:latest` | 300 MB | WS + REST polling ingestion                    |
| `polymarket_engine`   | `polymarket-bot:latest` | 600 MB | Profiler + ConfidenceEngine + PaperTrader + GraphEngine + Telegram + Scheduler + Watchdog |
| `polymarket_registry` | `polymarket-bot:latest` | 200 MB | Falcon leaderboard refresh + sync_markets       |
| `polymarket_api`      | `polymarket-bot:latest` | 200 MB | FastAPI dashboard on :8080                      |
| `polymarket_backups`  | `polymarket-bot:latest` | 200 MB | pg_dump nightly -> R2 (idle if `BACKUPS_ENABLED=false`) |

Total mem cap: **1.66 GB hard cap on 4 GB available** — comfortable margin for kernel + pgvector + transient batch peaks.

Tous les services applicatifs partagent la même image Docker `polymarket-bot:latest`. Le `command:` de chaque service dans le compose sélectionne l'entry point. Image multi-stage (builder + runtime), user non-root `polymarket` uid=1000.

### Healthchecks

| Service     | Probe                                       |
|-------------|---------------------------------------------|
| postgres    | `pg_isready`                                |
| redis       | `redis-cli ping`                            |
| observer    | `scripts/docker_healthcheck.py` (DB+Redis)  |
| engine      | `scripts/docker_healthcheck.py`             |
| registry    | `scripts/docker_healthcheck.py`             |
| api         | `GET /healthz` -> 200                       |
| backups     | `pg_dump --version`                         |

### Scheduler (APScheduler dans `polymarket_engine`)

| Job                  | Schedule           | Effet                                                  |
|----------------------|--------------------|--------------------------------------------------------|
| `nightly_batch`      | cron 03:00 UTC     | Hawkes refit + LogReg refit + retention prune          |
| `redis_cleanup`      | cron 04:00 UTC     | Purge orphan heartbeats + dedup keys expirés           |
| `killswitch_sync`    | interval 300 s     | Lit le killswitch DB, met en cache Redis               |
| `watchdog`           | interval 30 s      | Vérifie que profiler/confidence/paper_trader/graph/telegram_bot sont vivants |
| `refresh_thresholds` | interval 300 s     | Recalcule la maturity système, regénère adaptive thresholds |

---

## Storage estimate (year 1)

| Données                       | Volume/jour | Retention      | Total an 1   |
|-------------------------------|-------------|----------------|--------------|
| Leaders (registry)            | ~2000 rows  | Permanent      | ~10 MB       |
| Trades observés               | ~50-100 MB  | 90 days roll   | ~5-9 GB      |
| Positions reconstructed       | ~1 MB       | Permanent      | ~365 MB      |
| Follower edges                | ~500K rows  | Permanent      | ~200 MB      |
| Leader profiles (JSONB)       | ~2000 rows  | Permanent      | ~50 MB       |
| Paper trades + decision_log   | ~1 MB       | Permanent      | ~365 MB      |
| **Total**                     |             |                | **~10-15 GB** |

Le SSD 40 GB du CX23 absorbe sans problème.

---

## Memory map (running totals on the prod VM)

```
PostgreSQL 15        ~300 MB (shared_buffers=64MB, work_mem=4MB)
Redis 7.2            ~64 MB  (maxmemory 64mb, allkeys-lru)
Observer             ~300 MB (WS + REST + asyncpg pool)
Engine               ~600 MB (heaviest — profiler + ML + scheduler + telegram)
Registry             ~200 MB (Falcon polling, lightweight)
API                  ~200 MB (FastAPI + uvicorn + 1s push loop)
Backups              ~200 MB (idle most of the day, peaks during pg_dump)
                     ────────
Allocated            1.66 GB  (hard caps in docker-compose.prod.yml)
Kernel + buffers     ~700 MB
Free                 ~1.6 GB  (headroom for Hawkes batch nocturne)
```

---

## Hot vs cold paths

```
HOT PATH (continuous, < 100 ms per decision)
├── Polymarket WS -> Observer -> Redis pub/sub trades:observed
├── ConfidenceEngine reads Redis cache (pre-computed Thompson + Kelly inputs)
├── PaperTrader executes (or LiveTrader gated by killswitch)
└── RAM: ~150 MB constant (per-decision allocations recycled fast)

WARM PATH (per trade observed, O(1))
├── Beta-Binomial follower_edges, Dirichlet category, EWMA sizing
├── PositionTracker open->close
└── RAM: ~50 MB constant

COLD PATH (nightly batch 03:00 UTC, ~10 min)
├── Hawkes MLE fit: 200 leaders × ~1 s = ~200 s sequential
├── Bayesian LogReg: 200 leaders × ~2 s = ~400 s sequential
├── LightGBM (weekly only): 1 retrain global = ~60 s
├── Redis precompute = ~1 s
└── RAM peak: ~200 MB (sequential, not parallel)
```

---

## Configuration knobs

The full reference is `src/config.py`. Two layers:

1. **Boot defaults** (env-driven, immutable at runtime). E.g. `INITIAL_LEADER_COUNT`, `FALCON_REFRESH_INTERVAL_S`, `EWMA_LAMBDA`, `MIN_FALCON_SCORE`, `BATCH_HOUR_UTC`.
2. **Runtime-mutable** (live cockpit) — `src/control/runtime_config.py`. Every key in `ALLOWED_KEYS` is editable from the dashboard's RISK & CONFIG tab via `POST /api/risk/update`. Values are validated against `BOUNDS`, persisted in Redis (`runtime_config:overrides`), and propagate to the engine within 30 s. Currently editable: `risk_per_trade_pct`, `max_total_exposure_pct`, `kelly_fraction`, `max_drawdown_stop_pct`, `min_signal_strength`, `max_concurrent_positions`, `cooldown_seconds`, `max_consecutive_losses`, `max_recent_losses_per_market`, `fade_size_ratio`.

---

## External dashboards

```
Dashboard:    http://89.167.23.215:8080            (public, read-only API + WS)
SSH:          ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
Hetzner:      https://console.hetzner.cloud
DNS:          (none — direct IP for now, no TLS terminator)
```

Pas encore de monitoring externe — UptimeRobot + R2 backups sont les deux items pending dans la roadmap.
