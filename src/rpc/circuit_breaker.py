"""Per-provider circuit breaker for the RPC abstraction layer.

WAVE-1 ARCHITECT SKELETON. Bodies are intentionally not implemented;
Wave-2 implementers will fill them in. See
docs/ROUND_6_THE_SPINE.md § 3.2.

State machine:

    CLOSED  ── 5 consecutive failures ──>  OPEN
       ^                                    │
       │                                60s cooldown
       │                                    ▼
       └── probe succeeds ──  HALF_OPEN  (single trial call)
                                  │
                                  └── probe fails ──>  OPEN  (reset cooldown)

Threshold (5 failures) and cooldown (60s) are tunable via
``src.config.settings.RPC_CIRCUIT_BREAKER_THRESHOLD`` /
``RPC_CIRCUIT_BREAKER_COOLDOWN_S``.

State transitions emit:
  * ``polybot_rpc_circuit_breaker_open{provider}`` gauge updates
  * ``rpc_health_history`` rows with ``circuit_state`` changes
    (see migration 023)
"""

from __future__ import annotations

import enum
import time


class CircuitState(enum.Enum):
    """One of the three states the breaker can be in.

    CLOSED      — healthy, calls flow through.
    OPEN        — tripped, calls are refused locally without an RPC roundtrip.
    HALF_OPEN   — cooldown elapsed, a single probe call is allowed; its
                  outcome decides whether to fully close again or re-OPEN.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider circuit breaker.

    Tracks consecutive failures, transitions to OPEN at the threshold,
    schedules a HALF_OPEN probe after the cooldown, and resets on a
    successful probe. Thread-/coroutine-safe — the RPCClient is async
    and many coroutines share a single breaker per provider.
    """

    def __init__(
        self,
        provider_name: str,
        failure_threshold: int = 5,
        cooldown_s: float = 60.0,
    ) -> None:
        """
        Args:
            provider_name: Symbolic name of the provider (used as metric
                label and for ``rpc_health_history.provider``).
            failure_threshold: How many consecutive failures trip the
                breaker. Default 5 matches the spec.
            cooldown_s: Seconds to wait in OPEN before attempting the
                HALF_OPEN probe. Default 60s.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    @property
    def state(self) -> CircuitState:
        """Current state of the breaker. Read-only public API."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def open(self) -> None:
        """Force the breaker to OPEN. Resets the cooldown timer.

        Called by external orchestrators that want to take a provider
        out of rotation (e.g. operator-driven during incident response).
        Most state transitions happen implicitly via record_failure().
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def close(self) -> None:
        """Force the breaker to CLOSED. Resets the failure counter.

        Called by record_success() in normal operation, or manually by
        an operator confirming a provider has recovered.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def record_success(self) -> None:
        """Mark an RPC call as successful.

        From CLOSED: resets the consecutive-failure counter (already 0).
        From HALF_OPEN: transitions back to CLOSED.
        From OPEN: undefined — the breaker should refuse calls in OPEN,
            so this shouldn't fire. Defensive: treat as transition to
            CLOSED via close().
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def record_failure(self) -> None:
        """Mark an RPC call as failed.

        Increments the consecutive-failure counter. If the counter
        reaches failure_threshold, transitions CLOSED -> OPEN and
        starts the cooldown. From HALF_OPEN, any failure goes back to
        OPEN with the cooldown reset.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def allow_request(self) -> bool:
        """Returns True if a caller may attempt an RPC call.

        Called by the ProviderPool's acquire() path before yielding a
        provider. In CLOSED: always True. In OPEN: True only if the
        cooldown has elapsed AND we transition to HALF_OPEN (single
        probe). In HALF_OPEN: False (one probe is already in flight).
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def time_until_retry_s(self) -> float:
        """Seconds remaining until the cooldown expires.

        Used by the ProviderPool's "block on fallback" path and the
        Prometheus gauge. Returns 0.0 if already CLOSED or HALF_OPEN.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
