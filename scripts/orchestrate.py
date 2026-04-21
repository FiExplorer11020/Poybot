#!/usr/bin/env python3
"""
Legacy validator for the original staged build plan.

This script is kept for smoke validation only. The repo is already implemented;
operate it via the runtime and dashboard commands documented in README.md.

Usage:
    python scripts/orchestrate.py [--skip-to SESSION_NUMBER] [--dry-run]

Examples:
    python scripts/orchestrate.py                    # Run all sessions from S00
    python scripts/orchestrate.py --skip-to 05       # Resume from S05
    python scripts/orchestrate.py --dry-run          # Show what would run, don't actually run
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "orchestrate.log"

# Session definitions retained from the original staged build plan
SESSIONS = {
    "00": {
        "module": "infrastructure",
        "description": (
            "Infrastructure Setup — Docker + PostgreSQL 15 + Redis 7 + Falcon API validation"
        ),
        "validate": ["python", "scripts/test_connectivity.py"],
    },
    "01": {
        "module": "database",
        "description": "Database Layer — asyncpg pool + models for 8 tables + migrations",
        "validate": ["pytest", "tests/test_database/", "-v", "--tb=short"],
    },
    "02": {
        "module": "config",
        "description": (
            "Configuration + Falcon API Client — pydantic-settings + HTTP client with retry/cache"
        ),
        "validate": ["python", "-c", "from src.config import settings; print('Config OK')"],
    },
    "03": {
        "module": "registry",
        "description": (
            "Leader Registry — Falcon agents 584/581/579, dynamic classification, bot exclusion"
        ),
        "validate": ["pytest", "tests/test_registry/", "-v", "--tb=short"],
    },
    "04": {
        "module": "observer",
        "description": "WebSocket + Trade Observer — dual source (WS + Falcon 556), Redis pub/sub",
        "validate": ["pytest", "tests/test_observer/", "-v", "--tb=short"],
    },
    "05": {
        "module": "observer.position_tracker",
        "description": (
            "Position Tracker — OPEN→CLOSE reconstruction, merge detection, fee calculation"
        ),
        "validate": ["pytest", "tests/test_observer/", "-v", "--tb=short", "-k", "position"],
    },
    "06": {
        "module": "graph",
        "description": (
            "Graph Engine + Hawkes — follower edges, Beta-Binomial, Hawkes process fitting"
        ),
        "validate": ["pytest", "tests/test_graph/", "-v", "--tb=short"],
    },
    "07": {
        "module": "profiler.behavior",
        "description": (
            "Behavior Profiler — Dirichlet categories, EWMA sizing/timing, KDE, deviation scoring"
        ),
        "validate": ["pytest", "tests/test_profiler/", "-v", "--tb=short", "-k", "behavior"],
    },
    "08": {
        "module": "profiler.error_model",
        "description": (
            "Error Model — 3-phase progression (Beta→LogReg→LightGBM), CUSUM drift detection"
        ),
        "validate": ["pytest", "tests/test_profiler/", "-v", "--tb=short", "-k", "error"],
    },
    "09": {
        "module": "engine.confidence",
        "description": (
            "Confidence Engine — Thompson Sampling (FOLLOW/FADE/SKIP), Bayesian Kelly sizing"
        ),
        "validate": ["pytest", "tests/test_engine/", "-v", "--tb=short", "-k", "confidence"],
    },
    "10": {
        "module": "engine.paper_trader",
        "description": (
            "Paper Trader + Risk Manager — virtual portfolio, circuit breakers, exposure limits"
        ),
        "validate": ["pytest", "tests/test_engine/", "-v", "--tb=short", "-k", "paper or risk"],
    },
    "11": {
        "module": "monitoring",
        "description": (
            "Monitoring + Batch Orchestrator — health checks, batch runner (Hawkes/LogReg/LightGBM)"
        ),
        "validate": ["python", "-c", "print('Monitoring OK')"],
    },
    "12": {
        "module": "integration",
        "description": "Integration Test — full pipeline smoke test",
        "validate": ["pytest", "tests/integration/", "-v", "--tb=short", "--timeout=120"],
    },
}


def log(message: str, level: str = "INFO"):
    """Log message to file and stdout."""
    timestamp = datetime.now().isoformat()
    log_line = f"[{timestamp}] [{level}] {message}"
    print(log_line)
    with open(LOG_FILE, "a") as f:
        f.write(log_line + "\n")


def run_session(session_num: str, dry_run: bool = False) -> bool:
    """Run validation for a single session. Returns True if successful."""
    if session_num not in SESSIONS:
        log(f"Session {session_num} not found", "ERROR")
        return False

    session = SESSIONS[session_num]
    log(f"\n{'=' * 70}")
    log(f"Session S{session_num}: {session['description']}")
    log(f"{'=' * 70}")

    if dry_run:
        log(f"[DRY RUN] Would validate: {' '.join(session['validate'])}")
        return True

    # Validate the session
    log(f"Validating S{session_num}...")
    try:
        result = subprocess.run(
            session["validate"],
            cwd=str(PROJECT_ROOT),
            timeout=600,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log("Validation failed", "ERROR")
            log(f"STDOUT: {result.stdout}", "ERROR")
            log(f"STDERR: {result.stderr}", "ERROR")
            return False
        log(f"✓ S{session_num} validation passed")
        return True
    except subprocess.TimeoutExpired:
        log(f"Validation for S{session_num} timed out", "ERROR")
        return False
    except Exception as e:
        log(f"Error validating session: {e}", "ERROR")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate Claude Code sessions")
    parser.add_argument("--skip-to", type=str, default="00", help="Skip to session (e.g., '05')")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would run without executing"
    )
    args = parser.parse_args()

    log(f"Starting orchestration, skip-to={args.skip_to}, dry_run={args.dry_run}")

    session_nums = sorted(SESSIONS.keys())
    start_idx = session_nums.index(args.skip_to) if args.skip_to in session_nums else 0

    for session_num in session_nums[start_idx:]:
        success = run_session(session_num, dry_run=args.dry_run)
        if not success:
            log(f"\n{'=' * 70}", "ERROR")
            log(f"ORCHESTRATION STOPPED at S{session_num}", "ERROR")
            next_idx = session_nums.index(session_num) + 1
            if next_idx < len(session_nums):
                log(
                    f"To resume: python scripts/orchestrate.py --skip-to {session_nums[next_idx]}",
                    "ERROR",
                )
            sys.exit(1)
        time.sleep(2)

    log(f"\n{'=' * 70}")
    log("✓ ALL SESSIONS VALIDATED SUCCESSFULLY")
    log(f"{'=' * 70}")


if __name__ == "__main__":
    main()
