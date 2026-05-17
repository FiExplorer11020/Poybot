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
# Round 8 (The Lens) — strategy-conditional weights. Imported here so the
# confidence engine module is the single point of integration; everything
# else is gated by RuntimeConfig at runtime.
from src.strategy_classifier.model import STRATEGY_WEIGHTS


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

        # ── 2026-05-17 round 3: excluded-leader guard ───────────────────
        # The leaders.excluded flag is the most authoritative deny signal
        # in the system — set by `enrich_leaders` for `falcon_no_data`
        # wallets, by structural-bot detection, by post-mortem operator
        # action, and (round 3 quick-win) anywhere a wallet is known to
        # be untradable. Run BEFORE `_trade_age_s` so an excluded wallet
        # never costs us a downstream fetch or a stale_trade log entry.
        # The lookup is a single-row query on the small `leaders` table
        # (PK index lookup, sub-ms). On any DB failure we silently fall
        # through to the legacy behavior (excluded=False) — the same
        # defensive default the rest of the engine uses, so a transient
        # DB blip can't accidentally widen acceptance.
        gate_state = await self._get_leader_gate_state(wallet)
        if gate_state.get("excluded"):
            exclude_reason = (
                str(gate_state.get("exclude_reason") or "unspecified").strip()
                or "unspecified"
            )
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                0.0,
                f"leader_excluded|reason={exclude_reason}",
            )
            return None

        trade_time, trade_age_s = self._trade_age_s(trade)
        if trade_age_s > float(settings.LIVE_DECISION_MAX_TRADE_AGE_S):
            # 2026-05-17 round 2 diagnosis: this gate was silently dropping
            # 224 of 225 leader trades per hour (99.6%) because the realistic
            # observer-to-engine latency (api_wallet REST poll cadence +
            # publish + subscriber callback) is 200-400 s — well above the
            # original 120 s cap. We log to decision_log now so future
            # operators can SEE the gate fire instead of silent-skipping.
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                0.0,
                f"stale_trade|age={trade_age_s:.0f}s|max={int(settings.LIVE_DECISION_MAX_TRADE_AGE_S)}s",
            )
            return None

        # Reject HIGH-price FOLLOW entries (entry > 0.85). When the
        # market has already moved into the high-probability zone, the
        # leader's edge is captured by price. Upside is bounded at +18%
        # (to 1.0) while downside is up to -99%. We saw 4 trades lose
        # ~$550 total at entry 0.99 → exit 0.01.
        #
        # We DO allow low-price entries (≤ 0.15) — they offer asymmetric
        # upside (BTC trade #2: 0.002 → 0.59 = +29,400% return). If a
        # leader buys NO at 0.05, we follow. If the market resolves NO,
        # we gain massively; if it resolves YES, we lose only the
        # premium we paid.
        try:
            entry_price = float(trade.get("price") or 0.5)
        except (TypeError, ValueError):
            entry_price = 0.5
        if entry_price >= 0.85:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                entry_price,
                f"high_price_follow_blocked|p={entry_price:.3f}",
            )
            return None

        # Liquidity gate: skip markets with no realized volume in the
        # last 24h. Backtest (24h decisions) showed 62% of FOLLOWs were
        # on illiquid markets with NO follow-up trade activity — useless
        # for trading even if all other gates pass. Threshold $5k matches
        # the maintenance loop's coverage tier.
        #
        # Strategy upgrade 2026-05-17: `markets.volume_24h` is often
        # stale or 0 (the 580 vol24h=0 SKIPs/24h that dominated the
        # SKIP reasons came from data-freshness, not real zero volume).
        # Fall back to a query on `trades_observed` (sum of size_usdc
        # in the last 24h) when `markets.volume_24h` is 0/NULL. If both
        # sources are zero → keep the SKIP and log the dual-zero case
        # for debugging.
        market_volume = 0.0
        volume_source = "markets.volume_24h"
        observed_volume = 0.0
        try:
            async with get_db() as conn:
                vol_row = await conn.fetchrow(
                    "SELECT volume_24h FROM markets WHERE market_id = $1",
                    market_id,
                )
                if vol_row and vol_row["volume_24h"]:
                    market_volume = float(vol_row["volume_24h"] or 0)
                if market_volume <= 0.0:
                    # Fallback: trades_observed last 24h. Exclude
                    # source='onchain' rows: their price=0 placeholder
                    # is harmless here (we only sum size_usdc) but the
                    # `market_id = token_id` placeholder means the
                    # row would match a different liquidity pool than
                    # the real market — yielding inflated, attribution-
                    # less volume. Older rows without a source value
                    # still flow through (IS DISTINCT FROM is NULL-safe).
                    obs_row = await conn.fetchrow(
                        """
                        SELECT COALESCE(SUM(size_usdc), 0) AS vol
                        FROM trades_observed
                        WHERE market_id = $1
                          AND time >= NOW() - INTERVAL '24 hours'
                          AND source IS DISTINCT FROM 'onchain'
                        """,
                        market_id,
                    )
                    if obs_row and obs_row["vol"] is not None:
                        observed_volume = float(obs_row["vol"] or 0)
                        if observed_volume > 0.0:
                            market_volume = observed_volume
                            volume_source = "trades_observed.last_24h"
        except Exception:
            market_volume = 0.0
        if market_volume < 5000.0:
            if market_volume == 0.0 and observed_volume == 0.0:
                logger.debug(
                    f"low_market_liquidity DUAL ZERO market={market_id} "
                    f"wallet={wallet}: markets.volume_24h=0 AND "
                    "trades_observed last 24h=0"
                )
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                market_volume,
                f"low_market_liquidity|vol24h={market_volume:.0f}|src={volume_source}",
            )
            return None

        readiness = await self._get_readiness(wallet)

        # ── 2026-05-17 round 3: cold-start floor ─────────────────────────
        # Hard floor on (internal_resolved + external_resolved) before
        # any FOLLOW/FADE signal fires. Runs after `_get_readiness` so we
        # have both counts in hand and before Thompson sampling so a
        # zero-history wallet never costs us a posterior update. The
        # tier-specific resolved gates further downstream still apply —
        # this is a system-wide minimum that catches wallets that slip
        # past the per-tier knobs (e.g. via FADE-only path or missing
        # Falcon data). Cheap insurance: one comparison on data we
        # already have.
        internal_resolved = int(readiness.get("positions_resolved", 0) or 0)
        external_resolved = int(readiness.get("external_resolved_count", 0) or 0)
        cold_start_floor = int(
            await self._read_min_leader_total_resolved()
        )
        if (internal_resolved + external_resolved) < cold_start_floor:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                0.0,
                (
                    f"cold_start_zero_resolved|internal={internal_resolved}"
                    f"|external={external_resolved}|min={cold_start_floor}"
                ),
            )
            return None

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

        # ── Strategy upgrade 2026-05-17 (Tier 1 fix #2+#3) — live-match gate ─
        # Reject signals on markets that look like a LIVE sport/eSports
        # match (resolves in MINUTES, not hours/days). The legacy
        # `MIN_HOURS_TO_RESOLUTION_FOLLOW=6h` gate is conceptually wrong
        # here because `markets.end_date` is the dispute-window
        # expiration, not the moment of resolution. On 2026-05-17 the
        # bot lost 9 trades at -96/98% by following leaders into IPL /
        # eSports matches that resolved in MINUTES while the time-to-
        # end_date filter saw ~169h of runway and waved them through.
        # The detector combines (1) Agent A's authoritative `markets.
        # is_live_match` Gamma flag, (2) regex on the question, (3) a
        # today-date heuristic, and (4) a sports-volume spike. Runs
        # BEFORE the leader_quality_gate so we don't waste posterior
        # math on a market that's about to settle. The redundant
        # paper_trader.open_trade check provides defense in depth in
        # case the engine is bypassed by a direct router push.
        try:
            from src.economics.live_match_detector import (
                is_live_match,
                live_match_block_enabled,
            )
            live_is, live_reason = await is_live_match(market_id)
            block_enabled = await live_match_block_enabled()
        except Exception as exc:
            logger.debug(
                f"live_match_detector: predicate failed for "
                f"market={market_id}: {exc}"
            )
            live_is, live_reason, block_enabled = False, "no_match", False
        if live_is and block_enabled:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                0.0,
                f"live_match_blocked|signal={live_reason}",
            )
            return None

        # ── Strategy upgrade 2026-05-17 round 2 — Falcon-prior + tier gate ─
        # Replaces the prior internal-only `leader_quality_gate`. Two
        # changes:
        #   (1) The posterior counts are fused with the Falcon Wallet
        #       360 track record via `_compute_effective_metrics`. A
        #       leader Falcon has observed 200 trades on but we only
        #       reconstructed 5 still passes the resolved gate.
        #   (2) The resolved + winrate floors are TIER-SPECIFIC
        #       (`_classify_leader_tier` returns A/B/C from
        #       `falcon_score` OR `confirmed_followers`). Tier A
        #       (Falcon-validated) gets the loosest gate; Tier C
        #       (cold-start, no validation) keeps the legacy strict
        #       gate so we don't silently widen risk.
        # FADE intentionally bypasses the winrate gate (it targets
        # losing leaders by construction) but is still subject to the
        # tier-specific resolved floor.
        try:
            from src.control.runtime_config import get_runtime_config
            cfg = get_runtime_config()
            effective_cfg = await cfg.effective()

            min_signal_strength_cfg = float(
                effective_cfg.get(
                    "min_signal_strength",
                    getattr(settings, "MIN_SIGNAL_STRENGTH", 0.30),
                )
            )
            # `kelly_fraction` knob (default 0.50). Previously defined in
            # runtime_config but never read by the engine; the live path
            # was effectively running full Kelly (1.0×).
            kelly_fraction_mul = float(
                effective_cfg.get(
                    "kelly_fraction",
                    getattr(settings, "KELLY_FRACTION", 0.50),
                )
            )
            # Falcon-prior discount + per-tier floors.
            falcon_discount = float(
                effective_cfg.get(
                    "falcon_external_discount",
                    getattr(settings, "FALCON_EXTERNAL_DISCOUNT", 0.5),
                )
            )
            tier_a_min_resolved = int(
                effective_cfg.get(
                    "tier_a_min_resolved",
                    getattr(settings, "TIER_A_MIN_RESOLVED", 10),
                )
            )
            tier_a_min_winrate = float(
                effective_cfg.get(
                    "tier_a_min_winrate",
                    getattr(settings, "TIER_A_MIN_WINRATE", 0.50),
                )
            )
            tier_b_min_resolved = int(
                effective_cfg.get(
                    "tier_b_min_resolved",
                    getattr(settings, "TIER_B_MIN_RESOLVED", 20),
                )
            )
            tier_b_min_winrate = float(
                effective_cfg.get(
                    "tier_b_min_winrate",
                    getattr(settings, "TIER_B_MIN_WINRATE", 0.55),
                )
            )
            tier_c_min_resolved = int(
                effective_cfg.get(
                    "tier_c_min_resolved",
                    getattr(settings, "TIER_C_MIN_RESOLVED", 30),
                )
            )
            tier_c_min_winrate = float(
                effective_cfg.get(
                    "tier_c_min_winrate",
                    getattr(settings, "TIER_C_MIN_WINRATE", 0.55),
                )
            )
            tier_a_falcon_threshold = float(
                effective_cfg.get(
                    "tier_a_falcon_threshold",
                    getattr(settings, "TIER_A_FALCON_THRESHOLD", 50.0),
                )
            )
            tier_b_falcon_threshold = float(
                effective_cfg.get(
                    "tier_b_falcon_threshold",
                    getattr(settings, "TIER_B_FALCON_THRESHOLD", 20.0),
                )
            )
            tier_a_follower_count = int(
                effective_cfg.get(
                    "tier_a_follower_count",
                    getattr(settings, "TIER_A_FOLLOWER_COUNT", 5),
                )
            )
            tier_b_follower_count = int(
                effective_cfg.get(
                    "tier_b_follower_count",
                    getattr(settings, "TIER_B_FOLLOWER_COUNT", 3),
                )
            )
        except Exception as exc:
            logger.debug(f"leader_quality_gate: runtime_config read failed: {exc}")
            min_signal_strength_cfg = float(getattr(settings, "MIN_SIGNAL_STRENGTH", 0.30))
            kelly_fraction_mul = float(getattr(settings, "KELLY_FRACTION", 0.50))
            falcon_discount = float(getattr(settings, "FALCON_EXTERNAL_DISCOUNT", 0.5))
            tier_a_min_resolved = int(getattr(settings, "TIER_A_MIN_RESOLVED", 10))
            tier_a_min_winrate = float(getattr(settings, "TIER_A_MIN_WINRATE", 0.50))
            tier_b_min_resolved = int(getattr(settings, "TIER_B_MIN_RESOLVED", 20))
            tier_b_min_winrate = float(getattr(settings, "TIER_B_MIN_WINRATE", 0.55))
            tier_c_min_resolved = int(getattr(settings, "TIER_C_MIN_RESOLVED", 30))
            tier_c_min_winrate = float(getattr(settings, "TIER_C_MIN_WINRATE", 0.55))
            tier_a_falcon_threshold = float(getattr(settings, "TIER_A_FALCON_THRESHOLD", 50.0))
            tier_b_falcon_threshold = float(getattr(settings, "TIER_B_FALCON_THRESHOLD", 20.0))
            tier_a_follower_count = int(getattr(settings, "TIER_A_FOLLOWER_COUNT", 5))
            tier_b_follower_count = int(getattr(settings, "TIER_B_FOLLOWER_COUNT", 3))

        # Tier classification: Falcon-validated leaders get a faster
        # path. Tie-break order is A → B → C (Falcon-validated wins).
        tier = self._classify_leader_tier(
            falcon_score=readiness.get("falcon_score"),
            follower_count=readiness.get("confirmed_followers"),
            tier_a_falcon=tier_a_falcon_threshold,
            tier_b_falcon=tier_b_falcon_threshold,
            tier_a_followers=tier_a_follower_count,
            tier_b_followers=tier_b_follower_count,
        )
        if tier == "A":
            tier_min_resolved = tier_a_min_resolved
            tier_min_winrate = tier_a_min_winrate
        elif tier == "B":
            tier_min_resolved = tier_b_min_resolved
            tier_min_winrate = tier_b_min_winrate
        else:
            tier_min_resolved = tier_c_min_resolved
            tier_min_winrate = tier_c_min_winrate

        # Bayesian fusion of internal + Falcon-external posteriors.
        effective_resolved, effective_winrate = self._compute_effective_metrics(
            profile=profile,
            readiness=readiness,
            discount=falcon_discount,
        )

        follow_gate_passes = (
            effective_resolved >= tier_min_resolved
            and effective_winrate >= tier_min_winrate
        )
        # FADE bypasses the winrate gate (intentionally targets losers)
        # but still uses the tier-specific resolved floor.
        fade_gate_passes = effective_resolved >= tier_min_resolved

        if not follow_gate_passes and not fade_gate_passes:
            # Both sides fail. The dominant failure is the resolved
            # floor — if FADE's resolved-only gate also failed,
            # there's not enough Bayesian evidence for either path.
            # SKIP reason includes tier + effective for log analysis,
            # matching the spec format
            # `leader_resolved_too_low|tier=A|effective=8|min=10`.
            if effective_resolved < tier_min_resolved:
                reason = (
                    f"leader_resolved_too_low|tier={tier}"
                    f"|effective={effective_resolved}|min={tier_min_resolved}"
                )
            else:
                reason = (
                    f"leader_winrate_too_low|tier={tier}"
                    f"|effective={effective_winrate:.3f}|min={tier_min_winrate:.3f}"
                )
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                0.0,
                0.0,
                0.0,
                effective_winrate,
                reason,
            )
            return None

        # Restrict ready-sides downstream to honour the gate result.
        if not follow_gate_passes:
            follow_ready = False
        if not fade_gate_passes:
            fade_ready = False

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

        # ── Round 10 (The Truth Test) — causal gate ──────────────────────
        # Gated by runtime config flag `causal_gating_enabled`. When
        # disabled (default), behavior is byte-identical to pre-R10.
        # When enabled, consult causal_estimates for the (leader, pool)
        # pair; if the IV-adjusted CI does NOT exclude zero positively,
        # halve follow_confidence and BLOCK volume_anticipation entries.
        causal_gate = await self._maybe_apply_causal_gate(wallet, trade_context)
        if causal_gate is not None:
            thompson_follow *= float(causal_gate.get("follow_multiplier", 1.0))
            trade_context["causal_gate"] = {
                "result": causal_gate.get("result"),
                "ate": causal_gate.get("ate"),
                "ci_low": causal_gate.get("ci_low"),
                "ci_high": causal_gate.get("ci_high"),
                "pool_class": causal_gate.get("pool_class"),
            }

        # ── Round 8 (The Lens) — strategy-conditional weighting ────────
        # Gated by runtime config flag. When disabled (default) we leave
        # the Thompson outputs untouched, byte-identical to pre-R8.
        strategy_weights = await self._maybe_get_strategy_weights(wallet)
        if strategy_weights is not None:
            thompson_follow *= float(strategy_weights.get("follow", 1.0))
            thompson_fade *= float(strategy_weights.get("fade", 1.0))
            # We can't clamp to [0, 1] because Thompson values can exceed 1
            # after a >1 multiplier — downstream comparisons are pairwise
            # so the absolute scale doesn't matter, but log it once for
            # provenance.
            trade_context["strategy_weights_applied"] = {
                "follow": float(strategy_weights.get("follow", 1.0)),
                "fade": float(strategy_weights.get("fade", 1.0)),
                "skip": float(strategy_weights.get("skip", 1.0)),
                "primary_strategy": strategy_weights.get("primary_strategy"),
            }

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

        # Tunable floor — was 0.25, lowered to 0.05 to unblock cold-start
        # decisions when profiler hasn't accumulated stability data yet.
        # The behavioral-penalty path (follow_penalty/fade_penalty above)
        # still scales size down for unstable wallets — this gate is
        # only a "kill switch" for truly degenerate cases.
        if process_score < 0.05:
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

        # ── Strategy upgrade 2026-05-17 — min_signal_strength gate ────
        # Wire the long-dead `min_signal_strength` knob (defined in
        # runtime_config since the cockpit was built, but never read by
        # evaluate()). Reject the decision when post-Thompson confidence
        # is below the floor. Default 0.30 — still permissive enough that
        # the bot can learn from edge cases, but high enough to kill the
        # bottom-decile signals that produced losing trades.
        if confidence < min_signal_strength_cfg:
            await self._log_decision(
                wallet,
                market_id,
                "skip",
                thompson_follow,
                thompson_fade,
                0.0,
                confidence,
                f"below_min_signal_strength|conf={confidence:.3f}|"
                f"min={min_signal_strength_cfg:.3f}|action={action}",
            )
            return None

        state = self._thompson.get(wallet, {})
        alpha, beta_ = state.get(action, [DEFAULT_ALPHA, DEFAULT_BETA])
        entry_price = float(trade.get("price", 0.5) or 0.5)
        market_price = entry_price if action == "follow" else max(0.01, 1.0 - entry_price)
        kelly_fraction, size_usdc = self._kelly_size(
            action=action,
            alpha=float(alpha),
            beta_=float(beta_),
            market_price=market_price,
            kelly_fraction_multiplier=kelly_fraction_mul,
        )

        # Floor multiplier at 0.20 so behavior penalties scale size DOWN
        # to 20% of Kelly, never to 0. Pre-fix this zeroed out 18 FOLLOWs
        # yesterday because every active leader trips behavioral
        # heuristics (burst_trading, aggressive_scale_in, etc.).
        # Active = penalized but still tradable; truly degenerate
        # wallets are caught by the kill-switch process_score < 0.05.
        penalty_multiplier = max(0.20, 1.0 - context_penalty)
        kelly_fraction = round(kelly_fraction * penalty_multiplier, 4)
        size_usdc = round(size_usdc * penalty_multiplier, 2)
        # Instead of skipping when size dips below MIN after penalty,
        # floor to MIN — the gate is "trade at least the min" not
        # "don't trade". This keeps cold-start outcomes flowing into
        # the Thompson posterior so the bot can learn. Skipping here
        # was the dominant SKIP reason for non-extreme-price FOLLOWs
        # and was starving the learning loop.
        if 0.0 < size_usdc < settings.MIN_POSITION_USDC:
            size_usdc = float(settings.MIN_POSITION_USDC)
            kelly_fraction = settings.MIN_POSITION_USDC / settings.PAPER_CAPITAL_USDC

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
            decision=decision,
        )
        return decision

    async def _maybe_apply_causal_gate(
        self,
        wallet: str,
        trade_context: dict,
    ) -> dict[str, Any] | None:
        """Round 10 (The Truth Test) — return causal gate decision or None.

        Returns None (no-op path) when EITHER:
          1. The runtime flag ``causal_gating_enabled`` is False
             (default — shadow phase).
          2. No ``causal_estimates`` row exists for the (leader, pool)
             pair, or the DB read fails.

        When the flag is on and a causal estimate exists, returns a
        dict with:

            {
              "result": "allowed" | "downgraded" | "blocked",
              "follow_multiplier": 1.0 | CAUSAL_GATE_FOLLOW_PENALTY,
              "ate": float,
              "ci_low": float,
              "ci_high": float,
              "pool_class": str,
            }

        Decision rules (per spec § 3.5):
          * CI strictly excludes 0 with ci_low > 0   -> "allowed" (full conf)
          * CI strictly excludes 0 with ci_high < 0  -> "downgraded"
          * CI brackets 0 (no clean evidence)        -> "downgraded"
        """
        try:
            from src.control.runtime_config import get_runtime_config

            cfg = get_runtime_config()
            effective = await cfg.effective()
            enabled = bool(effective.get("causal_gating_enabled", False))
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(f"Causal gate: runtime_config read failed: {exc}")
            return None

        if not enabled:
            return None

        # Resolve the pool_class for this leader. We prefer the R8
        # strategy fingerprint already on `trade_context['wallet_strategy']`
        # (set above when present); otherwise default to 'all_followers'
        # to align with the R9 daemon's graceful-degradation pool name.
        pool_class = (
            (trade_context or {}).get("wallet_strategy")
            or "all_followers"
        )

        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        causal_ate,
                        causal_ate_ci_low,
                        causal_ate_ci_high,
                        wu_hausman_p,
                        first_stage_f,
                        convergence
                    FROM causal_estimates
                    WHERE leader_wallet = $1
                      AND pool_class = $2
                    ORDER BY estimated_at DESC
                    LIMIT 1
                    """,
                    wallet,
                    pool_class,
                )
        except Exception as exc:
            logger.debug(
                f"Causal gate: causal_estimates read failed for {wallet}: {exc}"
            )
            self._inc_causal_gate_metric("allowed")
            return None

        if not row or row["causal_ate"] is None:
            # No causal evidence available - use default behavior. This
            # is the "missing causal_estimates row" path: per spec the
            # gate should treat absence as "downgrade" to be on the
            # safe side (we don't have evidence the leader is causally
            # influential). We mirror that here.
            self._inc_causal_gate_metric("downgraded")
            return {
                "result": "downgraded",
                "follow_multiplier": float(
                    getattr(settings, "CAUSAL_GATE_FOLLOW_PENALTY", 0.5)
                ),
                "ate": None,
                "ci_low": None,
                "ci_high": None,
                "pool_class": pool_class,
            }

        ate = float(row["causal_ate"])
        ci_low = float(row["causal_ate_ci_low"]) if row["causal_ate_ci_low"] is not None else float("nan")
        ci_high = float(row["causal_ate_ci_high"]) if row["causal_ate_ci_high"] is not None else float("nan")
        convergence = str(row["convergence"] or "")

        # Acceptance rule: CI strictly excludes 0 with positive sign.
        # If ci_low or ci_high is NaN (rare path) we treat as "no
        # evidence" -> downgrade. Weak-instrument fits are also
        # downgraded — the spec § 6 risk row "Instrument invalidity"
        # mitigation is exactly this.
        has_positive_evidence = (
            convergence == "converged"
            and ci_low == ci_low  # NaN check
            and ci_high == ci_high
            and ci_low > 0
        )

        if has_positive_evidence:
            self._inc_causal_gate_metric("allowed")
            return {
                "result": "allowed",
                "follow_multiplier": 1.0,
                "ate": ate,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "pool_class": pool_class,
            }

        self._inc_causal_gate_metric("downgraded")
        return {
            "result": "downgraded",
            "follow_multiplier": float(
                getattr(settings, "CAUSAL_GATE_FOLLOW_PENALTY", 0.5)
            ),
            "ate": ate,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "pool_class": pool_class,
        }

    @staticmethod
    def _inc_causal_gate_metric(result: str) -> None:
        """Best-effort Prometheus increment for the R10 gate."""
        try:
            from src.monitoring.metrics import (
                confidence_engine_causal_gates_total,
            )

            confidence_engine_causal_gates_total.labels(result=result).inc()
        except Exception:
            pass

    async def _maybe_get_strategy_weights(
        self,
        wallet: str,
    ) -> dict[str, Any] | None:
        """Round 8 (The Lens) — return per-leader strategy weights or None.

        Returns None (no-op path) when EITHER:
          1. The runtime flag ``strategy_conditional_confidence_enabled``
             is False (default — shadow phase).
          2. The leader's ``classification_json -> strategy_fingerprint``
             is missing or the classifier hasn't run yet.

        When both are present, returns the
        :data:`src.strategy_classifier.model.STRATEGY_WEIGHTS` row for
        the leader's primary strategy, augmented with the strategy name
        for audit logging.

        This method is dependency-injected via RuntimeConfig — the test
        suite can flip it without touching DB-backed state.
        """
        # Step 1: read the runtime flag. We MUST NOT cache the result
        # locally — the operator can flip the flag at any time and we
        # want the next decision to honor it.
        try:
            from src.control.runtime_config import get_runtime_config

            cfg = get_runtime_config()
            effective = await cfg.effective()
            enabled = bool(
                effective.get("strategy_conditional_confidence_enabled", False)
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(f"Strategy gate: runtime_config read failed: {exc}")
            return None

        if not enabled:
            return None

        # Step 2: read the leader's classification fingerprint. Defensive
        # against missing rows, partial JSON, and unknown strategy
        # classes (forward-compat with future taxonomy growth).
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT classification_json
                    FROM leaders
                    WHERE wallet_address = $1
                    """,
                    wallet,
                )
        except Exception as exc:  # pragma: no cover
            logger.debug(f"Strategy gate: DB read failed for {wallet}: {exc}")
            return None

        if not row:
            return None
        raw = row["classification_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, dict):
            return None
        fingerprint = raw.get("strategy_fingerprint")
        if not isinstance(fingerprint, dict):
            return None
        primary = fingerprint.get("primary_strategy")
        if primary not in STRATEGY_WEIGHTS:
            # Unknown class (forward-compat / older fingerprint). No-op.
            return None
        weights = dict(STRATEGY_WEIGHTS[primary])
        weights["primary_strategy"] = primary
        return weights

    # ── Strategy upgrade 2026-05-17 round 2 — Falcon prior + tiers ────
    # Two pure helpers that fuse the externally-reported Falcon
    # Wallet 360 track record into the Bayesian gate. Kept as
    # @staticmethod so the test suite can exercise them without
    # constructing an engine + mocking the DB.

    @staticmethod
    def _compute_effective_metrics(
        profile: dict | None,
        readiness: dict | None,
        discount: float,
    ) -> tuple[int, float]:
        """Combine internal + Falcon-external posterior counts.

        Returns ``(effective_resolved, effective_winrate)``:

            effective_resolved = MAX(
                internal_resolved,
                int(external_resolved * discount)
            )

            effective_winrate  = (internal_wins
                                  + discount * external_wins + 1)
                               / (internal_resolved
                                  + discount * external_resolved + 2)

        The +1 / +2 in the winrate formula is Laplace smoothing —
        an empty profile (Beta(1,1) uninformed prior) gives 0.5, the
        right answer for "no evidence either way".

        The ``MAX`` choice in effective_resolved (vs SUM) is
        deliberate: a Falcon-validated leader with 100 external
        trades but only 5 internal should NOT need 30 internal to
        clear Tier C; conversely a leader with 200 internal already
        clears the gate and the external evidence is a no-op. SUM
        would double-count overlapping observations.

        ``profile`` shape: the standard leader_profiles.profile_json
        dict (``accuracy.overall``, ``accuracy.resolved_count``,
        ``accuracy.by_category``). Missing keys default to 0/0.5.

        ``readiness`` shape: the dict returned by ``_get_readiness``
        — must carry the new ``external_*`` keys. Older callers
        passing a legacy 3-key dict get external=0 and the function
        degrades to internal-only behaviour.
        """
        profile = profile or {}
        readiness = readiness or {}
        accuracy = (profile.get("accuracy") or {}) if isinstance(profile, dict) else {}

        internal_resolved = int(readiness.get("positions_resolved", 0) or 0)
        try:
            internal_winrate = float(accuracy.get("overall", 0.0) or 0.0)
        except (TypeError, ValueError):
            internal_winrate = 0.0
        # Convert the size-weighted Beta posterior MEAN back into a
        # wins-vs-losses split. We only need integer-scale counts for
        # the fusion math; the rounding error is bounded by 1 and
        # gets absorbed by the Laplace smoothing.
        internal_wins = round(internal_winrate * internal_resolved)

        external_resolved = int(readiness.get("external_resolved_count", 0) or 0)
        external_wins = int(readiness.get("external_wins", 0) or 0)
        # Note: we don't actually need external_losses for the fusion
        # — wins / resolved is the full sufficient statistic. We
        # ignore the slot to keep the math simple.

        try:
            d = float(discount)
        except (TypeError, ValueError):
            d = 0.5
        # Bound the discount defensively: a negative or >1 value
        # would either flip the prior (nonsense) or weight external
        # MORE than internal (the operator intent is "trust internal
        # more"; a true 50/50 fusion is at d=1.0).
        d = max(0.0, min(1.0, d))

        effective_resolved = max(
            internal_resolved,
            int(external_resolved * d),
        )

        numerator = internal_wins + d * external_wins + 1.0
        denominator = internal_resolved + d * external_resolved + 2.0
        # Denominator is always >= 2 thanks to the Laplace +2, so
        # division by zero is impossible.
        effective_winrate = numerator / denominator

        return effective_resolved, float(effective_winrate)

    @staticmethod
    def _classify_leader_tier(
        falcon_score: float | None,
        follower_count: int | None,
        *,
        tier_a_falcon: float = 50.0,
        tier_b_falcon: float = 20.0,
        tier_a_followers: int = 5,
        tier_b_followers: int = 3,
    ) -> str:
        """Return the leader's tier ("A", "B", "C").

        Tier rules (A wins ties — Falcon-validated leaders get the
        looser gate first):

            Tier A: falcon_score >= tier_a_falcon
                    OR confirmed_followers >= tier_a_followers
            Tier B: falcon_score >= tier_b_falcon
                    OR confirmed_followers >= tier_b_followers
            Tier C: else (legacy strict gate)

        Inputs are clamped to (0, ∞) — negatives and Nones land as 0
        and degrade to Tier C cleanly. This matches the production
        contract: a missing falcon_score (NULL in the leaders table)
        means "Falcon doesn't recognise this wallet", which is
        exactly the cold-start case Tier C handles.
        """
        try:
            fs = float(falcon_score or 0.0)
        except (TypeError, ValueError):
            fs = 0.0
        try:
            fc = int(follower_count or 0)
        except (TypeError, ValueError):
            fc = 0
        fs = max(0.0, fs)
        fc = max(0, fc)
        if fs >= tier_a_falcon or fc >= tier_a_followers:
            return "A"
        if fs >= tier_b_falcon or fc >= tier_b_followers:
            return "B"
        return "C"

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
        kelly_fraction_multiplier: float = 1.0,
    ) -> tuple[float, float]:
        """
        Bayesian Kelly fraction with shrinkage.

        p  = posterior mean = alpha / (alpha + beta_)
        b  = market odds = (1 - market_price) / market_price
        f* = (p * b - (1 - p)) / b
        shrinkage = 1 - variance / p^2

        ``kelly_fraction_multiplier`` applies the operator-tunable
        fractional-Kelly knob (RuntimeConfig key ``kelly_fraction``,
        default 0.50). The 2% MAX_POSITION_PCT cap is still enforced
        AFTER the multiplier so the dollar cap is the binding constraint
        even at full Kelly.
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

        # Cold-start floor: when Thompson posterior is uniform (Beta(1,1)
        # ⇒ p=0.5) AND the market is balanced (mp≈0.5) the f_star ≈ 0
        # and the bot never opens a position despite a high-confidence
        # FOLLOW signal. Apply a small fixed Kelly so we accumulate
        # outcomes and the posterior can converge. The hard cap below
        # still bounds total risk.
        min_kelly_cold_start = 0.005  # 0.5% of capital
        if alpha + beta_ <= 6 and kelly_fraction < min_kelly_cold_start:
            kelly_fraction = min_kelly_cold_start

        # Apply the operator-tunable fractional-Kelly multiplier BEFORE
        # the dollar cap so 0.5×Kelly with f*=0.04 lands at 0.02
        # (= MAX_POSITION_PCT cap). Clamp to [0, 1] to defend against a
        # bad override that would scale UP (>1) — the multiplier is a
        # de-leverage knob by design.
        clamped_mul = max(0.0, min(1.0, float(kelly_fraction_multiplier)))
        kelly_fraction = kelly_fraction * clamped_mul

        max_size = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT
        if action == "fade":
            max_size *= settings.FADE_SIZE_RATIO

        size_usdc = max(0.0, min(kelly_fraction * settings.PAPER_CAPITAL_USDC, max_size))
        if 0.0 < size_usdc < settings.MIN_POSITION_USDC:
            size_usdc = float(settings.MIN_POSITION_USDC)  # floor to min, never zero
            kelly_fraction = max_size / settings.PAPER_CAPITAL_USDC if max_size > 0 else kelly_fraction

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
                          AND source IS DISTINCT FROM 'onchain'
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
            # QW1 (audit 2026-05-17) — propagate leader side + signal price
            # downstream so decision_router._build_payload can surface them
            # at the top of the JSON payload. The paper_trader gates
            # `leader_sell_side` and `leader_price_drift` read these from
            # the decision dict, so without this stamping they always saw
            # None and silently no-op'd in production.
            "side": side,
            "price": market_price,
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

    async def _get_leader_gate_state(self, wallet: str) -> dict:
        """Cheap lookup of `(excluded, exclude_reason)` for the wallet.

        2026-05-17 round 3 quick-win patch. Used by `evaluate` BEFORE any
        other gate so an excluded wallet never costs us downstream work.
        The query is a PK lookup on the small `leaders` table
        (sub-millisecond on production). On any DB failure we return
        ``{"excluded": False, "exclude_reason": None}`` — the same
        defensive default the rest of the engine uses, so a transient
        Postgres blip CANNOT accidentally widen acceptance.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT excluded, exclude_reason
                    FROM leaders
                    WHERE wallet_address = $1
                    """,
                    wallet,
                )
                if row:
                    return {
                        "excluded": bool(row["excluded"]),
                        "exclude_reason": row["exclude_reason"],
                    }
        except Exception as exc:
            logger.debug(
                f"_get_leader_gate_state failed for {wallet}: {exc}"
            )
        return {"excluded": False, "exclude_reason": None}

    async def _read_min_leader_total_resolved(self) -> int:
        """Resolve the cold-start floor (`min_leader_total_resolved`).

        2026-05-17 round 3 quick-win. RuntimeConfig wins over the static
        `settings.MIN_LEADER_TOTAL_RESOLVED` default so the operator can
        tighten / relax via the dashboard cockpit without redeploying.
        Best-effort: never raises. A config-layer outage falls back to
        the env-driven static default (5 by default).
        """
        try:
            from src.control.runtime_config import get_runtime_config
            cfg = get_runtime_config()
            effective = await cfg.effective()
            raw = effective.get("min_leader_total_resolved")
            if raw is not None:
                return int(raw)
        except Exception as exc:
            logger.debug(
                f"_read_min_leader_total_resolved: runtime_config read "
                f"failed: {exc}"
            )
        return int(getattr(settings, "MIN_LEADER_TOTAL_RESOLVED", 5))

    async def _get_readiness(self, wallet: str) -> dict:
        """Load leader readiness stats from DB.

        Strategy upgrade 2026-05-17 round 2: ALSO pulls the Falcon
        prior columns (``leader_profiles.external_*``, populated by
        ``scripts/import_falcon_external_stats_2026_05_17.py``) and
        the leader's ``falcon_score`` so the tier classifier and the
        ``_compute_effective_metrics`` Bayesian fusion can run without
        a second DB roundtrip. Older callers that don't care about
        the new fields still see ``trades_observed`` /
        ``positions_resolved`` / ``confirmed_followers`` at the same
        keys with the same semantics.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM trades_observed t
                         WHERE t.wallet_address = $1
                           AND t.source IS DISTINCT FROM 'onchain') AS trades_observed,
                        COALESCE(lp.positions_resolved, 0) AS positions_resolved,
                        (SELECT COUNT(*) FROM follower_edges fe
                         WHERE fe.leader_wallet = $1
                           AND fe.co_occurrences >= 5
                           AND fe.same_direction_rate >= 0.7) AS confirmed_followers,
                        COALESCE(lp.external_resolved_count, 0) AS external_resolved_count,
                        COALESCE(lp.external_wins, 0) AS external_wins,
                        COALESCE(lp.external_losses, 0) AS external_losses,
                        COALESCE(l.falcon_score, 0) AS falcon_score
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
                        # Strategy 2026-05-17 round 2 — Falcon prior fields.
                        "external_resolved_count": int(
                            row["external_resolved_count"] or 0
                        ),
                        "external_wins": int(row["external_wins"] or 0),
                        "external_losses": int(row["external_losses"] or 0),
                        "falcon_score": float(row["falcon_score"] or 0.0),
                    }
        except Exception as e:
            logger.debug(f"Readiness check failed for {wallet}: {e}")
        return {
            "trades_observed": 0,
            "positions_resolved": 0,
            "confirmed_followers": 0,
            "external_resolved_count": 0,
            "external_wins": 0,
            "external_losses": 0,
            "falcon_score": 0.0,
        }

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
        decision: Any | None = None,
    ) -> None:
        audit_payload = signal_audit or {}
        decision_id: int | None = None
        try:
            async with get_db() as conn:
                decision_id = await conn.fetchval(
                    """
                    INSERT INTO decision_log
                        (leader_wallet, market_id, action, thompson_follow, thompson_fade,
                         kelly_fraction, confidence, reason, strategy_track,
                         economic_model_version, signal_audit)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                    RETURNING id
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
                # R13 — same-transaction prediction snapshot. Spec § 3.1 +
                # audit § 9.A: EVERY decision_log row gets a sister
                # decision_predictions row so the nightly calibration
                # batch can score each model's at-decision-time output —
                # including SKIPs (where only Thompson samples are
                # available, but those still calibrate follow_confidence
                # and fade_confidence). Failures here MUST NOT break the
                # decision write — wrapped in its own try/except.
                if decision_id is not None:
                    try:
                        from src.calibration import (
                            DecisionPrediction,
                            record_decision_predictions,
                        )
                        if decision is not None:
                            predictions = DecisionPrediction.from_decision_context(decision)
                        else:
                            # SKIP path: build a minimal prediction from
                            # the Thompson samples already in hand. Other
                            # model fields stay NULL → loss aggregator
                            # silently excludes them for this row.
                            predictions = DecisionPrediction(
                                follow_confidence=float(t_follow),
                                fade_confidence=float(t_fade),
                                predicted_at=datetime.now(tz=timezone.utc),
                            )
                        await record_decision_predictions(
                            conn,
                            int(decision_id),
                            predictions,
                        )
                    except Exception as r13_exc:
                        logger.debug(
                            f"R13 prediction snapshot skipped (decision_id={decision_id}): {r13_exc}"
                        )
        except Exception as e:
            logger.warning(f"Extended decision log failed, retrying legacy insert: {e}")
            try:
                async with get_db() as conn:
                    decision_id = await conn.fetchval(
                        """
                        INSERT INTO decision_log
                            (leader_wallet, market_id, action, thompson_follow, thompson_fade,
                             kelly_fraction, confidence, reason)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        RETURNING id
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
                    # Best-effort R13 snapshot on the legacy path too —
                    # same fire-on-every-row semantics as the primary path.
                    if decision_id is not None:
                        try:
                            from src.calibration import (
                                DecisionPrediction,
                                record_decision_predictions,
                            )
                            if decision is not None:
                                predictions = DecisionPrediction.from_decision_context(decision)
                            else:
                                predictions = DecisionPrediction(
                                    follow_confidence=float(t_follow),
                                    fade_confidence=float(t_fade),
                                    predicted_at=datetime.now(tz=timezone.utc),
                                )
                            await record_decision_predictions(
                                conn,
                                int(decision_id),
                                predictions,
                            )
                        except Exception:
                            pass
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
            # JSONB columns come back from asyncpg as plain strings unless
            # a codec was registered. Parse them defensively so callers
            # always see a dict (or {} on parse failure).
            raw_compat = self._row_value(row, "compatibility", {}) or {}
            if isinstance(raw_compat, str):
                try:
                    raw_compat = json.loads(raw_compat) if raw_compat else {}
                except Exception:
                    raw_compat = {}
            if not isinstance(raw_compat, dict):
                raw_compat = {}
            return FeeSnapshot(
                market_id=str(self._row_value(row, "market_id", "")),
                token_id=str(self._row_value(row, "token_id", "")),
                fee_enabled=bool(self._row_value(row, "fee_enabled", True)),
                fee_rate=Decimal(str(self._row_value(row, "fee_rate", "0"))),
                maker_fee_rate=Decimal(str(self._row_value(row, "maker_fee_rate", "0"))),
                source=str(self._row_value(row, "source", "fee_snapshots")),
                captured_at=self._parse_dt_value(self._row_value(row, "captured_at")),
                compatibility=raw_compat,
                economic_model_version=str(
                    self._row_value(row, "economic_model_version", ECONOMIC_MODEL_VERSION)
                ),
            )
        except Exception as exc:
            logger.warning(f"Invalid fee snapshot row ignored: {exc}")
            return None

    async def _load_book_snapshot(self, market_id: str, token_id: str) -> BookSnapshotRef | None:
        """Read book:last cache, fall back to just-in-time CLOB fetch.

        The maintenance loop refreshes top-1500 markets every 2 min. For
        markets outside that set (which leaders trade often), the cache
        is empty. Rather than rejecting with `missing_book_snapshot`,
        we do a synchronous CLOB book fetch (1-2s) so the gate passes
        for the long tail. This sub-routine is also what populates the
        cache for the next decision on the same market.
        """
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"book:last:{market_id}:{token_id}")
        except Exception as exc:
            logger.warning(f"Live book lookup failed for {market_id}/{token_id}: {exc}")
            raw = None

        if raw:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
                best_bid = payload.get("best_bid")
                best_ask = payload.get("best_ask")
                if best_bid is not None and best_ask is not None:
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

        # Just-in-time fallback: fetch from CLOB and cache.
        try:
            import aiohttp
            import time as _time
            headers = {"User-Agent": "polymarket-leader-bot/1.0"}
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            if not bids or not asks:
                return None
            best_bid = str(bids[0].get("price"))
            best_ask = str(asks[0].get("price"))
            now_ts = _time.time()
            payload = {
                "market_id": market_id,
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "observed_ts": now_ts,
                "captured_at": now_ts,
                "source": "jit_fetch",
            }
            # Cache for the next decision
            try:
                await self._redis.set(
                    f"book:last:{market_id}:{token_id}",
                    json.dumps(payload),
                    ex=600,
                )
            except Exception:
                pass
            return BookSnapshotRef(
                market_id=market_id,
                token_id=token_id,
                best_bid=Decimal(best_bid),
                best_ask=Decimal(best_ask),
                captured_at=datetime.fromtimestamp(now_ts, tz=timezone.utc),
                source="jit_fetch",
                reference=payload,
            )
        except Exception as exc:
            logger.debug(f"JIT book fetch failed for {market_id}/{token_id}: {exc}")
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

        # max_book_age_s relaxed from 10s → 180s. Polymarket book moves
        # slowly between trades (most prediction markets see < 1 update/
        # min). The maintenance loop refreshes top 1500 markets every
        # 2 min, so a 3-min book staleness window matches the refresh
        # cadence with a safety margin. The strict 10s was designed for
        # latency-sensitive HFT, not the leader-following swing strategy.
        audit = evaluate_signal_gate(
            strategy_track=StrategyTrack.LEADER_SWING,
            market_id=decision.market_id,
            token_id=decision.token_id,
            token_map_ok=token_map_ok,
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
            max_book_age_s=180.0,
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

