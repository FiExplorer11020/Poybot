from pathlib import Path

import pytest


def test_v1_migration_contains_required_invalidation_surfaces():
    sql = Path("docs/migrations/003_v1_economic_spine.sql").read_text()

    required = [
        "v1_label_invalidations",
        "ALTER TABLE paper_trades",
        "ALTER TABLE decision_log",
        "ALTER TABLE leader_profiles",
        "ALTER TABLE positions_reconstructed",
        "fee_snapshots",
        "signal_audits",
        "economic_model_version",
        "strategy_track",
        "invalidated_at",
    ]

    for token in required:
        assert token in sql


def test_invalidation_script_targets_old_pnl_and_learning_labels():
    script = Path("scripts/invalidate_pre_v1_labels.py").read_text()

    required = [
        "paper_trades",
        "decision_log",
        "leader_profiles",
        "error_model_blob",
        "decision_learning",
        "v1_label_invalidations",
        "pre_v1_economic_reset",
    ]

    for token in required:
        assert token in script


def test_env_example_does_not_ship_falcon_secret():
    env_text = Path(".env.example").read_text()

    assert "FALCON_API_KEY=" in env_text
    assert "Bearer " not in env_text
    assert "eyJhbGci" not in env_text
    assert "sk-" not in env_text


def test_run_all_passes_risk_manager_to_paper_trader():
    source = Path("scripts/run_all.py").read_text()

    assert "risk_manager = RiskManager()" in source
    assert "risk_manager=risk_manager" in source


@pytest.mark.skip(
    reason="docs/PHASE_A_BACKTESTER_DESIGN.md was removed by the Round 6 "
    "stale-doc cleanup (commit 4c91b1d); the canonical economics now live "
    "in docs/ROUND_6_THE_SPINE.md and docs/audit/. Re-enable once a "
    "follow-up doc replacement is decided."
)
def test_phase_a_doc_points_to_canonical_economics():
    doc = Path("docs/PHASE_A_BACKTESTER_DESIGN.md").read_text()

    assert "canonical V1 economics" in doc
    assert "leader_swing" in doc
    assert "micro_reactive" in doc
    assert "shares * fee_rate * price * (1 - price)" in doc


def test_runtime_queries_exclude_invalidated_or_unversioned_pnl_labels():
    """Dashboard, risk, and learning queries must not aggregate pre-V1 PnL."""
    from src.economics.versioning import valid_paper_trade_filter, valid_position_filter

    assert valid_paper_trade_filter() == (
        "economic_model_version = 'v1.0.0' AND invalidated_at IS NULL"
    )
    assert valid_position_filter() == (
        "economic_model_version = 'v1.0.0' AND invalidated_at IS NULL"
    )

    sources = {
        "src/api/queries.py": Path("src/api/queries.py").read_text(),
        "src/engine/risk_manager.py": Path("src/engine/risk_manager.py").read_text(),
        "src/profiler/behavior_profiler.py": Path("src/profiler/behavior_profiler.py").read_text(),
        "src/profiler/error_model.py": Path("src/profiler/error_model.py").read_text(),
    }

    for path, source in sources.items():
        assert "valid_paper_trade_filter" in source or "valid_position_filter" in source, path


def test_profile_learning_requires_valid_v1_learning_version():
    """Decision-learning aggregates must ignore invalidated pre-V1 profile learning."""
    from src.economics.versioning import valid_profile_learning_filter

    assert valid_profile_learning_filter("leader_profiles") == (
        "leader_profiles.economic_model_version = 'v1.0.0' "
        "AND leader_profiles.learning_invalidated_at IS NULL"
    )

    source = Path("src/api/queries.py").read_text()

    assert "valid_profile_learning_filter" in source
