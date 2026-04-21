#!/bin/bash
# Legacy helper from the original staged build plan.
# Usage: ./scripts/run_session.sh 03

SESSION=$1

if [ -z "$SESSION" ]; then
    echo "Usage: ./scripts/run_session.sh [00-13]"
    exit 1
fi

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG_FILE="$PROJECT_ROOT/orchestrate.log"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] [INFO] $1" | tee -a "$LOG_FILE"
}

log "Starting Session $SESSION..."

# Change to project root
cd "$PROJECT_ROOT"

# This script is intentionally minimal and kept only as a legacy helper.
case $SESSION in
    00)
        log "Session 00: Infrastructure Setup"
        docker-compose up -d
        python scripts/test_connectivity.py
        ;;
    01)
        log "Session 01: Database Layer"
        claude -p "Read CLAUDE.md section 5 and src/database/CLAUDE.md and implement the complete database layer..."
        pytest tests/test_database/ -v
        ;;
    02)
        log "Session 02: Configuration"
        claude -p "Read CLAUDE.md sections 7 and 12 and implement src/config.py..."
        python -c "from src.config import settings; print('Config OK')"
        ;;
    *)
        log "Session $SESSION: Launch Claude Code manually using README.md + CLAUDE.md"
        log "Then run: ./scripts/validate_session.sh $SESSION"
        ;;
esac

if [ $? -eq 0 ]; then
    log "✓ Session $SESSION completed"
else
    log "✗ Session $SESSION failed"
    exit 1
fi
