"""Provider pool for the multi-RPC abstraction.

Wave-2 implementation. A :class:`ProviderPool` holds N :class:`RPCProvider`
instances ranked by priority and exposes :meth:`acquire` which returns the
best available provider given current circuit-breaker + token-bucket state.

Acquisition algorithm:
  1. Scan providers by priority ascending.
  2. Skip those in ProviderState.UNHEALTHY.
  3. Skip those whose breaker.can_attempt() is False (the breaker arms
     a HALF_OPEN probe if cooldown elapsed -- can_attempt() True).
  4. For each candidate, try its bucket.try_acquire() (fast path).
  5. If the fast path picks one, yield it.
  6. If none fast-path-available, wait min(refill_time, cooldown) and
     retry. Bound by ``RPC_ACQUIRE_TIMEOUT_S`` -- raises
     :class:`NoRPCProviderAvailable` if exhausted.
"""

from __future__ import annotations

import asyncio
import enum
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from loguru import logger

from src.rpc.circuit_breaker import CircuitBreaker, CircuitState
from src.rpc.rate_limiter import AdaptiveTokenBucket

try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        rpc_fallback_total,
    )
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    rpc_fallback_total = _NoOpLabel()  # type: ignore[assignment]


# Default upper bound for ProviderPool.acquire() before it raises
# NoRPCProviderAvailable. Lives here (not in src/config.py) because
# Wave-1 didn't add it to settings; an operator override would land
# alongside the rest of the RPC tuning knobs in a Wave-3 patch.
RPC_ACQUIRE_TIMEOUT_S: float = 5.0


class NoRPCProviderAvailable(RuntimeError):
    """Raised when ProviderPool.acquire() exhausts its wait budget with
    every provider either UNHEALTHY, breaker-open, or rate-limited."""

    pass


