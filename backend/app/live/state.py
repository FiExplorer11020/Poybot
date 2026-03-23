from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import WebSocket

try:
    import orjson
except ModuleNotFoundError:  # pragma: no cover - fallback for lighter test envs
    orjson = None

try:
    import websockets
except ModuleNotFoundError:  # pragma: no cover - fallback for lighter test envs
    websockets = None

from app.clients.gamma import GammaClient
from app.core.settings import get_settings
from app.services.price_state_cache import CachedTopOfBook, PriceStateCache
from app.services.adaptive_strategy import AdaptiveStrategyEngine, PortfolioState, RiskConfig
from app.services.risk_guard import (
    APIFailureHaltException,
    DrawdownHaltException,
    RiskGuard,
)
from app.services.trade_executor import ExecutionRequest, TradeExecutor
from app.utils.polymarket import parse_json_list_field

log = logging.getLogger(__name__)


@dataclass
class BotState:
    running: bool = True
    paused: bool = False
    started_at: float = field(default_factory=time.time)


@dataclass
class MarketRuntime:
    market_id: str
    title: str
    end_date: str
    token_id_yes: str
    token_id_no: str
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    mid_price: float = 0.5
    spread: float = 0.0
    volatility: float = 0.0
    liquidity_score: float = 0.0
    expected_edge: float = 0.0
    entry_threshold: float = 0.0
    signal_strength: float = 0.0
    direction: str = "HOLD"
    est_profit: float = 0.0
    detected: bool = False
    last_update_ts: float = 0.0
    last_trade_ts: float = 0.0
    cache_observed_at: datetime | None = None
    observations: int = 0
    complement_gap: float = 0.0
    price_delta: float = 0.0
    momentum: float = 0.0
    open_trade_id: str | None = None
    bootstrap_only: bool = False
    quote_source: str = "seed"

    def display(self) -> dict:
        cache_age_ms = None
        if self.cache_observed_at is not None:
            observed_at = self.cache_observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            else:
                observed_at = observed_at.astimezone(timezone.utc)
            cache_age_ms = max(
                0, int((datetime.now(timezone.utc) - observed_at).total_seconds() * 1000)
            )
        return {
            "market_id": self.market_id,
            "title": self.title,
            "end_date": self.end_date,
            "token_id_yes": self.token_id_yes,
            "token_id_no": self.token_id_no,
            "best_bid": round(self.yes_bid, 4),
            "best_ask": round(self.yes_ask, 4),
            "mid_price": round(self.mid_price, 4),
            "spread": round(self.spread, 4),
            "volatility": round(self.volatility, 6),
            "liquidity_score": round(self.liquidity_score, 4),
            "expected_edge": round(self.expected_edge, 6),
            "entry_threshold": round(self.entry_threshold, 6),
            "signal_strength": round(self.signal_strength, 4),
            "direction": self.direction,
            "est_profit": round(self.est_profit, 3),
            "detected": self.detected,
            "observations": self.observations,
            "complement_gap": round(self.complement_gap, 4),
            "price_delta": round(self.price_delta, 6),
            "momentum": round(self.momentum, 6),
            "stale_seconds": (
                round(max(0.0, time.time() - self.last_update_ts), 3)
                if self.last_update_ts
                else None
            ),
            "open_position": self.open_trade_id is not None,
            "bootstrap_only": self.bootstrap_only,
            "quote_source": self.quote_source,
            "cache_age_ms": cache_age_ms,
        }


