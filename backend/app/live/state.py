from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
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
from app.live.pipeline import (
    AnalyticsLayer,
    DecisionEngine,
    IngestionLayer,
    PortfolioState,
    as_utc,
    iso_or_none,
    utc_now,
)
from app.services.adaptive_strategy import AdaptiveStrategyEngine, RiskConfig
from app.services.price_state_cache import CachedTopOfBook, PriceStateCache
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
    active_run_started_at: float | None = field(default_factory=time.time)
    accumulated_run_seconds: float = 0.0
    last_command_at: float = field(default_factory=time.time)
    stopped_at: float | None = None

    @property
    def status(self) -> str:
        if self.paused:
            return "PAUSED"
        if self.running:
            return "RUNNING"
        return "STOPPED"

    def uptime_seconds(self, now_ts: float | None = None) -> int:
        current = now_ts if now_ts is not None else time.time()
        total = self.accumulated_run_seconds
        if self.running and not self.paused and self.active_run_started_at is not None:
            total += current - self.active_run_started_at
        return max(0, int(total))

    def apply_command(self, command: str) -> None:
        now_ts = time.time()
        self.last_command_at = now_ts

        if command == "start":
            if self.status == "STOPPED":
                self.accumulated_run_seconds = 0.0
                self.started_at = now_ts
                self.stopped_at = None
            self.running = True
            self.paused = False
            if self.active_run_started_at is None:
                self.active_run_started_at = now_ts
            return

        if command == "pause":
            if self.running and not self.paused and self.active_run_started_at is not None:
                self.accumulated_run_seconds += now_ts - self.active_run_started_at
            self.paused = True
            self.running = True
            self.active_run_started_at = None
            return

        if command == "stop":
            if self.running and not self.paused and self.active_run_started_at is not None:
                self.accumulated_run_seconds += now_ts - self.active_run_started_at
            self.running = False
            self.paused = False
            self.active_run_started_at = None
            self.stopped_at = now_ts
            return

        raise ValueError(f"unsupported command: {command}")


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
    rolling_mean: float = 0.0
    rolling_std: float = 0.0
    z_score: float = 0.0
    no_mid_price: float = 0.0
    pressure: float = 0.0
    imbalance: float = 0.0
    regime: str = "normal"
    freshness_ms: int = 0
    source_delay_ms: int = 0
    decision_action: str = "HOLD"
    decision_summary: str = ""
    decision_rejections: list[str] = field(default_factory=list)
    decision_reasons: list[str] = field(default_factory=list)
    explain: list[str] = field(default_factory=list)

    def display(self) -> dict:
        return {
            "market_id": self.market_id,
            "title": self.title,
            "end_date": self.end_date,
            "token_id_yes": self.token_id_yes,
            "token_id_no": self.token_id_no,
            "best_bid": round(self.yes_bid, 4),
            "best_ask": round(self.yes_ask, 4),
            "mid_price": round(self.mid_price, 4),
            "no_mid_price": round(self.no_mid_price, 4),
            "spread": round(self.spread, 4),
            "volatility": round(self.volatility, 6),
            "rolling_mean": round(self.rolling_mean, 6),
            "rolling_std": round(self.rolling_std, 6),
            "z_score": round(self.z_score, 4),
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
            "pressure": round(self.pressure, 6),
            "imbalance": round(self.imbalance, 6),
            "regime": self.regime,
            "stale_seconds": round(self.freshness_ms / 1000, 3),
            "freshness_ms": self.freshness_ms,
            "source_delay_ms": self.source_delay_ms,
            "open_trade_id": self.open_trade_id,
            "open_position": self.open_trade_id is not None,
            "bootstrap_only": self.bootstrap_only,
            "quote_source": self.quote_source,
            "cache_age_ms": self.freshness_ms,
            "decision_action": self.decision_action,
            "decision_summary": self.decision_summary,
            "decision_rejections": self.decision_rejections,
            "decision_reasons": self.decision_reasons,
            "explain": self.explain,
        }


