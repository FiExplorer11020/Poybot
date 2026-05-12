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
})

REDIS_KEY = "runtime_config:overrides"
REDIS_PUBSUB_CHANNEL = "runtime_config:changed"
_CACHE_TTL_S = 30.0


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
        "min_signal_strength": float(getattr(settings, "FADE_MIN_CONFIDENCE", 0.65)),
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
    }


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

            values: dict[str, Any] = {}
            if self._redis is not None:
                try:
                    raw = await self._redis.get(REDIS_KEY)
                    if raw:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        values = json.loads(raw)
                        if not isinstance(values, dict):
                            values = {}
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
                # Coerce ints and floats.
                elif k in {"max_concurrent_positions", "cooldown_seconds",
                         "max_consecutive_losses", "max_recent_losses_per_market"}:
                    coerced = int(v)
                else:
                    coerced = float(v)
            except (TypeError, ValueError):
                rejected.append(f"{k}: not numeric ({v!r})")
                continue
            # Boolean keys bypass bounds — they're already {0,1}.
            if k not in BOOLEAN_KEYS:
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