class LiveHub:
    """Live market runtime with ephemeral analysis and stricter trade gating."""

    def __init__(self, price_cache: PriceStateCache | None = None) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.bot_state = BotState()
        self.price_cache = price_cache or PriceStateCache()
        self.strategy = AdaptiveStrategyEngine(RiskConfig())
        self.executor = TradeExecutor()
        self._risk_guard = RiskGuard(self.strategy.cfg, self.executor)
        self._markets_by_id: dict[str, MarketRuntime] = {}
        self._token_to_market: dict[str, tuple[str, str]] = {}
        self._history: list[dict] = []
        self._trades: list[dict] = []
        self._latest_tick = self._initial_snapshot()
        self._ws_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._started = False

    def _initial_snapshot(self) -> dict:
        return {
            "latency_ms": 0,
            "portfolio_total": 25_000.0,
            "capital_in_trade": 0.0,
            "stats": {
                "total_pnl": 0.0,
                "win_rate": 0.0,
                "avg_profit": 0.0,
                "active_markets": 0,
                "detected_arbs_today": 0,
                "open_positions": 0,
            },
        }

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True

        if os.getenv("PYTEST_CURRENT_TEST"):
            if not self._markets_by_id:
                self._bootstrap_markets()
            self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
            return

        settings = get_settings()
        gamma = GammaClient(settings.polymarket_gamma_base_url)
        try:
            await asyncio.wait_for(self._load_active_markets(gamma), timeout=4.0)
        except Exception as exc:
            log.error("Gamma API error while loading market universe: %s", exc)
        finally:
            await gamma.close()

        if not self._markets_by_id:
            self._bootstrap_markets()

        self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
        if websockets is not None:
            self._ws_task = asyncio.create_task(self._listen_to_clob())
        self._refresh_task = asyncio.create_task(self._refresh_stale_prices_loop())

    async def shutdown(self) -> None:
        tasks = [task for task in (self._ws_task, self._refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ws_task = None
        self._refresh_task = None
        self._started = False
        await self.executor.close()

    async def _load_active_markets(self, gamma: GammaClient) -> None:
        settings = get_settings()
        page_size = max(settings.max_page_size, 100)
        max_pages = 15
        now = datetime.now(timezone.utc)

        for page in range(max_pages):
            markets = await gamma.fetch_markets(
                limit=page_size, offset=page * page_size, active=True, closed=False
            )
            if not markets:
                break
            for raw_market in markets:
                runtime = self._runtime_from_gamma(raw_market, now)
                if runtime is None or runtime.market_id in self._markets_by_id:
                    continue
                self._markets_by_id[runtime.market_id] = runtime
                self._token_to_market[runtime.token_id_yes] = (runtime.market_id, "YES")
                self._token_to_market[runtime.token_id_no] = (runtime.market_id, "NO")

    def _runtime_from_gamma(self, raw_market: dict, now: datetime) -> MarketRuntime | None:
        if raw_market.get("closed", True):
            return None

        token_ids = parse_json_list_field(raw_market.get("clobTokenIds"))
        if not isinstance(token_ids, list) or len(token_ids) < 2:
            return None

        end_str = (
            raw_market.get("endDateIso")
            or raw_market.get("endDate")
            or raw_market.get("end_date_iso")
        )
        if not end_str:
            return None
        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        if end_date < now:
            return None

        outcome_prices = parse_json_list_field(raw_market.get("outcomePrices", '["0.5", "0.5"]'))
        try:
            yes_mid = float(outcome_prices[0]) if outcome_prices else 0.5
        except Exception:
            yes_mid = 0.5

        yes_mid = max(0.01, min(0.99, yes_mid))
        no_mid = max(0.01, min(0.99, 1 - yes_mid))
        spread_est = max(0.01, min(0.04, 0.02 + abs(yes_mid - 0.5) * 0.01))

        return MarketRuntime(
            market_id=str(raw_market.get("conditionId") or raw_market.get("id") or ""),
            title=str(raw_market.get("question") or raw_market.get("title") or "Unknown market"),
            end_date=end_date.isoformat(),
            token_id_yes=str(token_ids[0]),
            token_id_no=str(token_ids[1]),
            yes_bid=max(0.01, yes_mid - (spread_est / 2)),
            yes_ask=min(0.99, yes_mid + (spread_est / 2)),
            no_bid=max(0.01, no_mid - (spread_est / 2)),
            no_ask=min(0.99, no_mid + (spread_est / 2)),
            mid_price=yes_mid,
            spread=spread_est,
            bootstrap_only=False,
            quote_source="gamma_seed",
        )

    def _bootstrap_markets(self) -> None:
        fallback_markets = [
            MarketRuntime(
                market_id="bootstrap-market-1",
                title="Fallback market loaded without Gamma connectivity",
                end_date=datetime.now(timezone.utc).isoformat(),
                token_id_yes="bootstrap-yes-1",
                token_id_no="bootstrap-no-1",
                yes_bid=0.48,
                yes_ask=0.50,
                no_bid=0.50,
                no_ask=0.52,
                mid_price=0.49,
                spread=0.02,
                bootstrap_only=True,
                quote_source="bootstrap",
            ),
            MarketRuntime(
                market_id="bootstrap-market-2",
                title="Second fallback market for offline tests",
                end_date=datetime.now(timezone.utc).isoformat(),
                token_id_yes="bootstrap-yes-2",
                token_id_no="bootstrap-no-2",
                yes_bid=0.57,
                yes_ask=0.59,
                no_bid=0.41,
                no_ask=0.43,
                mid_price=0.58,
                spread=0.02,
                bootstrap_only=True,
                quote_source="bootstrap",
            ),
        ]
        for runtime in fallback_markets:
            self._markets_by_id[runtime.market_id] = runtime
            self._token_to_market[runtime.token_id_yes] = (runtime.market_id, "YES")
            self._token_to_market[runtime.token_id_no] = (runtime.market_id, "NO")

    async def _listen_to_clob(self) -> None:
        settings = get_settings()
        if websockets is None:
            return
        token_ids = list(self._token_to_market.keys())
        if not token_ids:
            return

        chunk_size = 100
        while True:
            try:
                async with websockets.connect(
                    settings.polymarket_clob_ws_url,
                    ping_interval=15,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    heartbeat_task = asyncio.create_task(self._send_market_heartbeats(ws))
                    for idx in range(0, len(token_ids), chunk_size):
                        payload = {
                            "assets_ids": token_ids[idx : idx + chunk_size],
                            "type": "market",
                        }
                        encoded = (
                            orjson.dumps(payload) if orjson is not None else json.dumps(payload)
                        )
                        await ws.send(encoded)

                    try:
                        async for raw in ws:
                            if self._is_non_json_ws_message(raw):
                                continue
                            try:
                                events = (
                                    orjson.loads(raw) if orjson is not None else json.loads(raw)
                                )
                            except Exception as exc:
                                log.debug("Skipping non-JSON CLOB message: %s", exc)
                                continue
                            if not isinstance(events, list):
                                events = [events]
                            for event in events:
                                await self._apply_ws_event(event)
                    finally:
                        heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("CLOB WS disconnected: %s. Reconnecting in 5s...", exc)
                await asyncio.sleep(5)

    async def _send_market_heartbeats(self, ws) -> None:
        while True:
            await asyncio.sleep(8)
            await ws.send("PING")

    @staticmethod
    def _is_non_json_ws_message(raw: str | bytes) -> bool:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        stripped = raw.strip()
        if not stripped:
            return True
        if stripped.upper() in {"PING", "PONG"}:
            return True
        return stripped[0] not in "[{"

    async def _apply_ws_event(self, event: dict) -> None:
        event_type = event.get("event_type")
        if event_type == "price_change":
            for change in event.get("price_changes") or event.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                asset_id = str(change.get("asset_id") or event.get("asset_id") or "")
                market_ref = self._token_to_market.get(asset_id)
                if market_ref is None:
                    continue
                market_id, side = market_ref
                market = self._markets_by_id.get(market_id)
                if market is None:
                    continue

                best_bid = self._coerce_optional_price(change.get("best_bid"))
                best_ask = self._coerce_optional_price(change.get("best_ask"))
                if best_bid is None and best_ask is None:
                    level_price = self._coerce_optional_price(change.get("price"))
                    change_side = str(change.get("side") or "").upper()
                    if change_side == "BUY":
                        best_bid = level_price
                    elif change_side == "SELL":
                        best_ask = level_price
                if best_bid is None and best_ask is None:
                    continue
                self._update_market_book(market, side, best_bid, best_ask)
            return

        asset_id = str(event.get("asset_id") or "")
        market_ref = self._token_to_market.get(asset_id)
        if market_ref is None:
            return
        market_id, side = market_ref
        market = self._markets_by_id.get(market_id)
        if market is None:
            return

        best_bid = None
        best_ask = None
        if event_type == "book":
            bids = event.get("bids") or []
            asks = event.get("asks") or []
            if bids:
                best_bid = max(float(b["price"]) for b in bids)
            if asks:
                best_ask = min(float(a["price"]) for a in asks)
        if best_bid is None and best_ask is None:
            return

        self._update_market_book(market, side, best_bid, best_ask)

    @staticmethod
    def _coerce_optional_price(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _update_market_book(
        self,
        market: MarketRuntime,
        side: str,
        best_bid: float | None,
        best_ask: float | None,
    ) -> None:
        if side == "YES":
            if best_bid is not None:
                market.yes_bid = max(0.01, min(0.99, best_bid))
            if best_ask is not None:
                market.yes_ask = max(0.01, min(0.99, best_ask))
        else:
            if best_bid is not None:
                market.no_bid = max(0.01, min(0.99, best_bid))
            if best_ask is not None:
                market.no_ask = max(0.01, min(0.99, best_ask))

        if market.yes_ask and market.yes_bid and market.yes_ask < market.yes_bid:
            market.yes_ask = market.yes_bid
        if market.no_ask and market.no_bid and market.no_ask < market.no_bid:
            market.no_ask = market.no_bid

        if market.yes_bid and market.yes_ask:
            market.mid_price = (market.yes_bid + market.yes_ask) / 2
            market.spread = max(0.0, market.yes_ask - market.yes_bid)
        market.last_update_ts = time.time()
        market.observations += 1
        market.quote_source = "live_ws"

    @staticmethod
    def _cache_book_timestamp(book: CachedTopOfBook) -> datetime:
        observed_at = book.observed_at
        if observed_at.tzinfo is None:
            return observed_at.replace(tzinfo=timezone.utc)
        return observed_at.astimezone(timezone.utc)

    @staticmethod
    def _cache_price_to_float(value: Decimal | None) -> float | None:
        if value is None:
            return None
        return float(value)

    def _set_market_book_side(
        self,
        market: MarketRuntime,
        side: str,
        best_bid: float | None,
        best_ask: float | None,
    ) -> None:
        if side == "YES":
            if best_bid is not None:
                market.yes_bid = max(0.01, min(0.99, best_bid))
            if best_ask is not None:
                market.yes_ask = max(0.01, min(0.99, best_ask))
            if market.yes_ask and market.yes_bid and market.yes_ask < market.yes_bid:
                market.yes_ask = market.yes_bid
            return

        if best_bid is not None:
            market.no_bid = max(0.01, min(0.99, best_bid))
        if best_ask is not None:
            market.no_ask = max(0.01, min(0.99, best_ask))
        if market.no_ask and market.no_bid and market.no_ask < market.no_bid:
            market.no_ask = market.no_bid

    async def _hydrate_market_from_cache(self, market: MarketRuntime) -> bool:
        yes_book = await self.price_cache.get(market.market_id, token_id=market.token_id_yes)
        if yes_book is None:
            market.cache_observed_at = None
            market.detected = False
            return False

        best_bid = self._cache_price_to_float(yes_book.best_bid)
        best_ask = self._cache_price_to_float(yes_book.best_ask)
        if best_bid is None or best_ask is None:
            market.cache_observed_at = None
            market.detected = False
            return False

        self._set_market_book_side(market, "YES", best_bid, best_ask)

        no_book = await self.price_cache.get(market.market_id, token_id=market.token_id_no)
        if no_book is not None:
            self._set_market_book_side(
                market,
                "NO",
                self._cache_price_to_float(no_book.best_bid),
                self._cache_price_to_float(no_book.best_ask),
            )

        if yes_book.mid_price is not None:
            market.mid_price = float(yes_book.mid_price)
        else:
            market.mid_price = self._mid_or_zero(market.yes_bid, market.yes_ask)

        if yes_book.spread is not None:
            market.spread = max(0.0, float(yes_book.spread))
        else:
            market.spread = max(0.0, market.yes_ask - market.yes_bid)

        observed_at = self._cache_book_timestamp(yes_book)
        market.cache_observed_at = observed_at
        market.last_update_ts = observed_at.timestamp()
        market.quote_source = "price_cache"
        return True

    async def _refresh_stale_prices_loop(self) -> None:
        refresh_interval = max(2, self.strategy.cfg.signal_staleness_seconds)
        await asyncio.sleep(refresh_interval)
        while True:
            try:
                await self._refresh_stale_prices_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Stale refresh failed: %s", exc)
            await asyncio.sleep(refresh_interval)

    async def _refresh_stale_prices_once(self) -> None:
        now = time.time()
        stale_markets = [
            market
            for market in self._refresh_candidates()
            if not market.token_id_yes.startswith("bootstrap-")
            and (
                market.last_update_ts == 0
                or now - market.last_update_ts > self.strategy.cfg.signal_staleness_seconds * 4
            )
        ][:10]
        if not stale_markets:
            return

        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [self._refresh_one_market(client, market) for market in stale_markets]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_one_market(self, client: httpx.AsyncClient, market: MarketRuntime) -> None:
        yes_resp = await client.get(
            "https://clob.polymarket.com/price",
            params={"token_id": market.token_id_yes, "side": "BUY"},
        )
        no_resp = await client.get(
            "https://clob.polymarket.com/price",
            params={"token_id": market.token_id_no, "side": "BUY"},
        )
        if yes_resp.status_code != 200 or no_resp.status_code != 200:
            return

        yes_price = float(yes_resp.json().get("price", 0))
        no_price = float(no_resp.json().get("price", 0))
        if not (0 < yes_price < 1 and 0 < no_price < 1):
            return

        spread_est = max(0.01, min(0.04, abs(1 - (yes_price + no_price)) + 0.01))
        self._update_market_book(
            market, "YES", yes_price - (spread_est / 2), yes_price + (spread_est / 2)
        )
        self._update_market_book(
            market, "NO", no_price - (spread_est / 2), no_price + (spread_est / 2)
        )
        market.quote_source = "rest_refresh"

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        await ws.send_json({"type": "bootstrap", "payload": self.snapshot()})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    def snapshot(self) -> dict:
        return {
            "bot": {
                "status": (
                    "PAUSED"
                    if self.bot_state.paused
                    else ("RUNNING" if self.bot_state.running else "STOPPED")
                ),
                "uptime_seconds": int(time.time() - self.bot_state.started_at),
                "latency_ms": self._latest_tick["latency_ms"],
            },
            "risk_config": {
                "risk_per_trade_pct": self.strategy.cfg.risk_per_trade_pct,
                "max_total_exposure_pct": self.strategy.cfg.max_total_exposure_pct,
                "kelly_fraction": self.strategy.cfg.kelly_fraction,
                "max_drawdown_stop_pct": self.strategy.cfg.max_drawdown_stop_pct,
                "fee_bps": self.strategy.cfg.fee_bps,
                "allocation_mode": self.strategy.cfg.allocation_mode,
                "manual_notional_amount": self.strategy.cfg.manual_notional_amount,
                "max_concurrent_positions": self.strategy.cfg.max_concurrent_positions,
                "max_positions_per_tick": self.strategy.cfg.max_positions_per_tick,
                "min_observations": self.strategy.cfg.min_observations,
            },
            "stats": {
                **self._latest_tick["stats"],
                "portfolio_total": self._latest_tick["portfolio_total"],
                "capital_in_trade": self._latest_tick["capital_in_trade"],
                "pnl_percent": self._calculate_pnl_pct(),
            },
            "markets": self._display_markets(),
            "price_history": self._history[-336:],
            "recent_trades": self._trades[:300],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def set_command(self, command: str) -> dict:
        cmd = command.lower()
        if cmd == "start":
            self.bot_state.running = True
            self.bot_state.paused = False
            self.bot_state.started_at = time.time()
        elif cmd == "pause":
            self.bot_state.paused = True
        elif cmd == "stop":
            self.bot_state.running = False
            self.bot_state.paused = False
        else:
            raise ValueError(f"unsupported command: {command}")
        payload = self.snapshot()
        await self.broadcast({"type": "control", "payload": payload})
        return payload

    async def update_config(self, config_updates: dict) -> dict:
        for key, value in config_updates.items():
            if hasattr(self.strategy.cfg, key):
                setattr(self.strategy.cfg, key, value)
        payload = self.snapshot()
        await self.broadcast({"type": "control", "payload": payload})
        return payload

    def _sync_risk_guard(self) -> None:
        self._risk_guard.cfg = self.strategy.cfg
        self._risk_guard.executor = self.executor

    async def _handle_risk_halt(
        self,
        exc: DrawdownHaltException | APIFailureHaltException,
    ) -> None:
        self._sync_risk_guard()
        await self.set_command("stop")
        cancel_result = await self._risk_guard.cancel_all_open_orders()
        reason, details = self._risk_halt_details(exc)
        await self.broadcast(
            {
                "type": "halt",
                "reason": reason,
                "details": {
                    **details,
                    **cancel_result,
                },
            }
        )

    @staticmethod
    def _risk_halt_details(
        exc: DrawdownHaltException | APIFailureHaltException,
    ) -> tuple[str, dict]:
        if isinstance(exc, DrawdownHaltException):
            return (
                "drawdown",
                {
                    "current_drawdown_pct": exc.current_drawdown_pct,
                    "threshold_pct": exc.threshold_pct,
                },
            )
        return (
            "api_failures",
            {
                "consecutive_failures": exc.consecutive_failures,
                "threshold": 3,
            },
        )

    async def execute_trade(self, market_id: str, market_title: str) -> dict:
        market = self._markets_by_id.get(market_id)
        if market is None:
            raise ValueError("market not found")
        if market.open_trade_id is not None:
            raise ValueError("market already has an open position")

        portfolio = PortfolioState(
            equity=float(self._latest_tick["portfolio_total"])
            + float(self._latest_tick["stats"]["total_pnl"]),
            capital_in_trade=float(self._latest_tick["capital_in_trade"]),
            total_pnl=float(self._latest_tick["stats"]["total_pnl"]),
        )
        self._sync_risk_guard()
        await self._risk_guard.check_drawdown(portfolio)
        await self._risk_guard.check_api_health()

        notional, risk_pct = self.strategy.size_position(
            portfolio, expected_edge=market.expected_edge
        )
        if notional <= 0:
            raise ValueError("risk limits block new trade")

        side = market.direction if market.direction in {"BUY_YES", "BUY_NO"} else "BUY_YES"
        entry_price = self._entry_price_for_side(market, side)
        if entry_price <= 0:
            raise ValueError("market has no executable quote")
        size = round(notional / max(entry_price, 0.01), 4)
        token_id = market.token_id_yes if side == "BUY_YES" else market.token_id_no

        try:
            execution = await self.executor.execute(
                ExecutionRequest(
                    market_id=market_id,
                    market_title=market_title,
                    token_id=token_id,
                    side=side,
                    price=entry_price,
                    size=size,
                    notional=notional,
                    risk_pct=risk_pct,
                    expected_edge=market.expected_edge,
                )
            )
            self._risk_guard.record_api_success()
        except Exception as exc:
            self._risk_guard.record_api_failure(exc)
            await self._risk_guard.check_api_health()
            raise

        opened_at = datetime.now(timezone.utc).isoformat()
        trade = {
            "id": f"ord-{int(time.time() * 1000)}",
            "order_id": execution["order_id"],
            "execution_mode": execution["execution_mode"],
            "exchange_status": execution["exchange_status"],
            "tx_hash": execution["tx_hash"],
            "token_id": token_id,
            "market_id": market_id,
            "market_title": market_title,
            "side": side,
            "price": round(entry_price, 4),
            "size": size,
            "notional": notional,
            "risk_pct": risk_pct,
            "kelly": round(self.strategy.cfg.kelly_fraction, 3),
            "expected_edge": round(market.expected_edge, 6),
            "pnl_abs": 0.0,
            "pnl_pct": 0.0,
            "fees": 0.0,
            "status": "OPEN",
            "timestamp": opened_at,
            "closed_at": None,
            "unrealized_pnl_abs": 0.0,
            "unrealized_pnl_pct": 0.0,
        }

        self._trades.insert(0, trade)
        market.open_trade_id = trade["id"]
        market.last_trade_ts = time.time()
        self._latest_tick["capital_in_trade"] = round(
            self._latest_tick["capital_in_trade"] + notional, 2
        )
        self._recompute_trade_stats()
        await self.broadcast({"type": "trade", "payload": trade})
        return trade

    async def close_position(self, trade_id: str) -> dict:
        trade = next((item for item in self._trades if item["id"] == trade_id), None)
        if trade is None or trade["status"] != "OPEN":
            raise ValueError("active trade not found")

        market = self._markets_by_id.get(trade["market_id"])
        if market is None:
            raise ValueError("market not found")

        exit_price = self._exit_price_for_side(market, trade["side"])
        outcome = self.strategy.estimate_round_trip_pnl(
            entry_price=float(trade["price"]),
            exit_price=exit_price,
            size=float(trade["size"]),
        )
        trade["fees"] = outcome["fees"]
        trade["pnl_abs"] = outcome["pnl_abs"]
        trade["pnl_pct"] = outcome["pnl_pct"]
        trade["status"] = "CLOSED"
        trade["closed_at"] = datetime.now(timezone.utc).isoformat()
        trade["exchange_status"] = "CLOSED"
        trade["unrealized_pnl_abs"] = 0.0
        trade["unrealized_pnl_pct"] = 0.0

        market.open_trade_id = None
        self._latest_tick["capital_in_trade"] = max(
            0.0, round(self._latest_tick["capital_in_trade"] - trade["notional"], 2)
        )
        self._latest_tick["stats"]["total_pnl"] = round(
            self._latest_tick["stats"]["total_pnl"] + trade["pnl_abs"], 4
        )
        self._recompute_trade_stats()

        await self.broadcast({"type": "trade_closed", "payload": trade})
        return trade

    async def tick(self) -> None:
        if not self.bot_state.running or self.bot_state.paused:
            return

        started = time.perf_counter()
        now = time.time()
        candidates: list[MarketRuntime] = []

        for market in self._markets_by_id.values():
            if not await self._hydrate_market_from_cache(market):
                continue
            if market.yes_bid <= 0 or market.yes_ask <= 0:
                market.detected = False
                continue

            eval_out = self.strategy.evaluate_market(
                market.market_id, market.yes_bid, market.yes_ask
            )
            market.mid_price = eval_out["mid_price"]
            market.spread = eval_out["spread"]
            market.volatility = eval_out["volatility"]
            market.liquidity_score = eval_out["liquidity_score"]
            market.expected_edge = eval_out["expected_edge"]
            market.entry_threshold = eval_out["entry_threshold"]
            market.signal_strength = eval_out["signal_strength"]
            market.direction = eval_out["direction"]
            market.est_profit = max(0.0, eval_out["expected_edge"] * 100)
            market.price_delta = eval_out["price_delta"]
            market.momentum = eval_out["momentum"]
            market.observations = max(market.observations, eval_out["observations"])

            no_mid = self._mid_or_zero(market.no_bid, market.no_ask)
            market.complement_gap = abs((market.mid_price + no_mid) - 1) if no_mid else 0.0
            has_live_quote = market.last_update_ts > 0 and not market.bootstrap_only
            is_stale = (not has_live_quote) or (
                (now - market.last_update_ts) > self.strategy.cfg.signal_staleness_seconds
            )
            cooled_down = (now - market.last_trade_ts) >= self.strategy.cfg.cooldown_seconds
            coherent = market.complement_gap <= 0.02 if no_mid else True

            market.detected = bool(
                eval_out["detected"]
                and has_live_quote
                and not is_stale
                and coherent
                and cooled_down
                and market.open_trade_id is None
                and market.expected_edge > 0.002  # Stricter P0 floor
            )
            if market.detected:
                candidates.append(market)

        open_trades = [trade for trade in self._trades if trade["status"] == "OPEN"]
        for trade in list(open_trades):
            market = self._markets_by_id.get(trade["market_id"])
            if market is None:
                continue

            exit_price = self._exit_price_for_side(market, trade["side"])
            unrealized = self.strategy.estimate_round_trip_pnl(
                entry_price=float(trade["price"]),
                exit_price=exit_price,
                size=float(trade["size"]),
            )
            trade["unrealized_pnl_abs"] = unrealized["pnl_abs"]
            trade["unrealized_pnl_pct"] = unrealized["pnl_pct"]

            opened_at = datetime.fromisoformat(trade["timestamp"]).timestamp()
            age = now - opened_at
            signal_weakened = market.expected_edge < (market.entry_threshold * 0.6)
            signal_reversed = (trade["side"] == "BUY_YES" and market.direction == "BUY_NO") or (
                trade["side"] == "BUY_NO" and market.direction == "BUY_YES"
            )
            market_stale = (
                market.last_update_ts > 0
                and (now - market.last_update_ts) > self.strategy.cfg.signal_staleness_seconds * 2
            )
            if (
                age >= self.strategy.cfg.max_holding_seconds
                or signal_weakened
                or signal_reversed
                or market_stale
            ):
                try:
                    await self.close_position(trade["id"])
                except ValueError:
                    pass

        slots = max(0, self.strategy.cfg.max_concurrent_positions - self._count_open_positions())
        allowance = min(slots, self.strategy.cfg.max_positions_per_tick)
        if allowance > 0:
            ranked = sorted(
                candidates,
                key=lambda market: (
                    market.signal_strength,
                    market.expected_edge,
                    -market.spread,
                    market.observations,
                ),
                reverse=True,
            )
            for market in ranked[:allowance]:
                try:
                    await self.execute_trade(market.market_id, market.title)
                except (DrawdownHaltException, APIFailureHaltException) as exc:
                    await self._handle_risk_halt(exc)
                    return
                except ValueError:
                    continue
                except Exception as exc:
                    log.warning("Trade execution failed for %s: %s", market.market_id, exc)
                    continue

        self._latest_tick["latency_ms"] = int((time.perf_counter() - started) * 1000)
        self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
        self._latest_tick["stats"]["detected_arbs_today"] += len(candidates[:allowance])
        self._latest_tick["stats"]["open_positions"] = self._count_open_positions()

        point = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio": self._latest_tick["portfolio_total"]
            + self._latest_tick["stats"]["total_pnl"],
            "pnl_pct": self._calculate_pnl_pct(),
        }
        self._history.append(point)
        self._history = self._history[-1200:]

        await self.broadcast(
            {
                "type": "tick",
                "payload": {
                    "latency_ms": self._latest_tick["latency_ms"],
                    "stats": self.snapshot()["stats"],
                    "markets": self._display_markets(),
                    "point": point,
                },
            }
        )

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            targets = list(self._connections)
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(ws)

    def pnl_series(self, timeframe: str) -> list[dict]:
        counts = {"24h": 96, "7d": 336, "30d": 720, "90d": 1200}
        return self._history[-counts.get(timeframe, 336) :]

    def _display_markets(self) -> list[dict]:
        return [market.display() for market in self._refresh_candidates()]

    def _refresh_candidates(self) -> list[MarketRuntime]:
        ranked = sorted(
            self._markets_by_id.values(),
            key=lambda market: (
                1 if market.open_trade_id else 0,
                1 if market.detected else 0,
                market.signal_strength,
                market.last_update_ts,
            ),
            reverse=True,
        )
        return ranked[: self.strategy.cfg.display_market_limit]

    def _entry_price_for_side(self, market: MarketRuntime, side: str) -> float:
        if side == "BUY_NO":
            return (
                market.no_ask
                or self._mid_or_zero(market.no_bid, market.no_ask)
                or max(0.01, min(0.99, 1 - market.mid_price))
            )
        return market.yes_ask or market.mid_price

    def _exit_price_for_side(self, market: MarketRuntime, side: str) -> float:
        if side == "BUY_NO":
            return (
                market.no_bid
                or self._mid_or_zero(market.no_bid, market.no_ask)
                or max(0.01, min(0.99, 1 - market.mid_price))
            )
        return market.yes_bid or market.mid_price

    def _mid_or_zero(self, bid: float, ask: float) -> float:
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return 0.0

    def _count_open_positions(self) -> int:
        return sum(1 for trade in self._trades if trade["status"] == "OPEN")

    def _recompute_trade_stats(self) -> None:
        closed_trades = [trade for trade in self._trades if trade["status"] == "CLOSED"]
        if not closed_trades:
            self._latest_tick["stats"]["win_rate"] = 0.0
            self._latest_tick["stats"]["avg_profit"] = 0.0
            return
        wins = sum(1 for trade in closed_trades if trade["pnl_abs"] > 0)
        self._latest_tick["stats"]["win_rate"] = round((wins / len(closed_trades)) * 100, 2)
        self._latest_tick["stats"]["avg_profit"] = round(
            sum(trade["pnl_abs"] for trade in closed_trades) / len(closed_trades), 4
        )

    def _calculate_pnl_pct(self) -> float:
        base = self._latest_tick["portfolio_total"]
        if base <= 0:
            return 0.0
        return round((self._latest_tick["stats"]["total_pnl"] / base) * 100, 3)


live_hub = LiveHub(price_cache=PriceStateCache())
