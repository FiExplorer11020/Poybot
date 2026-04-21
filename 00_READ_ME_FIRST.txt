╔════════════════════════════════════════════════════════════════════════════════╗
║                                                                                ║
║                POLYMARKET LEADER INTELLIGENCE BOT — CURRENT STATE             ║
║                                                                                ║
║  This repo is no longer in bootstrap/session mode. It already contains the    ║
║  bot runtime, the ML/adaptation layer, and the live dashboard.                ║
║                                                                                ║
╚════════════════════════════════════════════════════════════════════════════════╝

📚 READ THIS FIRST:

1. README.md                 ← current runbook
2. CLAUDE.md                ← master architecture and implementation context
3. docs/INFRA.md            ← infra and deployment notes
4. src/*/CLAUDE.md          ← module-level architecture notes


🚀 LOCAL STARTUP:

    ./START.sh
    python -m pip install -e ".[dev]"
    docker-compose up -d
    python scripts/setup_db.py
    python scripts/run_all.py
    python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000

Dashboard:
    http://127.0.0.1:8000


🧠 CURRENT CAPABILITIES:

    ✓ Falcon leader refresh and enrichment
    ✓ Live trade ingestion and backfill
    ✓ Position reconstruction
    ✓ Leader → follower graph updates
    ✓ Behavior profiling and error modeling
    ✓ Confidence engine + paper trader
    ✓ FastAPI dashboard + live WebSocket updates


🛠️ USEFUL COMMANDS:

    python scripts/test_connectivity.py
    python scripts/health_check.py
    python scripts/batch_runner.py
    python scripts/backfill_decision_learning.py
    pytest -q


⚠️ OPERATIONS NOTES:

1. Keep only one `python scripts/run_all.py` process running at a time.
2. Keep only one dashboard server running at a time.
3. The system is paper trading only unless explicitly changed.
4. Falcon and Polymarket API health can affect live freshness without the local stack being down.


🗂️ LEGACY NOTE:

The old session-by-session implementation guides were intentionally removed because
they no longer reflect the state of the project.
 