class LiveHub:
    """Live orchestrator that separates ingestion, analytics, and decisions."""

    def __init__(self, price_cache: PriceStateCache | None = None) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.bot_state = BotState()
        self.price_cache = price_cache or PriceStateCache()
        self.strategy = AdaptiveStrategyEngine(RiskConfig())
        self.executor = TradeExecutor()
        self._risk_guard = RiskGuard(self.strategy.cfg, self.executor)
        self.ingestion = IngestionLayer()
        self.analytics = AnalyticsLayer()
        self.decision_engine = DecisionEngine()
        self._markets_by_id: dict[str, MarketRuntime] = {}
        self._token_to_market: dict[str, tuple[str, str]] = {}
        self._history: list[dict] = []
        self._signal_history: list[dict] = []
        self._trades: list[dict] = []
        self._logs: list[dict] = []
        self._latest_tick = self._initial_snapshot()
        self._latest_ingestion: dict = self.ingestion.health_snapshot(utc_now(), display_limit=12)
        self._latest_analytics_rows: list[dict] = []
        self._latest_analytics_summary: dict = {
            "tracked_markets": 0,
            "opportunity_count": 0,
            "top_signal_score": 0.0,
            "top_edge": 0.0,
            "avg_freshness_ms": 0,
            "avg_volatility": 0.0,
        }
        self._latest_decisions: list[dict] = []
        self._latest_decision_summary: dict = {
            "actionable_count": 0,
            "open_count": 0,
            "close_count": 0,
            "reduce_count": 0,
            "reject_count": 0,
            "slots_remaining": self.strategy.cfg.max_concurrent_positions,
            "exposure_remaining": self._latest_tick["portfolio_total"],
        }
        self._ws_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._started = False

    def _initial_snapshot(self) -> dict:
        return {
            "latency_ms": 0,
            "cycle_latency_ms": 0,
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

    def _log(
        self,
        *,
        level: str,
        category: str,
        message: str,
        market_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        self._logs.append(
            {
                "timestamp": utc_now().isoformat(),
                "level": level,
                "category": category,
                "message": message,
                "market_id": market_id,
                "details": details or {},
            }
        )
        self._logs = self._logs[-200:]

    def _sync_runtime_catalog(self) -> None:
        for market in self._markets_by_id.values():
            self.ingestion.register_market(
                market_id=market.market_id,
                title=market.title,
                end_date=market.end_date,
                token_id_yes=market.token_id_yes,
                token_id_no=market.token_id_no,
                bootstrap_only=market.bootstrap_only,
            )
            self._token_to_market.setdefault(market.token_id_yes, (market.market_id, "YES"))
            self._token_to_market.setdefault(market.token_id_no, (market.market_id, "NO"))

    def _register_market(self, runtime: MarketRuntime) -> None:
        self._markets_by_id[runtime.market_id] = runtime
        self._token_to_market[runtime.token_id_yes] = (runtime.market_id, "YES")
        self._token_to_market[runtime.token_id_no] = (runtime.market_id, "NO")
        self.ingestion.register_market(
            market_id=runtime.market_id,
            title=runtime.title,
            end_date=runtime.end_date,
            token_id_yes=runtime.token_id_yes,
            token_id_no=runtime.token_id_no,
            bootstrap_only=runtime.bootstrap_only,
        )
        observed_at = as_utc(runtime.cache_observed_at) or utc_now()
        self._record_quote(
            runtime=runtime,
            side="YES",
            best_bid=runtime.yes_bid,
            best_ask=runtime.yes_ask,
            source=runtime.quote_source,
            observed_at=observed_at,
        )
        self._record_quote(
            runtime=runtime,
            side="NO",
            best_bid=runtime.no_bid,
            best_ask=runtime.no_ask,
            source=runtime.quote_source,
            observed_at=observed_at,
        )

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True

        if os.getenv("PYTEST_CURRENT_TEST"):
            if not self._markets_by_id:
                self._bootstrap_markets()
            self._sync_runtime_catalog()
            self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
            return

        settings = get_settings()
        gamma = GammaClient(settings.polymarket_gamma_base_url)
        try:
            await asyncio.wait_for(self._load_active_markets(gamma), timeout=4.0)
        except Exception as exc:
            log.error("Gamma API error while loading market universe: %s", exc)
            self._log(level="warning", category="startup", message=f"gamma load failed: {exc}")
        finally:
            await gamma.close()

        if not self._markets_by_id:
            self._bootstrap_markets()

        self._sync_runtime_catalog()
        self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
        if websockets is not None:
            self._ws_task = asyncio.create_task(self._listen_to_clob())
        self._refresh_task = asyncio.create_task(self._refresh_stale_prices_loop())
        self._log(
            level="info",
            category="startup",
            message=f"live runtime started with {len(self._markets_by_id)} markets",
        )

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
        now = utc_now()
        for page in range(15):
            markets = await gamma.fetch_markets(
                limit=page_size,
                offset=page * page_size,
                active=True,
                closed=False,
            )
            if not markets:
                break
            for raw_market in markets:
                runtime = self._runtime_from_gamma(raw_market, now)
                if runtime is None or runtime.market_id in self._markets_by_id:
                    continue
                self._register_market(runtime)

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
        except Exception:
            return None
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
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
            no_mid_price=no_mid,
            spread=spread_est,
            bootstrap_only=False,
            quote_source="gamma_seed",
        )

    def _bootstrap_markets(self) -> None:
        fallback_markets = [
            MarketRuntime(
                market_id="bootstrap-market-1",
                title="Fallback market loaded without Gamma connectivity",
                end_date=utc_now().isoformat(),
                token_id_yes="bootstrap-yes-1",
                token_id_no="bootstrap-no-1",
                yes_bid=0.48,
                yes_ask=0.50,
                no_bid=0.50,
                no_ask=0.52,
                mid_price=0.49,
                no_mid_price=0.51,
                spread=0.02,
                bootstrap_only=True,
                quote_source="bootstrap",
            ),
            MarketRuntime(
                market_id="bootstrap-market-2",
                title="Second fallback market for offline tests",
                end_date=utc_now().isoformat(),
                token_id_yes="bootstrap-yes-2",
                token_id_no="bootstrap-no-2",
                yes_bid=0.57,
                yes_ask=0.59,
                no_bid=0.41,
                no_ask=0.43,
                mid_price=0.58,
                no_mid_price=0.42,
                spread=0.02,
                bootstrap_only=True,
                quote_source="bootstrap",
            ),
        ]
        for runtime in fallback_markets:
            self._register_market(runtime)

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
                self.ingestion.update_source_status("live_ws", "connecting")
                async with websockets.connect(
                    settings.polymarket_clob_ws_url,
                    ping_interval=15,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    self.ingestion.update_source_status("live_ws", "live")
                    heartbeat_task = asyncio.create_task(self._send_market_heartbeats(ws))
                    for idx in range(0, len(token_ids), chunk_size):
                        payload = {
                            "assets_ids": token_ids[idx : idx + chunk_size],
                            "type": "market",
                        }
                        encoded = orjson.dumps(payload) if orjson is not None else json.dumps(payload)
                        await ws.send(encoded)

                    try:
                        async for raw in ws:
                            if self._is_non_json_ws_message(raw):
                                continue
                            try:
                                events = orjson.loads(raw) if orjson is not None else json.loads(raw)
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
                self.ingestion.update_source_status(
                    "live_ws",
                    "degraded",
                    note=f"reconnecting: {exc}",
                )
                self._log(level="warning", category="ingestion", message=f"ws reconnecting: {exc}")
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
        self._sync_runtime_catalog()
        event_type = event.get("event_type")
        observed_at = utc_now()
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
                self._record_quote(
                    runtime=market,
                    side=side,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    source="live_ws",
                    observed_at=observed_at,
                    received_at=observed_at,
                    raw_event=change,
                )
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

        self._record_quote(
            runtime=market,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            source="live_ws",
            observed_at=observed_at,
            received_at=observed_at,
            raw_event=event,
        )

    @staticmethod
    def _coerce_optional_price(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _record_quote(
        self,
        *,
        runtime: MarketRuntime,
        side: str,
        best_bid: float | None,
        best_ask: float | None,
        source: str,
        observed_at: datetime | None = None,
        received_at: datetime | None = None,
        raw_event: dict | None = None,
    ) -> bool:
        current_bid = runtime.yes_bid if side == "YES" else runtime.no_bid
        current_ask = runtime.yes_ask if side == "YES" else runtime.no_ask
        next_bid = current_bid if best_bid is None else float(best_bid)
        next_ask = current_ask if best_ask is None else float(best_ask)
        if next_bid <= 0 and next_ask <= 0:
            return False
        if next_bid <= 0:
            next_bid = next_ask
        if next_ask <= 0:
            next_ask = next_bid
        if side == "YES":
            runtime.yes_bid = max(0.01, min(0.99, next_bid))
            runtime.yes_ask = max(runtime.yes_bid, min(0.99, next_ask))
            runtime.mid_price = (runtime.yes_bid + runtime.yes_ask) / 2
            runtime.spread = max(0.0, runtime.yes_ask - runtime.yes_bid)
        else:
            runtime.no_bid = max(0.01, min(0.99, next_bid))
            runtime.no_ask = max(runtime.no_bid, min(0.99, next_ask))
            runtime.no_mid_price = self._mid_or_zero(runtime.no_bid, runtime.no_ask)

        if side == "YES" and runtime.no_bid and runtime.no_ask:
            runtime.no_mid_price = self._mid_or_zero(runtime.no_bid, runtime.no_ask)
        if side == "NO":
            runtime.no_mid_price = self._mid_or_zero(runtime.no_bid, runtime.no_ask)

        observed = as_utc(observed_at) or utc_now()
        runtime.last_update_ts = observed.timestamp()
        runtime.quote_source = source
        if source == "price_cache":
            runtime.cache_observed_at = observed

        return self.ingestion.ingest_quote(
            market_id=runtime.market_id,
            side=side,
            best_bid=runtime.yes_bid if side == "YES" else runtime.no_bid,
            best_ask=runtime.yes_ask if side == "YES" else runtime.no_ask,
            observed_at=observed,
            received_at=received_at,
            source=source,
            raw_event=raw_event,
        )

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

    async def _hydrate_market_from_cache(self, market: MarketRuntime) -> bool:
        yes_book = await self.price_cache.get(market.market_id, token_id=market.token_id_yes)
        if yes_book is None:
            market.cache_observed_at = None
            return False

        best_bid = self._cache_price_to_float(yes_book.best_bid)
        best_ask = self._cache_price_to_float(yes_book.best_ask)
        if best_bid is None or best_ask is None:
            market.cache_observed_at = None
            return False

        observed_at = self._cache_book_timestamp(yes_book)
        self._record_quote(
            runtime=market,
            side="YES",
            best_bid=best_bid,
            best_ask=best_ask,
            source="price_cache",
            observed_at=observed_at,
        )

        no_book = await self.price_cache.get(market.market_id, token_id=market.token_id_no)
        if no_book is not None:
            self._record_quote(
                runtime=market,
                side="NO",
                best_bid=self._cache_price_to_float(no_book.best_bid),
                best_ask=self._cache_price_to_float(no_book.best_ask),
                source="price_cache",
                observed_at=self._cache_book_timestamp(no_book),
            )
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
        self._sync_runtime_catalog()
        now_ts = time.time()
        stale_markets = [
            market
            for market in self._refresh_candidates()
            if not market.token_id_yes.startswith("bootstrap-")
            and (
                market.last_update_ts == 0
                or now_ts - market.last_update_ts > self.strategy.cfg.signal_staleness_seconds * 4
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

        observed_at = utc_now()
        spread_est = max(0.01, min(0.04, abs(1 - (yes_price + no_price)) + 0.01))
        self._record_quote(
            runtime=market,
            side="YES",
            best_bid=yes_price - (spread_est / 2),
            best_ask=yes_price + (spread_est / 2),
            source="rest_refresh",
            observed_at=observed_at,
        )
        self._record_quote(
            runtime=market,
            side="NO",
            best_bid=no_price - (spread_est / 2),
            best_ask=no_price + (spread_est / 2),
            source="rest_refresh",
            observed_at=observed_at,
        )

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        await ws.send_json({"type": "bootstrap", "payload": self.snapshot()})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    def snapshot(self) -> dict:
        now = utc_now()
        status = self.bot_state.status
        risk_cfg = self.strategy.cfg
        snapshot = {
            "clock": {
                "server_time": now.isoformat(),
                "cycle_interval_ms": 250,
            },
            "bot": {
                "status": status,
                "uptime_seconds": self.bot_state.uptime_seconds(now.timestamp()),
                "latency_ms": self._latest_tick["latency_ms"],
                "cycle_latency_ms": self._latest_tick["cycle_latency_ms"],
                "started_at": datetime.fromtimestamp(self.bot_state.started_at, timezone.utc).isoformat(),
                "active_run_started_at": iso_or_none(
                    datetime.fromtimestamp(self.bot_state.active_run_started_at, timezone.utc)
                    if self.bot_state.active_run_started_at is not None
                    else None
                ),
                "accumulated_run_seconds": round(self.bot_state.accumulated_run_seconds, 3),
                "last_command_at": datetime.fromtimestamp(
                    self.bot_state.last_command_at, timezone.utc
                ).isoformat(),
                "stopped_at": iso_or_none(
                    datetime.fromtimestamp(self.bot_state.stopped_at, timezone.utc)
                    if self.bot_state.stopped_at is not None
                    else None
                ),
                "execution_enabled": status == "RUNNING",
            },
            "risk_config": {
                "risk_per_trade_pct": risk_cfg.risk_per_trade_pct,
                "max_total_exposure_pct": risk_cfg.max_total_exposure_pct,
                "kelly_fraction": risk_cfg.kelly_fraction,
                "max_drawdown_stop_pct": risk_cfg.max_drawdown_stop_pct,
                "fee_bps": risk_cfg.fee_bps,
                "base_entry_threshold": risk_cfg.base_entry_threshold,
                "spread_cap": risk_cfg.spread_cap,
                "allocation_mode": risk_cfg.allocation_mode,
                "manual_notional_amount": risk_cfg.manual_notional_amount,
                "min_observations": risk_cfg.min_observations,
                "min_signal_strength": risk_cfg.min_signal_strength,
                "max_concurrent_positions": risk_cfg.max_concurrent_positions,
                "max_positions_per_tick": risk_cfg.max_positions_per_tick,
                "cooldown_seconds": risk_cfg.cooldown_seconds,
                "signal_staleness_seconds": risk_cfg.signal_staleness_seconds,
                "max_holding_seconds": risk_cfg.max_holding_seconds,
                "display_market_limit": risk_cfg.display_market_limit,
            },
            "stats": {
                **self._latest_tick["stats"],
                "portfolio_total": self._latest_tick["portfolio_total"],
                "capital_in_trade": self._latest_tick["capital_in_trade"],
                "pnl_percent": self._calculate_pnl_pct(),
            },
            "ingestion": self._latest_ingestion,
            "analytics": {
                "summary": self._latest_analytics_summary,
                "opportunities": [
                    row
                    for row in self._latest_analytics_rows
                    if row.get("decision_action") == "OPEN"
                ][:12],
                "leaderboard": self._latest_analytics_rows[:24],
                "history": self._signal_history[-336:],
            },
            "decision_engine": {
                "summary": self._latest_decision_summary,
                "ranked": self._latest_decisions[:24],
            },
            "positions": self._positions_snapshot(),
            "markets": self._display_markets(),
            "price_history": self._history[-336:],
            "recent_trades": self._trades[:300],
            "logs": self._logs[-80:],
            "timestamp": now.isoformat(),
        }
        return snapshot

    async def set_command(self, command: str) -> dict:
        cmd = command.lower()
        self.bot_state.apply_command(cmd)
        self._log(level="info", category="control", message=f"bot command {cmd}")
        payload = self.snapshot()
        await self.broadcast({"type": "control", "payload": payload})
        return payload

    async def update_config(self, config_updates: dict) -> dict:
        for key, value in config_updates.items():
            if hasattr(self.strategy.cfg, key):
                setattr(self.strategy.cfg, key, value)
        self._sync_risk_guard()
        self._log(level="info", category="config", message="strategy config updated", details=config_updates)
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
        self._log(level="error", category="risk", message=f"risk halt: {reason}", details=details)
        await self.broadcast(
            {
                "type": "halt",
                "reason": reason,
                "details": {**details, **cancel_result},
                "snapshot": self.snapshot(),
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

        portfolio = self._portfolio_state()
        self._sync_risk_guard()
        await self._risk_guard.check_drawdown(portfolio)
        await self._risk_guard.check_api_health()

        notional, risk_pct = self.strategy.size_position(
            portfolio,
            expected_edge=market.expected_edge,
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

        opened_at = utc_now().isoformat()
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
            self._latest_tick["capital_in_trade"] + notional,
            2,
        )
        self._recompute_trade_stats()
        self._log(
            level="info",
            category="decision",
            message=f"opened {side} position",
            market_id=market_id,
            details={"notional": notional, "expected_edge": market.expected_edge},
        )
        await self.broadcast(
            {
                "type": "trade",
                "payload": trade,
                "snapshot": self.snapshot(),
            }
        )
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
        trade["closed_at"] = utc_now().isoformat()
        trade["exchange_status"] = "CLOSED"
        trade["unrealized_pnl_abs"] = 0.0
        trade["unrealized_pnl_pct"] = 0.0

        market.open_trade_id = None
        self._latest_tick["capital_in_trade"] = max(
            0.0,
            round(self._latest_tick["capital_in_trade"] - trade["notional"], 2),
        )
        self._latest_tick["stats"]["total_pnl"] = round(
            self._latest_tick["stats"]["total_pnl"] + trade["pnl_abs"],
            4,
        )
        self._recompute_trade_stats()
        self._log(
            level="info",
            category="decision",
            message="closed position",
            market_id=trade["market_id"],
            details={"pnl_abs": trade["pnl_abs"], "pnl_pct": trade["pnl_pct"]},
        )
        await self.broadcast(
            {
                "type": "trade_closed",
                "payload": trade,
                "snapshot": self.snapshot(),
            }
        )
        return trade

    def _portfolio_state(self) -> PortfolioState:
        return PortfolioState(
            equity=float(self._latest_tick["portfolio_total"]) + float(self._latest_tick["stats"]["total_pnl"]),
            capital_in_trade=float(self._latest_tick["capital_in_trade"]),
            total_pnl=float(self._latest_tick["stats"]["total_pnl"]),
        )

    def _update_open_positions_mark_to_market(self, now_ts: float) -> None:
        open_trades = [trade for trade in self._trades if trade["status"] == "OPEN"]
        for trade in open_trades:
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
            age_seconds = now_ts - opened_at
            if age_seconds >= self.strategy.cfg.max_holding_seconds:
                trade["_close_reason"] = "max_holding_time"

    async def tick(self) -> None:
        self._sync_runtime_catalog()
        if self.bot_state.status != "RUNNING":
            return

        started = time.perf_counter()
        now_dt = utc_now()
        now_ts = now_dt.timestamp()

        for market in self._markets_by_id.values():
            hydrated = await self._hydrate_market_from_cache(market)
            state = self.ingestion.market(market.market_id)
            if not hydrated or state is None or state.yes.latest is not None:
                continue
            observed_at = (
                as_utc(market.cache_observed_at)
                or (
                    datetime.fromtimestamp(market.last_update_ts, timezone.utc)
                    if market.last_update_ts
                    else None
                )
                or now_dt
            )
            if market.yes_bid > 0 and market.yes_ask > 0:
                self._record_quote(
                    runtime=market,
                    side="YES",
                    best_bid=market.yes_bid,
                    best_ask=market.yes_ask,
                    source=market.quote_source,
                    observed_at=observed_at,
                )
            if market.no_bid > 0 and market.no_ask > 0:
                self._record_quote(
                    runtime=market,
                    side="NO",
                    best_bid=market.no_bid,
                    best_ask=market.no_ask,
                    source=market.quote_source,
                    observed_at=observed_at,
                )

        analytics_rows, analytics_summary = self.analytics.evaluate(
            self.ingestion,
            self.strategy.cfg,
            now_dt,
        )
        enriched_rows = []
        for row in analytics_rows:
            runtime = self._markets_by_id.get(row.market_id)
            if runtime is None:
                enriched_rows.append(row)
                continue
            legacy_eval = self.strategy.evaluate_market(runtime.market_id, runtime.yes_bid, runtime.yes_ask)
            rank_score = max(
                row.rank_score,
                float(legacy_eval.get("signal_strength", row.signal_strength)),
            )
            enriched_rows.append(
                replace(
                    row,
                    volatility=float(legacy_eval.get("volatility", row.volatility)),
                    liquidity_score=float(legacy_eval.get("liquidity_score", row.liquidity_score)),
                    expected_edge=float(legacy_eval.get("expected_edge", row.expected_edge)),
                    entry_threshold=float(legacy_eval.get("entry_threshold", row.entry_threshold)),
                    signal_strength=float(legacy_eval.get("signal_strength", row.signal_strength)),
                    direction=str(legacy_eval.get("direction", row.direction)),
                    price_delta=float(legacy_eval.get("price_delta", row.price_delta)),
                    observations=max(row.observations, int(legacy_eval.get("observations", row.observations))),
                    momentum=float(legacy_eval.get("momentum", row.momentum)),
                    rank_score=rank_score,
                )
            )
        analytics_rows = enriched_rows
        analytics_summary = {
            "tracked_markets": len(analytics_rows),
            "opportunity_count": sum(
                1 for row in analytics_rows if row.signal_strength >= self.strategy.cfg.min_signal_strength
            ),
            "top_signal_score": round(
                max((row.signal_strength for row in analytics_rows), default=0.0),
                4,
            ),
            "top_edge": round(max((row.expected_edge for row in analytics_rows), default=0.0), 6),
            "avg_freshness_ms": int(
                sum(row.freshness_ms for row in analytics_rows) / len(analytics_rows)
            )
            if analytics_rows
            else 0,
            "avg_volatility": round(
                sum(row.volatility for row in analytics_rows) / len(analytics_rows),
                6,
            )
            if analytics_rows
            else 0.0,
        }
        open_positions_by_market = {
            trade["market_id"]: trade
            for trade in self._trades
            if trade["status"] == "OPEN"
        }
        decisions, decision_summary = self.decision_engine.evaluate(
            analytics_rows,
            bot_status=self.bot_state.status,
            cfg=self.strategy.cfg,
            now_ts=now_ts,
            portfolio=self._portfolio_state(),
            open_positions_by_market=open_positions_by_market,
            last_trade_ts_by_market={
                market_id: runtime.last_trade_ts
                for market_id, runtime in self._markets_by_id.items()
            },
        )

        decision_by_market = {item.market_id: item.to_snapshot() for item in decisions}
        analytics_rows_display: list[dict] = []
        for row in analytics_rows:
            runtime = self._markets_by_id.get(row.market_id)
            if runtime is None:
                continue
            decision = decision_by_market.get(row.market_id, {})
            runtime.mid_price = row.mid_price
            runtime.no_mid_price = row.no_mid_price
            runtime.spread = row.spread
            runtime.volatility = row.volatility
            runtime.rolling_mean = row.rolling_mean
            runtime.rolling_std = row.rolling_std
            runtime.z_score = row.z_score
            runtime.liquidity_score = row.liquidity_score
            runtime.expected_edge = row.expected_edge
            runtime.entry_threshold = row.entry_threshold
            runtime.signal_strength = row.signal_strength
            runtime.direction = row.direction
            runtime.est_profit = max(0.0, row.expected_edge * 100)
            runtime.observations = row.observations
            runtime.complement_gap = row.complement_gap
            runtime.price_delta = row.price_delta
            runtime.momentum = row.momentum
            runtime.pressure = row.pressure
            runtime.imbalance = row.imbalance
            runtime.quote_source = row.quote_source
            runtime.regime = row.regime
            runtime.freshness_ms = row.freshness_ms
            runtime.source_delay_ms = row.source_delay_ms
            runtime.detected = decision.get("action") == "OPEN"
            runtime.decision_action = decision.get("action", "HOLD")
            runtime.decision_summary = decision.get("summary", "")
            runtime.decision_rejections = decision.get("rejections", [])
            runtime.decision_reasons = decision.get("reasons", [])
            runtime.explain = row.explain
            analytics_rows_display.append(
                row.to_market_row(decision=decision, open_trade_id=runtime.open_trade_id)
            )

        self._update_open_positions_mark_to_market(now_ts)

        for trade in [item for item in self._trades if item["status"] == "OPEN"]:
            close_reason = trade.pop("_close_reason", None)
            decision = decision_by_market.get(trade["market_id"])
            should_close = close_reason is not None or (
                decision is not None and decision["action"] == "CLOSE" and decision["executable"]
            )
            if not should_close:
                continue
            try:
                await self.close_position(trade["id"])
            except ValueError:
                continue

        open_decisions = [
            item
            for item in decisions
            if item.action == "OPEN" and item.executable
        ]
        allowance = min(
            max(0, self.strategy.cfg.max_concurrent_positions - self._count_open_positions()),
            self.strategy.cfg.max_positions_per_tick,
        )
        for decision in open_decisions[:allowance]:
            runtime = self._markets_by_id.get(decision.market_id)
            if runtime is None:
                continue
            try:
                await self.execute_trade(runtime.market_id, runtime.title)
            except (DrawdownHaltException, APIFailureHaltException) as exc:
                await self._handle_risk_halt(exc)
                return
            except ValueError:
                continue
            except Exception as exc:
                log.warning("Trade execution failed for %s: %s", runtime.market_id, exc)
                self._log(
                    level="warning",
                    category="decision",
                    message=f"trade execution failed: {exc}",
                    market_id=runtime.market_id,
                )

        self._latest_ingestion = self.ingestion.health_snapshot(now_dt, display_limit=12)
        self._latest_tick["latency_ms"] = self._latest_ingestion["avg_freshness_ms"]
        self._latest_tick["cycle_latency_ms"] = int((time.perf_counter() - started) * 1000)
        self._latest_tick["stats"]["active_markets"] = len(self._markets_by_id)
        self._latest_tick["stats"]["detected_arbs_today"] = analytics_summary["opportunity_count"]
        self._latest_tick["stats"]["open_positions"] = self._count_open_positions()

        point = {
            "timestamp": now_dt.isoformat(),
            "portfolio": self._latest_tick["portfolio_total"] + self._latest_tick["stats"]["total_pnl"],
            "pnl_pct": self._calculate_pnl_pct(),
        }
        self._history.append(point)
        self._history = self._history[-1200:]
        self._signal_history.append(
            {
                "timestamp": now_dt.isoformat(),
                "opportunity_count": analytics_summary["opportunity_count"],
                "top_signal_score": analytics_summary["top_signal_score"],
                "avg_freshness_ms": analytics_summary["avg_freshness_ms"],
                "data_latency_ms": self._latest_ingestion["avg_source_delay_ms"],
            }
        )
        self._signal_history = self._signal_history[-1200:]

        self._latest_analytics_rows = analytics_rows_display
        self._latest_analytics_summary = analytics_summary
        self._latest_decisions = [item.to_snapshot() for item in decisions]
        self._latest_decision_summary = decision_summary

        await self.broadcast({"type": "tick", "payload": self.snapshot()})

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
                market.expected_edge,
                -market.freshness_ms,
            ),
            reverse=True,
        )
        return ranked[: self.strategy.cfg.display_market_limit]

    def _positions_snapshot(self) -> dict:
        open_positions = []
        for trade in self._trades:
            if trade["status"] != "OPEN":
                continue
            market = self._markets_by_id.get(trade["market_id"])
            open_positions.append(
                {
                    "trade_id": trade["id"],
                    "market_id": trade["market_id"],
                    "market_title": trade["market_title"],
                    "side": trade["side"],
                    "entry_price": trade["price"],
                    "size": trade["size"],
                    "notional": trade["notional"],
                    "unrealized_pnl_abs": trade.get("unrealized_pnl_abs", 0.0),
                    "unrealized_pnl_pct": trade.get("unrealized_pnl_pct", 0.0),
                    "decision_action": market.decision_action if market is not None else "HOLD",
                    "decision_summary": market.decision_summary if market is not None else "",
                }
            )
        equity = self._latest_tick["portfolio_total"] + self._latest_tick["stats"]["total_pnl"]
        exposure_pct = 0.0 if equity <= 0 else (self._latest_tick["capital_in_trade"] / equity) * 100
        return {
            "open_count": len(open_positions),
            "capital_in_trade": self._latest_tick["capital_in_trade"],
            "exposure_pct": round(exposure_pct, 3),
            "items": open_positions,
        }

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

    @staticmethod
    def _mid_or_zero(bid: float, ask: float) -> float:
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
            sum(trade["pnl_abs"] for trade in closed_trades) / len(closed_trades),
            4,
        )

    def _calculate_pnl_pct(self) -> float:
        base = self._latest_tick["portfolio_total"]
        if base <= 0:
            return 0.0
        return round((self._latest_tick["stats"]["total_pnl"] / base) * 100, 3)


live_hub = LiveHub(price_cache=PriceStateCache())
