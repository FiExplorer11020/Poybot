"""
Watchdog (S3.10) — keeps the engine container alive 24/7.

Surveils the long-running coroutines registered by `src/engine/main.py`
(profiler, confidence engine, paper trader, telegram bot) and:

    * detects a crashed asyncio.Task (one that completed before
      stop_event was set, OR whose result is an exception),
    * detects a frozen task whose application-level heartbeat hasn't
      ticked in WATCHDOG_HEARTBEAT_TIMEOUT_S,
    * restarts it via a user-supplied factory, with linear backoff,
    * gives up after WATCHDOG_MAX_RESTARTS consecutive failures, in
      which case it publishes engine:crash AND trips the global
      stop_event so the container exits cleanly (Docker / systemd will
      then restart the whole process — which is the right granularity
      for a multi-component freeze).

Heartbeat protocol:
    * Components write a monotonic timestamp to the Redis key
      `heartbeat:{name}` with TTL = 4 × interval. Watchdog reads.
    * No heartbeat = no liveness signal. We don't restart on missing
      heartbeat alone — we'd flap on slow-starting components — only
      if the timestamp is older than WATCHDOG_HEARTBEAT_TIMEOUT_S.

Why all this rather than "let Docker restart on crash":
    * One slow coroutine inside a healthy process should not require
      tearing down ConfidenceEngine and reloading 200 leader profiles
      from Postgres. Per-component restart preserves the warm state.
    * Watchdog supplies the signal AND the muscle: alerts hit
      Telegram via the existing `engine:crash` channel.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from loguru import logger

from src.config import settings


REDIS_HEARTBEAT_PREFIX = "heartbeat:"
ENGINE_CRASH_CHANNEL = "engine:crash"


# --------------------------------------------------------------------------- #
# Types                                                                        #
# --------------------------------------------------------------------------- #


# Factory that returns a fresh awaitable for the component's main loop.
# The watchdog calls factory() on each (re)start, wraps it in
# asyncio.create_task, and tracks the resulting Task.
ComponentFactory = Callable[[], Awaitable[None]]


@dataclass
class ComponentState:
    name: str
    factory: ComponentFactory
    task: Optional[asyncio.Task] = None
    restart_count: int = 0
    last_restart_at: float = 0.0
    last_started_at: float = 0.0  # for restart-counter forgiveness
    last_failure_reason: Optional[str] = None
    # Optional: if heartbeats are enabled for this component, set the
    # expected interval (seconds). 0 = no heartbeat check.
    heartbeat_interval_s: int = 0
    # Track whether we've already published a crash for the current
    # restart streak so we don't spam Telegram on every retry.
    crash_published: bool = False


# --------------------------------------------------------------------------- #
# Heartbeat helpers (importable by components)                                 #
# --------------------------------------------------------------------------- #


async def write_heartbeat(redis_client, name: str, ttl_s: int = 120) -> None:
    """Components call this from their busy loop to signal liveness.

    Best-effort: a Redis hiccup must not crash the component."""
    try:
        await redis_client.set(
            f"{REDIS_HEARTBEAT_PREFIX}{name}",
            str(time.time()),
            ex=ttl_s,
        )
    except Exception as e:
        logger.debug(f"watchdog: heartbeat write for {name!r} failed: {e}")


async def read_heartbeat(redis_client, name: str) -> Optional[float]:
    """Returns the last heartbeat timestamp (epoch seconds) or None."""
    try:
        raw = await redis_client.get(f"{REDIS_HEARTBEAT_PREFIX}{name}")
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return float(raw)
    except Exception as e:
        logger.debug(f"watchdog: heartbeat read for {name!r} failed: {e}")
        return None


# --------------------------------------------------------------------------- #
# Watchdog                                                                     #
# --------------------------------------------------------------------------- #


class Watchdog:
    """Per-component liveness monitor. Wired into the scheduler via
    `add_interval('watchdog', wd.tick, seconds=WATCHDOG_HEARTBEAT_INTERVAL_S)`."""

    def __init__(
        self,
        *,
        redis_client,
        stop_event: asyncio.Event,
        max_restarts: Optional[int] = None,
        backoff_s: Optional[int] = None,
        heartbeat_timeout_s: Optional[int] = None,
        restart_reset_s: Optional[int] = None,
    ) -> None:
        self._redis = redis_client
        self._stop_event = stop_event
        self._max_restarts = (
            max_restarts if max_restarts is not None else settings.WATCHDOG_MAX_RESTARTS
        )
        self._backoff_s = (
            backoff_s if backoff_s is not None else settings.WATCHDOG_RESTART_BACKOFF_S
        )
        self._heartbeat_timeout_s = (
            heartbeat_timeout_s
            if heartbeat_timeout_s is not None
            else settings.WATCHDOG_HEARTBEAT_TIMEOUT_S
        )
        self._restart_reset_s = (
            restart_reset_s
            if restart_reset_s is not None
            else settings.WATCHDOG_RESTART_RESET_S
        )
        self._components: dict[str, ComponentState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def register(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        heartbeat_interval_s: int = 0,
        autostart: bool = True,
    ) -> None:
        """Register a component and (by default) start its task immediately.

        `factory` is called on every (re)start to get a fresh awaitable —
        do NOT pass an already-awaited coroutine, or the second restart
        will fail with a "coroutine already awaited" error.
        """
        if name in self._components:
            logger.warning(f"watchdog: {name!r} already registered, replacing")
        state = ComponentState(
            name=name,
            factory=factory,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        self._components[name] = state
        if autostart:
            await self._spawn(state)

    def names(self) -> list[str]:
        return sorted(self._components.keys())

    async def stop_all(self) -> None:
        """Cancel every tracked task. Called from main.py finally clause."""
        async with self._lock:
            for state in self._components.values():
                if state.task and not state.task.done():
                    state.task.cancel()
            for state in self._components.values():
                if state.task is not None:
                    try:
                        await state.task
                    except (asyncio.CancelledError, Exception):
                        pass

    # ------------------------------------------------------------------ #
    # Core: probe one tick                                                #
    # ------------------------------------------------------------------ #

    async def tick(self) -> None:
        """Probe every component once. Called by the scheduler. Holds the
        lock for the entire pass so register() and stop_all() don't race."""
        async with self._lock:
            now = time.time()
            for state in list(self._components.values()):
                await self._check_one(state, now)

    async def _check_one(self, state: ComponentState, now: float) -> None:
        # Forgive old restart counters once the component has been stable
        # for a while.
        if (
            state.restart_count > 0
            and state.last_started_at > 0
            and (now - state.last_started_at) >= self._restart_reset_s
            and state.task is not None
            and not state.task.done()
        ):
            logger.info(
                f"watchdog: {state.name!r} stable for "
                f"{int(now - state.last_started_at)}s — resetting restart counter"
            )
            state.restart_count = 0
            state.crash_published = False

        crashed = state.task is None or state.task.done()
        crash_reason: Optional[str] = None

        if state.task is not None and state.task.done():
            try:
                # Force re-raise of the exception (if any) so we can log it.
                exc = state.task.exception()
            except asyncio.CancelledError:
                exc = None
            except Exception as e:  # InvalidStateError, ...
                exc = e
            if exc is not None:
                crash_reason = f"{type(exc).__name__}: {exc}"

        # Heartbeat freeze detection (only for live tasks with heartbeats
        # enabled).
        if (
            not crashed
            and state.heartbeat_interval_s > 0
            and state.task is not None
            and not state.task.done()
        ):
            last_hb = await read_heartbeat(self._redis, state.name)
            # Tolerate cold start: ignore freeze detection during the
            # first 2 × interval after spawning.
            cold_start_grace = 2 * state.heartbeat_interval_s
            if (
                state.last_started_at > 0
                and (now - state.last_started_at) > cold_start_grace
            ):
                if last_hb is None or (now - last_hb) > self._heartbeat_timeout_s:
                    crashed = True
                    age = "missing" if last_hb is None else f"{int(now - last_hb)}s"
                    crash_reason = f"heartbeat freeze (last={age})"

        if not crashed:
            return

        # Stop event already set? Don't restart, we're shutting down.
        if self._stop_event.is_set():
            logger.info(
                f"watchdog: {state.name!r} ended during shutdown — not restarting"
            )
            return

        state.last_failure_reason = crash_reason or "task ended"
        state.restart_count += 1
        logger.warning(
            f"watchdog: {state.name!r} crashed ({state.last_failure_reason}); "
            f"restart {state.restart_count}/{self._max_restarts}"
        )

        if state.restart_count > self._max_restarts:
            logger.error(
                f"watchdog: {state.name!r} exceeded max restarts "
                f"({self._max_restarts}); giving up and stopping engine"
            )
            await self._publish_crash(
                state,
                f"exceeded max restarts ({self._max_restarts})",
                fatal=True,
            )
            self._stop_event.set()
            return

        # Surface to Telegram on every restart, but throttle: once per
        # streak. The "fatal" path above is published separately.
        if not state.crash_published:
            await self._publish_crash(state, state.last_failure_reason or "?")
            state.crash_published = True

        # Linear backoff before re-spawning.
        backoff = self._backoff_s * state.restart_count
        if backoff > 0:
            logger.info(
                f"watchdog: backing off {backoff}s before restarting {state.name!r}"
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                # Stop set during backoff — give up.
                return
            except asyncio.TimeoutError:
                pass

        await self._spawn(state)

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _spawn(self, state: ComponentState) -> None:
        try:
            coro = state.factory()
        except Exception:
            logger.exception(
                f"watchdog: factory for {state.name!r} raised — cannot start"
            )
            return
        state.task = asyncio.create_task(coro, name=f"watchdog:{state.name}")
        state.last_started_at = time.time()
        state.last_restart_at = state.last_started_at
        logger.info(f"watchdog: started {state.name!r}")

    async def _publish_crash(
        self,
        state: ComponentState,
        reason: str,
        *,
        fatal: bool = False,
    ) -> None:
        try:
            payload = {
                "component": state.name,
                "error_type": "WatchdogRestart" if not fatal else "WatchdogFatal",
                "error": reason,
                "restart_count": state.restart_count,
                "max_restarts": self._max_restarts,
            }
            await self._redis.publish(ENGINE_CRASH_CHANNEL, json.dumps(payload))
        except Exception as e:
            logger.warning(f"watchdog: failed to publish crash for {state.name!r}: {e}")
