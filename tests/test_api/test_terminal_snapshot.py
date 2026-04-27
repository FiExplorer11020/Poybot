from datetime import datetime, timezone

import pytest


@pytest.fixture
def sample_terminal_inputs():
    now = datetime(2026, 4, 27, 11, 45, tzinfo=timezone.utc)
    return {
        "overview": {
            "total_pnl": 125.5,
            "capital": 10000.0,
            "equity": 10125.5,
            "win_rate": 0.62,
            "activity_feed": [
                {
                    "time": now.isoformat(),
                    "market_id": "0xmkt-observed",
                    "market_question": "Will BTC close above $100k this month?",
                    "market_category": "crypto",
                    "wallet_address": "0xleader",
                    "side": "BUY",
                    "size_usdc": 88.0,
                }
            ],
        },
        "ml": {
            "avg_process_score": 0.44,
        },
        "system": {
            "leaders": {"active": 12},
        },
        "positions_live": [
            {
                "id": 11,
                "question": "Will BTC close above $100k this month?",
                "direction": "yes",
                "entry_price": 0.58,
                "size_usdc": 250.0,
                "unrealized_pnl": 12.5,
                "strategy": "follow",
                "confidence": 0.71,
                "age_s": 42,
            }
        ],
        "positions": {
            "open": [],
            "closed": [
                {
                    "id": 10,
                    "closed_at": now.isoformat(),
                    "opened_at": now.isoformat(),
                    "question": "Will BTC close above $100k this month?",
                    "direction": "yes",
                    "entry_price": 0.55,
                    "exit_price": 0.61,
                    "size_usdc": 150.0,
                    "fee_paid_usdc": 1.2,
                    "pnl_usdc": 15.0,
                    "pnl_pct": 10.0,
                    "status": "closed",
                }
            ],
        },
        "decisions": [
            {
                "market_id": "0xmkt-decision",
                "question": "Will ETH settle above $4k this week?",
                "action": "follow",
                "confidence": 0.82,
                "reason": "leader_alignment",
                "signal_audit": {"accepted": True},
                "trace": {
                    "gate_result": "accepted",
                    "execution_result": "open",
                    "refusal_reason": None,
                },
                "ml_snapshot": {"reason_codes": ["leader_alignment"]},
            }
        ],
        "decision_stats": {
            "totals": {"total": 4},
        },
        "risk": {
            "paper_capital": 10000.0,
            "drawdown_pct": 1.25,
            "open_count": 1,
        },
        "readiness": {
            "global": {
                "bars": {
                    "data_accumulation_pct": 81,
                    "first_position_readiness_pct": 74,
                    "belief_stability_pct": 92,
                    "portfolio_accumulation_pct": 63,
                    "v1_go_no_go_pct": 56,
                },
                "blockers": ["low_token_map_coverage"],
            },
            "markets": [
                {
                    "market_id": "leader:0xabc",
                    "question": "Leader readiness candidate 0xabc",
                    "state": "NO_GO_DATA",
                    "bars": {
                        "first_position_readiness_pct": 74,
                        "v1_go_no_go_pct": 0,
                    },
                    "blockers": ["low_token_map_coverage"],
                    "last_transition_reason": "no_go_data",
                }
            ],
        },
        "data_quality": {
            "markets": {"total": 40},
            "feed": {"ws_healthy": True},
        },
        "health": {
            "db": True,
            "redis": True,
            "websocket": True,
            "websocket_connected": True,
            "last_message_age_s": 0.2,
            "book_age_p95_s": 1.8,
            "pipeline_stage_health": {
                "book_quality_snapshots_5m": 155,
                "last_book_snapshot_age_s": 0.5,
                "stage_status": {
                    "book_capture": "healthy",
                    "readiness_persistence": "active",
                },
            },
        },
        "market_rows": [
            {
                "market_id": "0xmkt-live",
                "token_id": "0xtoken-yes",
                "title": "Will BTC close above $100k this month?",
                "direction": "YES",
                "mid_price": 0.63,
                "spread": 0.012,
                "spread_bps": 120.0,
                "freshness_ms": 1800,
                "source_delay_ms": 420,
                "observations": 18,
                "messages_last_minute": 5,
                "detected": True,
                "signal_strength": 0.78,
                "decision_action": "open",
                "quote_source": "book_quality_snapshots",
            }
        ],
        "observed_trades": [
            {
                "id": "obs-1",
                "timestamp": now.isoformat(),
                "market_title": "Will BTC close above $100k this month?",
                "side": "BUY",
                "price": 0.63,
                "notional": 88.0,
                "status": "observed",
            }
        ],
        "runtime": {
            "started_at": now.isoformat(),
            "uptime_seconds": 321,
            "cycle_latency_ms": 185.0,
            "last_command_at": None,
            "control_available": False,
            "config_mutable": False,
        },
        "logs": [
            {
                "timestamp": now.isoformat(),
                "level": "INFO",
                "category": "observer",
                "message": "WebSocket connected",
            }
        ],
    }


def test_build_terminal_snapshot_maps_backend_sections(sample_terminal_inputs):
    from src.api.terminal_snapshot import build_terminal_snapshot

    snapshot = build_terminal_snapshot(**sample_terminal_inputs)

    assert snapshot["stats"]["total_pnl"] == 125.5
    assert snapshot["stats"]["portfolio_total"] == 10125.5
    assert snapshot["stats"]["open_positions"] == 1
    assert snapshot["analytics"]["summary"]["tracked_markets"] == 40
    assert snapshot["analytics"]["summary"]["opportunity_count"] == 1
    assert snapshot["bot"]["status"] == "running"
    assert snapshot["bot"]["execution_enabled"] is False
    assert snapshot["positions"]["open_count"] == 1
    assert snapshot["positions"]["items"][0]["market_title"] == "Will BTC close above $100k this month?"
    assert snapshot["decision_engine"]["summary"]["actionable_count"] == 1
    assert snapshot["decision_engine"]["ranked"][0]["action"] == "open"
    assert snapshot["ingestion"]["live_markets"] == 1
    assert snapshot["recent_trades"][0]["market_title"] == "Will BTC close above $100k this month?"
    assert snapshot["risk_config"]["paper_only"] is True
    assert snapshot["logs"][0]["category"] == "observer"


def test_build_terminal_snapshot_falls_back_to_readiness_candidates_when_decisions_empty(
    sample_terminal_inputs,
):
    from src.api.terminal_snapshot import build_terminal_snapshot

    sample_terminal_inputs["decisions"] = []
    snapshot = build_terminal_snapshot(**sample_terminal_inputs)

    assert snapshot["decision_engine"]["ranked"][0]["title"] == "Leader readiness candidate 0xabc"
    assert snapshot["decision_engine"]["ranked"][0]["action"] == "skip"
    assert snapshot["decision_engine"]["ranked"][0]["executable"] is False
    assert "low_token_map_coverage" in snapshot["decision_engine"]["ranked"][0]["rejections"]


def test_parse_loguru_line_extracts_structured_log_entry():
    from src.api.terminal_snapshot import parse_loguru_line

    entry = parse_loguru_line(
        "2026-04-27 13:02:01.123 | INFO     | src.observer.websocket_client:_connect_and_run:92 - WebSocket connected"
    )

    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["category"] == "observer.websocket_client"
    assert entry["message"] == "WebSocket connected"
