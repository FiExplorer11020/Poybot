import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.live.state import MarketRuntime, live_hub
from app.services.price_state_cache import PriceStateCache
from app.main import app
from app.services.adaptive_strategy import RiskConfig


@pytest.fixture(autouse=True)
def reset_live_hub() -> None:
    live_hub._connections.clear()
    live_hub._markets_by_id.clear()
    live_hub._token_to_market.clear()
    live_hub._history.clear()
    live_hub._trades.clear()
    live_hub._latest_tick = live_hub._initial_snapshot()
    live_hub.price_cache = PriceStateCache()
    live_hub.strategy.cfg = RiskConfig()
    live_hub.bot_state.running = True
    live_hub.bot_state.paused = False
    live_hub.bot_state.started_at = time.time()
    live_hub._started = False


def test_live_summary_contains_risk_config() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/live-summary")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert "risk_config" in payload
    assert "stats" in payload
    assert "markets" in payload


def test_execute_trade_returns_polymarket_shaped_fields() -> None:
    live_hub._markets_by_id["test-market"] = MarketRuntime(
        market_id="test-market",
        title="Test market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
        yes_bid=0.48,
        yes_ask=0.49,
        no_bid=0.50,
        no_ask=0.51,
        mid_price=0.485,
        spread=0.01,
        volatility=0.001,
        liquidity_score=0.6,
        expected_edge=0.03,
        entry_threshold=0.01,
        signal_strength=1.5,
        direction="BUY_YES",
        est_profit=3.0,
        detected=True,
        last_update_ts=10.0,
        observations=12,
        complement_gap=0.01,
    )
    with TestClient(app) as client:
        before = client.get("/api/v1/live-summary").json()["data"]
        market = next(item for item in before["markets"] if item["market_id"] == "test-market")
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


def test_close_trade_updates_trade_status() -> None:
    live_hub._markets_by_id["test-market"] = MarketRuntime(
        market_id="test-market",
        title="Test market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
        yes_bid=0.48,
        yes_ask=0.49,
        no_bid=0.50,
        no_ask=0.51,
        mid_price=0.485,
        spread=0.01,
        volatility=0.001,
        liquidity_score=0.6,
        expected_edge=0.03,
        entry_threshold=0.01,
        signal_strength=1.5,
        direction="BUY_YES",
        est_profit=3.0,
        detected=True,
        last_update_ts=time.time(),
        observations=12,
        complement_gap=0.01,
    )
    with TestClient(app) as client:
        opened = client.post(
            "/api/v1/markets/test-market/execute",
            json={"market_title": "Test market"},
        )
        assert opened.status_code == 200
        trade = opened.json()["data"]
        closed = client.post(f"/api/v1/trades/{trade['id']}/close")

    assert closed.status_code == 200
    payload = closed.json()["data"]
    assert payload["status"] == "CLOSED"
    assert payload["closed_at"] is not None


def test_price_change_event_uses_documented_payload_shape() -> None:
    live_hub._markets_by_id["test-market"] = MarketRuntime(
        market_id="test-market",
        title="Test market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
    )
    live_hub._token_to_market["yes-token"] = ("test-market", "YES")

    asyncio.run(
        live_hub._apply_ws_event(
            {
                "event_type": "price_change",
                "market": "test-market",
                "price_changes": [
                    {
                        "asset_id": "yes-token",
                        "price": "0.50",
                        "side": "BUY",
                        "best_bid": "0.50",
                        "best_ask": "0.52",
                    }
                ],
            }
        )
    )

    market = live_hub._markets_by_id["test-market"]
    assert market.yes_bid == 0.5
    assert market.yes_ask == 0.52
    assert market.quote_source == "live_ws"
