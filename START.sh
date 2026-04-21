#!/bin/bash
# START.sh — quick local bootstrap helper

set -e

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$PROJECT_ROOT"

echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  POLYMARKET BOT — LOCAL BOOTSTRAP                                 ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""
echo "Checking local prerequisites..."

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ Docker not found. Install Docker Desktop first."
    exit 1
fi
echo "✓ Docker found"

if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ Python 3 not found. Install Python 3.11+ first."
    exit 1
fi
echo "✓ Python 3 found"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✓ Created .env from .env.example"
else
    echo "✓ .env already present"
fi

mkdir -p logs

echo ""
echo "Next steps:"
echo "  1. python -m pip install -e \".[dev]\""
echo "  2. Add FALCON_API_KEY to .env"
echo "  3. docker-compose up -d"
echo "  4. python scripts/setup_db.py"
echo "  5. python scripts/test_connectivity.py"
echo "  6. python scripts/run_all.py"
echo "  7. python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000"
echo ""
echo "Useful checks:"
echo "  - python scripts/health_check.py"
echo "  - pytest -q"
echo ""
echo "Documentation:"
echo "  - README.md"
echo "  - CLAUDE.md"
echo "  - docs/INFRA.md"
