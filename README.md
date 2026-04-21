# Polymarket Leader Intelligence Engine

Bot de trading orienté intelligence de wallets Polymarket. Le système observe les leaders, reconstruit leurs positions, cartographie leurs followers, profile leurs comportements, modélise leurs erreurs, puis prend des décisions paper trading `FOLLOW` / `FADE` / `SKIP`.

## Current Scope

- Registry Falcon pour leaderboard, enrichissement Wallet360 et classification des leaders
- Ingestion temps réel des trades via WebSocket + backfill Falcon
- Reconstruction des positions `OPEN -> CLOSE`
- Graphe leader -> follower avec probabilité de suivi et batch Hawkes
- Profiling comportemental + modèle d'erreur hiérarchique
- Confidence engine, paper trader, risk manager
- Dashboard FastAPI avec WebSocket live

## Local Run

```bash
python -m pip install -e ".[dev]"
cp .env.example .env
# add FALCON_API_KEY to .env

docker-compose up -d
python scripts/setup_db.py
python scripts/test_connectivity.py

# bot runtime
python scripts/run_all.py

# dashboard API
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Dashboard: `http://127.0.0.1:8000`

## Useful Commands

```bash
python scripts/health_check.py
python scripts/batch_runner.py
python scripts/backfill_decision_learning.py
python scripts/bootstrap_leaders.py
pytest -q
```

## Documentation

- [CLAUDE.md](CLAUDE.md): master architecture and implementation context
- [docs/INFRA.md](docs/INFRA.md): infra and deployment notes
- `src/*/CLAUDE.md`: module-level architecture notes

## Runtime Map

```text
registry -> observer -> graph -> profiler -> engine
    \          |          |          |          |
     \         v          v          v          v
      ------ PostgreSQL 15 <-> Redis 7 <-> FastAPI dashboard
```

## Notes

- The old session-by-session implementation guides were removed because the project has moved beyond the initial bootstrap phase.
- `scripts/orchestrate.py` and `scripts/run_session.sh` are now legacy helpers, not the primary way to operate the repo.
