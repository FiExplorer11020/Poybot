from src.engine.neural_readiness import (
    DecisionState,
    ReadinessInputs,
    StrategyTrack,
    build_neural_readiness_snapshot,
)


def test_missing_fee_caps_first_position_readiness():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={"fee_snapshot_coverage_pct": None, "token_map_coverage_pct": 100.0},
            activation=[],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )

    assert snapshot["global"]["bars"]["first_position_readiness_pct"] <= 50
    assert "missing_fee_snapshot" in snapshot["global"]["blockers"]


def test_micro_reactive_is_capped_without_book_freshness():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": None,
                "fee_snapshot_coverage_pct": 100.0,
                "token_map_coverage_pct": 100.0,
            },
            activation=[],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )

    assert snapshot["tracks"]["micro_reactive"]["bars"]["data_accumulation_pct"] <= 40
    assert "missing_book_freshness" in snapshot["tracks"]["micro_reactive"]["blockers"]


def test_candidate_market_reaches_candidate_state_from_activation_queue():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": 3.0,
                "fee_snapshot_coverage_pct": 100.0,
                "token_map_coverage_pct": 100.0,
            },
            activation=[
                {
                    "wallet_address": "0xabc",
                    "strategy": "directional",
                    "follow_readiness_pct": 82,
                    "fade_readiness_pct": 35,
                }
            ],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )

    assert snapshot["markets"][0]["state"] == DecisionState.CANDIDATE_SIGNAL.value
    assert snapshot["tracks"][StrategyTrack.LEADER_SWING.value]["top_candidates"]


def test_high_drawdown_blocks_portfolio_accumulation():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": 2.0,
                "fee_snapshot_coverage_pct": 100.0,
                "token_map_coverage_pct": 100.0,
            },
            activation=[
                {
                    "wallet_address": "0xabc",
                    "follow_readiness_pct": 92,
                    "fade_readiness_pct": 72,
                }
            ],
            risk={"open_count": 1, "drawdown_pct": 8.0},
            ml={"follow": {"samples": 20, "win_rate": 0.6}},
        )
    )

    assert snapshot["global"]["bars"]["portfolio_accumulation_pct"] <= 25
    assert "risk_drawdown_high" in snapshot["global"]["blockers"]


def test_data_accumulation_counts_are_exposed():
    snapshot = build_neural_readiness_snapshot(
        ReadinessInputs(
            health={
                "book_age_p95_s": None,
                "fee_snapshot_coverage_pct": 100.0,
                "fee_snapshot_coverage_source": "markets.fee_rate_pct",
                "token_map_coverage_pct": 70.0,
                "data_accumulation_counts": {
                    "total_markets": 10,
                    "token_mapped_markets": 7,
                    "fee_snapshot_tokens": 0,
                },
            },
            activation=[],
            risk={"open_count": 0, "drawdown_pct": 0.0},
            ml={},
        )
    )

    accumulation = snapshot["global"]["data_accumulation"]
    assert accumulation["counts"]["total_markets"] == 10
    assert accumulation["fee_snapshot_coverage_source"] == "markets.fee_rate_pct"
