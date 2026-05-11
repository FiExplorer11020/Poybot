"""Per-provider circuit breaker for the RPC abstraction layer.

Wave-2 implementation. State machine:

    CLOSED  -- N consecutive failures -->  OPEN
       ^                                    |
       |                              cooldown_s
       |                                    v
       +-- probe succeeds --   HALF_OPEN  (single trial call)
                                  |
                                  +-- probe fails -->  OPEN  (reset cooldown)

Threshold (default 5) and cooldown (default 60s) come from
``settings.RPC_CIRCUIT_BREAKER_THRESHOLD`` / ``RPC_CIRCUIT_BREAKER_COOLDOWN_S``.
State transitions update the per-provider Prometheus gauge.
"""

from __future__ import annotations

import enum
import time

from loguru import logger

try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        rpc_circuit_breaker_open,
    )
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def set(self, *_args, **_kwargs):
            return None

    rpc_circuit_breaker_open = _NoOpLabel()  # type: ignore[assignment]


class CircuitState(enum.Enum):
    """One of the three states the breaker can be in."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider circuit breaker. Tracks consecutive failures,
    transitions to OPEN at the threshold, allows a single HALF_OPEN
    probe after cooldown, and resets on a successful probe."""

    def __init__(
        self,
        provider_name: str,
        failure_threshold: int = 5,
        cooldown_s: float = 60.0,
    ) -> None:
        self.provider_name = provider_name
        self.failure_threshold = int(max(1, failure_threshold))
        self.cooldown_s = float(max(0.0, cooldown_s))
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        # Set when transitioning into OPEN; we compute time_until_retry
        # from now() - _opened_at relative to cooldown_s.
        self._opened_at: float = 0.0
        # Set when a HALF_OPEN probe is dispatched. allow_request() is
        # responsible for ensuring only ONE probe is dispatched at a time.
        self._half_open_in_flight: bool = False
        self._set_gauge(0)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _now(self) -> float:
        return time.monotonic()

    def _set_gauge(self, value: int) -> None:
        """Emit the prom gauge update.

        Contract per task spec: gauge is 1 iff the breaker is OPEN or
        HALF_OPEN (the spec says "set(0/1) on state changes" and the
        metric description in metrics.py reads "1 iff the per-provider
        circuit breaker is currently OPEN or HALF_OPEN")."""
        try:
            rpc_circuit_breaker_open.labels(provider=self.provider_name).set(value)
        except Exception:  # pragma: no cover — defensive
            pass

    def _transition(self, new_state: CircuitState) -> None:
        """Centralised state transition. Updates the gauge and logs."""
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        if new_state == CircuitState.CLOSED:
            self._set_gauge(0)
            self._consecutive_failures = 0
            self._half_open_in_flight = False
        elif new_state == CircuitState.OPEN:
            self._set_gauge(1)
            self._opened_at = self._now()
            self._half_open_in_flight = False
        elif new_state == CircuitState.HALF_OPEN:
            # Gauge stays at 1 — the breaker is still "not fully closed".
            self._set_gauge(1)
        logger.info(
            "CircuitBreaker[{}]: {} -> {}",
            self.provider_name,
            old.value,
            new_state.value,
        )

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CircuitState:
        """Current state. Read-only public API.

        Side effect: if we're in OPEN and the cooldown has expired,
        this call surfaces that fact (transitioning to HALF_OPEN is
        deferred to allow_request() / can_attempt() which decide whether
        a probe is actually dispatched). The state property does not
        transition — it stays OPEN until allow_request() arms HALF_OPEN.
        """
        return self._state

    def open(self) -> None:
        """Force the breaker to OPEN. Resets the cooldown timer."""
        self._transition(CircuitState.OPEN)

    def close(self) -> None:
        """Force the breaker to CLOSED. Resets the failure counter."""
        self._transition(CircuitState.CLOSED)

    def record_success(self) -> None:
        """Mark an RPC call as successful.

        CLOSED -> stays CLOSED (counter cleared).
        HALF_OPEN -> CLOSED (probe succeeded).
        OPEN -> CLOSED (defensive — caller shouldn't have made the call).
        """
        if self._state == CircuitState.CLOSED:
            self._consecutive_failures = 0
            return
        # From HALF_OPEN or (defensively) OPEN, close fully.
        self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Mark an RPC call as failed.

        CLOSED: increments counter; if it reaches threshold, transitions
        to OPEN with cooldown starting now.

        HALF_OPEN: any failure goes back to OPEN with cooldown reset.

        OPEN: defensive — keep cooldown ticking from the original
        opening time. (If the caller is racing the cooldown, they may
        have made a call we now refuse to count toward a fresh trip.)
        """
        if self._state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
            return
        if self._state == CircuitState.OPEN:
            # No counter logic; cooldown is already running.
            return
        # CLOSED — count and possibly trip.
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._transition(CircuitState.OPEN)

    def allow_request(self) -> bool:
        """Returns True if a caller may attempt an RPC call.

        CLOSED: always True.
        OPEN: True only if cooldown elapsed AND we transition to
            HALF_OPEN (single-shot probe arming).
        HALF_OPEN: False if a probe is already in flight, True if not
            (and we arm).

        Note: the arming has side effects — once True is returned from
        OPEN, the breaker is HALF_OPEN and subsequent calls return False
        until record_success() / record_failure() resolves the probe.
        """
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if self.cooldown_s <= 0 or (
                self._now() - self._opened_at
            ) >= self.cooldown_s:
                # Arm the HALF_OPEN probe.
                self._transition(CircuitState.HALF_OPEN)
                self._half_open_in_flight = True
                return True
            return False
        # HALF_OPEN
        if self._half_open_in_flight:
            return False
        self._half_open_in_flight = True
        return True

    # Alias matching the spec's "can_attempt()" naming.
    def can_attempt(self) -> bool:
        """Alias of :meth:`allow_request`."""
        return self.allow_request()

    def time_until_retry_s(self) -> float:
        """Seconds remaining until the cooldown expires.

        Returns 0.0 if already CLOSED or HALF_OPEN, or if the cooldown
        has already elapsed (so a probe is overdue)."""
        if self._state != CircuitState.OPEN:
            return 0.0
        remaining = self.cooldown_s - (self._now() - self._opened_at)
        return max(0.0, remaining)