class ProviderState(enum.Enum):
    """High-level lifecycle state of an RPCProvider.

    HEALTHY     — passes health checks, breaker is CLOSED, in rotation.
    DEGRADED    — breaker has tripped recently or latency p95 is elevated.
                  Still in rotation as a fallback.
    UNHEALTHY   — manually disabled by operator, or URL not configured.
                  Skipped entirely.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class RPCProvider:
    """One Polygon RPC provider with its own auth, rate budget, breaker."""

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
    itself. :class:`src.rpc.client.RPCClient` owns the httpx /
    websockets sessions and uses ``async with pool.acquire() as
    provider:`` to pick a target.
    """

    def __init__(
        self,
        providers: list[RPCProvider],
        acquire_timeout_s: float = RPC_ACQUIRE_TIMEOUT_S,
    ) -> None:
        # Sort by priority ascending so iteration order matches selection
        # order without re-sorting on every acquire().
        self._providers: list[RPCProvider] = sorted(providers, key=lambda p: p.priority)
        self._acquire_timeout_s = float(acquire_timeout_s)
        # Per-provider stats counters. We track the breaker/bucket
        # internal state via their own stats() methods, but also keep
        # pool-level success/failure counters because the pool is the
        # only place that observes both halves of the in-flight call.
        self._stats: dict[str, dict[str, int]] = {
            p.name: {"acquisitions": 0, "successes": 0, "failures": 0, "fallbacks": 0}
            for p in self._providers
        }
        # Mark providers with an empty URL as UNHEALTHY so they stay out
        # of rotation. This is how the local-Erigon entry behaves when
        # ERIGON_RPC_HTTPS_URL (or LOCAL_ERIGON_RPC_URL) is unset --
        # the pool just falls through to the paid providers.
        for p in self._providers:
            if not p.url:
                p.state = ProviderState.UNHEALTHY
                logger.info(
                    "Provider {} has no URL configured; marking UNHEALTHY",
                    p.name,
                )

    @property
    def size(self) -> int:
        """Number of providers in the pool (all states)."""
        return len(self._providers)

    @property
    def providers(self) -> list[RPCProvider]:
        """Read-only view of the configured providers (priority-sorted)."""
        return list(self._providers)

    def _eligible(self, p: RPCProvider) -> bool:
        """True iff provider p is eligible for selection right now."""
        if p.state == ProviderState.UNHEALTHY:
            return False
        if p.breaker is not None and not p.breaker.can_attempt():
            return False
        return True

    def _pick_fast(
        self, exclude: set[str] | None = None
    ) -> RPCProvider | None:
        """Synchronous scan: return the highest-priority provider that
        is eligible AND has a token ready. None if every candidate would
        block. Providers whose name is in ``exclude`` are skipped --
        the failover loop in RPCClient uses this to avoid re-picking
        a provider that just failed within the same call."""
        exclude = exclude or set()
        for p in self._providers:
            if p.name in exclude:
                continue
            if not self._eligible(p):
                continue
            if p.bucket is None or p.bucket.try_acquire():
                return p
        return None

    def _next_wait_s(self) -> float:
        """How long the slowest-of-fastest options would take to free up.

        Combines:
          * breaker.time_until_retry_s() for OPEN providers
          * 1.0 / refill_per_sec for rate-limited HEALTHY providers
        Returns a small positive number; never negative, never huge.
        """
        candidates: list[float] = []
        for p in self._providers:
            if p.state == ProviderState.UNHEALTHY:
                continue
            if p.breaker is not None and p.breaker.state == CircuitState.OPEN:
                t = p.breaker.time_until_retry_s()
                if t > 0:
                    candidates.append(t)
            if p.bucket is not None and not p.bucket.unlimited:
                refill = max(p.bucket.refill, 1e-6)
                candidates.append(1.0 / refill)
        if not candidates:
            return 0.05
        # Cap individual sleep to 0.2s so we re-check eligibility quickly.
        return max(0.01, min(0.2, min(candidates)))

    @asynccontextmanager
    async def acquire(
        self, exclude: set[str] | None = None
    ) -> AsyncIterator[RPCProvider]:
        """Yield the best available provider for a single RPC call.

        Args:
            exclude: Optional set of provider names to skip. Used by
                :class:`RPCClient` to avoid re-picking a provider that
                just failed within the same JSON-RPC call's retry loop.

        On exit:
          * If the caller raised, record_failure() on the breaker.
          * If the caller succeeded, record_success() on the breaker.

        Emits ``polybot_rpc_fallback_total{from_provider, to_provider}``
        whenever the picked provider is NOT the highest-priority
        non-unhealthy one.
        """
        deadline = time.monotonic() + self._acquire_timeout_s
        exclude_set = set(exclude or ())
        picked: RPCProvider | None = None
        while True:
            picked = self._pick_fast(exclude_set)
            if picked is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                raise NoRPCProviderAvailable(
                    f"All {self.size} RPC providers unavailable after "
                    f"{self._acquire_timeout_s:.1f}s"
                )
            await asyncio.sleep(min(self._next_wait_s(), max(0.0, deadline - now)))

        # ---- Fallback metric: emit if we skipped a higher-priority
        # ---- provider that *would* normally be preferred (i.e. it
        # ---- was eligible only in the "URL configured" sense, but
        # ---- its breaker/bucket said no). The "from" we record is the
        # ---- highest-priority configured provider that was skipped.
        skipped_from: str | None = None
        for p in self._providers:
            if p.name == picked.name:
                break
            if p.state == ProviderState.UNHEALTHY:
                # Unhealthy providers (URL unset, manually disabled) are
                # NOT counted as fallbacks -- they're not "available"
                # in the first place.
                continue
            skipped_from = p.name
            break
        if skipped_from is not None:
            try:
                rpc_fallback_total.labels(
                    from_provider=skipped_from,
                    to_provider=picked.name,
                ).inc()
            except Exception:  # pragma: no cover — defensive
                pass
            self._stats[picked.name]["fallbacks"] += 1

        self._stats[picked.name]["acquisitions"] += 1

        try:
            yield picked
        except BaseException:
            if picked.breaker is not None:
                picked.breaker.record_failure()
            self._stats[picked.name]["failures"] += 1
            raise
        else:
            if picked.breaker is not None:
                picked.breaker.record_success()
            self._stats[picked.name]["successes"] += 1

    def report_429(self, provider_name: str) -> None:
        """Forwarder for RPCClient to penalise a provider's bucket after
        a 429 / rate-limit response."""
        for p in self._providers:
            if p.name == provider_name and p.bucket is not None:
                p.bucket.penalise()
                return

    def stats(self) -> list[dict[str, Any]]:
        """Snapshot of every provider's bucket + breaker state for
        /metrics and the dashboard's RPC health panel."""
        out: list[dict[str, Any]] = []
        for p in self._providers:
            entry: dict[str, Any] = {
                "name": p.name,
                "priority": p.priority,
                "state": p.state.value,
                "url_configured": bool(p.url),
                "counters": dict(self._stats.get(p.name, {})),
            }
            if p.bucket is not None:
                entry["bucket"] = p.bucket.stats()
            if p.breaker is not None:
                entry["breaker"] = {
                    "state": p.breaker.state.value,
                    "consecutive_failures": p.breaker._consecutive_failures,
                    "time_until_retry_s": p.breaker.time_until_retry_s(),
                }
            out.append(entry)
        return out

    async def health_check_loop(self) -> None:  # pragma: no cover — Wave 3
        """Background task placeholder.

        Wave-2 ships the structural pieces (pool selection, breaker,
        bucket, client) but the periodic eth_blockNumber probe + the
        rpc_health_history insert (migration 023) is Wave-3 scope. The
        loop is structured here so the RPCClient.start()/close()
        lifecycle already knows how to manage it.
        """
        # Intentional no-op: returning immediately keeps the contract
        # callable from RPCClient without affecting Wave-2 acceptance.
        return None
