from fastapi.testclient import TestClient

from app.main import app


def test_live_summary_contains_risk_controls() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/live-summary")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert "risk" in payload
    assert "config" in payload["risk"]
    assert "gauges" in payload["risk"]


def test_risk_config_update_clamps_values() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/risk/config",
            json={
                "config": {
                    "risk_per_trade_pct": 9,
                    "max_total_exposure_pct": 40,
                    "kelly_fraction_multiplier": 2,
                    "max_drawdown_auto_stop_pct": 1,
                },
                "toggles": {"pause_on_high_latency": False},
            },
        )
    assert resp.status_code == 200
    config = resp.json()["data"]["risk"]["config"]
    assert config["risk_per_trade_pct"] == 5.0
    assert config["max_total_exposure_pct"] == 20.0
    assert config["kelly_fraction_multiplier"] == 1.0
    assert config["max_drawdown_auto_stop_pct"] == 3.0
