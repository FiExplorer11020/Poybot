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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from src.config import settings
from src.events.schemas import DecisionMade

if TYPE_CHECKING:
    from src.engine.confidence_engine import Decision


REDIS_DECISIONS_PAPER_CHANNEL = "decisions"
REDIS_DECISIONS_LIVE_CHANNEL = "decisions:live"

# Round 9 (The Web) — entry-policy reason tag. The volume_anticipation
# decision still routes to the same paper/live channels, but the
# action='volume_anticipation' marker lets PaperTrader/LiveTrader and
# the dashboard tell it apart from FOLLOW/FADE.
VOLUME_ANTICIPATION_ACTION = "volume_anticipation"


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
    """In-memory router. Stateless across calls — each `route` is independent.

    Round 9 (The Web) optional extension: when constructed with a
    ``volume_predictor`` + ``drift_detector`` pair, the router can emit
    an additional ``volume_anticipation`` decision on every leader
    trade. This path is **gated by the runtime config flag**
    ``volume_anticipation_enabled`` (default False). When the flag is
    off, the router's behavior is byte-identical to pre-R9.

    See ``maybe_emit_volume_anticipation`` for the policy entry point.
    """

    def __init__(
        self,
        redis_client,
        volume_predictor: Any = None,
        drift_detector: Any = None,
        runtime_config: Any = None,
    ) -> None:
        self._redis = redis_client
        # Round 9 deps — all optional, all defaulting to None for
        # backward compat with existing call sites.
        self._volume_predictor = volume_predictor
        self._drift_detector = drift_detector
        self._runtime_config = runtime_config

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
        # Validate the payload through the typed schema. The model keeps
        # the legacy lower-case action vocabulary AND the new core fields
        # (time, decision_id). On drift the build raises and we fall back
        # to the raw dict — better to ship a slightly-malformed event
        # than to silence a decision.
        try:
            event_model = DecisionMade.model_validate(payload)
            encoded = event_model.model_dump_json()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                f"DecisionRouter: schema validation failed, "
                f"falling back to raw payload: {exc}"
            )
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
        to change their parsing.

        Audit 2026-05-17 (QW1): the legacy payload omitted ``side`` and
        ``price`` at the top level. The downstream paper_trader gates
        ``leader_sell_side`` (open_trade ~L678) and ``leader_price_drift``
        (~L953) read ``decision.get("side")`` and ``decision.get("price")``
        respectively. Without these fields propagated they always returned
        None, leaving both gates inert in production. Sourced from
        ``trade_context`` (which the upstream
        ConfidenceEngine._build_trade_context now stamps with
        ``side`` and ``price``).
        """
        ctx = decision.trade_context or {}
        # QW1: surface leader side + signal price at the top level so the
        # paper_trader gates can read them without spelunking into
        # ``trade_context``. Accept multiple legacy key spellings to keep
        # backward compat with any caller that already passed them in.
        side = ctx.get("side") or ctx.get("trade_side") or ctx.get("leader_side")
        price = (
            ctx.get("market_price")
            if ctx.get("market_price") is not None
            else ctx.get("price") or ctx.get("trade_price") or ctx.get("leader_price")
        )
        # Canonical core fields required by src/events/schemas.py
        # (time + decision_id). `decision_id` is a deterministic-ish UUID
        # so consumers can dedup retransmissions; `time` is ISO-formatted
        # so it survives the json.dumps → json.loads round-trip without
        # needing custom encoders on the consumer side. The legacy keys
        # below stay verbatim — PaperTrader/LiveTrader gates depend on
        # `kelly_fraction`, lower-case `action`, etc.
        return {
            "time": datetime.now(tz=timezone.utc).isoformat(),
            "decision_id": str(uuid.uuid4()),
            "action": decision.action,
            "leader_wallet": decision.leader_wallet,
            "market_id": decision.market_id,
            "market_question": ctx.get("market_question"),
            "market_category": ctx.get("market_category"),
            "market_type": ctx.get("market_type"),
            "token_id": decision.token_id,
            # QW1 — top-level side/price required by paper_trader gates.
            "side": side,
            "price": price,
            "size_usdc": decision.size_usdc,
            "kelly": decision.kelly_fraction,
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

    # ------------------------------------------------------------------ #
    # Round 9 (The Web) — volume_anticipation policy                      #
    # ------------------------------------------------------------------ #

    async def maybe_emit_volume_anticipation(
        self,
        signal_decision: "Decision",
        current_capital: float,
        market_depth_usdc: float = 0.0,
    ) -> Optional["RoutingResult"]:
        """Decide whether to fire a volume_anticipation entry.

        Per spec § 3.4: complementary to FOLLOW. Both can fire on the
        same leader trade — the bot has two kinds of edge
        (leader-correctness, follower-flow).

        Args:
            signal_decision: the upstream Decision (used for leader,
                market, token context).
            current_capital: the bot's current bankroll in USDC.
            market_depth_usdc: optional depth signal; if zero we use a
                conservative fallback.

        Returns:
            RoutingResult if a volume_anticipation entry was published,
            None when the flag is off / predictor missing / drift fired
            / threshold not exceeded. Callers MUST tolerate None — this
            is an opt-in path.

        Gating order (short-circuits as soon as a gate trips):
            1. Runtime flag ``volume_anticipation_enabled`` must be True
            2. ``volume_predictor`` must be available
            3. Drift detector must NOT flag the leader (when available)
            4. Predicted total volume > threshold
            5. Computed Kelly size > 0 (we never emit zero-size entries)
        """
        # Gate 1: flag off → behavior is byte-identical to pre-R9.
        if not await self._volume_anticipation_enabled():
            return None
        # Gate 2: predictor must be wired by the caller.
        if self._volume_predictor is None:
            return None

        try:
            forecast = await self._volume_predictor.forecast(
                leader_wallet=signal_decision.leader_wallet,
                trade_size_usdc=float(signal_decision.size_usdc or 0.0),
            )
        except Exception as exc:
            logger.warning(
                f"DecisionRouter: volume forecast failed for "
                f"{signal_decision.leader_wallet[:10]}: {exc}"
            )
            return None

        # Gate 3: drift detector. If wired AND drift detected → bail.
        if self._drift_detector is not None:
            try:
                drift = await self._drift_detector.evaluate(
                    signal_decision.leader_wallet
                )
                if getattr(drift, "drift_detected", False):
                    logger.info(
                        f"DecisionRouter: suppressing volume_anticipation for "
                        f"{signal_decision.leader_wallet[:10]} (drift)"
                    )
                    return None
            except Exception as exc:
                logger.debug(
                    f"DecisionRouter: drift evaluator threw, proceeding: {exc}"
                )

        # Gate 4: total predicted volume vs threshold.
        threshold = await self._volume_anticipation_threshold()
        total_v = float(forecast.get("total_volume_usdc", 0.0) or 0.0)
        if total_v < threshold:
            return None

        # Gate 5: size > 0 AND > MIN_POSITION_USDC.
        kelly = self._kelly_from_volume(
            expected_volume_usdc=total_v,
            market_depth_usdc=market_depth_usdc,
            confidence=float(forecast.get("confidence", 0.0) or 0.0),
        )
        max_pos_pct = float(getattr(settings, "MAX_POSITION_PCT", 0.02))
        min_pos_usdc = float(getattr(settings, "MIN_POSITION_USDC", 50.0))
        position_size = min(
            kelly * max(current_capital, 0.0),
            max_pos_pct * max(current_capital, 0.0),
        )
        if position_size < min_pos_usdc:
            return None

        # Build the synthetic Decision and route.
        from src.engine.confidence_engine import Decision  # local import for cycles

        va_decision = Decision(
            action=VOLUME_ANTICIPATION_ACTION,
            leader_wallet=signal_decision.leader_wallet,
            market_id=signal_decision.market_id,
            token_id=signal_decision.token_id,
            size_usdc=float(position_size),
            kelly_fraction=float(kelly),
            thompson_follow=signal_decision.thompson_follow,
            thompson_fade=signal_decision.thompson_fade,
            confidence=float(forecast.get("confidence", 0.0) or 0.0),
            reason=(
                f"volume_anticipation E[volume]={total_v:.0f}"
                f" by_pool={','.join(forecast.get('by_pool', {}).keys())}"
            ),
            trade_context={
                **(signal_decision.trade_context or {}),
                "volume_forecast": forecast,
                "volume_anticipation": True,
            },
            context_penalty=signal_decision.context_penalty,
            signal_audit=signal_decision.signal_audit,
            strategy_track=signal_decision.strategy_track,
            economic_model_version=signal_decision.economic_model_version,
        )
        return await self.route(va_decision)

    async def _volume_anticipation_enabled(self) -> bool:
        """Resolve the runtime config flag. Defaults to False on any
        failure (safe-by-default: no R9 surface area when in doubt)."""
        if self._runtime_config is None:
            return False
        try:
            return bool(await self._runtime_config.get("volume_anticipation_enabled"))
        except Exception:
            return False

    async def _volume_anticipation_threshold(self) -> float:
        """Resolve the runtime threshold. Falls back to settings on any
        failure."""
        default_thresh = float(
            getattr(settings, "VOLUME_ANTICIPATION_THRESHOLD_USDC", 5000.0)
        )
        if self._runtime_config is None:
            return default_thresh
        try:
            v = await self._runtime_config.get(
                "volume_anticipation_threshold_usdc"
            )
            if v is None:
                return default_thresh
            return float(v)
        except Exception:
            return default_thresh

    @staticmethod
    def _kelly_from_volume(
        expected_volume_usdc: float,
        market_depth_usdc: float,
        confidence: float,
    ) -> float:
        """Map predicted follower-pool volume to a Kelly-like fraction.

        Heuristic per spec § 3.4: scale Kelly by the (sqrt of) expected
        flow relative to the market depth, modulated by the predictor's
        confidence in the forecast. The hard cap MAX_POSITION_PCT is
        applied upstream in ``maybe_emit_volume_anticipation``.

        Args:
            expected_volume_usdc: predicted next-window follower-pool
                volume.
            market_depth_usdc: rough market liquidity. 0 → fallback to
                a conservative cap.
            confidence: predictor's confidence in [0, 1].

        Returns:
            A non-negative Kelly fraction (typically << 1). The caller
            multiplies by current capital and applies the hard cap.
        """
        import math

        if expected_volume_usdc <= 0.0 or confidence <= 0.0:
            return 0.0
        depth = market_depth_usdc if market_depth_usdc > 0 else 100_000.0
        # Scale: at ratio=1 (volume == depth), un-confidence-adjusted
        # Kelly ≈ 0.05 (5%). Cap implicitly through MAX_POSITION_PCT
        # upstream.
        ratio = max(0.0, expected_volume_usdc / depth)
        kelly = 0.05 * math.sqrt(ratio) * confidence
        # Hard ceiling — never recommend > 50% of capital (sanity).
        return float(min(0.5, max(0.0, kelly)))
