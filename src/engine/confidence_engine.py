"""
Confidence Engine — Thompson Sampling decision (FOLLOW/FADE/SKIP) + Bayesian Kelly sizing.
Subscribes to trades:observed (is_leader=True), emits decisions.
"""

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import numpy as np
from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber
from src.control.redis_streams import StreamConsumer
from src.database.connection import get_db
from src.economics.gates import BookSnapshotRef, evaluate_signal_gate
from src.economics.models import ECONOMIC_MODEL_VERSION, FeeSnapshot, StrategyTrack
from src.profiler.behavior_profiler import (
    _cyclical_time_features,
    _default_profile,
    _get_category_accuracy,
    _hours_since_category_trade,
    _hours_since_position_loss,
    _infer_reason_codes,
    _reason_penalty_from_profile,
)


@dataclass
class Decision:
    action: str  # 'follow', 'fade', 'skip'
    leader_wallet: str
    market_id: str
    token_id: str
    size_usdc: float
    kelly_fraction: float
    thompson_follow: float
    thompson_fade: float
    confidence: float
    reason: str
    trade_context: dict | None = None
    context_penalty: float = 0.0
    signal_audit: dict | None = None
    strategy_track: str = StrategyTrack.LEADER_SWING.value
    economic_model_version: str = ECONOMIC_MODEL_VERSION


REDIS_TRADES_CHANNEL = "trades:observed"
# Phase 3 round 1: durable equivalent. The confidence engine consumes
# from the Streams path as its primary source; pub/sub is retained as
# a TODO(phase3-round2) safety net (dual-read, idempotent dispatch).
TRADES_STREAM_NAME = "trades:stream"
TRADES_STREAM_GROUP = "confidence"
REDIS_DECISIONS_CHANNEL = "decisions"
CACHE_PREFIX = "confidence:leader:"

# Default Beta(1,1) uniform prior
DEFAULT_ALPHA = 1.0
DEFAULT_BETA = 1.0


