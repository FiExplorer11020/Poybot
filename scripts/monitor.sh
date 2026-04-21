#!/bin/bash
# Monitor orchestration progress in real-time
# Usage: ./scripts/monitor.sh
# Keep this running in a terminal to watch progress

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG_FILE="$PROJECT_ROOT/orchestrate.log"

clear

while true; do
    clear
    echo "╔════════════════════════════════════════════════════════════════════╗"
    echo "║  POLYMARKET BOT — ORCHESTRATION MONITOR                            ║"
    echo "║  Updating every 5 seconds... (Ctrl+C to stop)                      ║"
    echo "╚════════════════════════════════════════════════════════════════════╝"
    echo ""

    if [ ! -f "$LOG_FILE" ]; then
        echo "No log file found yet. Has orchestration started?"
        sleep 5
        continue
    fi

    echo "Last 20 log entries:"
    echo "—————————————————————————————————————————————————————————————————————"
    tail -20 "$LOG_FILE"
    echo ""

    # Count statuses
    STARTED=$(grep -c "Starting Session" "$LOG_FILE" 2>/dev/null || echo 0)
    COMPLETED=$(grep -c "validation passed" "$LOG_FILE" 2>/dev/null || echo 0)
    FAILED=$(grep -c "\[ERROR\]" "$LOG_FILE" 2>/dev/null || echo 0)

    echo "—————————————————————————————————————————————————————————————————————"
    echo "Status Summary:"
    echo "  Sessions started:    $STARTED"
    echo "  Sessions validated:  $COMPLETED"
    echo "  Errors encountered:  $FAILED"
    echo ""

    # Check which session is currently running
    CURRENT=$(tail -5 "$LOG_FILE" | grep "Starting Session" | tail -1 | grep -oE "Session [0-9]+")
    if [ -n "$CURRENT" ]; then
        echo "Currently running: $CURRENT"
    fi

    # Check if complete
    if grep -q "ALL SESSIONS COMPLETED" "$LOG_FILE"; then
        echo ""
        echo "✓ ORCHESTRATION COMPLETE!"
        echo "See PROGRESS_TRACKER.xlsx for details."
        exit 0
    fi

    # Check if stopped due to error
    if grep -q "ORCHESTRATION STOPPED" "$LOG_FILE"; then
        echo ""
        echo "✗ ORCHESTRATION STOPPED DUE TO ERROR"
        echo "See orchestrate.log for details."
        exit 1
    fi

    echo ""
    sleep 5
done
