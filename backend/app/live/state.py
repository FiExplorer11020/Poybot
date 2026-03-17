from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import WebSocket

from app.services.adaptive_strategy import AdaptiveStrategyEngine, PortfolioState, RiskConfig
from app.services.trade_executor import ExecutionRequest, TradeExecutor


@dataclass
class BotState:
    running: bool = True
    paused: bool = False
    started_at: float = field(default_factory=time.time)


class LiveHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.bot_state = BotState()
        self._latest_tick: dict = self._seed_snapshot()
        self._history: deque[dict] = deque(maxlen=1200)
        self._trades: deque[dict] = deque(maxlen=300)
        self.strategy = AdaptiveStrategyEngine(RiskConfig())
        self.executor = TradeExecutor()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        await ws.send_json({"type": "bootstrap", "payload": self.snapshot()})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    def snapshot(self) -> dict:
        now = time.time()
        uptime = int(now - self.bot_state.started_at)
        pnl_pct = self._calculate_pnl_pct()
        return {
            "bot": {
                "status": "PAUSED" if self.bot_state.paused else ("RUNNING" if self.bot_state.running else "STOPPED"),
                "uptime_seconds": uptime,
                "latency_ms": self._latest_tick["latency_ms"],
            },
            "risk_config": {
                "risk_per_trade_pct": self.strategy.cfg.risk_per_trade_pct,
                "max_total_exposure_pct": self.strategy.cfg.max_total_exposure_pct,
                "kelly_fraction": self.strategy.cfg.kelly_fraction,
                "max_drawdown_stop_pct": self.strategy.cfg.max_drawdown_stop_pct,
                "fee_bps": self.strategy.cfg.fee_bps,
            },
            "stats": {
                **self._latest_tick["stats"],
                "portfolio_total": self._latest_tick["portfolio_total"],
                "capital_in_trade": self._latest_tick["capital_in_trade"],
                "pnl_percent": pnl_pct,
            },
            "markets": self._latest_tick["markets"],
            "price_history": list(self._history),
            "recent_trades": list(self._trades),
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

    async def execute_trade(self, market_id: str, market_title: str) -> dict:
        market = next((x for x in self._latest_tick["markets"] if x["market_id"] == market_id), None)
        if not market:
            raise ValueError("market not found")

        portfolio = PortfolioState(
            equity=float(self._latest_tick["portfolio_total"]),
            capital_in_trade=float(self._latest_tick["capital_in_trade"]),
            total_pnl=float(self._latest_tick["stats"]["total_pnl"]),
        )
        notional, risk_pct = self.strategy.size_position(portfolio, expected_edge=float(market["expected_edge"]))
        if notional <= 0:
            raise ValueError("risk limits block new trade")

        side = market["direction"]
        price = market["best_ask"]
        size = round(notional / max(price, 0.01), 4)
        outcome = self.strategy.estimate_trade_outcome(
            notional=notional,
            spread=float(market["spread"]),
            volatility=float(market["volatility"]),
            expected_edge=float(market["expected_edge"]),
        )
        execution = await self.executor.execute(
            ExecutionRequest(
                market_id=market_id,
                market_title=market_title,
                token_id=market["token_id_yes"] if side == "BUY_YES" else market["token_id_no"],
                side=side,
                price=price,
                size=size,
                notional=notional,
                risk_pct=risk_pct,
                expected_edge=float(market["expected_edge"]),
            )
        )

        trade = {
            "id": f"ord-{int(time.time()*1000)}",
            "order_id": execution["order_id"],
            "tx_hash": execution["tx_hash"],
            "execution_mode": execution["execution_mode"],
            "exchange_status": execution["exchange_status"],
            "market_id": market_id,
            "market_title": market_title,
            "token_id": market["token_id_yes"] if side == "BUY_YES" else market["token_id_no"],
            "side": side,
            "price": round(price, 4),
            "size": size,
            "notional": notional,
            "risk_pct": risk_pct,
            "kelly": round(self.strategy.cfg.kelly_fraction, 3),
            "slippage": outcome["slippage"],
            "fees": outcome["fees"],
            "pnl_abs": outcome["pnl_abs"],
            "pnl_pct": outcome["pnl_pct"],
            "status": "FILLED" if outcome["pnl_abs"] >= -notional * 0.05 else "RISK_REJECT",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._trades.appendleft(trade)
        self._latest_tick["stats"]["total_pnl"] = round(self._latest_tick["stats"]["total_pnl"] + outcome["pnl_abs"], 4)
        self._latest_tick["capital_in_trade"] = round(self._latest_tick["capital_in_trade"] + notional, 2)
        await self.broadcast({"type": "trade", "payload": trade})
        return trade

    async def tick(self) -> None:
        if not self.bot_state.running or self.bot_state.paused:
            return
        latency = max(15, min(190, int(random.gauss(58, 18))))

        for market in self._latest_tick["markets"]:
            mid = market["mid_price"] + random.uniform(-0.018, 0.018)
            bid = max(0.01, min(0.99, mid - random.uniform(0.004, 0.015)))
            ask = max(bid, min(0.99, mid + random.uniform(0.004, 0.015)))
            eval_out = self.strategy.evaluate_market(market["market_id"], bid, ask)
            market.update(eval_out)
            market["est_profit"] = round(max(0.0, eval_out["expected_edge"] * 100), 3)

        detected = sum(1 for x in self._latest_tick["markets"] if x["detected"])
        self._latest_tick["latency_ms"] = latency
        self._latest_tick["stats"].update(
            {
                "win_rate": round(max(20, min(95, self._latest_tick["stats"]["win_rate"] + random.uniform(-0.2, 0.3))), 2),
                "avg_profit": round(max(0.1, min(15.0, self._latest_tick["stats"]["avg_profit"] + random.uniform(-0.1, 0.15))), 2),
                "active_markets": len(self._latest_tick["markets"]),
                "detected_arbs_today": self._latest_tick["stats"]["detected_arbs_today"] + max(0, detected - 2),
            }
        )

        point = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio": self._latest_tick["portfolio_total"] + self._latest_tick["stats"]["total_pnl"],
            "pnl_pct": self._calculate_pnl_pct(),
        }
        self._history.append(point)

        await self.broadcast(
            {
                "type": "tick",
                "payload": {
                    "latency_ms": latency,
                    "stats": self.snapshot()["stats"],
                    "markets": self._latest_tick["markets"],
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
        n = counts.get(timeframe, 336)
        return list(self._history)[-n:]

    def _calculate_pnl_pct(self) -> float:
        base = self._latest_tick["portfolio_total"]
        if base <= 0:
            return 0.0
        return round((self._latest_tick["stats"]["total_pnl"] / base) * 100, 3)

    @staticmethod
    def _seed_snapshot() -> dict:
        markets = [
            {
                "market_id": f"MKT-{idx+1}",
                "title": title,
                "token_id_yes": f"TOKEN-YES-{idx+1}",
                "token_id_no": f"TOKEN-NO-{idx+1}",
                "best_bid": round(0.42 + idx * 0.05, 4),
                "best_ask": round(0.45 + idx * 0.05, 4),
                "mid_price": round(0.435 + idx * 0.05, 4),
                "spread": 0.03,
                "volatility": 0.002,
                "liquidity_score": 0.6,
                "expected_edge": 0.01,
                "entry_threshold": 0.007,
                "direction": "BUY_YES",
                "est_profit": 1.0,
                "detected": False,
            }
            for idx, title in enumerate(
                [
                    "US Election 2028: Democrat wins?",
                    "BTC above 100k by year-end?",
                    "Fed cuts rates before Q4?",
                    "ETH ETF net inflow positive this week?",
                    "US recession announced in 2026?",
                    "Trump vs Biden rematch confirmed?",
                ]
            )
        ]
        return {
            "latency_ms": 64,
            "portfolio_total": 25000.00,
            "capital_in_trade": 6200.00,
            "stats": {
                "total_pnl": 845.25,
                "win_rate": 62.4,
                "avg_profit": 2.9,
                "active_markets": len(markets),
                "detected_arbs_today": 4,
            },
            "markets": markets,
        }


live_hub = LiveHub()
