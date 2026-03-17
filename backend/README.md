# Poybot MVP — Backend + Frontend Trading Dashboard

## What is included now

This repository now runs a full local MVP with:
- Backend API + workers (FastAPI, SQLAlchemy, ARQ)
- Real-time websocket dashboard feed
- Frontend with two pages:
  - **Dashboard** (live scanner + bot trade list + money management metrics)
  - **PnL Analytics** (timeframe chart)
- Data acquisition architecture redesigned to support full-market scaling foundations:
  - tradable universe builder for active markets
  - subscription plan sharding for all token tickers

## New portfolio / money-management metrics shown on dashboard

- Portfolio global amount (`portfolio_total`)
- Capital currently in trade (`capital_in_trade`)
- PnL in absolute dollars (`total_pnl`)
- PnL in percentage (`pnl_percent`)

## New trade list behavior

Bottom table now shows bot entries with **Polymarket bet names** (`market_title`) and trade details:
- side
- price
- size
- notional
- pnl abs
- pnl %
- timestamp


## Wallet integration scaffold (backend + frontend)

A wallet auth flow is now available for frontend integration:
- `POST /api/v1/wallet/nonce` → creates a nonce + sign-in message for an address
- `POST /api/v1/wallet/verify` → verifies signed payload format + creates an app session token
- `GET /api/v1/wallet/session` → validates current bearer session
- `POST /api/v1/wallet/disconnect` → revokes bearer session

> MVP note: this is an integration scaffold for wallet plug-in and app session wiring.
> Use a production-grade on-chain signature verification library and persistent session store before live trading.

## API additions

- `GET /api/v1/live-summary`
- `POST /api/v1/bot/control`
- `POST /api/v1/markets/{market_id}/simulate-exec` with `{ "market_title": "..." }`
- `GET /api/v1/trades/bot-history`
- `GET /api/v1/portfolio/pnl-by-timeframe?timeframe=24h|7d|30d|90d`
- `WS /ws/live`

## Data acquisition redesign (scalable architecture)

Added new ingestion architecture modules:

- `app/ingestion/universe.py`
  - builds active tradable universe from Gamma
  - keeps market metadata + all token ids needed for full-platform trading

- `app/ingestion/stream_manager.py`
  - builds token subscription shards (chunking)
  - designed to scale websocket ingestion over the full ticker set

These modules create the foundation to ingest and trade across all active Polymarket tickers.

## Local quick start (Docker)
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
docker compose up --build
```

In another terminal:

```bash
cd backend
docker compose run --rm api alembic upgrade head
```

Open:
- Frontend: http://localhost:3000
- Backend docs: http://localhost:8000/docs

## Local run without Docker

Backend:

```bash
cd backend
pip install -e .[dev]
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
cp .env.local.example .env.local
npm install
npm run dev
```

## Environment variables

Backend (`backend/.env`):
- `POSTGRES_DSN`
- `REDIS_URL`
- `API_AUTH_TOKEN` (optional, protects sensitive control endpoints)
- `LIVE_WS_TOKEN` (optional, protects `/ws/live`)
- `ENABLE_RATE_LIMIT` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`
- `POLYMARKET_GAMMA_BASE_URL`
- `POLYMARKET_CLOB_REST_BASE_URL`
- `POLYMARKET_CLOB_WS_URL`
- `DEFAULT_PAGE_SIZE`
- `MAX_PAGE_SIZE`

Frontend (`frontend/.env.local` when not using docker compose env):
- `NEXT_PUBLIC_API_BASE`
- `NEXT_PUBLIC_LIVE_WS_TOKEN` (must match `LIVE_WS_TOKEN` when WS auth is enabled)

## Migrations

Two revisions are now expected:
- `0001_initial`
- `0002_portfolio_bot_trades`

## Notes

- Current MVP trading entries are simulated execution events with realistic fields.
- The architecture now supports adding persistent detailed historical trade/position endpoints from the new `bot_trades` and `portfolio_snapshots` tables.

## Trading specification

Quant/risk rules are now formalized in `TRADING_SPEC.md` and implemented in runtime via `app/services/adaptive_strategy.py` + `app/live/state.py`.
This includes Polymarket-compatible probability bounds, dynamic edge thresholds, spread/volatility penalties, risk caps, Kelly scaling, and cost-aware execution simulation.
