#!/bin/bash
# Validate that a session completed successfully
# Usage: ./scripts/validate_session.sh 03

SESSION=$1

if [ -z "$SESSION" ]; then
    echo "Usage: ./scripts/validate_session.sh [00-13]"
    exit 1
fi

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"

echo "Validating Session $SESSION..."

case $SESSION in
    00)
        # Check Docker is running
        docker ps -q --filter "name=polymarket" >/dev/null
        if [ $? -eq 0 ]; then
            echo "✓ Session 00 validated: Docker containers running"
            exit 0
        else
            echo "✗ Session 00 failed: Docker containers not running"
            exit 1
        fi
        ;;
    01)
        # Run database tests
        pytest tests/test_database/ -v --tb=short
        exit $?
        ;;
    02)
        # Test config loading
        python -c "from src.config import settings; assert settings.DATABASE_URL; print('✓ Config loaded')" 2>&1
        exit $?
        ;;
    03)
        pytest tests/test_collector/test_websocket_client.py -v --tb=short
        exit $?
        ;;
    04)
        pytest tests/test_collector/test_orderbook_collector.py -v --tb=short
        exit $?
        ;;
    05)
        pytest tests/test_collector/ -v --tb=short -k "trade_collector or leaderboard"
        exit $?
        ;;
    06)
        pytest tests/test_detection/test_spike_detector.py -v --tb=short
        exit $?
        ;;
    07)
        pytest tests/test_detection/test_wallet_attributor.py -v --tb=short
        exit $?
        ;;
    08)
        pytest tests/test_detection/test_community_detector.py -v --tb=short
        exit $?
        ;;
    09)
        pytest tests/test_scoring/ -v --tb=short
        exit $?
        ;;
    10)
        pytest tests/test_trading/test_signal_generator.py tests/test_trading/test_paper_trader.py -v --tb=short
        exit $?
        ;;
    11)
        pytest tests/test_trading/test_risk_manager.py -v --tb=short
        exit $?
        ;;
    12)
        # Just check that monitoring modules load
        python -c "from src.monitoring.metrics import *; print('✓ Monitoring loaded')"
        exit $?
        ;;
    13)
        pytest tests/integration/test_e2e.py -v --tb=short --timeout=120
        exit $?
        ;;
    *)
        echo "Unknown session: $SESSION"
        exit 1
        ;;
esac
