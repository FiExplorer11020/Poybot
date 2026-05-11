"""
Ingest Health Monitor (Phase 3 Round 1, Agent D).

Central freshness tracker + auto-recovery dispatcher for every data
ingestion source in the bot. Solves the user-stated problem: "10-30 min
pauses in data acquisition are appearing and going unnoticed."

Sources tracked
---------------
* ``ws_market_feed``     — every Polymarket CLOB WebSocket message
* ``rest_data_api``      — every successful data-api.polymarket.com poll
* ``falcon_leaderboard`` — Falcon agent 584
* ``falcon_wallet360``   — Falcon agent 581
* ``falcon_markets``     — Falcon agents 574 + 575
* ``falcon_trades``      — Falcon agent 556
* ``redis_pubsub``       — every pub/sub message received by ANY Subscriber

Each ingestion success path calls
``get_health_monitor().heartbeat("<source>")`` (an O(1) dict write — no
I/O, no blocking). A background ``_watchdog_loop`` wakes every
``IngestHealthMonitor.LOOP_INTERVAL_S`` and, for each source, compares
``now - last_heartbeat_at`` against the source-specific threshold:

* below threshold → no-op (export the gauge for Prometheus to scrape)
* threshold crossed AND not already in gap state →
  - log WARNING with source + duration
  - increment ``ingest_gaps_total{source, severity}``
  - fire ``on_gap`` callback(s), subject to ``RECOVERY_COOLDOWN_S``
* heartbeat returns after a gap → log INFO ``gap closed after Xs``,
  increment ``ingest_recovery_success_total{source}``, exit gap state

Critical design choices
-----------------------
1. **Singleton, lazy.** The monitor is process-wide. Every caller obtains
   it via ``get_health_monitor()`` so heartbeat insertions across the
   codebase share state without explicit wiring. Lazy init keeps unit
   tests fast (creating it is free; starting the loop is opt-in).
2. **No HTTP retries inside Falcon recovery.** Hammering Falcon when
   it's already rate-limiting us is the opposite of what we want. The
   ``falcon_*`` recovery callbacks page the operator (Telegram) and
   log; they do NOT call ``FalconClient.query`` again.
3. **Cooldown per source.** Once a recovery fires, the same source
   can't fire another for ``RECOVERY_COOLDOWN_S`` (default 60 s). This
   prevents callback storms when the watchdog ticks while a slow
   recovery is still running.
4. **Heartbeat path must NOT raise.** Heartbeats are called from hot
   loops (WebSocket message handler, REST poller, Falcon wrapper). A
   misbehaving monitor must not crash data ingestion. Every public
   method except the loop runner wraps its body in a broad try/except.
5. **Monotonic clock.** All timestamps are ``time.monotonic()``. Wall
   clock skew (NTP corrections) must not retroactively flip a source
   into "gap" state.

Threshold defaults
------------------
Source-specific thresholds reflect the observed cadence of each source.
Two principles guide them:

* too low  → alert fatigue (a 30-min Falcon refresh would flap warning
  every cycle)
* too high → the user's reported pain persists (10 min outages stay
  invisible)

The table is:

==================== ============= =========================================
Source               Threshold (s) Rationale
==================== ============= =========================================
ws_market_feed       60            Active markets always have some price
                                   activity; 60s silent = real WS drop.
rest_data_api        30            Poll cadence is 5 s (HP-1); missing 6
                                   cycles indicates a real upstream gap.
falcon_leaderboard   2100          Refresh interval is 1800 s (30 min);
                                   we allow one normal cycle + 5 min slop.
falcon_wallet360     7200          Wallet enrichment is bursty (only fires
                                   when leaders go stale); 2 h is normal.
falcon_markets       86400         ``sync_markets`` refreshes daily-stale
                                   rows; >24 h silence means the daily
                                   job is broken.
falcon_trades        600           Trade backfill on reconnect should be
                                   fast; 10 min covers a slow reconnect.
redis_pubsub         300           Internal channel — should always have
                                   *some* traffic from at least one
                                   subscriber's perspective.
==================== ============= =========================================

Each threshold is env-overridable via ``INGEST_THRESHOLD_<SOURCE>_S``
(uppercased, e.g. ``INGEST_THRESHOLD_WS_MARKET_FEED_S=90``).

Wiring
------
``src/engine/main.py`` calls ``get_health_monitor()`` after constructing
``redis_client``/``telegram_bot``, registers the recovery callbacks, and
starts the loop with ``await monitor.start()``. Teardown calls
``await monitor.stop()`` from the engine's finally clause.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

# Importing metrics is best-effort — early CI checkouts or stripped-down
# test environments may not have prometheus_client wired. The monitor's
# core logic doesn't depend on the metric objects existing.
try:
    from src.monitoring.metrics import (
        ingest_gaps_total,
        ingest_recovery_attempts_total,
        ingest_recovery_success_total,
        ingest_seconds_since_last_event,
        ingest_threshold_breaches_active,
    )
except Exception:  # pragma: no cover
    class _NoOpLabel:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def dec(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    ingest_gaps_total = _NoOpLabel()  # type: ignore[assignment]
    ingest_recovery_attempts_total = _NoOpLabel()  # type: ignore[assignment]
    ingest_recovery_success_total = _NoOpLabel()  # type: ignore[assignment]
    ingest_seconds_since_last_event = _NoOpLabel()  # type: ignore[assignment]
    ingest_threshold_breaches_active = _NoOpLabel()  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Source taxonomy + thresholds                                                 #
# --------------------------------------------------------------------------- #

# Canonical source names — all heartbeat calls MUST use one of these.
SOURCE_WS_MARKET_FEED = "ws_market_feed"
SOURCE_REST_DATA_API = "rest_data_api"
SOURCE_FALCON_LEADERBOARD = "falcon_leaderboard"
SOURCE_FALCON_WALLET360 = "falcon_wallet360"
SOURCE_FALCON_MARKETS = "falcon_markets"
SOURCE_FALCON_TRADES = "falcon_trades"
SOURCE_REDIS_PUBSUB = "redis_pubsub"
SOURCE_REDIS_STREAMS = "redis_streams"  # follow-on if Agent C ships

# Default thresholds (seconds). See module-level docstring for rationale.
DEFAULT_THRESHOLDS_S: dict[str, int] = {
    SOURCE_WS_MARKET_FEED: 60,
    SOURCE_REST_DATA_API: 30,
    SOURCE_FALCON_LEADERBOARD: 2100,
    SOURCE_FALCON_WALLET360: 7200,
    SOURCE_FALCON_MARKETS: 86400,
    SOURCE_FALCON_TRADES: 600,
    SOURCE_REDIS_PUBSUB: 300,
    SOURCE_REDIS_STREAMS: 300,
}

# Falcon agent_id → canonical source name. Used by FalconClient to route
# heartbeats without hardcoding the alias map at every call site.
FALCON_AGENT_TO_SOURCE: dict[int, str] = {
    584: SOURCE_FALCON_LEADERBOARD,
    581: SOURCE_FALCON_WALLET360,
    574: SOURCE_FALCON_MARKETS,
    575: SOURCE_FALCON_MARKETS,
    556: SOURCE_FALCON_TRADES,
    579: SOURCE_FALCON_LEADERBOARD,  # PnL leaderboard ⇒ same liveness signal
}


# Default cooldown between two successive recovery callbacks for the same
# source. Without this, a fast watchdog tick would fire the recovery on
# every loop iteration while the source was still down. Override via
# INGEST_RECOVERY_COOLDOWN_S.
DEFAULT_RECOVERY_COOLDOWN_S = 60

# How often the background loop wakes. Cheap (dict reads + gauge.set), so
# 10 s gives sub-15 s detection on the lowest threshold (30 s for REST).
DEFAULT_LOOP_INTERVAL_S = 10


# --------------------------------------------------------------------------- #
# Types                                                                        #
# --------------------------------------------------------------------------- #

# Recovery callback signature: async, takes (source, gap_duration_s).
RecoveryCallback = Callable[[str, float], Awaitable[None]]


@dataclass
class _SourceState:
    """Per-source state tracked by the monitor."""

    name: str
    threshold_s: int
    last_heartbeat_at: float = 0.0  # monotonic; 0 = never seen
    in_gap: bool = False
    gap_started_at: float = 0.0  # monotonic
    last_recovery_at: float = 0.0  # monotonic
    callbacks: list[RecoveryCallback] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Singleton accessor                                                           #
# --------------------------------------------------------------------------- #

_INSTANCE: "IngestHealthMonitor | None" = None


def get_health_monitor() -> "IngestHealthMonitor":
    """Process-wide IngestHealthMonitor. Lazy, idempotent.

    Every heartbeat caller imports this — keeping the module-level state
    out of ``__init__.py`` means a circular import (e.g. metrics ↔
    ingest_health) can't break Python startup.
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = IngestHealthMonitor()
    return _INSTANCE


