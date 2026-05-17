"""
Configurable alert rules (S3.11).

Persisted in Redis under TELEGRAM_ALERTS_REDIS_KEY. Schema: JSON list of
AlertRule dicts. Surfaced via /alert list|add|remove commands, evaluated
once per minute by the scheduler.

Rule types currently supported:
  * drawdown        — fire when portfolio drawdown >= threshold (e.g. 0.05)
  * daily_loss      — fire when today's net pnl <= -threshold ($)
  * win_rate_below  — fire when rolling 24h win rate < threshold (0-1)
  * idle_minutes    — fire when no paper trades opened for N minutes

The evaluator is intentionally stateless beyond a per-rule "last fired
at" timestamp stored in the rule itself (refresh cooldown ALERT_COOLDOWN_S
prevents spam — 30 min between repeated fires of the same rule).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from src.config import settings


# Min seconds between consecutive fires of the same rule. Without it, a
# rule that's "currently true" (e.g. drawdown stays above threshold)
# would fire every evaluation tick.
ALERT_COOLDOWN_S = 1800  # 30 min

SUPPORTED_TYPES = ("drawdown", "daily_loss", "win_rate_below", "idle_minutes")


@dataclass
class AlertRule:
    id: str
    rule_type: str
    threshold: float
    created_at: float = field(default_factory=time.time)
    last_fired_at: float = 0.0

    @property
    def channel(self) -> str:
        # Synthetic channel for /alert list rendering — these rules don't
        # consume a Redis channel, they reduce DB+state into a fire event.
        return f"alerts:{self.rule_type}"

    @property
    def condition(self) -> str:
        labels = {
            "drawdown": ">=",
            "daily_loss": "<= -",
            "win_rate_below": "<",
            "idle_minutes": ">=",
        }
        return labels.get(self.rule_type, "?")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rule_type": self.rule_type,
            "threshold": self.threshold,
            "created_at": self.created_at,
            "last_fired_at": self.last_fired_at,
            # Computed fields for /alert list rendering:
            "channel": self.channel,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AlertRule":
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:8]),
            rule_type=str(d.get("rule_type", "?")),
            threshold=float(d.get("threshold", 0.0)),
            created_at=float(d.get("created_at") or time.time()),
            last_fired_at=float(d.get("last_fired_at") or 0.0),
        )


class AlertsManager:
    """In-memory cache + Redis persistence for AlertRule[]."""

    def __init__(self, *, redis_client) -> None:
        self._redis = redis_client
        self._key = settings.TELEGRAM_ALERTS_REDIS_KEY
        self._rules: list[AlertRule] = []
        self._loaded = False

    async def load(self) -> None:
        """Pull from Redis. Tolerates a missing key (empty rule list)."""
        if self._redis is None:
            self._rules = []
            self._loaded = True
            return
        try:
            raw = await self._redis.get(self._key)
        except Exception as e:
            logger.warning(f"AlertsManager.load: {e}")
            raw = None
        if not raw:
            self._rules = []
            self._loaded = True
            return
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            data = json.loads(raw)
            self._rules = [AlertRule.from_dict(d) for d in data if isinstance(d, dict)]
        except Exception as e:
            logger.warning(f"AlertsManager: corrupt rules JSON, resetting: {e}")
            self._rules = []
        self._loaded = True

    async def _persist(self) -> None:
        if self._redis is None:
            return
        try:
            data = [r.to_dict() for r in self._rules]
            await self._redis.set(self._key, json.dumps(data))
        except Exception as e:
            logger.warning(f"AlertsManager._persist: {e}")

    async def list_rules(self) -> list[dict]:
        if not self._loaded:
            await self.load()
        return [r.to_dict() for r in self._rules]

    async def add_rule(self, *, rule_type: str, threshold: float) -> AlertRule:
        if not self._loaded:
            await self.load()
        rule_type = rule_type.strip().lower()
        if rule_type not in SUPPORTED_TYPES:
            raise ValueError(
                f"unknown rule type {rule_type!r}; supported: {SUPPORTED_TYPES}"
            )
        rule = AlertRule(id=uuid.uuid4().hex[:8], rule_type=rule_type, threshold=float(threshold))
        self._rules.append(rule)
        await self._persist()
        return rule

    async def remove_rule(self, rule_id: str) -> bool:
        if not self._loaded:
            await self.load()
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        if len(self._rules) == before:
            return False
        await self._persist()
        return True

    # ------------------------------------------------------------------ #
    # Evaluation (called once per scheduler tick, ~60s)                   #
    # ------------------------------------------------------------------ #

    async def evaluate(self, *, paper_trader=None) -> list[tuple[AlertRule, str]]:
        """Return a list of (rule, message) pairs to fire this tick.

        Stateless beyond the per-rule cooldown so the caller can render
        with whatever formatter and route through the notifier.
        """
        if not self._loaded:
            await self.load()
        if not self._rules:
            return []

        fired: list[tuple[AlertRule, str]] = []
        now = time.time()
        state = await self._snapshot_state(paper_trader=paper_trader)

        for rule in self._rules:
            if now - rule.last_fired_at < ALERT_COOLDOWN_S:
                continue
            msg = self._evaluate_one(rule, state)
            if msg is None:
                continue
            rule.last_fired_at = now
            fired.append((rule, msg))

        if fired:
            # Persist updated last_fired_at timestamps.
            await self._persist()
        return fired

    async def _snapshot_state(self, *, paper_trader=None) -> dict:
        """Snapshot the metrics the rules need. One DB hit per evaluation tick."""
        from src.database.connection import get_db
        from src.engine.portfolio_state import load_state

        state: dict = {
            "drawdown_pct": 0.0,
            "daily_pnl": 0.0,
            "win_rate_24h": None,
            "idle_minutes": None,
        }
        try:
            ps = await load_state()
            peak = float(ps.peak_capital or settings.PAPER_CAPITAL_USDC)
            current = float(ps.capital)
            if peak > 0:
                state["drawdown_pct"] = max(0.0, (peak - current) / peak)
        except Exception as e:
            logger.debug(f"alerts._snapshot_state: portfolio: {e}")

        try:
            async with get_db() as conn:
                pnl = await conn.fetchval(
                    "SELECT COALESCE(SUM(pnl_usdc), 0) FROM paper_trades "
                    "WHERE status = 'closed' "
                    "  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                )
                state["daily_pnl"] = float(pnl or 0.0)

                wr = await conn.fetchrow(
                    "SELECT COUNT(*) FILTER (WHERE pnl_usdc > 0) AS wins, "
                    "       COUNT(*) AS total "
                    "FROM paper_trades "
                    "WHERE status = 'closed' "
                    "  AND closed_at >= NOW() - INTERVAL '24 hours'"
                )
                if wr and int(wr["total"] or 0) >= 5:
                    # Only meaningful with a minimum sample.
                    state["win_rate_24h"] = float(wr["wins"]) / float(wr["total"])

                idle = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(opened_at)))/60.0 "
                    "FROM paper_trades"
                )
                if idle is not None:
                    state["idle_minutes"] = float(idle)
        except Exception as e:
            logger.debug(f"alerts._snapshot_state: db: {e}")

        return state

    @staticmethod
    def _evaluate_one(rule: AlertRule, state: dict) -> Optional[str]:
        """Return a human-readable message if the rule should fire; else None."""
        rt = rule.rule_type
        thr = rule.threshold

        if rt == "drawdown":
            dd = state.get("drawdown_pct") or 0.0
            if dd >= thr:
                return (
                    f"🔔 ALERT (rule {rule.id}) — drawdown {dd:.1%} "
                    f">= threshold {thr:.1%}"
                )
            return None

        if rt == "daily_loss":
            pnl = state.get("daily_pnl") or 0.0
            if pnl <= -thr:
                return (
                    f"🔔 ALERT (rule {rule.id}) — today's pnl ${pnl:.2f} "
                    f"≤ -${thr:.2f}"
                )
            return None

        if rt == "win_rate_below":
            wr = state.get("win_rate_24h")
            if wr is not None and wr < thr:
                return (
                    f"🔔 ALERT (rule {rule.id}) — 24h win rate {wr:.1%} "
                    f"< threshold {thr:.1%}"
                )
            return None

        if rt == "idle_minutes":
            idle = state.get("idle_minutes")
            if idle is not None and idle >= thr:
                return (
                    f"🔔 ALERT (rule {rule.id}) — no trade opened for "
                    f"{idle:.0f} min (≥ {thr:.0f} min threshold)"
                )
            return None

        return None
