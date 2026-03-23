from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import time

import pytest

from app.live.state import LiveHub, MarketRuntime
from app.services.price_state_cache import CachedTopOfBook, PriceStateCache


@pytest.mark.anyio
async def test_price_change_event_uses_current_polymarket_shape() -> None:
    hub = LiveHub()
    hub._markets_by_id["cond-1"] = MarketRuntime(
        market_id="cond-1",
        title="Test market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
        yes_bid=0.45,
        yes_ask=0.46,
        no_bid=0.54,
        no_ask=0.55,
    )
    hub._token_to_market["yes-token"] = ("cond-1", "YES")
    hub._token_to_market["no-token"] = ("cond-1", "NO")

    await hub._apply_ws_event(
        {
            "market": "cond-1",
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "yes-token",
                    "side": "BUY",
                    "price": "0.47",
                    "size": "12",
                    "best_bid": "0.47",
                    "best_ask": "0.49",
                },
                {
                    "asset_id": "yes-token",
                    "side": "SELL",
                    "price": "0.49",
                    "size": "7",
                    "best_bid": "0.47",
                    "best_ask": "0.49",
                },
            ],
        }
    )

    market = hub._markets_by_id["cond-1"]
    assert market.yes_bid == 0.47
    assert market.yes_ask == 0.49
    assert market.quote_source == "live_ws"


def test_pnl_series_respects_requested_timeframe() -> None:
    hub = LiveHub()
    hub._history = [
        {"timestamp": str(idx), "portfolio": 25_000 + idx, "pnl_pct": 0.1} for idx in range(1_400)
    ]

    assert len(hub.pnl_series("24h")) == 96
    assert len(hub.pnl_series("7d")) == 336
    assert len(hub.pnl_series("30d")) == 720
    assert len(hub.pnl_series("90d")) == 1200


@pytest.mark.anyio
async def test_stale_refresh_prioritizes_display_slice(monkeypatch) -> None:
    hub = LiveHub()
    hub.strategy.cfg.display_market_limit = 1

    prioritized = MarketRuntime(
        market_id="priority-market",
        title="Priority market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="priority-yes",
        token_id_no="priority-no",
        yes_bid=0.51,
        yes_ask=0.52,
        signal_strength=4.0,
    )
    background = MarketRuntime(
        market_id="background-market",
        title="Background market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="background-yes",
        token_id_no="background-no",
        yes_bid=0.48,
        yes_ask=0.49,
        signal_strength=0.1,
    )

    hub._markets_by_id[prioritized.market_id] = prioritized
    hub._markets_by_id[background.market_id] = background

    refreshed: list[str] = []

    async def fake_refresh_one_market(_client, market: MarketRuntime) -> None:
        refreshed.append(market.market_id)

    monkeypatch.setattr(hub, "_refresh_one_market", fake_refresh_one_market)

    await hub._refresh_stale_prices_once()

    assert refreshed == ["priority-market"]


@pytest.mark.asyncio
async def test_tick_uses_price_cache_and_exposes_cache_age_ms() -> None:
    cache = PriceStateCache()
    hub = LiveHub(price_cache=cache)
    hub.strategy.cfg.max_positions_per_tick = 0
    hub.strategy.cfg.max_concurrent_positions = 0

    observed_at = datetime.now(timezone.utc) - timedelta(milliseconds=250)
    await cache.set(
        CachedTopOfBook(
            market_id="cond-1",
            token_id="yes-token",
            best_bid=Decimal("0.41"),
            best_ask=Decimal("0.43"),
            mid_price=Decimal("0.42"),
            spread=Decimal("0.02"),
            observed_at=observed_at,
        )
    )

    hub._markets_by_id["cond-1"] = MarketRuntime(
        market_id="cond-1",
        title="Cached market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
    )

    calls: list[tuple[str, float, float]] = []

    def fake_evaluate_market(market_id: str, best_bid: float, best_ask: float) -> dict:
        calls.append((market_id, best_bid, best_ask))
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": 0.42,
            "spread": 0.02,
            "volatility": 0.001,
            "liquidity_score": 0.7,
            "expected_edge": 0.0,
            "entry_threshold": 0.01,
            "signal_strength": 0.0,
            "detected": False,
            "direction": "HOLD",
            "price_delta": 0.0,
            "observations": 1,
            "momentum": 0.0,
        }

    hub.strategy.evaluate_market = fake_evaluate_market

    await hub.tick()

    market = hub._markets_by_id["cond-1"]
    assert calls == [("cond-1", 0.41, 0.43)]
    assert market.yes_bid == 0.41
    assert market.yes_ask == 0.43
    assert market.quote_source == "price_cache"

    snapshot_market = next(
        item for item in hub.snapshot()["markets"] if item["market_id"] == "cond-1"
    )
    assert snapshot_market["cache_age_ms"] is not None
    assert snapshot_market["cache_age_ms"] >= 200


