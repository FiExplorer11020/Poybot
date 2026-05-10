"""
DecisionRouter — routes a Decision to the paper trader, the live trader,
both, or neither (S2.7).

Why a router (and not just the existing `decisions` channel):
  * Paper-only and live-only deployments need a single switch without
    redeploying code or rewiring channels.
  * In dual mode (paper running alongside live as a "shadow benchmark"),
    we want the live trader to see only a subset of decisions — the
    high-confidence ones, the larger sizes, the markets in our
    allowlist. Paper still sees everything so we can compare paper
    fills vs live fills on the same signals later.
  * The "current mode" must be flippable at runtime (Telegram cmd, API
    endpoint, or just a manual `redis-cli SET`) — not by editing env
    and restarting. So the router reads a Redis key on every routing
    decision, with the env var as a fallback.

The router is called in-memory by ConfidenceEngine right after a
Decision is created. It performs the routing logic and publishes onto
the appropriate Redis channel(s) — `decisions` for paper, `decisions:live`
for live. It does NOT subscribe to any channel itself; it's a one-shot
function call per decision.

Failure modes:
  * Redis read of the override key fails -> log warning, fall back to env
    TRADING_MODE.
  * Redis publish fails on one channel -> log warning, continue (best-effort,
    same as the previous _emit() in ConfidenceEngine).
  * Decision.action == 'skip' -> nothing routed. We never publish skip
    decisions; they only matter for telemetry, which is logged elsewhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

from loguru import logger

from src.config import settings

if TYPE_CHECKING:
    from src.engine.confidence_engine import Decision


REDIS_DECISIONS_PAPER_CHANNEL = "decisions"
REDIS_DECISIONS_LIVE_CHANNEL = "decisions:live"


class TradingMode(str, Enum):
    """Master mode for the bot — controls whether decisions reach the
    paper trader, the live trader, or both."""
    PAPER = "paper"
    LIVE = "live"
    DUAL = "dual"

    @classmethod
    def parse(cls, raw: Optional[str]) -> Optional["TradingMode"]:
        """Lenient parser: returns None on garbage input, never raises."""
        if not raw:
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


@dataclass
class RoutingResult:
    """Outcome of a single routing decision. Returned for observability
    and tests; the router still publishes itself."""
    routed_to_paper: bool
    routed_to_live: bool
    mode: TradingMode
    skipped_reason: Optional[str] = None  # set when the decision wasn't routed anywhere


class DecisionRouter:
    """In-memory router. Stateless across calls — each `route` is independent."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    # ------------------------------------------------------------------ #
    # Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def route(self, decision: "Decision") -> RoutingResult:
        """Route a decision to the appropriate channel(s).

        Returns a RoutingResult. The router publishes itself; the caller
        does NOT need to call `_emit` separately — this method replaces
        ConfidenceEngine._emit() completely.
        """
        # 'skip' decisions never get published anywhere. They're recorded
        # in `decisions_log` upstream for telemetry, but no trader consumes
        # them.
        if decision.action == "skip":
            return RoutingResult(
                routed_to_paper=False, routed_to_live=False,
                mode=await self._active_mode(),
                skipped_reason="action_is_skip",
            )

        mode = await self._active_mode()
        payload = self._build_payload(decision)
        encoded = json.dumps(payload)

        routed_paper = False
        routed_live = False

        if mode in (TradingMode.PAPER, TradingMode.DUAL):
            routed_paper = await self._publish(REDIS_DECISIONS_PAPER_CHANNEL, encoded)

        if mode in (TradingMode.LIVE, TradingMode.DUAL):
            if self._passes_live_filter(decision):
                routed_live = await self._publish(REDIS_DECISIONS_LIVE_CHANNEL, encoded)
            else:
                logger.info(
                    f"DecisionRouter: live filter rejected "
                    f"market={decision.market_id[:14]}… "
                    f"action={decision.action} confidence={decision.confidence:.3f} "
                    f"size={decision.size_usdc}$"
                )

        skipped = None
        if not routed_paper and not routed_live:
            skipped = "no_channel_matched"
        return RoutingResult(
            routed_to_paper=routed_paper,
            routed_to_live=routed_live,
            mode=mode,
            skipped_reason=skipped,
        )

    # ------------------------------------------------------------------ #
    # Mode resolution: env default + Redis override                       #
    # ------------------------------------------------------------------ #

    async def _active_mode(self) -> TradingMode:
        """Resolve the effective trading mode. Order:
          1. Redis key (if set + parses as a valid mode) — runtime override
          2. Settings.TRADING_MODE — boot-time default
          3. PAPER — last-resort safe fallback
        """
        # Try Redis override.
        try:
            raw = await self._redis.get(settings.TRADING_MODE_OVERRIDE_KEY)
            if isinstance(raw, bytes):
                raw = raw.decode()
            override = TradingMode.parse(raw)
            if override is not None:
                return override
        except Exception as e:
            logger.warning(
                f"DecisionRouter: Redis override read failed, "
                f"falling back to env TRADING_MODE: {e}"
            )

        env_mode = TradingMode.parse(settings.TRADING_MODE)
        if env_mode is not None:
            return env_mode
        # Misconfigured env: be safe.
        logger.warning(
            f"DecisionRouter: invalid TRADING_MODE={settings.TRADING_MODE!r}, "
            f"defaulting to PAPER"
        )
        return TradingMode.PAPER

    # ------------------------------------------------------------------ #
    # Live filter — applied ONLY when sending to live                     #
    # ------------------------------------------------------------------ #

    def _passes_live_filter(self, decision: "Decision") -> bool:
        """Decide whether a decision is eligible for the live channel.
        Paper has no filter — every non-skip decision goes to paper in
        paper/dual modes."""
        if decision.confidence < settings.LIVE_FILTER_CONFIDENCE_MIN:
            return False
        if decision.size_usdc < settings.LIVE_FILTER_SIZE_MIN_USDC:
            return False
        allowlist = self._parsed_allowlist()
        if allowlist and decision.market_id not in allowlist:
            return False
        return True

    @staticmethod
    def _parsed_allowlist() -> set[str]:
        raw = settings.LIVE_MARKET_ALLOWLIST or ""
        return {m.strip() for m in raw.split(",") if m.strip()}

    # ------------------------------------------------------------------ #
    # Payload + publish                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_payload(decision: "Decision") -> dict:
        """Build the JSON payload — kept identical to the legacy
        ConfidenceEngine._emit() so PaperTrader / LiveTrader don't need
        to change their parsing."""
        ctx = decision.trade_context or {}
        return {
            "action": decision.action,
            "leader_wallet": decision.leader_wallet,
            "market_id": decision.market_id,
            "market_question": ctx.get("market_question"),
            "market_category": ctx.get("market_category"),
            "market_type": ctx.get("market_type"),
            "token_id": decision.token_id,
            "size_usdc": decision.size_usdc,
            "kelly_fraction": decision.kelly_fraction,
            "confidence": decision.confidence,
            "thompson_follow": decision.thompson_follow,
            "thompson_fade": decision.thompson_fade,
            "reason": decision.reason,
            "wallet_type": ctx.get("wallet_type"),
            "wallet_strategy": ctx.get("wallet_strategy"),
            "wallet_horizon": ctx.get("wallet_horizon"),
            "wallet_influence": ctx.get("wallet_influence"),
            "trade_context": ctx,
            "context_penalty": decision.context_penalty,
            "strategy_track": decision.strategy_track,
            "economic_model_version": decision.economic_model_version,
            "signal_audit": decision.signal_audit or {},
        }

    async def _publish(self, channel: str, payload: str) -> bool:
        """Best-effort publish. Returns True if Redis acknowledged the
        publish, False on any error (we don't want a Redis hiccup to
        break the upstream ConfidenceEngine loop)."""
        try:
            await self._redis.publish(channel, payload)
            return True
        except Exception as e:
            logger.warning(f"DecisionRouter: publish to {channel} failed: {e}")
            return False
