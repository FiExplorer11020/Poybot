"""
RuntimeConfig — minimal mutable config layer for risk + execution params.

Defaults come from settings (env-driven). Overrides are persisted in Redis
(key: ``runtime_config:overrides``) so they survive container restarts and
are visible to every service in the docker-compose stack.

Reads are cheap (in-memory cache, refreshed every 30s). Writes go through
``set_overrides`` which (1) validates the keys against ``ALLOWED_KEYS``,
(2) bounds-checks values against ``BOUNDS``, (3) persists to Redis, and
(4) notifies via Redis pub/sub on ``runtime_config:changed`` so the
RiskManager / ConfidenceEngine / PaperTrader can react immediately.

This is the back-end half of the "Risk & Config Option 2" UI cockpit: the
dashboard's RiskConfig form POSTs to ``/api/risk/update`` which calls
``set_overrides`` here.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber

# Keys the dashboard is allowed to flip at runtime. Anything not in this
# set is rejected by ``set_overrides`` to prevent accidental edits to
# system-critical knobs.
ALLOWED_KEYS: dict[str, str] = {
    "risk_per_trade_pct": "Per-trade max position as fraction of bankroll (Kelly cap).",
    "max_total_exposure_pct": "Max simultaneous exposure on a single market.",
    "kelly_fraction": "Fractional-Kelly multiplier (1.0 = full Kelly).",
    "max_drawdown_stop_pct": "Hard stop when drawdown reaches this fraction of peak.",
    "min_signal_strength": "FADE/FOLLOW gate on confidence engine output.",
    "max_concurrent_positions": "Hard cap on simultaneous open paper positions.",
    "cooldown_seconds": "Re-entry cooldown on a market after closing a paper trade.",
    "max_consecutive_losses": "Trip the warm-breaker after this many losses in a row.",
    "max_recent_losses_per_market": "Cap on losing trades on the same market in a 24h window.",
    "fade_size_ratio": "Multiplier applied to FADE positions vs FOLLOW (typically <1).",
    # Round 8 (The Lens) — gate for the strategy-conditional confidence
    # path. When False (default) the confidence engine is byte-identical
    # to pre-Round-8 behavior. When True, STRATEGY_WEIGHTS multipliers
    # (defined in src.strategy_classifier.model) modulate the Thompson
    # output per leader's classified strategy class.
    "strategy_conditional_confidence_enabled": (
        "Round 8 gate: apply per-strategy FOLLOW/FADE/SKIP weight multipliers "
        "to the Thompson sample. Boolean, default False (shadow phase)."
    ),
    # Round 9 (The Web) — volume anticipation entry policy. When False
    # (default), decision_router behavior is byte-identical to the R8
    # baseline. When True, the FollowerVolumePredictor is consulted on
    # every leader trade and a volume_anticipation entry fires when
    # predicted next-window follower-pool volume exceeds the threshold
    # below. The drift detector still suppresses entries on leaders
    # whose Hawkes coupling has decayed even when the flag is True.
    "volume_anticipation_enabled": (
        "Round 9 gate: fire volume_anticipation entries when "
        "FollowerVolumePredictor.total_volume_usdc > "
        "volume_anticipation_threshold_usdc. Boolean, default False "
        "(shadow phase)."
    ),
    "volume_anticipation_threshold_usdc": (
        "Round 9 threshold: minimum predicted next-window follower-pool "
        "volume (USDC) for a volume_anticipation entry to fire. Numeric, "
        "default 5000."
    ),
    # Round 10 (The Truth Test) — causal gate. When False (default)
    # the confidence engine is byte-identical to pre-R10 behavior.
    # When True, the engine consults causal_estimates for the (leader,
    # pool) pair and: (a) downgrades follow_confidence by
    # CAUSAL_GATE_FOLLOW_PENALTY when the IV-adjusted CI does NOT
    # exclude zero positively, and (b) BLOCKS volume_anticipation
    # entries on those pairs entirely. The flag stays OFF until the
    # methodology audit gate (spec § 6, ~1 week external causal-
    # inference expert) signs off + 60-day A/B Sharpe + max-drawdown
    # passes.
    "causal_gating_enabled": (
        "Round 10 gate: when True, downgrade follow_confidence and "
        "block volume_anticipation entries when the IV-adjusted causal "
        "ATE for the (leader, pool) pair does not exclude zero "
        "positively. Boolean, default False (shadow phase)."
    ),
    # Round 7 (The Front Door) — mempool intent router master gate.
    # Registered 2026-05-17 so the LAB dashboard can flip it without
    # operator SSH access. The TODO at intent_router.py:581 is now
    # closed. The flag remains off until 30-day shadow-soak completes
    # + CLOBClientWrapper sign+submit split + p50 < 250ms verified.
    "prefill_live_enabled": (
        "Round 7 gate: enable IntentRouter live firing of pre-signed "
        "orders when a leader's tx is detected in the mempool. Boolean, "
        "default False (shadow mode)."
    ),
    # Strategy upgrade 2026-05-17 — Phase 3 cohort selection knobs.
    # These wire backtest-confirmed filters into the live decision flow
    # so the operator can tune them from the dashboard.
    "min_entry_price": (
        "Strategy 2026-05-17: minimum entry_ask (BUY price). Trades on "
        "tokens below this floor lose money on average per backtest. "
        "Default 0.40, applies to both FOLLOW and FADE."
    ),
    "max_entry_price": (
        "Strategy 2026-05-17: maximum entry_ask. Trades above this "
        "ceiling have asymmetric downside. Default 0.92."
    ),
    "max_holding_period_s": (
        "Strategy 2026-05-17: hard close on any open trade past this "
        "holding period. Default 86400 (24h) — the win-rate cliff in "
        "the backtest data."
    ),
    # Strategy upgrade 2026-05-17 — sport-specific safety net (Tier 1 #4+#5).
    # Sport markets resolve in minutes (not hours); the generic 12h cap
    # holds positions through the entire match and into the resolution
    # wipe. Tighter cap + tighter stop = exit-time safety net for any
    # sport trade that slipped past the open-time live-match filter.
    "sport_max_holding_s": (
        "Strategy 2026-05-17 (Tier 1 #4): hard close on any sport trade "
        "past this holding period. Default 1800 (30 min) vs 43200 (12h) "
        "for non-sport. Catches positions that opened before the live-"
        "match filter deployed or slipped past it. Force-closes at the "
        "current bid with reason holding_cap_sport."
    ),
    "stop_loss_sport": (
        "Strategy 2026-05-17 (Tier 1 #5): stop-loss threshold for "
        "category='sports' trades. Default 0.03 (3%) vs 0.08 (8%) for "
        "non-sport. Sport prices move 5-15% in seconds during the event; "
        "the legacy 8% gives too much room for catastrophic resolution "
        "loss. Applies to BOTH FOLLOW and FADE on sport markets."
    ),
    "category_whitelist": (
        "Strategy 2026-05-17: comma-separated allowed market categories. "
        "Default 'sports,crypto,macro' — the cohorts with positive "
        "edge per backtest. politics and unknown are blocked."
    ),
    # Strategy upgrade 2026-05-17 — live-match detector (Tier 1 #2+#3).
    # Master gate + volume-spike threshold for src/economics/
    # live_match_detector.is_live_match. The predicate is consulted by
    # BOTH confidence_engine.evaluate AND paper_trader.open_trade (defense
    # in depth). Flipping the master gate to False keeps the predicate
    # running (dashboard counters still tally) but stops it from
    # short-circuiting trades.
    "live_match_block_enabled": (
        "Strategy 2026-05-17 (Tier 1 #2+#3): master gate for the "
        "live-match detector. When True, signals on markets matching "
        "is_live_match() are rejected from BOTH confidence_engine.evaluate "
        "and paper_trader.open_trade with reason "
        "live_match_blocked|signal=<reason>. Default True."
    ),
    "live_match_volume_threshold": (
        "Strategy 2026-05-17 (Tier 1 #3): USDC threshold for the volume-"
        "spike heuristic inside the live-match detector. Only applied "
        "when category='sports'. Default 50000."
    ),
    # Strategy upgrade 2026-05-17 round 3 (quick-win patch) — book-wall
    # guard. Reject open_trade when (best_ask - best_bid) >= this value.
    # Post-mortem evidence: the 11 -97% losses all opened on books with
    # spread >= 0.50 (binary pre-resolution wall). Default 0.50, runtime-
    # tunable so the operator can tighten during incident response.
    "book_wall_max_spread": (
        "Strategy 2026-05-17 round 3 (quick-win): maximum allowed bid-ask "
        "spread (in price units, 0..1) for open_trade. Rejects entries "
        "into pre-resolution binary walls. Default 0.50."
    ),
    # Strategy upgrade 2026-05-17 round 3 (quick-win patch) — cold-start
    # floor. Hard floor on (internal_resolved + external_resolved) before
    # ANY signal from a leader is accepted. Catches zero-history leaders
    # that slipped past tier-specific gates. Default 5.
    "min_leader_total_resolved": (
        "Strategy 2026-05-17 round 3 (quick-win): minimum combined "
        "(internal + external) resolved positions for a leader before any "
        "FOLLOW/FADE signal fires. Default 5."
    ),
    # ── Audit 2026-05-17 (QW2) — whitelist for audit-introduced constants ─
    # Logs showed `runtime_config: legacy hash key '...' not in ALLOWED_KEYS
    # — dropped` for these knobs whenever an operator HSET them on Redis
    # (the dashboard cockpit silently ignored them). Adding them here
    # restores both the dashboard write path AND the legacy hand-edit
    # path. Bounds chosen so the loosest sensible value is reachable but
    # accidental typos (negative, 100x) are rejected.
    "min_hours_to_resolution_follow": (
        "Audit 2026-05-17 (QW2): minimum hours until market resolution "
        "required to open a FOLLOW trade. Default 6.0. Mirrors "
        "settings.MIN_HOURS_TO_RESOLUTION_FOLLOW."
    ),
    "min_hours_to_resolution_fade": (
        "Audit 2026-05-17 (QW2): minimum hours until market resolution "
        "required to open a FADE trade. Default 6.0. Mirrors "
        "settings.MIN_HOURS_TO_RESOLUTION_FADE."
    ),
    "max_book_age_paper_s": (
        "Audit 2026-05-17 (QW2): max acceptable age (seconds) of a "
        "`book:last:*` cache entry before paper_trader._get_book_quote "
        "rejects it. Default 60.0. Mirrors settings.MAX_BOOK_AGE_PAPER_S."
    ),
    "max_leader_price_drift": (
        "Audit 2026-05-17 (QW2): max allowed drift between leader's "
        "signal price and the bot's actual entry ask. Default 0.35. "
        "Mirrors settings.MAX_LEADER_PRICE_DRIFT."
    ),
    "preclose_hours_before_resolution": (
        "Audit 2026-05-17 (QW2): how many hours before market resolution "
        "to force-close open trades to avoid the indeterminate-outcome "
        "deferral path. Default 0.25 (15 min). Set to 0 to disable."
    ),
    "max_trade_return_ratio": (
        "Audit 2026-05-17 (QW2): single-trade return ratio above which "
        "the close is logged to `paper:audit:suspicious_close`. Default "
        "5.0 (500%). Mirrors settings.MAX_TRADE_RETURN_RATIO."
    ),
    "monitor_tick_s": (
        "Audit 2026-05-17 (QW2): default monitor loop cadence (seconds) "
        "when no open trades are within URGENT_MONITOR_HOURS of "
        "resolution. Default 60.0. Mirrors settings.MONITOR_TICK_S."
    ),
    "urgent_monitor_tick_s": (
        "Audit 2026-05-17 (QW2): tightened monitor cadence (seconds) "
        "when any open trade is near its market's resolution. Default "
        "5.0. Mirrors settings.URGENT_MONITOR_TICK_S."
    ),
    "urgent_monitor_hours": (
        "Audit 2026-05-17 (QW2): hours-until-resolution threshold that "
        "tips the monitor loop from default to urgent cadence. Default "
        "1.0. Mirrors settings.URGENT_MONITOR_HOURS."
    ),
    "min_leader_resolved_for_follow": (
        "Strategy 2026-05-17: minimum positions_resolved on a leader "
        "before FOLLOW signals are accepted. Default 30."
    ),
    "min_leader_resolved_for_fade": (
        "Strategy 2026-05-17: minimum positions_resolved on a leader "
        "before FADE signals are accepted. Default 30."
    ),
    "min_leader_winrate_for_follow": (
        "Strategy 2026-05-17: minimum posterior win-rate (Beta mean of "
        "accuracy.overall) for FOLLOW to fire. Default 0.55. Does NOT "
        "apply to FADE (which intentionally targets losing leaders)."
    ),
    # Strategy upgrade 2026-05-17 round 2 — Falcon prior + tiers.
    # Lever B: discount applied to externally-reported (Falcon Wallet
    # 360) winning_trades / losing_trades / total_trades counts when
    # they get fused into the Bayesian gate. 0.5 = trust internal 2×
    # more than external. Lower values lean on internal observations
    # (matures slower); higher values lean on Falcon (faster cold-start
    # at the cost of reporting bias).
    "falcon_external_discount": (
        "Strategy 2026-05-17 round 2: discount applied to Falcon Wallet "
        "360 winning_trades / losing_trades when fused into the "
        "effective_resolved/effective_winrate posterior. Default 0.5 "
        "(internal 2× weight)."
    ),
    # Lever C: tier-specific gates. Tier A leaders (Falcon-validated)
    # trade earlier with a softer winrate floor; Tier C (no
    # validation) keeps the existing strict gate.
    "tier_a_min_resolved": (
        "Strategy 2026-05-17 round 2: minimum effective_resolved for "
        "Tier A (Falcon-validated) leaders. Default 10."
    ),
    "tier_a_min_winrate": (
        "Strategy 2026-05-17 round 2: minimum effective_winrate for "
        "Tier A FOLLOW. Default 0.50."
    ),
    "tier_b_min_resolved": (
        "Strategy 2026-05-17 round 2: minimum effective_resolved for "
        "Tier B leaders. Default 20."
    ),
    "tier_b_min_winrate": (
        "Strategy 2026-05-17 round 2: minimum effective_winrate for "
        "Tier B FOLLOW. Default 0.55."
    ),
    "tier_c_min_resolved": (
        "Strategy 2026-05-17 round 2: minimum effective_resolved for "
        "Tier C (cold-start, no validation) leaders. Default 30. This "
        "mirrors the legacy min_leader_resolved_for_follow knob."
    ),
    "tier_c_min_winrate": (
        "Strategy 2026-05-17 round 2: minimum effective_winrate for "
        "Tier C FOLLOW. Default 0.55."
    ),
    "tier_a_falcon_threshold": (
        "Strategy 2026-05-17 round 2: minimum falcon_score to qualify "
        "for Tier A. Default 50.0."
    ),
    "tier_b_falcon_threshold": (
        "Strategy 2026-05-17 round 2: minimum falcon_score to qualify "
        "for Tier B. Default 20.0."
    ),
    "tier_a_follower_count": (
        "Strategy 2026-05-17 round 2: minimum confirmed_followers (OR "
        "with falcon_score) to qualify for Tier A. Default 5."
    ),
    "tier_b_follower_count": (
        "Strategy 2026-05-17 round 2: minimum confirmed_followers (OR "
        "with falcon_score) to qualify for Tier B. Default 3."
    ),
}

# Inclusive (min, max) bounds for each editable key. Writes outside the
# bounds are rejected with a 400 from the API endpoint.
BOUNDS: dict[str, tuple[float, float]] = {
    "risk_per_trade_pct": (0.001, 0.10),
    "max_total_exposure_pct": (0.01, 0.50),
    "kelly_fraction": (0.05, 1.0),
    "max_drawdown_stop_pct": (0.05, 0.50),
    "min_signal_strength": (0.0, 1.0),
    "max_concurrent_positions": (1, 100),
    "cooldown_seconds": (0, 86400),
    "max_consecutive_losses": (1, 50),
    "max_recent_losses_per_market": (1, 50),
    "fade_size_ratio": (0.1, 2.0),
    # Boolean coerced through 0/1 numeric bounds — see set_overrides
    # coercion block below where keys ending in '_enabled' use the
    # boolean-coerce path. Stored as 0.0 / 1.0 in the JSON blob.
    "strategy_conditional_confidence_enabled": (0.0, 1.0),
    # Round 9 — bounds for both new keys.
    "volume_anticipation_enabled": (0.0, 1.0),
    # Threshold lower bound = MIN_POSITION_USDC; upper bound is a
    # reasonable ceiling that catches accidental typos (1M).
    "volume_anticipation_threshold_usdc": (50.0, 1_000_000.0),
    # Round 10 — boolean flag (coerced to {0, 1}).
    "causal_gating_enabled": (0.0, 1.0),
    # Round 7 — boolean flag (coerced to {0, 1}).
    "prefill_live_enabled": (0.0, 1.0),
    # Strategy upgrade 2026-05-17 — Phase 3 cohort filters.
    "min_entry_price": (0.0, 1.0),
    "max_entry_price": (0.0, 1.0),
    # 60s lower bound (so we never close instantly), 30d upper bound (TIMEOUT_DAYS).
    "max_holding_period_s": (60, 30 * 86_400),
    # Sport-specific safety net. 60s floor mirrors the non-sport cap
    # (we never want to close instantly); 6h ceiling matches the FOLLOW
    # min-runway gate — any sport position held > 6h is by definition
    # not the live-match scenario this filter targets.
    "sport_max_holding_s": (60, 6 * 3_600),
    # 0.5% floor (slippage + fee covers most no-news ticks) to 30% ceiling
    # (anything wider effectively disables the stop and reintroduces the
    # pre-fix -97% loss). Default 3% lives mid-range, validated by the
    # paper-trader audit log on Tier 1 sport losses.
    "stop_loss_sport": (0.005, 0.30),
    "min_leader_resolved_for_follow": (0, 10_000),
    "min_leader_resolved_for_fade": (0, 10_000),
    "min_leader_winrate_for_follow": (0.0, 1.0),
    # Strategy upgrade 2026-05-17 round 2 — Falcon prior + tier knobs.
    # falcon_external_discount in (0, 1] — 0 disables the prior, 1
    # weights external equal to internal. We bound at 0.0..1.0
    # (inclusive) so the operator can shut it off entirely.
    "falcon_external_discount": (0.0, 1.0),
    # Tier resolved gates have the same envelope as the legacy
    # min_leader_resolved_* knob; bounds are deliberately wide so the
    # operator can tighten (50+) or loosen (0) as the data evolves.
    "tier_a_min_resolved": (0, 10_000),
    "tier_a_min_winrate": (0.0, 1.0),
    "tier_b_min_resolved": (0, 10_000),
    "tier_b_min_winrate": (0.0, 1.0),
    "tier_c_min_resolved": (0, 10_000),
    "tier_c_min_winrate": (0.0, 1.0),
    # Tier-A/B falcon-score thresholds. Falcon scores observed in
    # production sit in [0, 200] so 1000 is a comfortable upper
    # bound that catches typos.
    "tier_a_falcon_threshold": (0.0, 1000.0),
    "tier_b_falcon_threshold": (0.0, 1000.0),
    # Confirmed-follower counts. We bound 0..1000; in production the
    # follower-edge population caps near 200 confirmed per leader.
    "tier_a_follower_count": (0, 1000),
    "tier_b_follower_count": (0, 1000),
    # `category_whitelist` is a STRING — handled separately in
    # set_overrides (see STRING_KEYS below); no numeric bound to enforce.
    # Strategy upgrade 2026-05-17 — live-match detector knobs.
    "live_match_block_enabled": (0.0, 1.0),
    # Volume threshold: lower bound = 0 (disabling the spike signal),
    # upper bound = $10M (catches accidental decimal-point typos).
    "live_match_volume_threshold": (0.0, 10_000_000.0),
    # Strategy upgrade 2026-05-17 round 3 (quick-win) — book-wall +
    # cold-start floor. Spread bounds are [0.01, 1.0]: 0.01 still permits
    # ordinary fills, 1.0 effectively disables the gate. Cold-start floor
    # bounds [0, 1000]: 0 disables the gate, 1000 is a sanity ceiling.
    "book_wall_max_spread": (0.01, 1.0),
    "min_leader_total_resolved": (0, 1000),
    # ── Audit 2026-05-17 (QW2) — bounds for audit-introduced constants ─
    # Hours-to-resolution gates: floor at 1h (lower than this kills FOLLOW
    # entirely), ceiling at 168h (7d — anything wider is effectively no
    # gate, since most leader swings close within 48h).
    "min_hours_to_resolution_follow": (1.0, 168.0),
    "min_hours_to_resolution_fade": (1.0, 168.0),
    # Book staleness cap: floor 5s (anything tighter trashes legitimate
    # fresh quotes), ceiling 600s (10 min — beyond that you're trading
    # blind, which is the whole point of this gate).
    "max_book_age_paper_s": (5.0, 600.0),
    # Drift gate is a ratio in [0, 1] but we cap at 0.5: 0.5 = "the bot
    # filled at a price differing from the leader's by 50%", which is
    # already pathological. Floor 0.05 = strictest sane setting.
    "max_leader_price_drift": (0.05, 0.5),
    # Preclose: 0 disables, 4h ceiling (any wider and we're closing well
    # ahead of resolution and losing the legitimate hold-to-resolution
    # tail-bet edge case).
    "preclose_hours_before_resolution": (0.0, 4.0),
    # Suspicious-close return cap: floor 2x (anything below this catches
    # legitimate big swings), ceiling 100x (any wider effectively
    # disables the audit signal).
    "max_trade_return_ratio": (2.0, 100.0),
    # Monitor cadence: tick floors at 1s (avoid CPU spin), ceiling 300s
    # (5 min — wider and we miss too many entry/exit windows). Urgent
    # cadence has tighter bounds because it runs in the last 60 min
    # before resolution.
    "monitor_tick_s": (1.0, 300.0),
    "urgent_monitor_tick_s": (1.0, 60.0),
    # Urgent-window threshold: 0.1h = 6 min (tightest sensible bound for
    # the heartbeat-vs-tick race), 6h ceiling matches MIN_HOURS_TO_RESOLUTION
    # so we never run urgent for an entire FOLLOW runway.
    "urgent_monitor_hours": (0.1, 6.0),
}

# Keys that store booleans (not floats). set_overrides coerces these
# through Python's standard truthy/falsy semantics so {"...": True} and
# {"...": "true"} and {"...": 1} all land as the same boolean override.
BOOLEAN_KEYS: frozenset[str] = frozenset({
    "strategy_conditional_confidence_enabled",
    # Round 9 — volume_anticipation gate.
    "volume_anticipation_enabled",
    # Round 10 — causal gating flag.
    "causal_gating_enabled",
    # Round 7 — mempool intent router live-firing gate.
    "prefill_live_enabled",
    # Strategy upgrade 2026-05-17 — live-match detector master gate.
    "live_match_block_enabled",
})

# Keys that store free-form strings (e.g. CSV lists). set_overrides
# accepts them as-is after `.strip()`, skipping numeric coercion AND
# bounds-checking. Whitelist them explicitly so a typo can't silently
# add a string key under a numeric-only contract.
STRING_KEYS: frozenset[str] = frozenset({
    # Strategy upgrade 2026-05-17 — comma-separated category whitelist.
    "category_whitelist",
})

# Keys that store integers. set_overrides coerces these via int(v) so a
# JSON-decoded float (e.g. 30.0 from the dashboard form) lands as 30.
INTEGER_KEYS: frozenset[str] = frozenset({
    "max_concurrent_positions",
    "cooldown_seconds",
    "max_consecutive_losses",
    "max_recent_losses_per_market",
    # Strategy upgrade 2026-05-17 — Phase 3 integer knobs.
    "max_holding_period_s",
    # Sport-specific holding cap (Tier 1 #4) — stored as int seconds.
    "sport_max_holding_s",
    "min_leader_resolved_for_follow",
    "min_leader_resolved_for_fade",
    # Strategy upgrade 2026-05-17 round 2 — tier resolved + follower
    # gates land as integers (counts).
    "tier_a_min_resolved",
    "tier_b_min_resolved",
    "tier_c_min_resolved",
    "tier_a_follower_count",
    "tier_b_follower_count",
    # Strategy upgrade 2026-05-17 round 3 (quick-win) — cold-start floor
    # is a count of positions.
    "min_leader_total_resolved",
})

REDIS_KEY = "runtime_config:overrides"
# Legacy / operator-facing hash key. The dashboard API writes the JSON
# blob under ``REDIS_KEY``; operators editing Redis by hand sometimes
# reach for a hash (HSET runtime_config:risk <field> <value>). The
# 2026-05-17 incident (paper_trade #25 opened on sports despite
# ``category_whitelist=crypto,macro`` being HSET 7 min before) traced
# to readers ignoring this surface. We now merge it underneath the
# JSON overrides so whichever surface the operator hits propagates
# within ``_CACHE_TTL_S`` seconds.
REDIS_LEGACY_HASH_KEY = "runtime_config:risk"
REDIS_PUBSUB_CHANNEL = "runtime_config:changed"
# 2026-05-17 fix: dropped 30 s → 5 s after the incident above. A risk
# knob change must take effect on the next signal eval, not 30 s later
# (the bot fires several decisions per minute on a hot leader). The
# pub/sub push-invalidation still drops latency to <100 ms when the
# operator uses the dashboard; the 5 s ceiling is the worst-case bound
# for hand-edited Redis keys that bypass the pub/sub channel.
_CACHE_TTL_S = 5.0


@dataclass
class _CachedOverrides:
    values: dict[str, Any]
    fetched_at: float


def _defaults_from_settings() -> dict[str, Any]:
    return {
        "risk_per_trade_pct": float(getattr(settings, "MAX_POSITION_PCT", 0.02)),
        "max_total_exposure_pct": float(getattr(settings, "MAX_MARKET_EXPOSURE_PCT", 0.25)),
        "kelly_fraction": float(getattr(settings, "KELLY_FRACTION", 0.5)),
        "max_drawdown_stop_pct": float(getattr(settings, "MAX_DRAWDOWN_STOP_PCT", 0.20)),
        # Strategy upgrade 2026-05-17: dedicated MIN_SIGNAL_STRENGTH
        # default replaces the FADE_MIN_CONFIDENCE fallback. The two
        # knobs serve different purposes: FADE_MIN_CONFIDENCE is the
        # error-model confidence gate for FADE only, while
        # min_signal_strength is the post-Thompson confidence floor for
        # BOTH FOLLOW and FADE.
        "min_signal_strength": float(getattr(settings, "MIN_SIGNAL_STRENGTH", 0.30)),
        "max_concurrent_positions": int(getattr(settings, "MAX_CONCURRENT_POSITIONS", 10)),
        "cooldown_seconds": int(getattr(settings, "PAPER_REENTRY_COOLDOWN_S", 300)),
        "max_consecutive_losses": int(getattr(settings, "MAX_CONSECUTIVE_LOSSES", 5)),
        "max_recent_losses_per_market": int(getattr(settings, "MAX_RECENT_LOSSES_PER_MARKET", 3)),
        "fade_size_ratio": float(getattr(settings, "FADE_SIZE_RATIO", 0.5)),
        # Round 8 — default OFF until operator flips it after A/B passes.
        "strategy_conditional_confidence_enabled": False,
        # Round 9 — default OFF until operator flips after 7 nights of
        # clean shadow fits + MAPE < 30% + Sharpe ≥ 1.3× baseline.
        "volume_anticipation_enabled": False,
        "volume_anticipation_threshold_usdc": float(
            getattr(settings, "VOLUME_ANTICIPATION_THRESHOLD_USDC", 5000.0)
        ),
        # Round 10 — default OFF until methodology audit + 60-day A/B
        # passes (spec § 6).
        "causal_gating_enabled": False,
        # Strategy upgrade 2026-05-17 — Phase 3 cohort filter defaults.
        "min_entry_price": float(getattr(settings, "MIN_ENTRY_PRICE", 0.40)),
        "max_entry_price": float(getattr(settings, "MAX_ENTRY_PRICE", 0.92)),
        "max_holding_period_s": int(getattr(settings, "MAX_HOLDING_PERIOD_S", 86_400)),
        # Sport-specific safety net (Tier 1 #4+#5). Defaults come from
        # settings so an operator can pin them via env, but the dashboard
        # cockpit can also flip them at runtime for incident response.
        "sport_max_holding_s": int(getattr(settings, "SPORT_MAX_HOLDING_S", 1_800)),
        "stop_loss_sport": float(getattr(settings, "STOP_LOSS_SPORT", 0.03)),
        "category_whitelist": str(
            getattr(settings, "CATEGORY_WHITELIST", "sports,crypto,macro")
        ),
        # Strategy upgrade 2026-05-17 — live-match detector defaults.
        "live_match_block_enabled": bool(
            getattr(settings, "LIVE_MATCH_BLOCK_ENABLED", True)
        ),
        "live_match_volume_threshold": float(
            getattr(settings, "LIVE_MATCH_VOLUME_THRESHOLD", 50_000.0)
        ),
        "min_leader_resolved_for_follow": int(
            getattr(settings, "MIN_LEADER_RESOLVED_FOR_FOLLOW", 30)
        ),
        "min_leader_resolved_for_fade": int(
            getattr(settings, "MIN_LEADER_RESOLVED_FOR_FADE", 30)
        ),
        "min_leader_winrate_for_follow": float(
            getattr(settings, "MIN_LEADER_WINRATE_FOR_FOLLOW", 0.55)
        ),
        # Strategy upgrade 2026-05-17 round 2 — Falcon prior + tiers.
        "falcon_external_discount": float(
            getattr(settings, "FALCON_EXTERNAL_DISCOUNT", 0.5)
        ),
        "tier_a_min_resolved": int(getattr(settings, "TIER_A_MIN_RESOLVED", 10)),
        "tier_a_min_winrate": float(getattr(settings, "TIER_A_MIN_WINRATE", 0.50)),
        "tier_b_min_resolved": int(getattr(settings, "TIER_B_MIN_RESOLVED", 20)),
        "tier_b_min_winrate": float(getattr(settings, "TIER_B_MIN_WINRATE", 0.55)),
        "tier_c_min_resolved": int(getattr(settings, "TIER_C_MIN_RESOLVED", 30)),
        "tier_c_min_winrate": float(getattr(settings, "TIER_C_MIN_WINRATE", 0.55)),
        "tier_a_falcon_threshold": float(
            getattr(settings, "TIER_A_FALCON_THRESHOLD", 50.0)
        ),
        "tier_b_falcon_threshold": float(
            getattr(settings, "TIER_B_FALCON_THRESHOLD", 20.0)
        ),
        "tier_a_follower_count": int(getattr(settings, "TIER_A_FOLLOWER_COUNT", 5)),
        "tier_b_follower_count": int(getattr(settings, "TIER_B_FOLLOWER_COUNT", 3)),
        # Strategy upgrade 2026-05-17 round 3 (quick-win) — book-wall +
        # cold-start floor defaults sourced from settings so an operator
        # can pin via env. Dashboard cockpit can flip at runtime.
        "book_wall_max_spread": float(
            getattr(settings, "BOOK_WALL_MAX_SPREAD", 0.50)
        ),
        "min_leader_total_resolved": int(
            getattr(settings, "MIN_LEADER_TOTAL_RESOLVED", 5)
        ),
        # ── Audit 2026-05-17 (QW2) — audit-introduced runtime knobs ────
        # Defaults sourced from settings so an env override (via .env)
        # still wins on first boot; the dashboard cockpit can then flip
        # them at runtime without an engine redeploy.
        "min_hours_to_resolution_follow": float(
            getattr(settings, "MIN_HOURS_TO_RESOLUTION_FOLLOW", 6.0)
        ),
        "min_hours_to_resolution_fade": float(
            getattr(settings, "MIN_HOURS_TO_RESOLUTION_FADE", 6.0)
        ),
        "max_book_age_paper_s": float(
            getattr(settings, "MAX_BOOK_AGE_PAPER_S", 60.0)
        ),
        "max_leader_price_drift": float(
            getattr(settings, "MAX_LEADER_PRICE_DRIFT", 0.35)
        ),
        "preclose_hours_before_resolution": float(
            getattr(settings, "PRECLOSE_HOURS_BEFORE_RESOLUTION", 0.25)
        ),
        "max_trade_return_ratio": float(
            getattr(settings, "MAX_TRADE_RETURN_RATIO", 5.0)
        ),
        "monitor_tick_s": float(getattr(settings, "MONITOR_TICK_S", 60.0)),
        "urgent_monitor_tick_s": float(
            getattr(settings, "URGENT_MONITOR_TICK_S", 5.0)
        ),
        "urgent_monitor_hours": float(
            getattr(settings, "URGENT_MONITOR_HOURS", 1.0)
        ),
    }


def _coerce_legacy_hash(
    raw: dict[Any, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    """Convert HGETALL output to a typed overrides dict.

    Redis hash values are always strings (or bytes when ``decode_responses
    =False``). The dashboard JSON path stores typed values; the legacy
    hash path needs explicit coercion so an HSET'd ``category_whitelist
    crypto,macro`` lands as ``str`` and ``max_consecutive_losses 3``
    lands as ``int`` (matching the dashboard contract).

    Coercion rules (in order):
      * key not in ``ALLOWED_KEYS`` → DROP + warn (typo guard, same
        guarantee as ``set_overrides``).
      * key in ``BOOLEAN_KEYS`` → truthy literals {true, 1, yes, on}.
      * key in ``STRING_KEYS`` → ``str(v).strip()``.
      * key in ``INTEGER_KEYS`` → ``int(float(v))`` (tolerates "30.0").
      * else (numeric / float) → ``float(v)``.
      * Coercion failure → DROP + warn (never raise — a malformed
        hand-edit must not break the engine's read path).
    """
    out: dict[str, Any] = {}
    for k_raw, v_raw in raw.items():
        key = k_raw.decode("utf-8") if isinstance(k_raw, (bytes, bytearray)) else str(k_raw)
        if key not in ALLOWED_KEYS:
            logger.warning(
                f"runtime_config: legacy hash key {key!r} not in "
                "ALLOWED_KEYS — dropped"
            )
            continue
        if isinstance(v_raw, (bytes, bytearray)):
            v = v_raw.decode("utf-8")
        else:
            v = v_raw
        try:
            if key in BOOLEAN_KEYS:
                if isinstance(v, bool):
                    coerced: Any = bool(v)
                elif isinstance(v, (int, float)):
                    coerced = bool(int(v))
                else:
                    coerced = str(v).strip().lower() in {"true", "1", "yes", "on"}
            elif key in STRING_KEYS:
                coerced = str(v).strip()
            elif key in INTEGER_KEYS:
                coerced = int(float(v))
            else:
                coerced = float(v)
        except (TypeError, ValueError) as exc:
            logger.warning(
                f"runtime_config: legacy hash key {key!r} value {v!r} "
                f"failed coercion ({exc}) — dropped"
            )
            continue
        out[key] = coerced
    return out


class RuntimeConfig:
    """Singleton — instantiate once at app startup via ``init_runtime_config``."""

    def __init__(self, redis_client: Any | None = None):
        self._redis = redis_client
        self._cache: _CachedOverrides | None = None
        self._lock = asyncio.Lock()
        # Hydrate the in-memory cache synchronously with defaults so callers
        # that hit the singleton before the first Redis fetch don't get
        # KeyError on ``effective()``.
        self._defaults = _defaults_from_settings()
        # Phase 2 Task D: subscribe to runtime_config:changed so dashboard
        # edits invalidate the local cache within milliseconds rather
        # than the 30s TTL. Audit Red Flag #6 called this out — the
        # channel existed (set_overrides publishes on every write) but
        # nothing consumed it. We build the Subscriber lazily so unit
        # tests that exercise the bootless fallback (`redis_client=None`)
        # don't open a TCP connection.
        self._subscriber: Subscriber | None = None

    async def effective(self) -> dict[str, Any]:
        """Return defaults merged with persisted overrides (overrides win)."""
        overrides = await self._load_overrides()
        merged = {**self._defaults, **overrides}
        return merged

    async def get(self, key: str) -> Any:
        snap = await self.effective()
        return snap.get(key)

    async def _load_overrides(self) -> dict[str, Any]:
        # Cheap path: in-memory cache.
        now = time.monotonic()
        if self._cache is not None and (now - self._cache.fetched_at) < _CACHE_TTL_S:
            return self._cache.values

        async with self._lock:
            # Re-check after taking the lock (another coroutine may have refreshed).
            if self._cache is not None and (time.monotonic() - self._cache.fetched_at) < _CACHE_TTL_S:
                return self._cache.values

            # Merge order (highest precedence last):
            #   1. legacy hash at REDIS_LEGACY_HASH_KEY (HGETALL)
            #   2. JSON blob at REDIS_KEY (set_overrides)
            # The dashboard writes to (2); hand-edits via
            # ``redis-cli HSET runtime_config:risk ...`` land in (1).
            # See REDIS_LEGACY_HASH_KEY docstring + 2026-05-17 incident.
            values: dict[str, Any] = {}
            if self._redis is not None:
                # (1) Legacy hash — best-effort, types coerced from
                # string against the defaults map.
                try:
                    hash_raw = await self._redis.hgetall(REDIS_LEGACY_HASH_KEY)
                    if hash_raw:
                        legacy = _coerce_legacy_hash(hash_raw, self._defaults)
                        if legacy:
                            values.update(legacy)
                            logger.debug(
                                f"runtime_config: legacy hash overrides "
                                f"applied keys={sorted(legacy)}"
                            )
                except Exception as exc:
                    logger.warning(
                        f"runtime_config: legacy hash load failed: {exc}"
                    )
                # (2) JSON blob — dashboard authoritative path.
                try:
                    raw = await self._redis.get(REDIS_KEY)
                    if raw:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            values.update(parsed)
                except Exception as exc:
                    logger.warning(f"runtime_config: redis load failed: {exc}")
            self._cache = _CachedOverrides(values=values, fetched_at=time.monotonic())
            return values

    async def set_overrides(
        self,
        edits: dict[str, Any],
        actor: str = "api",
    ) -> dict[str, Any]:
        """Validate, persist, broadcast. Returns the merged effective config."""
        clean: dict[str, Any] = {}
        rejected: list[str] = []
        for k, v in (edits or {}).items():
            if k not in ALLOWED_KEYS:
                rejected.append(f"{k}: not in ALLOWED_KEYS")
                continue
            try:
                # Coerce booleans (R8 strategy gate, future flags...).
                if k in BOOLEAN_KEYS:
                    if isinstance(v, bool):
                        coerced: Any = bool(v)
                    elif isinstance(v, (int, float)):
                        coerced = bool(int(v))
                    elif isinstance(v, str):
                        coerced = v.strip().lower() in {"true", "1", "yes", "on"}
                    else:
                        raise TypeError(f"cannot coerce {v!r} to bool")
                # Free-form strings (e.g. category whitelist CSV).
                elif k in STRING_KEYS:
                    if not isinstance(v, str):
                        raise TypeError(f"expected str for {k}, got {type(v).__name__}")
                    coerced = v.strip()
                # Coerce ints.
                elif k in INTEGER_KEYS:
                    coerced = int(v)
                # Default: float.
                else:
                    coerced = float(v)
            except (TypeError, ValueError):
                rejected.append(f"{k}: not numeric ({v!r})")
                continue
            # Boolean keys bypass bounds — they're already {0,1}. String
            # keys have no numeric bounds.
            if k not in BOOLEAN_KEYS and k not in STRING_KEYS:
                lo, hi = BOUNDS[k]
                if coerced < lo or coerced > hi:
                    rejected.append(f"{k}: {coerced} outside [{lo}, {hi}]")
                    continue
            clean[k] = coerced
        if not clean:
            raise ValueError("No valid edits. Rejected: " + "; ".join(rejected))

        # Merge over existing overrides so partial updates don't wipe the others.
        async with self._lock:
            existing = dict(self._cache.values) if self._cache else {}
            if self._redis is not None and not self._cache:
                try:
                    raw = await self._redis.get(REDIS_KEY)
                    if raw:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        existing = json.loads(raw) or {}
                except Exception:
                    existing = {}
            existing.update(clean)
            payload = json.dumps(existing)
            if self._redis is not None:
                try:
                    await self._redis.set(REDIS_KEY, payload)
                    await self._redis.publish(
                        REDIS_PUBSUB_CHANNEL,
                        json.dumps({"actor": actor, "edits": clean, "ts": time.time()}),
                    )
                except Exception as exc:
                    logger.warning(f"runtime_config: redis persist failed: {exc}")
            self._cache = _CachedOverrides(values=existing, fetched_at=time.monotonic())

        logger.info(f"runtime_config: {actor} updated {clean} (rejected: {rejected or 'none'})")
        return await self.effective()

    def invalidate_cache(self) -> None:
        """Force the next ``effective()`` call to re-fetch from Redis. Used by
        the pub/sub listener so other services pick up changes within seconds."""
        self._cache = None

    # ── Pub/sub push-invalidation ────────────────────────────────────────
    # The audit (Red Flag #6) noted that ``set_overrides`` already
    # publishes on ``runtime_config:changed`` but no one subscribed —
    # readers stayed on the 30s in-memory cache. Calling ``start_pubsub``
    # at process boot wires a reconnect-safe subscriber that invalidates
    # the cache on every publish, dropping propagation to <100ms.
    async def start_pubsub(self) -> None:
        """Subscribe to ``runtime_config:changed`` and invalidate on every flip.

        Safe to call multiple times: subsequent calls are no-ops. Safe to
        skip entirely — the 30s TTL still bounds staleness either way.
        """
        if self._subscriber is not None:
            return
        if self._redis is None:
            # Bootless fallback; nothing to subscribe to. The next call
            # to ``effective()`` will still hit defaults-only.
            return
        sub = Subscriber(settings.REDIS_URL, name="control.runtime_config")
        sub.register(REDIS_PUBSUB_CHANNEL, self._on_changed)
        # Reuse the wired redis client so test rigs using a shared
        # fakeredis instance see the same pub/sub graph as the publisher.
        await sub.start(redis_client=self._redis)
        self._subscriber = sub
        logger.info(
            f"RuntimeConfig: subscribed to {REDIS_PUBSUB_CHANNEL} "
            "for push-invalidation"
        )

    async def stop_pubsub(self) -> None:
        if self._subscriber is None:
            return
        await self._subscriber.stop()
        self._subscriber = None

    async def _on_changed(self, payload: Any, _channel: str) -> None:
        """Handler for ``runtime_config:changed``. Just invalidates the cache.

        We deliberately do NOT re-load synchronously here: the next
        consumer call to ``effective()`` will see ``self._cache is None``
        and refresh from Redis. That keeps the handler dirt-simple and
        thread-safe (the lock is in ``_load_overrides``).
        """
        try:
            edits = (
                payload.get("edits") if isinstance(payload, dict) else None
            )
        except Exception:
            edits = None
        self._cache = None
        logger.debug(
            f"RuntimeConfig: cache invalidated via pub/sub (edits={edits})"
        )


# ── Singleton wiring ─────────────────────────────────────────────────────────
_runtime_config: RuntimeConfig | None = None


def init_runtime_config(redis_client: Any | None = None) -> RuntimeConfig:
    global _runtime_config
    _runtime_config = RuntimeConfig(redis_client=redis_client)
    return _runtime_config


def get_runtime_config() -> RuntimeConfig:
    if _runtime_config is None:
        # Bootless fallback: defaults-only, no Redis. Useful for unit tests
        # and for very early reads during app startup.
        return RuntimeConfig(redis_client=None)
    return _runtime_config