class ConfidenceEngine:
    def __init__(
        self,
        redis_client,
        behavior_profiler=None,
        error_model=None,
        decision_router=None,
    ):
        self._redis = redis_client
        self._profiler = behavior_profiler
        self._error_model = error_model
        # If a router is provided, the engine delegates publishing to it
        # (paper / live / dual routing). If None, we keep the legacy
        # behaviour: publish directly to the "decisions" channel. This
        # keeps existing tests and bootstrap code working unchanged.
        self._router = decision_router
        self._running = False
        self._stop_event = asyncio.Event()
        # Per-wallet Thompson state: {wallet: {"follow": [a, b], "fade": [a, b]}}
        self._thompson: dict[str, dict] = {}
        # F-04: dedicated pub/sub client with reconnect+resubscribe.
        # TODO(phase3-round2): remove this pubsub subscription once the
        # Streams path has soaked. Dispatch is guarded by
        # `_seen_trade_keys` so the dual-read does NOT double-evaluate.
        self._subscriber = Subscriber(
            settings.REDIS_URL, name="engine.confidence"
        )
        self._subscriber.register(
            REDIS_TRADES_CHANNEL, self._on_trade_message
        )
        # Phase 3 round 1: durable Streams consumer.
        self._trades_consumer = StreamConsumer(
            settings.REDIS_URL,
            stream=TRADES_STREAM_NAME,
            group=TRADES_STREAM_GROUP,
            consumer_name=f"{TRADES_STREAM_GROUP}.1",
        )
        self._trades_consumer.register(self._on_trade_stream_entry)
        # IDEMPOTENCY: every successful evaluation INSERTs a row into
        # `decision_log` keyed by `(time, leader, market)`. A duplicate
        # dispatch is at worst a duplicate decision_log row; we also
        # gate at the front of the dispatcher to avoid a duplicate
        # Thompson sample on the same trade.
        self._seen_trade_keys: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        await self._subscriber.start()
        await self._trades_consumer.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        await self._trades_consumer.stop()
        await self._subscriber.stop()

    async def _on_trade_message(self, trade: dict, _channel: str) -> None:
        if not self._running:
            return
        await self._dispatch_trade(trade, source="pubsub")

    async def _on_trade_stream_entry(
        self, trade: dict, _stream: str, entry_id: str
    ) -> None:
        if not self._running:
            return
        await self._dispatch_trade(trade, source="stream", entry_id=entry_id)

    async def _dispatch_trade(
        self,
        trade: dict,
        *,
        source: str,
        entry_id: str | None = None,
    ) -> None:
        key = _confidence_trade_dedup_key(trade)
        if key and key in self._seen_trade_keys:
            return
        if key:
            self._seen_trade_keys.add(key)
            if len(self._seen_trade_keys) > 8_000:
                self._seen_trade_keys = set(
                    list(self._seen_trade_keys)[-4_000:]
                )
        try:
            if not trade.get("is_leader"):
                return
            decision = await self.evaluate(trade)
            if decision:
                if self._router is not None:
                    await self._router.route(decision)
                else:
                    await self._emit(decision)
        except Exception as e:
            logger.error(
                f"ConfidenceEngine error src={source} entry_id={entry_id}: {e}"
            )
            raise

    def _parse_trade_time(self, trade: dict) -> datetime:
        ts = trade.get("time")
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc)

    def _trade_age_s(self, trade: dict) -> tuple[datetime, float]:
        trade_time = self._parse_trade_time(trade)
        age_s = max(0.0, (datetime.now(tz=timezone.utc) - trade_time).total_seconds())
        return trade_time, age_s

    async def evaluate(self, trade: dict) -> Decision | None:
        """
        Main decision logic for a leader trade.
        Returns Decision (including SKIP) or None on invalid input.
        Logs every decision to decision_log.
        """
        wallet = trade.get("wallet_address", "")
        market_id = trade.get("market_id", "")
        token_id = trade.get("token_id", "")

        if not wallet or not market_id:
            return None

        trade_time, trade_age_s = self._trade_age_s(trade)
        if trade_age_s > float(settings.LIVE_DECISION_MAX_TRADE_AGE_S):
            logger.debug(
                f"Skipping stale leader trade for {wallet} on {market_id}: "
                f"age={trade_age_s:.1f}s source={trade.get('source')}"
            )
            return None

        readiness = await self._get_readiness(wallet)
        # Use adaptive thresholds (refreshed periodically by the engine
        # scheduler) so cold-start gates relax automatically once the
        # system has accumulated enough data. Falls back to settings.*
        # static cold floor if the cache hasn't been refreshed.
        from src.config import eff
        follow_ready = (
            readiness["trades_observed"] >= eff("FOLLOW_MIN_TRADES")
            and readiness["confirmed_followers"] >= eff("FOLLOW_MIN_FOLLOWERS")
        )
        fade_ready = readiness["positions_resolved"] >= eff("FADE_MIN_RESOLVED")

        if not follow_ready and not fade_ready:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                0.0,
                "insufficient_data",
            )
            return None

        profile = await self._get_profile_snapshot(wallet)

        if wallet not in self._thompson:
            seeded = await self._seed_thompson_from_cache(wallet)
            if not seeded:
                await self._seed_thompson_from_profile(wallet, profile)

        trade_context = await self._build_trade_context(wallet, trade, readiness, profile)
        trade_context["trade_source"] = trade.get("source")
        trade_context["trade_time"] = trade_time.isoformat()
        trade_context["trade_age_s"] = round(trade_age_s, 2)
        trade_context["live_candidate"] = True
        trade_context.setdefault(
            "market_question",
            trade.get("market_question") or trade.get("question") or market_id,
        )
        trade_context.setdefault(
            "market_category",
            trade.get("market_category") or trade_context.get("category") or "unknown",
        )
        trade_context.setdefault(
            "market_type",
            trade.get("market_type") or trade_context.get("market_category") or "unknown",
        )
        trade_context.setdefault(
            "wallet_type",
            trade.get("wallet_type")
            or ("leader" if trade.get("is_leader") else "market_participant"),
        )
        if trade.get("wallet_strategy"):
            trade_context.setdefault("wallet_strategy", trade.get("wallet_strategy"))
        if trade.get("wallet_horizon"):
            trade_context.setdefault("wallet_horizon", trade.get("wallet_horizon"))
        if trade.get("wallet_influence"):
            trade_context.setdefault("wallet_influence", trade.get("wallet_influence"))

        error_prediction = None
        if self._error_model is not None:
            try:
                error_prediction = await self._error_model.predict(wallet, trade_context)
                trade_context["p_error"] = error_prediction.p_error
                trade_context["error_confidence"] = error_prediction.confidence
                trade_context["error_phase"] = error_prediction.phase
            except Exception as exc:
                logger.debug(f"Error model prediction failed for {wallet}: {exc}")

        thompson_follow, thompson_fade = self._sample_thompson(wallet)

        follow_reason_codes = (
            self._profiler.get_reason_codes(profile, "follow", trade_context)
            if self._profiler is not None
            else _infer_reason_codes(profile, "follow", trade_context)
        )
        fade_reason_codes = (
            self._profiler.get_reason_codes(profile, "fade", trade_context)
            if self._profiler is not None
            else _infer_reason_codes(profile, "fade", trade_context)
        )
        follow_penalty = (
            self._profiler.get_reason_penalty(profile, "follow", trade_context)
            if self._profiler is not None
            else _reason_penalty_from_profile(profile, "follow", follow_reason_codes)
        )
        fade_penalty = (
            self._profiler.get_reason_penalty(profile, "fade", trade_context)
            if self._profiler is not None
            else _reason_penalty_from_profile(profile, "fade", fade_reason_codes)
        )
        process_score = float(trade_context.get("process_score", 0.5) or 0.5)
        process_penalty = max(0.0, 0.5 - process_score)
        follow_penalty = min(0.85, follow_penalty + process_penalty)
        fade_penalty = min(0.85, fade_penalty + process_penalty)

        adjusted_follow = thompson_follow * (1.0 - follow_penalty)
        adjusted_fade = thompson_fade * (1.0 - fade_penalty)

        n = readiness["trades_observed"]
        exploration = max(settings.THOMPSON_EXPLORATION_FLOOR, 1.0 / math.sqrt(max(n, 1)))

        if np.random.random() < exploration:
            if follow_ready and fade_ready:
                action = "follow" if adjusted_follow >= adjusted_fade else "fade"
            else:
                action = "follow" if follow_ready else "fade"
            reason = "exploration"
        elif not follow_ready and fade_ready:
            action = "fade"
            reason = "follow_not_ready"
        elif follow_ready and not fade_ready:
            action = "follow"
            reason = "fade_not_ready"
        else:
            action = "follow" if adjusted_follow >= adjusted_fade else "fade"
            reason = "risk_adjusted_thompson"

        context_penalty = follow_penalty if action == "follow" else fade_penalty
        selected_codes = follow_reason_codes if action == "follow" else fade_reason_codes

        if process_score < 0.25:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                thompson_follow,
                thompson_fade,
                0.0,
                process_score,
                "wallet_process_too_unstable",
            )
            return None

        if action == "fade":
            if (
                error_prediction is not None
                and error_prediction.confidence < eff("FADE_MIN_CONFIDENCE")
            ):
                await self._log_decision(
                    wallet,
                    market_id,
                    "skip",
                    thompson_follow,
                    thompson_fade,
                    0.0,
                    error_prediction.confidence,
                    "fade_confidence_too_low",
                )
                return None
            if error_prediction is not None and error_prediction.p_error < 0.55:
                await self._log_decision(
                    wallet,
                    market_id,
                    "skip",
                    thompson_follow,
                    thompson_fade,
                    0.0,
                    error_prediction.p_error,
                    "fade_edge_too_low",
                )
                return None
            if error_prediction is not None:
                confidence = max(
                    0.0,
                    min(1.0, (0.7 * error_prediction.p_error + 0.3 * adjusted_fade)),
                )
            else:
                confidence = max(0.0, min(1.0, adjusted_fade))
        else:
            if (
                error_prediction is not None
                and error_prediction.confidence >= 0.6
                and error_prediction.p_error >= 0.65
            ):
                await self._log_decision(
                    wallet,
                    market_id,
                    "skip",
                    thompson_follow,
                    thompson_fade,
                    0.0,
                    error_prediction.p_error,
                    "follow_error_risk_too_high",
                )
                return None
            confidence = max(0.0, min(1.0, adjusted_follow))

        state = self._thompson.get(wallet, {})
        alpha, beta_ = state.get(action, [DEFAULT_ALPHA, DEFAULT_BETA])
        entry_price = float(trade.get("price", 0.5) or 0.5)
        market_price = entry_price if action == "follow" else max(0.01, 1.0 - entry_price)
        kelly_fraction, size_usdc = self._kelly_size(
            action=action,
            alpha=float(alpha),
            beta_=float(beta_),
            market_price=market_price,
        )

        penalty_multiplier = max(0.0, 1.0 - context_penalty)
        kelly_fraction = round(kelly_fraction * penalty_multiplier, 4)
        size_usdc = round(size_usdc * penalty_multiplier, 2)
        if 0.0 < size_usdc < settings.MIN_POSITION_USDC:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                thompson_follow,
                thompson_fade,
                0.0,
                confidence,
                "context_penalty_below_min_size",
            )
            return None

        reason_suffix = f"risk={context_penalty:.2f}"
        if selected_codes:
            reason_suffix += f"|{','.join(selected_codes[:2])}"
        trade_context["selected_action"] = action
        trade_context["reason_codes"] = selected_codes
        trade_context["context_penalty"] = round(context_penalty, 4)
        trade_context["process_penalty"] = round(process_penalty, 4)

        decision = Decision(
            action=action,
            leader_wallet=wallet,
            market_id=market_id,
            token_id=token_id,
            size_usdc=size_usdc,
            kelly_fraction=kelly_fraction,
            thompson_follow=round(thompson_follow, 4),
            thompson_fade=round(thompson_fade, 4),
            confidence=round(confidence, 4),
            reason=f"{reason}|{reason_suffix}",
            trade_context=trade_context,
            context_penalty=round(context_penalty, 4),
        )
        decision.signal_audit = await self._build_signal_audit(decision)
        trade_context["signal_audit"] = decision.signal_audit

        await self._log_decision(
            wallet,
            market_id,
            action,
            thompson_follow,
            thompson_fade,
            kelly_fraction,
            confidence,
            decision.reason,
            signal_audit=decision.signal_audit,
            strategy_track=decision.strategy_track,
            economic_model_version=decision.economic_model_version,
        )
        return decision

    def _sample_thompson(self, wallet: str) -> tuple[float, float]:
        """Sample one value from each Beta distribution for this wallet."""
        state = self._thompson.get(wallet, {})
        a_follow, b_follow = state.get("follow", [DEFAULT_ALPHA, DEFAULT_BETA])
        a_fade, b_fade = state.get("fade", [DEFAULT_ALPHA, DEFAULT_BETA])
        r_follow = float(np.random.beta(a_follow, b_follow))
        r_fade = float(np.random.beta(a_fade, b_fade))
        return r_follow, r_fade

    def update_thompson(self, wallet: str, action: str, won: bool) -> None:
        """
        Update Beta posterior after observing an outcome.
        won=True  → alpha += 1
        won=False → beta += 1
        """
        if wallet not in self._thompson:
            self._thompson[wallet] = {
                "follow": [DEFAULT_ALPHA, DEFAULT_BETA],
                "fade": [DEFAULT_ALPHA, DEFAULT_BETA],
            }
        if action in self._thompson[wallet]:
            if won:
                self._thompson[wallet][action][0] += 1.0
            else:
                self._thompson[wallet][action][1] += 1.0

    async def record_outcome(self, wallet: str, action: str, won: bool, outcome: dict) -> dict:
        """
        Persist FOLLOW/FADE learning to leader_profiles so the system improves
        across process restarts instead of only in-memory.
        """
        self.update_thompson(wallet, action, won)
        if self._profiler is None:
            return {"reason_codes": [], "penalty": 0.0}
        payload = dict(outcome)
        payload["action"] = action
        payload["won"] = won
        try:
            return await self._profiler.record_decision_outcome(wallet, payload)
        except Exception as exc:
            logger.warning(f"Failed to persist decision outcome for {wallet}: {exc}")
            return {"reason_codes": [], "penalty": 0.0}

    def _kelly_size(
        self,
        action: str,
        alpha: float,
        beta_: float,
        market_price: float = 0.5,
    ) -> tuple[float, float]:
        """
        Bayesian Kelly fraction with shrinkage.

        p  = posterior mean = alpha / (alpha + beta_)
        b  = market odds = (1 - market_price) / market_price
        f* = (p * b - (1 - p)) / b
        shrinkage = 1 - variance / p^2
        """
        p = alpha / (alpha + beta_)
        if p <= 0 or p >= 1:
            return 0.0, 0.0

        mp = max(0.01, min(0.99, market_price))
        b = (1.0 - mp) / mp
        if b <= 0:
            return 0.0, 0.0

        f_star = (p * b - (1.0 - p)) / b
        variance = (alpha * beta_) / ((alpha + beta_) ** 2 * (alpha + beta_ + 1))
        shrinkage = max(0.0, 1.0 - variance / (p**2))

        kelly_fraction = max(0.0, f_star * shrinkage)

        max_size = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT
        if action == "fade":
            max_size *= settings.FADE_SIZE_RATIO

        size_usdc = max(0.0, min(kelly_fraction * settings.PAPER_CAPITAL_USDC, max_size))
        if 0.0 < size_usdc < settings.MIN_POSITION_USDC:
            size_usdc = 0.0
            kelly_fraction = 0.0

        return round(kelly_fraction, 4), round(size_usdc, 2)

    async def _seed_thompson_from_profile(self, wallet: str, profile: dict | None = None) -> None:
        """
        Seed Beta(α, β) from persisted decision learning when available, else from
        historical leader accuracy.
        """
        profile = profile or await self._get_profile_snapshot(wallet)

        try:
            learning = profile.get("decision_learning", {})
            follow_learning = learning.get("follow", {})
            fade_learning = learning.get("fade", {})
            follow_total = int(follow_learning.get("wins", 0)) + int(
                follow_learning.get("losses", 0)
            )
            fade_total = int(fade_learning.get("wins", 0)) + int(fade_learning.get("losses", 0))

            if follow_total + fade_total >= 4:
                self._thompson[wallet] = {
                    "follow": [
                        float(follow_learning.get("beta_a", DEFAULT_ALPHA)),
                        float(follow_learning.get("beta_b", DEFAULT_BETA)),
                    ],
                    "fade": [
                        float(fade_learning.get("beta_a", DEFAULT_ALPHA)),
                        float(fade_learning.get("beta_b", DEFAULT_BETA)),
                    ],
                }
                return

            acc = profile.get("accuracy", {})
            resolved = int(acc.get("resolved_count", 0) or 0)
            overall = float(acc.get("overall", 0.5) or 0.5)
            if resolved >= 10:
                wins = max(1.0, round(overall * resolved))
                losses = max(1.0, resolved - wins + 1)
                self._thompson[wallet] = {
                    "follow": [wins, losses],
                    "fade": [losses, wins],
                }
                return
        except Exception as e:
            logger.debug(f"Thompson seed failed for {wallet}: {e}")

        self._thompson[wallet] = {
            "follow": [DEFAULT_ALPHA, DEFAULT_BETA],
            "fade": [DEFAULT_ALPHA, DEFAULT_BETA],
        }

    async def _seed_thompson_from_cache(self, wallet: str) -> bool:
        if self._redis is None:
            return False
        getter = getattr(self._redis, "get", None)
        if not callable(getter):
            return False
        try:
            raw = await getter(f"{CACHE_PREFIX}{wallet}")
            if not raw:
                return False
            payload = json.loads(raw)
            self._thompson[wallet] = {
                "follow": [
                    float(payload.get("follow_alpha", DEFAULT_ALPHA)),
                    float(payload.get("follow_beta", DEFAULT_BETA)),
                ],
                "fade": [
                    float(payload.get("fade_alpha", DEFAULT_ALPHA)),
                    float(payload.get("fade_beta", DEFAULT_BETA)),
                ],
            }
            return True
        except Exception as exc:
            logger.debug(f"Failed to seed Thompson cache for {wallet}: {exc}")
            return False

    async def _get_profile_snapshot(self, wallet: str) -> dict:
        if self._profiler is not None:
            try:
                profile = await self._profiler.get_profile(wallet)
                if profile:
                    return profile
            except Exception as exc:
                logger.debug(f"Profile fetch via profiler failed for {wallet}: {exc}")
        return _default_profile()

    async def _build_trade_context(
        self,
        wallet: str,
        trade: dict,
        readiness: dict,
        profile: dict,
    ) -> dict:
        profile = profile or _default_profile()
        market_id = trade.get("market_id", "")
        token_id = trade.get("token_id", "")
        side = (trade.get("side") or "").upper()

        market_price = float(trade.get("price", 0.5) or 0.5)
        size_usdc = float(trade.get("size_usdc", 0.0) or 0.0)

        try:
            ts = trade.get("time")
            if isinstance(ts, str):
                trade_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                trade_time = datetime.now(tz=timezone.utc)
        except (TypeError, ValueError):
            trade_time = datetime.now(tz=timezone.utc)

        category = "unknown"
        liquidity_score = 0.5
        profile_maturity = 0.0
        recent_avg_price = None

        try:
            async with get_db() as conn:
                market_row = await conn.fetchrow(
                    "SELECT category, liquidity_score FROM markets WHERE market_id = $1",
                    market_id,
                )
                if market_row:
                    category = market_row["category"] or "unknown"
                    liquidity_score = float(market_row["liquidity_score"] or 0.5)

                maturity_row = await conn.fetchrow(
                    "SELECT profile_maturity FROM leader_profiles WHERE wallet_address = $1",
                    wallet,
                )
                if maturity_row:
                    profile_maturity = float(maturity_row["profile_maturity"] or 0.0)

                recent_row = await conn.fetchrow(
                    """
                    SELECT AVG(price) AS avg_price FROM (
                        SELECT price
                        FROM trades_observed
                        WHERE market_id = $1
                          AND token_id = $2
                          AND time < $3
                        ORDER BY time DESC
                        LIMIT 10
                    ) recent
                    """,
                    market_id,
                    token_id,
                    trade_time,
                )
                if recent_row and recent_row["avg_price"] is not None:
                    recent_avg_price = float(recent_row["avg_price"])
        except Exception as exc:
            logger.debug(f"Trade context DB lookup failed for {wallet}/{market_id}: {exc}")

        is_contrarian = False
        if recent_avg_price is not None:
            if side == "BUY":
                is_contrarian = market_price < recent_avg_price
            elif side == "SELL":
                is_contrarian = market_price > recent_avg_price

        ewma_size = float(profile.get("sizing", {}).get("ewma_size", 0.0) or 0.0)
        size_ratio = size_usdc / ewma_size if ewma_size > 0 and size_usdc > 0 else 1.0

        trade_context = {
            "category": category,
            "is_contrarian": is_contrarian,
            "size_usdc": size_usdc,
            "size_ratio": round(size_ratio, 4),
            "liquidity_score": liquidity_score,
            "market_price": market_price,
            "profile_maturity": profile_maturity,
            "confirmed_followers": readiness["confirmed_followers"],
            "positions_resolved": readiness["positions_resolved"],
            "trades_observed": readiness["trades_observed"],
            "recent_avg_price": recent_avg_price,
            "category_accuracy": round(_get_category_accuracy(profile, category), 4),
            "hours_since_category_last_trade": _hours_since_category_trade(
                profile,
                category,
                trade_time.isoformat(),
            ),
            "hours_since_last_loss": _hours_since_position_loss(
                profile,
                trade_time.isoformat(),
            ),
            **_cyclical_time_features(trade_time.isoformat()),
        }

        if self._profiler is not None:
            process_insights = self._profiler.get_process_insights(
                profile,
                {
                    "market_id": market_id,
                    "side": side,
                    "size_usdc": size_usdc,
                    "category": category,
                    "time": trade_time.isoformat(),
                },
            )
            trade_context.update(process_insights)
            trade_context["deviation_score"] = round(
                self._profiler.get_deviation_score(
                    profile,
                    {
                        "category": category,
                        "size_usdc": size_usdc,
                        "is_contrarian": is_contrarian,
                    },
                ),
                4,
            )
        else:
            trade_context["deviation_score"] = 0.0
            trade_context["process_score"] = 0.5
            trade_context["flip_rate"] = 0.0
            trade_context["scale_in_rate"] = 0.0
            trade_context["avg_interarrival_s"] = 0.0
            trade_context["hours_since_last_trade"] = None
            trade_context["interarrival_s"] = None
            trade_context["flip_flag"] = False
            trade_context["scale_in_flag"] = False

        return trade_context

    async def precompute_redis_cache(self) -> int:
        """
        Precompute wallet-level confidence state for the hot path.
        """
        if self._redis is None:
            return 0

        setter = getattr(self._redis, "set", None)
        if not callable(setter):
            return 0

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        l.wallet_address,
                        lp.profile_json,
                        COALESCE(lp.profile_maturity, 0) AS profile_maturity,
                        COALESCE(lp.positions_resolved, 0) AS positions_resolved,
                        COALESCE(lp.trades_observed, 0) AS trades_observed
                    FROM leaders l
                    LEFT JOIN leader_profiles lp ON lp.wallet_address = l.wallet_address
                    WHERE l.excluded = FALSE
                    """
                )
        except Exception as exc:
            logger.warning(f"Failed to precompute confidence cache: {exc}")
            return 0

        cached = 0
        for row in rows:
            wallet = row["wallet_address"]
            raw_profile = row["profile_json"]
            if isinstance(raw_profile, str):
                profile = json.loads(raw_profile) if raw_profile else _default_profile()
            else:
                profile = dict(raw_profile) if raw_profile else _default_profile()
            await self._seed_thompson_from_profile(wallet, profile)
            state = self._thompson.get(wallet, {})
            payload = {
                "follow_alpha": float(state.get("follow", [DEFAULT_ALPHA, DEFAULT_BETA])[0]),
                "follow_beta": float(state.get("follow", [DEFAULT_ALPHA, DEFAULT_BETA])[1]),
                "fade_alpha": float(state.get("fade", [DEFAULT_ALPHA, DEFAULT_BETA])[0]),
                "fade_beta": float(state.get("fade", [DEFAULT_ALPHA, DEFAULT_BETA])[1]),
                "profile_maturity": float(row["profile_maturity"] or 0.0),
                "positions_resolved": int(row["positions_resolved"] or 0),
                "trades_observed": int(row["trades_observed"] or 0),
                "process_score": float(
                    profile.get("decision_process", {}).get("process_score_ewma", 0.5) or 0.5
                ),
                "decision_learning": profile.get("decision_learning", {}),
            }
            try:
                await setter(
                    f"{CACHE_PREFIX}{wallet}",
                    json.dumps(payload),
                    ex=max(3600, int(settings.FALCON_CACHE_TTL_S)),
                )
                cached += 1
            except Exception as exc:
                logger.debug(f"Confidence cache write failed for {wallet}: {exc}")
        return cached

    async def _get_readiness(self, wallet: str) -> dict:
        """Load leader readiness stats from DB."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM trades_observed t
                         WHERE t.wallet_address = $1) AS trades_observed,
                        COALESCE(lp.positions_resolved, 0) AS positions_resolved,
                        (SELECT COUNT(*) FROM follower_edges fe
                         WHERE fe.leader_wallet = $1
                           AND fe.co_occurrences >= 5
                           AND fe.same_direction_rate >= 0.7) AS confirmed_followers
                    FROM leaders l
                    LEFT JOIN leader_profiles lp ON lp.wallet_address = l.wallet_address
                    WHERE l.wallet_address = $1
                    """,
                    wallet,
                )
                if row:
                    return {
                        "trades_observed": int(row["trades_observed"] or 0),
                        "positions_resolved": int(row["positions_resolved"] or 0),
                        "confirmed_followers": int(row["confirmed_followers"] or 0),
                    }
        except Exception as e:
            logger.debug(f"Readiness check failed for {wallet}: {e}")
        return {"trades_observed": 0, "positions_resolved": 0, "confirmed_followers": 0}

    async def _log_decision(
        self,
        wallet: str,
        market_id: str,
        action: str,
        t_follow: float,
        t_fade: float,
        kelly: float,
        confidence: float,
        reason: str,
        *,
        signal_audit: dict | None = None,
        strategy_track: str | None = None,
        economic_model_version: str | None = None,
    ) -> None:
        audit_payload = signal_audit or {}
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO decision_log
                        (leader_wallet, market_id, action, thompson_follow, thompson_fade,
                         kelly_fraction, confidence, reason, strategy_track,
                         economic_model_version, signal_audit)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                    """,
                    wallet,
                    market_id,
                    action,
                    round(t_follow, 4),
                    round(t_fade, 4),
                    round(kelly, 4),
                    round(confidence, 4),
                    reason,
                    strategy_track or StrategyTrack.LEADER_SWING.value,
                    economic_model_version or ECONOMIC_MODEL_VERSION,
                    json.dumps(audit_payload),
                )
        except Exception as e:
            logger.warning(f"Extended decision log failed, retrying legacy insert: {e}")
            try:
                async with get_db() as conn:
                    await conn.execute(
                        """
                        INSERT INTO decision_log
                            (leader_wallet, market_id, action, thompson_follow, thompson_fade,
                             kelly_fraction, confidence, reason)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        wallet,
                        market_id,
                        action,
                        round(t_follow, 4),
                        round(t_fade, 4),
                        round(kelly, 4),
                        round(confidence, 4),
                        reason,
                    )
            except Exception as fallback_exc:
                logger.error(f"Failed to log decision: {fallback_exc}")

    async def _emit(self, decision: Decision) -> None:
        """Publish decision to Redis decisions channel."""
        try:
            await self._redis.publish(
                REDIS_DECISIONS_CHANNEL,
                json.dumps(
                    {
                        "action": decision.action,
                        "leader_wallet": decision.leader_wallet,
                        "market_id": decision.market_id,
                        "market_question": (decision.trade_context or {}).get("market_question"),
                        "market_category": (decision.trade_context or {}).get("market_category"),
                        "market_type": (decision.trade_context or {}).get("market_type"),
                        "token_id": decision.token_id,
                        "size_usdc": decision.size_usdc,
                        "kelly_fraction": decision.kelly_fraction,
                        "confidence": decision.confidence,
                        "thompson_follow": decision.thompson_follow,
                        "thompson_fade": decision.thompson_fade,
                        "reason": decision.reason,
                        "wallet_type": (decision.trade_context or {}).get("wallet_type"),
                        "wallet_strategy": (decision.trade_context or {}).get("wallet_strategy"),
                        "wallet_horizon": (decision.trade_context or {}).get("wallet_horizon"),
                        "wallet_influence": (decision.trade_context or {}).get("wallet_influence"),
                        "trade_context": decision.trade_context or {},
                        "context_penalty": decision.context_penalty,
                        "strategy_track": decision.strategy_track,
                        "economic_model_version": decision.economic_model_version,
                        "signal_audit": decision.signal_audit or {},
                    }
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to emit decision: {e}")

    def _row_value(self, row: Any, key: str, default: Any = None) -> Any:
        try:
            return row[key]
        except Exception:
            return default

    def _parse_dt_value(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc)

    def _fee_snapshot_from_row(self, row: Any) -> FeeSnapshot | None:
        if not row:
            return None
        try:
            return FeeSnapshot(
                market_id=str(self._row_value(row, "market_id", "")),
                token_id=str(self._row_value(row, "token_id", "")),
                fee_enabled=bool(self._row_value(row, "fee_enabled", True)),
                fee_rate=Decimal(str(self._row_value(row, "fee_rate", "0"))),
                maker_fee_rate=Decimal(str(self._row_value(row, "maker_fee_rate", "0"))),
                source=str(self._row_value(row, "source", "fee_snapshots")),
                captured_at=self._parse_dt_value(self._row_value(row, "captured_at")),
                compatibility=dict(self._row_value(row, "compatibility", {}) or {}),
                economic_model_version=str(
                    self._row_value(row, "economic_model_version", ECONOMIC_MODEL_VERSION)
                ),
            )
        except Exception as exc:
            logger.warning(f"Invalid fee snapshot row ignored: {exc}")
            return None

    async def _load_book_snapshot(self, market_id: str, token_id: str) -> BookSnapshotRef | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"book:last:{market_id}:{token_id}")
        except Exception as exc:
            logger.warning(f"Live book lookup failed for {market_id}/{token_id}: {exc}")
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            best_bid = payload.get("best_bid")
            best_ask = payload.get("best_ask")
            if best_bid is None or best_ask is None:
                return None
            observed = (
                payload.get("captured_at")
                or payload.get("observed_at")
                or payload.get("observed_ts")
                or payload.get("source_timestamp")
            )
            return BookSnapshotRef(
                market_id=market_id,
                token_id=token_id,
                best_bid=Decimal(str(best_bid)),
                best_ask=Decimal(str(best_ask)),
                captured_at=self._parse_dt_value(observed),
                source=str(payload.get("source", "redis_book_last")),
                reference=payload,
            )
        except Exception as exc:
            logger.warning(f"Invalid live book snapshot ignored for {market_id}/{token_id}: {exc}")
            return None

    async def _record_signal_rejection(self, reason: str, decision: Decision) -> None:
        logger.warning(
            "SignalAudit rejected decision "
            f"reason={reason} market={decision.market_id} token={decision.token_id} "
            f"leader={decision.leader_wallet} action={decision.action}"
        )
        if self._redis is None:
            return
        try:
            await self._redis.hincrby("signals:rejected:1h", reason, 1)
            await self._redis.expire("signals:rejected:1h", 3600)
        except Exception as exc:
            logger.debug(f"Failed to increment signal rejection counter: {exc}")

    async def _build_signal_audit(self, decision: Decision) -> dict:
        token_map_ok = False
        fee_snapshot = None
        book_snapshot = await self._load_book_snapshot(decision.market_id, decision.token_id)
        audit_inputs = {
            "stage": "confidence_engine",
            "paper_only": True,
            "readiness_mode": "deterministic",
            "leader_wallet": decision.leader_wallet,
            "action": decision.action,
            "confidence": decision.confidence,
            "size_usdc": decision.size_usdc,
        }

        try:
            async with get_db() as conn:
                market_row = await conn.fetchrow(
                    """
                    SELECT token_yes, token_no
                    FROM markets
                    WHERE market_id = $1
                    """,
                    decision.market_id,
                )
                token_yes = self._row_value(market_row, "token_yes")
                token_no = self._row_value(market_row, "token_no")
                token_map_ok = bool(
                    token_yes
                    and token_no
                    and decision.token_id
                    and decision.token_id in {str(token_yes), str(token_no)}
                )
                audit_inputs["token_yes_present"] = bool(token_yes)
                audit_inputs["token_no_present"] = bool(token_no)

                fee_row = await conn.fetchrow(
                    """
                    SELECT market_id, token_id, fee_enabled, fee_rate, maker_fee_rate,
                           source, captured_at, compatibility, economic_model_version
                    FROM fee_snapshots
                    WHERE market_id = $1 AND token_id = $2
                    ORDER BY captured_at DESC
                    LIMIT 1
                    """,
                    decision.market_id,
                    decision.token_id,
                )
                fee_snapshot = self._fee_snapshot_from_row(fee_row)
        except Exception as exc:
            audit_inputs["lookup_error"] = str(exc)
            logger.warning(
                "SignalAudit input lookup failed "
                f"market={decision.market_id} token={decision.token_id}: {exc}"
            )

        audit = evaluate_signal_gate(
            strategy_track=StrategyTrack.LEADER_SWING,
            market_id=decision.market_id,
            token_id=decision.token_id,
            token_map_ok=token_map_ok,
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )
        payload = audit.to_metadata()
        payload["economic_model_version"] = ECONOMIC_MODEL_VERSION
        payload["inputs"] = {
            **payload.get("inputs", {}),
            **audit_inputs,
            "token_map_ok": token_map_ok,
            "has_fee_snapshot": fee_snapshot is not None,
            "has_book_snapshot": book_snapshot is not None,
        }
        if book_snapshot is not None:
            payload["book_reference"] = dict(book_snapshot.reference)
        if payload.get("accepted") is not True:
            await self._record_signal_rejection(
                str(payload.get("reject_reason") or "unknown_rejection"),
                decision,
            )
        return payload


def _confidence_trade_dedup_key(event: dict) -> str:
    """Canonical fingerprint for confidence engine's idempotency cache."""
    wallet = event.get("wallet_address") or ""
    market = event.get("market_id") or ""
    t = event.get("time") or ""
    side = event.get("side") or ""
    price = event.get("price") or ""
    size = event.get("size_usdc") or ""
    if not wallet or not market:
        return ""
    return f"{wallet}|{market}|{t}|{side}|{price}|{size}"

