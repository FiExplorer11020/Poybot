from fastapi.testclient import TestClient

from app.main import app


def test_live_summary_contains_risk_config() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/live-summary")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert "risk_config" in payload
    assert "stats" in payload
    assert "markets" in payload


def test_execute_trade_returns_polymarket_shaped_fields() -> None:
    with TestClient(app) as client:
        before = client.get("/api/v1/live-summary").json()["data"]
        market = before["markets"][0]
        resp = client.post(
            f"/api/v1/markets/{market['market_id']}/execute",
            json={"market_title": market["title"]},
        )
    assert resp.status_code == 200
    trade = resp.json()["data"]
    assert trade["execution_mode"] == "dry_run"
    assert trade["order_id"]
    assert trade["token_id"]
    assert trade["exchange_status"]
