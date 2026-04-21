# Infrastructure Guide — Zéro budget

## Phase 1 : Développement + Paper Trading (LOCAL)

**Tout tourne en local via Docker Compose.**

```
Ta machine (Mac/Windows/Linux)
├── Docker Desktop
│   ├── PostgreSQL 15 container (port 5432)
│   ├── Redis 7 container (port 6379)
│   └── (optionnel) Grafana container (port 3000)
└── Python processes
    ├── src/registry/main.py     (refresh Falcon toutes les heures)
    ├── src/observer/main.py     (WebSocket + trade tracking)
    └── src/engine/main.py       (decisions + paper trading)
```

**Requis** : Docker Desktop, 2 Go RAM libres, connexion internet stable, clé API Falcon.

**Note** : On n'utilise PAS TimescaleDB. Le volume de données (~50-100 MB/jour de trades
filtrés sur les leaders) ne justifie pas les hypertables. PostgreSQL standard suffit.

---

## Phase 2 : Production 24/7 (ORACLE CLOUD FREE TIER)

**Gratuit pour toujours.** Oracle Cloud Always Free :
- 2 AMD Compute instances (1 OCPU, 1 GB RAM chacune) — PERMANENT
- 2 Block Volumes (50 Go chacun) — PERMANENT
- 10 TB/mois de trafic sortant — PERMANENT

**URL** : https://www.oracle.com/cloud/free/

### Architecture 2 VMs

```
VM 1 — Le cerveau (chemin chaud + tiède)          VM 2 — Le collecteur (Falcon)
═══════════════════════════════════════            ══════════════════════════════
PostgreSQL 15          ~300 MB RAM                  Python runtime     ~100 MB
Redis 7                ~64 MB RAM                   Falcon API client  ~30 MB
WebSocket client       ~20 MB                       Scheduler hourly   ~20 MB
Trade Observer         ~50 MB                       Buffer             ~874 MB
Confidence Engine      ~50 MB
Paper Trader           ~30 MB
Batch worker (pic)     ~200 MB
Buffer                 ~310 MB
                       ─────────                                       ─────────
                       ~1024 MB                                        ~1024 MB
```

**Pourquoi 2 VMs** :
- Le batch nocturne (Hawkes fit, LogReg) consomme un pic de RAM → isolé du chemin chaud
- Falcon polling isolé → un timeout Falcon ne bloque pas le trading
- VM 2 sert de cache Falcon de secours si VM 1 est down

### Gestion mémoire PostgreSQL (1 GB RAM)

```
shared_buffers = 64MB
work_mem = 4MB
maintenance_work_mem = 32MB
effective_cache_size = 128MB
max_connections = 20
```

### Gestion Redis (64 MB)

```
maxmemory 64mb
maxmemory-policy allkeys-lru
appendonly yes
```

---

## Stockage estimé (année 1)

| Données | Volume/jour | Retention | Total année 1 |
|---------|-------------|-----------|---------------|
| Leaders (registry) | ~2000 rows | Permanent | ~10 MB |
| Trades observés | ~50-100 MB | 90 jours rolling | ~5-9 GB |
| Positions reconstruites | ~1 MB | Permanent | ~365 MB |
| Follower edges | ~500K rows | Permanent | ~200 MB |
| Leader profiles (JSONB) | ~2000 rows | Permanent | ~50 MB |
| Paper trades + decision log | ~1 MB | Permanent | ~365 MB |
| **Total** | | | **~10-15 GB** |

Largement dans les 50 GB du Block Volume Oracle.

---

## Docker Compose (local)

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:15
    ports: ["5432:5432"]
    environment:
      POSTGRES_DB: polymarket
      POSTGRES_USER: polymarket
      POSTGRES_PASSWORD: polymarket_dev_password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7.2-alpine
    ports: ["6379:6379"]
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru --appendonly yes
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

### Production overlay (docker-compose.prod.yml)

```yaml
services:
  postgres:
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 300M
    command: >
      postgres
      -c shared_buffers=64MB
      -c work_mem=4MB
      -c max_connections=20

  redis:
    restart: unless-stopped
    command: redis-server --maxmemory 64mb --maxmemory-policy allkeys-lru --appendonly yes
    deploy:
      resources:
        limits:
          memory: 64M
```

---

## Chemin chaud vs froid — Impact infra

```
CHEMIN CHAUD (continu, < 100ms par décision)
├── WebSocket Polymarket → Trade Observer → Redis pub/sub
├── Confidence Engine lit Redis cache (pré-calculé)
├── Paper Trader exécute
└── Impact RAM : ~150 MB constant

CHEMIN TIÈDE (par trade observé, O(1))
├── Beta-Binomial, Dirichlet, EWMA updates
├── Position Tracker state
└── Impact RAM : ~50 MB constant

CHEMIN FROID (batch 1x/jour à 3h AM, ~10 min)
├── Hawkes fit : 200 leaders × ~1s = ~200s séquentiel
├── LogReg bayésienne : 200 leaders × ~2s = ~400s séquentiel
├── LightGBM : 1 retrain global = ~60s (hebdomadaire)
├── Redis cache precompute = ~1s
└── Impact RAM : ~200 MB pic (séquentiel, pas parallèle)
```