@pytest.mark.asyncio
async def test_tick_skips_market_when_price_cache_is_empty() -> None:
    hub = LiveHub(price_cache=PriceStateCache())

    hub._markets_by_id["cond-1"] = MarketRuntime(
        market_id="cond-1",
        title="Missing cache market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
        yes_bid=0.33,
        yes_ask=0.35,
    )

    called = False

    def fake_evaluate_market(*args, **kwargs) -> dict:
        nonlocal called
        called = True
        return {}

    hub.strategy.evaluate_market = fake_evaluate_market

    await hub.tick()

    market = hub._markets_by_id["cond-1"]
    assert called is False
    assert market.detected is False
    assert market.cache_observed_at is None
    assert market.quote_source == "seed"


class DummyClob:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"canceled": []}
        self.cancel_calls = 0

    async def cancel_all_orders(self, headers=None, endpoint="/cancel-all") -> dict:
        self.cancel_calls += 1
        return self.response


class DummyExecutor:
    def __init__(
        self,
        clob: DummyClob | None = None,
        execution_error: Exception | None = None,
    ) -> None:
        self.clob = clob or DummyClob()
        self.execution_error = execution_error
        self.requests = []
        self.settings = SimpleNamespace(
            polymarket_api_key=None,
            polymarket_api_secret=None,
            polymarket_api_passphrase=None,
        )

    async def execute(self, req) -> dict:
        self.requests.append(req)
        if self.execution_error is not None:
            raise self.execution_error
        return {
            "execution_mode": "dry_run",
            "order_id": "dry-order",
            "exchange_status": "SIMULATED",
            "tx_hash": None,
            "raw": {},
        }

    async def close(self) -> None:
        return None


def _detected_eval(best_bid: float, best_ask: float) -> dict:
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": round((best_bid + best_ask) / 2, 4),
        "spread": round(best_ask - best_bid, 4),
        "volatility": 0.001,
        "liquidity_score": 0.8,
        "expected_edge": 0.02,
        "entry_threshold": 0.01,
        "signal_strength": 2.0,
        "detected": True,
        "direction": "BUY_YES",
        "price_delta": 0.001,
        "observations": 10,
        "momentum": 0.01,
    }


def _build_trade_candidate() -> MarketRuntime:
    return MarketRuntime(
        market_id="cond-drawdown",
        title="Drawdown guard market",
        end_date="2099-01-01T00:00:00+00:00",
        token_id_yes="yes-token",
        token_id_no="no-token",
        yes_bid=0.48,
        yes_ask=0.49,
        no_bid=0.50,
        no_ask=0.51,
        last_update_ts=time.time(),
    )


@pytest.mark.anyio
async def test_tick_does_not_halt_when_drawdown_is_below_threshold(monkeypatch) -> None:
    hub = LiveHub()
    hub._markets_by_id["cond-drawdown"] = _build_trade_candidate()
    hub.executor = DummyExecutor(clob=DummyClob({"canceled": ["ord-1"]}))
    hub.bot_state.running = True
    hub.bot_state.paused = False
    hub.strategy.cfg.max_positions_per_tick = 1
    hub.strategy.cfg.max_concurrent_positions = 1
    hub._latest_tick["stats"]["total_pnl"] = -2_250.0

    async def fake_hydrate(_market: MarketRuntime) -> bool:
        return True

    events: list[dict] = []

    async def fake_broadcast(payload: dict) -> None:
        events.append(payload)

    monkeypatch.setattr(hub, "_hydrate_market_from_cache", fake_hydrate)
    monkeypatch.setattr(hub, "broadcast", fake_broadcast)
    hub.strategy.evaluate_market = lambda market_id, best_bid, best_ask: _detected_eval(
        best_bid, best_ask
    )

    await hub.tick()

    assert hub.bot_state.running is True
    assert hub.executor.clob.cancel_calls == 0
    assert len(hub._trades) == 1
    assert all(event["type"] != "halt" for event in events)


@pytest.mark.anyio
async def test_tick_halts_and_cancels_orders_when_drawdown_exceeds_threshold(
    monkeypatch,
) -> None:
    hub = LiveHub()
    hub._markets_by_id["cond-drawdown"] = _build_trade_candidate()
    hub.executor = DummyExecutor(clob=DummyClob({"canceled": ["ord-1", "ord-2"]}))
    hub.bot_state.running = True
    hub.bot_state.paused = False
    hub.strategy.cfg.max_positions_per_tick = 1
    hub.strategy.cfg.max_concurrent_positions = 1
    hub._latest_tick["stats"]["total_pnl"] = -2_750.0

    async def fake_hydrate(_market: MarketRuntime) -> bool:
        return True

    events: list[dict] = []

    async def fake_broadcast(payload: dict) -> None:
        events.append(payload)

    monkeypatch.setattr(hub, "_hydrate_market_from_cache", fake_hydrate)
    monkeypatch.setattr(hub, "broadcast", fake_broadcast)
    hub.strategy.evaluate_market = lambda market_id, best_bid, best_ask: _detected_eval(
        best_bid, best_ask
    )

    await hub.tick()

    halt_event = next(event for event in events if event["type"] == "halt")

    assert hub.bot_state.running is False
    assert hub.executor.clob.cancel_calls == 1
    assert hub._trades == []
    assert halt_event["reason"] == "drawdown"
    assert halt_event["details"]["current_drawdown_pct"] == pytest.approx(0.11)
    assert halt_event["details"]["threshold_pct"] == pytest.approx(0.10)
    assert halt_event["details"]["cancelled"] == 2
    assert halt_event["details"]["errors"] == []