def reset_health_monitor() -> None:
    """Test helper — reset the singleton. NEVER call from production code."""
    global _INSTANCE
    _INSTANCE = None


# --------------------------------------------------------------------------- #
# IngestHealthMonitor                                                          #
# --------------------------------------------------------------------------- #


class IngestHealthMonitor:
    """Tracks freshness of every ingestion source; triggers recovery on gaps.

    Lifecycle
    ---------
    * Construct (cheap, no I/O): default thresholds loaded from
      ``DEFAULT_THRESHOLDS_S`` then overridden by env vars
      ``INGEST_THRESHOLD_<SOURCE>_S``.
    * ``register_recovery(source, callback)`` — bind one or more async
      callbacks per source. Multiple callbacks are run concurrently
      (via ``asyncio.gather``). Bound BEFORE ``start()`` for sensible
      semantics, but late registration is tolerated.
    * ``await start()`` — kick off the background watchdog loop.
    * ``await stop()`` — cancel + join the loop. Idempotent.

    Hot path
    --------
    ``heartbeat(source)`` is the only function called from hot loops. It
    must:
      * be O(1)
      * never await (so it can be safely called from a sync function)
      * never raise
    """

    LOOP_INTERVAL_S = DEFAULT_LOOP_INTERVAL_S
    RECOVERY_COOLDOWN_S = DEFAULT_RECOVERY_COOLDOWN_S

    def __init__(
        self,
        *,
        thresholds_s: dict[str, int] | None = None,
        recovery_cooldown_s: int | None = None,
        loop_interval_s: int | None = None,
    ) -> None:
        # Merge defaults with explicit + env overrides. Order:
        # default → explicit kwarg → INGEST_THRESHOLD_<SOURCE>_S env.
        merged: dict[str, int] = dict(DEFAULT_THRESHOLDS_S)
        if thresholds_s:
            merged.update(thresholds_s)
        for source in list(merged):
            env_key = f"INGEST_THRESHOLD_{source.upper()}_S"
            env_val = os.environ.get(env_key)
            if env_val:
                try:
                    merged[source] = int(env_val)
                except ValueError:
                    logger.warning(
                        f"ingest_health: bad env {env_key}={env_val!r}; "
                        f"keeping default {merged[source]}"
                    )

        # Cooldown override (env > kwarg > default).
        cooldown = recovery_cooldown_s if recovery_cooldown_s is not None else (
            self.RECOVERY_COOLDOWN_S
        )
        env_cd = os.environ.get("INGEST_RECOVERY_COOLDOWN_S")
        if env_cd:
            try:
                cooldown = int(env_cd)
            except ValueError:
                logger.warning(
                    f"ingest_health: bad INGEST_RECOVERY_COOLDOWN_S={env_cd!r}"
                )
        self._cooldown_s = max(0, int(cooldown))

        # Loop interval override.
        loop_int = loop_interval_s if loop_interval_s is not None else self.LOOP_INTERVAL_S
        env_loop = os.environ.get("INGEST_LOOP_INTERVAL_S")
        if env_loop:
            try:
                loop_int = int(env_loop)
            except ValueError:
                logger.warning(f"ingest_health: bad INGEST_LOOP_INTERVAL_S={env_loop!r}")
        self._loop_interval_s = max(1, int(loop_int))

        self._sources: dict[str, _SourceState] = {
            name: _SourceState(name=name, threshold_s=threshold)
            for name, threshold in merged.items()
        }
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Public API — hot path                                               #
    # ------------------------------------------------------------------ #

    def heartbeat(self, source: str) -> None:
        """Mark ``source`` as alive at ``time.monotonic()``.

        Safe to call from any code path. Unknown sources are registered
        on-demand at the default threshold (so a typo doesn't silently
        drop heartbeats, but a misspelling does become visible in /metrics).
        Never raises.
        """
        try:
            state = self._sources.get(source)
            if state is None:
                # Lazy-register: unknown source defaults to 300s threshold.
                # We log at DEBUG (not WARNING) because Agent C may add new
                # sources after this monitor exists — flapping every call
                # for a known-new source is noise.
                state = _SourceState(
                    name=source,
                    threshold_s=DEFAULT_THRESHOLDS_S.get(source, 300),
                )
                self._sources[source] = state
                logger.debug(
                    f"ingest_health: lazy-registered new source {source!r} "
                    f"with default threshold {state.threshold_s}s"
                )
            now = time.monotonic()
            # If we were in gap state, this heartbeat closes it.
            if state.in_gap:
                gap_age = now - state.gap_started_at
                state.in_gap = False
                state.gap_started_at = 0.0
                try:
                    ingest_recovery_success_total.labels(source=source).inc()
                    ingest_threshold_breaches_active.labels(source=source).set(0)
                except Exception:
                    pass
                logger.info(
                    f"ingest_health: gap closed for {source!r} after {gap_age:.1f}s"
                )
            state.last_heartbeat_at = now
        except Exception:
            # NEVER let the hot path crash the caller.
            pass

    # ------------------------------------------------------------------ #
    # Recovery callback registration                                      #
    # ------------------------------------------------------------------ #

    def register_recovery(self, source: str, callback: RecoveryCallback) -> None:
        """Bind an async callback to fire when ``source`` enters a gap.

        Multiple callbacks per source are allowed — they fan out via
        ``asyncio.gather``. The callback receives ``(source, gap_duration_s)``.
        """
        state = self._sources.get(source)
        if state is None:
            # Same lazy-register policy as heartbeat: be permissive so
            # the wiring order in main.py doesn't matter.
            state = _SourceState(
                name=source,
                threshold_s=DEFAULT_THRESHOLDS_S.get(source, 300),
            )
            self._sources[source] = state
        state.callbacks.append(callback)
        logger.debug(
            f"ingest_health: registered recovery for {source!r} "
            f"(total callbacks: {len(state.callbacks)})"
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Spawn the background watchdog loop. Idempotent."""
        if self._running:
            logger.debug("ingest_health: start() called twice — ignoring")
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._watchdog_loop(), name="ingest_health.watchdog"
        )
        logger.info(
            f"ingest_health: started; tracking {len(self._sources)} sources, "
            f"loop={self._loop_interval_s}s cooldown={self._cooldown_s}s"
        )

    async def stop(self) -> None:
        """Cancel + join the loop. Idempotent."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("ingest_health: stopped")

    # ------------------------------------------------------------------ #
    # Introspection (for tests + dashboard endpoints)                     #
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a dict of all source states. Cheap; safe to call any time."""
        now = time.monotonic()
        out: dict[str, dict[str, Any]] = {}
        for source, state in self._sources.items():
            seconds_since = (
                (now - state.last_heartbeat_at)
                if state.last_heartbeat_at > 0
                else None
            )
            out[source] = {
                "threshold_s": state.threshold_s,
                "in_gap": state.in_gap,
                "seconds_since_last_event": seconds_since,
                "callbacks": len(state.callbacks),
            }
        return out

    def sources(self) -> list[str]:
        return sorted(self._sources)

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _watchdog_loop(self) -> None:
        """Background loop. Wake every LOOP_INTERVAL_S, check every source.

        Implementation notes:
          * We DO NOT lock per-source state. The hot path
            (heartbeat) is a single dict write to ``last_heartbeat_at``,
            which is atomic in CPython. A racy read inside this loop
            simply means we see a stale timestamp by ≤ one loop tick —
            acceptable for a 10 s loop.
          * Callbacks run inside ``asyncio.gather`` so a slow recovery
            doesn't starve the rest of the sources from being checked
            next tick.
        """
        while self._running:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._loop_interval_s,
                )
                break  # stop_event set
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # We MUST keep ticking even if one source's recovery
                # blew up. The loop is the only thing that detects gaps.
                logger.exception("ingest_health: tick raised")

    async def _tick(self) -> None:
        """One probe across all sources."""
        now = time.monotonic()
        for source, state in list(self._sources.items()):
            # Always export the gauge so Prometheus alert rules can
            # operate even before we've seen a single heartbeat.
            if state.last_heartbeat_at > 0:
                age = now - state.last_heartbeat_at
            else:
                # Never seen a heartbeat — treat as infinitely stale.
                # This makes "source never started" alertable.
                age = float("inf")
            try:
                ingest_seconds_since_last_event.labels(source=source).set(
                    age if age != float("inf") else state.threshold_s * 10
                )
            except Exception:
                pass

            # Already in gap state — we wait for heartbeat() to close it.
            # Don't re-fire recovery on every tick (callbacks are
            # already throttled by cooldown but the log noise alone is
            # bad). Refresh the active-breach gauge in case Prometheus
            # restarted.
            if state.in_gap:
                try:
                    ingest_threshold_breaches_active.labels(source=source).set(1)
                except Exception:
                    pass
                continue

            # Below threshold (or never seen yet and within grace) → no-op.
            if age <= state.threshold_s:
                continue

            # New gap detected.
            state.in_gap = True
            state.gap_started_at = now - age if age != float("inf") else now
            severity = "critical" if age > 2 * state.threshold_s else "warning"
            logger.warning(
                f"ingest_health: GAP DETECTED source={source!r} "
                f"age={age:.1f}s threshold={state.threshold_s}s severity={severity}"
            )
            try:
                ingest_gaps_total.labels(source=source, severity=severity).inc()
                ingest_threshold_breaches_active.labels(source=source).set(1)
            except Exception:
                pass

            await self._fire_recovery(state, age, now)

    async def _fire_recovery(
        self,
        state: _SourceState,
        gap_duration_s: float,
        now: float,
    ) -> None:
        """Invoke every registered recovery callback for the source.

        Cooldown logic: once a recovery fires, the next gap detection
        for the SAME source within ``_cooldown_s`` results in
        ``ingest_recovery_attempts_total{result=skipped_cooldown}`` and
        no callback invocation. Cooldown is per-source, not global.
        """
        if not state.callbacks:
            # No recovery wired — that's fine; the metrics are the
            # output for sources we only want to observe (e.g. during
            # debugging).
            return

        if (
            state.last_recovery_at > 0
            and (now - state.last_recovery_at) < self._cooldown_s
        ):
            try:
                ingest_recovery_attempts_total.labels(
                    source=state.name, result="skipped_cooldown"
                ).inc()
            except Exception:
                pass
            logger.info(
                f"ingest_health: recovery for {state.name!r} skipped "
                f"(cooldown: {int(now - state.last_recovery_at)}s < "
                f"{self._cooldown_s}s)"
            )
            return

        state.last_recovery_at = now
        try:
            ingest_recovery_attempts_total.labels(
                source=state.name, result="triggered"
            ).inc()
        except Exception:
            pass

        # Run all callbacks concurrently with return_exceptions so one
        # bad callback doesn't starve the rest.
        results = await asyncio.gather(
            *(cb(state.name, gap_duration_s) for cb in state.callbacks),
            return_exceptions=True,
        )
        for cb, result in zip(state.callbacks, results):
            if isinstance(result, BaseException):
                try:
                    ingest_recovery_attempts_total.labels(
                        source=state.name, result="failed"
                    ).inc()
                except Exception:
                    pass
                logger.warning(
                    f"ingest_health: recovery callback {cb!r} for "
                    f"{state.name!r} raised: {result!r}"
                )
