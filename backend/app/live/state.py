from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import WebSocket


@dataclass
class BotState:
    running: bool = True
    paused: bool = False
    started_at: float = time.time()


class LiveHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.bot_state = BotState()
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
        payload = self.snapshot()
        await self.broadcast({"type": "control", "payload": payload})
        return payload

    async def simulate_execution(self, market_id: str) -> dict:
        sim = {
            "id": f"sim-{int(time.time()*1000)}",
            "market_id": market_id,
            "side": random.choice(["BUY_YES", "BUY_NO"]),
            "price": round(random.uniform(0.35, 0.68), 4),
            "size": random.randint(5, 30),
            "pnl": round(random.uniform(-1.2, 4.5), 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._trades.appendleft(sim)
        await self.broadcast({"type": "simulation", "payload": sim})
        return sim

    async def tick(self) -> None:
        if not self.bot_state.running or self.bot_state.paused:
            return
        latency = max(18, min(220, int(random.gauss(65, 20))))
        for market in self._latest_tick["markets"]:
            shift = random.uniform(-0.02, 0.02)
            market["mid_price"] = float(max(0.02, min(0.98, market["mid_price"] + shift)))
            market["best_bid"] = round(max(0.01, market["mid_price"] - random.uniform(0.005, 0.02)), 4)
            market["best_ask"] = round(min(0.99, market["mid_price"] + random.uniform(0.005, 0.02)), 4)
            market["spread"] = round(market["best_ask"] - market["best_bid"], 4)
            market["est_profit"] = round(max(0, (0.03 - market["spread"])) * 100, 2)
            market["detected"] = market["spread"] < 0.03

        detected = sum(1 for x in self._latest_tick["markets"] if x["detected"])
        pnl = round(self._latest_tick["stats"]["total_pnl"] + random.uniform(-1.5, 2.2), 2)
        self._latest_tick["latency_ms"] = latency
        self._latest_tick["stats"].update(
            {
                "total_pnl": pnl,
                "win_rate": round(max(20, min(95, self._latest_tick["stats"]["win_rate"] + random.uniform(-0.4, 0.6))), 2),
                "avg_profit": round(max(0.1, min(12.0, self._latest_tick["stats"]["avg_profit"] + random.uniform(-0.2, 0.3))), 2),
                "active_markets": len(self._latest_tick["markets"]),
                "detected_arbs_today": self._latest_tick["stats"]["detected_arbs_today"] + max(0, detected - 1),
            }
        )
        self._history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "value": self._latest_tick["markets"][0]["mid_price"],
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

    @staticmethod
    def _seed_snapshot() -> dict:
        base_markets = [
            {"market_id": f"MKT-{idx+1}", "title": f"Polymarket Signal #{idx+1}", "mid_price": float(Decimal("0.5") + Decimal(str((idx-2) * 0.03))), "best_bid": 0.48, "best_ask": 0.52, "spread": 0.04, "est_profit": 0.0, "detected": False}
            for idx in range(6)
        ]
        return {
            "latency_ms": 92,
            "stats": {
                "total_pnl": 125.45,
                "win_rate": 62.2,
                "avg_profit": 2.7,
                "active_markets": 6,
                "detected_arbs_today": 3,
            },
            "markets": base_markets,
        }


live_hub = LiveHub()
