# Poybot — Guide unique d'installation et d'exploitation (MVP complet)

> **Objectif de ce README**
>
> Ce document est conçu pour être **la seule documentation à lire** pour :
> 1) comprendre ce que fait le projet,
> 2) installer tout depuis un repository brut,
> 3) lancer le backend + frontend en local,
> 4) vérifier que le bot MVP fonctionne,
> 5) diagnostiquer les erreurs courantes.
>
> Si vous ne connaissez pas le code, vous devez quand même pouvoir suivre ce guide pas à pas.

---

## 1) Vue d'ensemble (ce que vous lancez exactement)

Le projet est un **MVP de bot d'intelligence Polymarket** avec deux parties :

- **Backend (FastAPI, Python)** :
  - ingestion et normalisation de données marché,
  - endpoints API pour le frontend,
  - endpoint WebSocket live pour pousser des updates en temps réel,
  - jobs worker (ARQ) pour synchroniser des données.
- **Frontend (Next.js)** :
  - dashboard unique (dark/neon) qui affiche :
    - statut bot (RUNNING/PAUSED/STOPPED),
    - uptime / latence,
    - cartes de markets scanner avec badge `DETECTED`,
    - graphe live,
    - table des dernières simulations d'exécution.

### Important sur l'état MVP

Le MVP privilégie la fiabilité de démo et le flux end-to-end.
Certaines données live scanner sont simulées côté hub mémoire pour garantir une démo stable même en cas de problème réseau externe.

---

## 2) Architecture rapide (sans entrer dans le code)

### Backend

- `app/main.py` : application FastAPI + route WebSocket `/ws/live`.
- `app/api/v1/` : routes REST (events, markets, summary, live-summary, control bot, simulate exec).
- `app/live/state.py` : hub mémoire temps réel (état bot, ticks live, broadcast WS).
- `app/ingestion/ws_ingestor.py` : ingestion websocket CLOB (batch + orjson).
- `app/models/` + Alembic : schéma PostgreSQL.
- `app/workers/tasks.py` : jobs ARQ (sync metadata, refresh trades).

### Frontend

- `frontend/app/page.tsx` : page unique.
- `frontend/components/Dashboard.tsx` : logique UI + WebSocket.
- `frontend/lib/types.ts` : types TS de payload live.

### Infra locale

- PostgreSQL (stockage principal)
- Redis (queue/jobs)
- ClickHouse (préparé pour analytics)
- API backend
- Worker backend
- Frontend Next.js

Le tout peut démarrer via Docker Compose.

---

## 3) Prérequis (obligatoires)

## Option recommandée (Docker)

- Docker installé
- Docker Compose installé
- Ports libres :
  - `3000` (frontend)
  - `8000` (backend)
  - `5432` (PostgreSQL)
  - `6379` (Redis)
  - `8123`/`9000` (ClickHouse)

## Option manuelle (sans Docker)

- Python 3.12
- Node.js 20+
- PostgreSQL local
- Redis local

> Si vous débutez : utilisez **Docker** (beaucoup plus simple).

---

## 4) Installation depuis repository brut (méthode Docker, recommandée)

Supposons que vous êtes au tout début, repo fraîchement cloné.

### Étape 1 — Cloner

```bash
git clone <URL_DU_REPO>
cd Poybot
```

### Étape 2 — Créer les variables backend

```bash
cd backend
cp .env.example .env
```

Vous pouvez laisser les valeurs par défaut pour un premier lancement.

### Étape 3 — Lancer toute la stack

Toujours dans `backend/` :

```bash
docker compose up --build
```

Ce que cela lance :
- backend API,
- worker,
- frontend,
- postgres,
- redis,
- clickhouse.

### Étape 4 — Appliquer la migration base de données

Dans un nouveau terminal :

```bash
cd Poybot/backend
docker compose run --rm api alembic upgrade head
```

### Étape 5 — Vérifier les URLs

- Frontend : http://localhost:3000
- Backend API docs : http://localhost:8000/docs
- Health backend : http://localhost:8000/health

---

## 5) Premier run guidé (ce que vous devez voir)

1. Ouvrir http://localhost:3000
2. En haut du dashboard :
   - titre `POLYMARKET ARB BOT MVP`
   - badge statut bot
   - uptime
   - latence
3. Cliquer `START`, `PAUSE`, `STOP` pour vérifier la commande bot.
4. Vérifier que les cartes scanner bougent (spreads, detected, etc.).
5. Cliquer `Simulate Exec` sur une carte.
6. Vérifier que la table basse se remplit avec une nouvelle ligne de simulation.

Si tout ceci est OK, le MVP fonctionne end-to-end.

---

## 6) Endpoints clés à connaître (minimum utile)

### Endpoints système

- `GET /health` → status simple
- `GET /ready` → readiness backend/DB

