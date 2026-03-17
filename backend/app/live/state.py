from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import WebSocket


@dataclass
class BotState:
    running: bool = True
    paused: bool = False
    started_at: float = field(default_factory=time.time)


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.8
    max_total_exposure_pct: float = 15.0
    kelly_fraction_multiplier: float = 0.65
    max_drawdown_auto_stop_pct: float = 8.0
    max_risk_per_trade_usdc: float = 500.0
    min_trade_size_usdc: float = 50.0


@dataclass
class RiskToggles:
    risk_managed_sizing: bool = True
    use_kelly_on_sum_positions: bool = True
    auto_close_on_resolution: bool = True
    pause_on_high_latency: bool = True


class LiveHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.bot_state = BotState()
        self.risk_config = RiskConfig()
        self.risk_toggles = RiskToggles()
        self.wallet_balance = 10_000.0
        self.current_total_exposure = 0.0
        self.current_drawdown_pct = 1.2
        self._high_latency_streak = 0
        self._open_positions_by_condition: dict[str, dict] = {}
        self._latest_tick: dict = self._seed_snapshot()
        self._history: deque[dict] = deque(maxlen=120)
        self._trades: deque[dict] = deque(maxlen=10)

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
        return {
            "bot": {
                "status": "PAUSED" if self.bot_state.paused else ("RUNNING" if self.bot_state.running else "STOPPED"),
                "uptime_seconds": uptime,
                "latency_ms": self._latest_tick["latency_ms"],
            },
            "stats": self._latest_tick["stats"],
            "markets": self._latest_tick["markets"],
            "price_history": list(self._history),
            "recent_simulations": list(self._trades),
            "risk": {
                "config": asdict(self.risk_config),
                "toggles": asdict(self.risk_toggles),
                "gauges": self._build_gauges(),
                "preview": self._latest_tick.get("preview", "No active opportunity"),
            },
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
        elif cmd == "close_all":
            self._open_positions_by_condition.clear()
            self.current_total_exposure = 0.0
        elif cmd == "emergency_stop":
            self.bot_state.running = False
            self.bot_state.paused = True
            self._open_positions_by_condition.clear()
            self.current_total_exposure = 0.0
        payload = self.snapshot()
        await self.broadcast({"type": "control", "payload": payload})
        return payload

    async def update_risk_config(self, payload: dict) -> dict:
        cfg = payload.get("config", {})
        toggles = payload.get("toggles", {})
        self.risk_config.risk_per_trade_pct = min(5.0, max(0.1, float(cfg.get("risk_per_trade_pct", self.risk_config.risk_per_trade_pct))))
        self.risk_config.max_total_exposure_pct = min(20.0, max(5.0, float(cfg.get("max_total_exposure_pct", self.risk_config.max_total_exposure_pct))))
        self.risk_config.kelly_fraction_multiplier = min(1.0, max(0.1, float(cfg.get("kelly_fraction_multiplier", self.risk_config.kelly_fraction_multiplier))))
        self.risk_config.max_drawdown_auto_stop_pct = min(20.0, max(3.0, float(cfg.get("max_drawdown_auto_stop_pct", self.risk_config.max_drawdown_auto_stop_pct))))
        self.risk_toggles.risk_managed_sizing = bool(toggles.get("risk_managed_sizing", self.risk_toggles.risk_managed_sizing))
        self.risk_toggles.use_kelly_on_sum_positions = bool(toggles.get("use_kelly_on_sum_positions", self.risk_toggles.use_kelly_on_sum_positions))
        self.risk_toggles.auto_close_on_resolution = bool(toggles.get("auto_close_on_resolution", self.risk_toggles.auto_close_on_resolution))
        self.risk_toggles.pause_on_high_latency = bool(toggles.get("pause_on_high_latency", self.risk_toggles.pause_on_high_latency))

        snapshot = self.snapshot()
        await self.broadcast({"type": "risk_config", "payload": snapshot})
        return snapshot

    async def simulate_execution(self, market_id: str) -> dict:
        sim = {
            "id": f"sim-{int(time.time()*1000)}",
            "market_id": market_id,
            "side": random.choice(["BUY_YES", "BUY_NO"]),
            "price": round(random.uniform(0.35, 0.68), 4),
            "size": random.randint(5, 30),
            "pnl": round(random.uniform(-1.2, 4.5), 2),
            "decision": "EXECUTED",
            "reason": "manual simulation",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._trades.appendleft(sim)
        await self.broadcast({"type": "simulation", "payload": sim})
        return sim

    async def tick(self) -> None:
        if not self.bot_state.running or self.bot_state.paused:
            return

        latency = max(18, min(220, int(random.gauss(65, 20))))
        self._latest_tick["latency_ms"] = latency
        if latency > 80:
            self._high_latency_streak += 1
        else:
            self._high_latency_streak = 0

        for market in self._latest_tick["markets"]:
            yes_mid_shift = random.uniform(-0.01, 0.01)
            yes_mid = float(max(0.05, min(0.95, market["yes_mid"] + yes_mid_shift)))
            no_mid = float(max(0.05, min(0.95, 1 - yes_mid + random.uniform(-0.01, 0.01))))
            market["best_bid_yes"] = round(max(0.01, yes_mid - random.uniform(0.001, 0.008)), 4)
            market["best_ask_yes"] = round(min(0.99, yes_mid + random.uniform(0.001, 0.008)), 4)
            market["best_bid_no"] = round(max(0.01, no_mid - random.uniform(0.001, 0.008)), 4)
            market["best_ask_no"] = round(min(0.99, no_mid + random.uniform(0.001, 0.008)), 4)
            market["bid_size"] = round(max(20, market["bid_size"] + random.uniform(-15, 25)), 2)
            market["ask_size"] = round(max(20, market["ask_size"] + random.uniform(-15, 25)), 2)
            market["average_size_24h"] = round(max(30, market["average_size_24h"] + random.uniform(-5, 5)), 2)
            market["yes_mid"] = yes_mid
            market["no_mid"] = no_mid

            decision = self._evaluate_market(market)
            market["detected"] = decision["decision"] == "EXECUTED"
            market["est_profit"] = round(max(0.0, decision["edge"] * 100), 2)
            market["spread"] = round(decision["spread"], 4)
            market["decision"] = decision["decision"]
            market["decision_reason"] = decision["reason"]
            self._latest_tick["preview"] = decision["preview"]
            if decision["decision"] == "EXECUTED":
                self._trades.appendleft(decision)

        detected = sum(1 for x in self._latest_tick["markets"] if x["detected"])
        pnl = round(self._latest_tick["stats"]["total_pnl"] + random.uniform(-1.5, 2.2), 2)
        self.current_drawdown_pct = max(0.0, min(25.0, self.current_drawdown_pct + random.uniform(-0.2, 0.4)))
        if self.current_drawdown_pct >= self.risk_config.max_drawdown_auto_stop_pct:
            self.bot_state.paused = True
            self._open_positions_by_condition.clear()
            self.current_total_exposure = 0.0
        if self.risk_toggles.pause_on_high_latency and self._high_latency_streak >= 10:
            self.bot_state.paused = True

        self._latest_tick["stats"].update(
            {
                "total_pnl": pnl,
                "win_rate": round(max(20, min(95, self._latest_tick["stats"]["win_rate"] + random.uniform(-0.4, 0.6))), 2),
                "avg_profit": round(max(0.1, min(12.0, self._latest_tick["stats"]["avg_profit"] + random.uniform(-0.2, 0.3))), 2),
                "active_markets": len(self._latest_tick["markets"]),
                "detected_arbs_today": self._latest_tick["stats"]["detected_arbs_today"] + detected,
            }
        )
        self._history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "value": self._latest_tick["markets"][0]["yes_mid"],
            }
        )
        await self.broadcast(
            {
                "type": "tick",
                "payload": {
                    "latency_ms": latency,
                    "stats": self._latest_tick["stats"],
                    "markets": self._latest_tick["markets"],
                    "price_point": self._history[-1],
                    "risk": {
                        "gauges": self._build_gauges(),
                        "preview": self._latest_tick.get("preview", "No active opportunity"),
                    },
                },
            }
        )

    def _evaluate_market(self, market: dict) -> dict:
        yes_price = market["best_ask_yes"]
        no_price = market["best_ask_no"]
        edge = 1 - (yes_price + no_price)
        mid_price = max((market["best_ask_yes"] + market["best_bid_yes"]) / 2, 0.01)
        spread = (market["best_ask_yes"] - market["best_bid_yes"]) / mid_price
        depth_score = min(market["bid_size"], market["ask_size"]) / max(market["average_size_24h"], 1)

        implied_prob = yes_price
        prob_true = market["estimated_true_prob_yes"]
        kelly_edge = prob_true - implied_prob
        odds = implied_prob / max(1 - implied_prob, 0.0001)
        kelly_fraction = max(0.0, kelly_edge / max(odds, 0.0001))
        kelly_fraction = min(kelly_fraction, 0.65) * self.risk_config.kelly_fraction_multiplier

        risk_multiplier = self.risk_config.risk_per_trade_pct / 100 if self.risk_toggles.risk_managed_sizing else 1.0
        size_usdc = self.wallet_balance * kelly_fraction * risk_multiplier
        size_usdc = min(size_usdc, self.risk_config.max_risk_per_trade_usdc)

        max_exposure = self.wallet_balance * (self.risk_config.max_total_exposure_pct / 100)
        new_exposure = self.current_total_exposure + size_usdc
        expected_latency = self._latest_tick["latency_ms"] + 8
        expected_slippage = spread * 0.3
        risk_score = (new_exposure / max(max_exposure, 1)) * (spread / 0.01) * (1 - min(depth_score, 1))

        condition_id = market["condition_id"]
        decision = "EXECUTED"
        reason = "all rules satisfied"
        checks = [
            (edge >= 0.005, "edge < 0.5%"),
            (risk_score <= 1.0, "risk_score > 1.0"),
            (new_exposure <= max_exposure, "max_total_exposure exceeded"),
            (size_usdc >= self.risk_config.min_trade_size_usdc, "size below min_trade_size"),
            (spread <= 0.015, "spread > 1.5%"),
            (depth_score >= 0.4, "depth_score < 0.4"),
            (self.bot_state.running and not self.bot_state.paused, "bot not RUNNING"),
            (condition_id not in self._open_positions_by_condition, "position already open on condition_id"),
            (kelly_fraction >= 0.15, "kelly_fraction < 0.15"),
            (self.current_total_exposure <= self.wallet_balance * 0.18, "total exposure > 18%"),
            (expected_latency <= 80 or not self.risk_toggles.pause_on_high_latency, "latency guardrail"),
        ]
        for ok, fail_reason in checks:
            if not ok:
                decision = "REJECTED"
                reason = fail_reason
                break

        if decision == "EXECUTED":
            self.current_total_exposure = new_exposure
            self._open_positions_by_condition[condition_id] = {
                "size_usdc": size_usdc,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }

        preview = f"Si je prends cet arb → new total exposure = {(new_exposure / self.wallet_balance) * 100:.1f}%"
        preview += " (safe)" if new_exposure <= max_exposure else " (DANGER)"

        return {
            "id": f"sim-{int(time.time()*1000)}",
            "market_id": market["market_id"],
            "side": "BUY_YES+NO",
            "price": round(yes_price, 4),
            "size": round(size_usdc, 2),
            "pnl": round((edge - expected_slippage) * size_usdc, 2),
            "decision": decision,
            "reason": reason,
            "edge": edge,
            "spread": spread,
            "risk_score": risk_score,
            "expected_latency": expected_latency,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "preview": preview,
        }

    def _build_gauges(self) -> dict:
        exposure_pct = (self.current_total_exposure / self.wallet_balance) * 100
        risk_taken_pct = exposure_pct * 0.12
        return {
            "total_portfolio_exposure_pct": round(exposure_pct, 2),
            "total_risk_taken_pct": round(risk_taken_pct, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
        }

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            targets = list(self._connections)
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(ws)

    @staticmethod
    def _seed_snapshot() -> dict:
        base_markets = [
            {
                "market_id": f"MKT-{idx+1}",
                "condition_id": f"COND-{idx+1}",
                "title": f"Polymarket Signal #{idx+1}",
                "best_bid_yes": 0.48,
                "best_ask_yes": 0.50,
                "best_bid_no": 0.48,
                "best_ask_no": 0.50,
                "yes_mid": 0.5,
                "no_mid": 0.5,
                "bid_size": 180.0,
                "ask_size": 175.0,
                "average_size_24h": 220.0,
                "estimated_true_prob_yes": 0.57,
                "spread": 0.02,
                "est_profit": 0.0,
                "detected": False,
                "decision": "REJECTED",
                "decision_reason": "pending",
            }
            for idx in range(6)
        ]
        return {
            "latency_ms": 52,
            "stats": {
                "total_pnl": 125.45,
                "win_rate": 62.2,
                "avg_profit": 2.7,
                "active_markets": 6,
                "detected_arbs_today": 3,
            },
            "markets": base_markets,
            "preview": "No active opportunity",
        }


live_hub = LiveHub()
