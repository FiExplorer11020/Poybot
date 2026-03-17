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
- `POLYMARKET_GAMMA_BASE_URL`
- `POLYMARKET_CLOB_REST_BASE_URL`
- `POLYMARKET_CLOB_WS_URL`
- `DEFAULT_PAGE_SIZE`
- `MAX_PAGE_SIZE`

Frontend (`frontend/.env.local` when not using docker compose env):
- `NEXT_PUBLIC_API_BASE`

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