### Endpoints live MVP

- `GET /api/v1/live-summary`
  - snapshot complet pour initialiser le dashboard
- `POST /api/v1/bot/control`
  - body : `{ "command": "start|pause|stop" }`
- `POST /api/v1/markets/{market_id}/simulate-exec`
  - génère une simulation d'exécution
- `WS /ws/live`
  - push en temps réel vers le frontend

### Endpoints data backend (phase 1)

- `GET /api/v1/events`
- `GET /api/v1/events/{event_id}`
- `GET /api/v1/markets`
- `GET /api/v1/markets/{market_id}`
- `GET /api/v1/markets/{market_id}/book`
- `GET /api/v1/markets/{market_id}/trades`
- `GET /api/v1/markets/{market_id}/price-history`
- `GET /api/v1/markets/{market_id}/summary`
- `GET /api/v1/tags`
- `GET /api/v1/system/sync-status`

---

## 7) Variables d'environnement (explication pratique)

## Backend (`backend/.env`)

Variables principales :

- `POSTGRES_DSN` : connexion DB backend
- `REDIS_URL` : connexion Redis pour worker
- `POLYMARKET_GAMMA_BASE_URL` : endpoint metadata
- `POLYMARKET_CLOB_REST_BASE_URL` : endpoint trades/book REST
- `POLYMARKET_CLOB_WS_URL` : websocket CLOB
- `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE` : pagination API

Par défaut, les valeurs du `.env.example` conviennent pour Docker.

## Frontend (`frontend/.env.local` si run hors Docker)

Créer depuis l'exemple :

```bash
cd frontend
cp .env.local.example .env.local
```

- `NEXT_PUBLIC_API_BASE=http://localhost:8000`

---

## 8) Lancement sans Docker (option avancée)

À faire seulement si vous savez gérer DB/Redis localement.

## Backend

```bash
cd Poybot/backend
pip install -e .[dev]
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Worker (dans un 2e terminal)

```bash
cd Poybot/backend
arq app.workers.tasks.WorkerSettings
```

## Frontend (dans un 3e terminal)

```bash
cd Poybot/frontend
cp .env.local.example .env.local
npm install
npm run dev
```

---

## 9) Commandes Make utiles (backend)

Depuis `backend/` :

```bash
make up         # docker compose up --build -d
make down       # stop + supprime volumes
make migrate    # alembic upgrade head
make run        # lance api en local
make test       # pytest
make lint       # ruff
make format     # black
make frontend   # lance frontend (hors docker)
```

---

## 10) Vérification rapide de diagnostic (checklist)

Si ça ne marche pas :

1. `docker compose ps` dans `backend/` : tous les services doivent être `Up`.
2. `curl http://localhost:8000/health` doit répondre `{ "status": "ok" }`.
3. Frontend chargé sur `http://localhost:3000`.
4. Si écran vide frontend :
   - vérifier `NEXT_PUBLIC_API_BASE`,
   - ouvrir console navigateur (erreurs WS/CORS).
5. Si erreurs DB :
   - relancer `alembic upgrade head`.

---

## 11) Reset complet propre (repartir de zéro)

```bash
cd Poybot/backend
docker compose down -v
docker compose up --build
docker compose run --rm api alembic upgrade head
```

Cela supprime les volumes (données locales).

---

## 12) Latence et performance (MVP actuel)

Optimisations déjà intégrées côté hot-path :

1. `orjson` pour réponses API et parsing WS
2. push WS direct backend → frontend (pas de polling)
3. cache état live en mémoire (`LiveHub`)
4. batch de flush en ingestion WS backend
5. reconnect WS plus agressif

Cible locale réaliste MVP :
- ~200-300ms en mode simple
- ~50-80ms sur le chemin live in-memory + websocket

---

## 13) Limites actuelles (assumées)

- Scanner live de la page MVP : comportement partiellement simulé pour fiabilité démo.
- Pas d'authentification utilisateur.
- Pas d'exécution réelle d'ordres (simulations uniquement).
- ClickHouse présent dans la stack, mais analytics avancées encore limitées en phase 1.

---

## 14) Workflow conseillé si vous êtes non-tech

1. Suivre section **4** exactement.
2. Vérifier section **5** (comportement visuel attendu).
3. En cas d'erreur, suivre section **10**.
4. Si toujours bloqué, faire section **11** (reset complet).

---

## 15) Résumé ultra-court (TL;DR)

```bash
git clone <URL_DU_REPO>
cd Poybot/backend
cp .env.example .env
docker compose up --build
# nouveau terminal
cd Poybot/backend
docker compose run --rm api alembic upgrade head
```

Puis ouvrir :
- http://localhost:3000 (dashboard)
- http://localhost:8000/docs (API)

Vous avez alors un MVP complet backend + frontend en local.
