"""Provider pool for the multi-RPC abstraction.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.2.

A :class:`ProviderPool` holds N :class:`RPCProvider` instances ranked
by priority and exposes :meth:`acquire` which returns the best
available provider given current circuit-breaker + token-bucket state.

Default provider lineup (overridable via settings.RPC_PROVIDER_PRIORITIES):
  priority 0 — local Erigon (~5 ms latency, no rate limit)
  priority 1 — Alchemy paid tier (~50 ms latency, 300 CU/s)
  priority 2 — QuickNode free tier (~80 ms latency, 15 req/min)

The pool round-robins WITHIN a priority tier only when multiple
providers share a tier; in practice priorities are unique so the
behaviour is "prefer 0, fall back to 1, fall back to 2".
"""

from __future__ import annotations

import enum
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from src.rpc.circuit_breaker import CircuitBreaker
from src.rpc.rate_limiter import AdaptiveTokenBucket


class ProviderState(enum.Enum):
    """High-level lifecycle state of an RPCProvider.

    Differs from :class:`CircuitState` — that's the breaker's view;
    this is the operator's view.

    HEALTHY     — passes health checks, breaker is CLOSED, in rotation.
    DEGRADED    — breaker has tripped recently or latency p95 is elevated.
                  Still in rotation as a fallback but de-prioritised.
    UNHEALTHY   — manually disabled by operator, or breaker has been OPEN
                  for > 3× cooldown without recovery. Skipped entirely.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class RPCProvider:
    """One Polygon RPC provider with its own auth, rate budget, breaker.

    Fields:
        name: Symbolic identifier ('local_erigon', 'alchemy', 'quicknode').
            Used as the Prometheus label and as
            ``rpc_health_history.provider``.
        url: HTTP(S) endpoint for JSON-RPC POSTs. WebSocket endpoint
            for subscriptions is derived (s/http/ws/) unless overridden.
        ws_url: Explicit WebSocket endpoint (for eth_subscribe).
            None => auto-derive from ``url``.
        priority: Lower is preferred. 0 = local node, 1+ = paid/free
            backups.
        api_key: Optional bearer token / API key for paid providers.
        bucket: Adaptive token bucket scoped to this provider.
        breaker: Circuit breaker scoped to this provider.
        state: Lifecycle state (operator-visible).
    """

    name: str
    url: str
    priority: int
    api_key: str = ""
    ws_url: str | None = None
    bucket: AdaptiveTokenBucket | None = field(default=None, repr=False)
    breaker: CircuitBreaker | None = field(default=None, repr=False)
    state: ProviderState = ProviderState.HEALTHY


class ProviderPool:
    """Holds N RPCProvider instances; exposes acquire() returning the
    best available provider given current circuit-breaker + budget state.

    The pool is purely a scheduling layer — it does not make HTTP calls
    itself. :class:`src.rpc.client.RPCClient` owns the aiohttp / websockets
    sessions and uses ``async with pool.acquire() as provider:`` to pick
    a target.
    """

    def __init__(self, providers: list[RPCProvider]) -> None:
        """
        Args:
            providers: Pre-constructed RPCProvider objects with their
                bucket + breaker attached. Wave 2 will add a classmethod
                ``from_settings()`` that builds these from
                ``settings.RPC_PROVIDER_PRIORITIES``.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    @property
    def size(self) -> int:
        """Number of providers in the pool (all states)."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[RPCProvider]:
        """Yield the best available provider for a single RPC call.

        Selection algorithm:
          1. Scan providers by priority ascending.
          2. Skip those in ProviderState.UNHEALTHY.
          3. Skip those whose breaker.allow_request() is False.
          4. For each candidate, try its bucket.try_acquire() (fast path).
          5. If the fast path picks one, yield it.
          6. If every candidate's bucket is empty, await
             bucket.acquire() on the highest-priority HEALTHY one
             (the slow path that paid the rate-limit cost).

        On exit:
          * If the caller raised, record_failure() on the breaker (the
            RPCClient catches the exception and decides whether it's
            actually a "provider's fault" failure or a user-input one).
          * If the caller succeeded, record_success() on the breaker.

        Emits:
          * ``polybot_rpc_fallback_total{from_provider, to_provider}``
            on every step away from priority 0 to a lower-priority one.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
        # unreachable but documents the async generator shape
        yield  # type: ignore[unreachable]

    async def health_check_loop(self) -> None:
        """Background task: every settings.RPC_HEALTHCHECK_INTERVAL_S,
        probe each provider with a cheap eth_blockNumber call.

        For each probe:
          * Record latency_ms via metrics.
          * Insert a row into rpc_health_history (migration 023).
          * Update ProviderState if the breaker / latency suggests
            degradation.

        Designed to run as a long-lived task started by RPCClient.start()
        and stopped by RPCClient.close().
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def report_429(self, provider_name: str) -> None:
        """Forwarder for RPCClient to penalise a provider's bucket after
        a 429 / rate-limit response. Increments the per-provider rate-
        limit counter and triggers bucket.penalise()."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def stats(self) -> list[dict[str, Any]]:
        """Snapshot of every provider's bucket + breaker state for
        /metrics and the dashboard's RPC health panel."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
