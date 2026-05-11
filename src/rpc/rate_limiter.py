"""Provider-aware adaptive token bucket for the RPC layer.

Wave-2 implementation. Mirrors :class:`src.registry.falcon_client._TokenBucket`
but with provider-aware tuning so a local Erigon node (no documented rate
limit, ~5 ms latency) gets effectively-infinite tokens while paid providers
(Alchemy / QuickNode) get their published rates.

The bucket itself is provider-agnostic; the :class:`ProviderPool` is
responsible for constructing one bucket per provider with the right
(capacity, refill_per_sec) tuple from settings.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

# Defensive metrics import. Same shape as FalconClient: if prometheus_client
# isn't around (older checkouts, sparse test envs), we get no-op stubs so
# production paths don't break.
try:
    from src.monitoring.metrics import rpc_calls_total  # type: ignore[attr-defined]
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    rpc_calls_total = _NoOpLabel()  # type: ignore[assignment]


class AdaptiveTokenBucket:
    """Provider-aware adaptive token bucket.

    See module docstring; contract mirrors FalconClient's `_TokenBucket`
    with three differences:
      * No multi-key pool (RPC providers each have a single endpoint).
      * ``provider_name`` (str) is the metric label.
      * ``unlimited`` shortcut: local-Erigon hot path skips the lock entirely.
    """

    def __init__(
        self,
        provider_name: str,
        capacity: float,
        refill_per_sec: float,
        backoff_s: float = 60.0,
        unlimited: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.capacity = float(max(1.0, capacity))
        self.base_refill = float(max(1e-6, refill_per_sec))
        self.refill = self.base_refill
        self.backoff_s = float(backoff_s)
        self.unlimited = bool(unlimited)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._penalty_until: float = 0.0

    def _now(self) -> float:
        return time.monotonic()

    def _refill_tokens(self) -> None:
        """Lazy refill on every read/write. Also restores base refill rate
        once a penalty window has elapsed."""
        now = self._now()
        if self._penalty_until and now >= self._penalty_until:
            self.refill = self.base_refill
            self._penalty_until = 0.0
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill)
            self._last_refill = now

    @property
    def tokens_available(self) -> float:
        """Current token count. Refills lazily on read."""
        self._refill_tokens()
        return self._tokens

    @property
    def penalty_active(self) -> bool:
        """True iff a 429-induced refill halving is still in effect."""
        if not self._penalty_until:
            return False
        return self._now() < self._penalty_until

    async def acquire(self) -> None:
        """Block until at least one token is available; debit it.

        ``unlimited=True`` short-circuits to immediate return. Otherwise
        computes exact sleep time for the next token, releasing the lock
        during sleep, retrying on wake. Individual sleeps are capped at
        ~1s so cancellation latency stays bounded.
        """
        if self.unlimited:
            return
        while True:
            async with self._lock:
                self._refill_tokens()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                missing = 1.0 - self._tokens
                wait_s = missing / max(self.refill, 1e-6)
            await asyncio.sleep(min(wait_s, 1.0))

    def try_acquire(self) -> bool:
        """Non-blocking variant. Never blocks, never raises."""
        if self.unlimited:
            return True
        # Mirror FalconClient._TokenBucket.try_acquire: skip if the lock
        # is held (some other coroutine is mid-acquire), otherwise refill
        # + debit under the implicit single-thread-asyncio guarantee.
        if self._lock.locked():
            return False
        self._refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def penalise(self) -> None:
        """Apply the 429 backoff: halve the refill rate for ``backoff_s``
        seconds, then restore the base rate. Bumps the rate-limited
        counter on the per-provider metric."""
        self.refill = max(self.base_refill / 2.0, 1e-6)
        self._penalty_until = self._now() + self.backoff_s
        try:
            rpc_calls_total.labels(
                provider=self.provider_name,
                method="*",
                result="rate_limited",
            ).inc()
        except Exception:  # pragma: no cover — defensive
            pass
        logger.warning(
            "RPC 429 on provider={}; halving refill {:.3f}->{:.3f}/s for {:.0f}s",
            self.provider_name,
            self.base_refill,
            self.refill,
            self.backoff_s,
        )

    def stats(self) -> dict[str, Any]:
        """Snapshot for /metrics and operator inspection."""
        self._refill_tokens()
        return {
            "provider": self.provider_name,
            "tokens": self._tokens,
            "capacity": self.capacity,
            "refill_per_sec": self.refill,
            "base_refill_per_sec": self.base_refill,
            "penalty_active": self.penalty_active,
            "penalty_until": self._penalty_until,
            "unlimited": self.unlimited,
        }
