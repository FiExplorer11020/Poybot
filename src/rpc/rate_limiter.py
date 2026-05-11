"""Provider-aware adaptive token bucket for the RPC layer.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.2.

This is the RPC analogue of :class:`src.registry.falcon_client._TokenBucket`.
The structure is the same — capacity, refill rate, 429-aware penalty
window — but with provider-aware tuning so the local Erigon node (no
documented rate limit, ~5 ms latency) gets effectively-infinite tokens
while paid providers (Alchemy's compute-units-per-second, QuickNode's
free-tier limits) get their published rates.

Per-provider tuning is supplied at construction; the bucket itself
doesn't know whether it's wrapping local-erigon or alchemy — that's the
ProviderPool's job. The bucket just enforces (capacity, refill_per_sec)
with adaptive backoff on rate-limit signals.
"""

from __future__ import annotations

from typing import Any


class AdaptiveTokenBucket:
    """Provider-aware adaptive token bucket.

    Mirrors the contract of FalconClient's _TokenBucket:
      * ``acquire()``  — async, blocks until at least 1 token is available.
      * ``try_acquire()`` — non-blocking, returns True iff a token was debited.
      * ``penalise()`` — called after a 429 / rate-limit response; halves
        the refill rate for ``backoff_s`` seconds then restores it.

    Provider tuning examples (Wave 2 will set the defaults in src/config.py):
      * ``local_erigon``  — capacity=10000, refill=10000.0  (effectively unlimited)
      * ``alchemy_paid``  — capacity=300,   refill=10.0     (10 CUPS, 300 burst)
      * ``quicknode_free``— capacity=25,    refill=15.0/60  (15 req/min free)

    Differences vs FalconClient._TokenBucket:
      * No multi-key pool (RPC providers use single endpoint + auth).
      * Provider name is the metric label (str) instead of a numeric
        key_index — matches the rpc_health_history.provider column.
      * Optional ``unlimited`` shortcut: when capacity is high enough
        that acquire() would never block in practice, bypass the sleep
        path entirely for the local-node hot path.
    """

    def __init__(
        self,
        provider_name: str,
        capacity: float,
        refill_per_sec: float,
        backoff_s: float = 60.0,
        unlimited: bool = False,
    ) -> None:
        """
        Args:
            provider_name: Symbolic name (e.g. 'local_erigon'). Used for
                Prometheus metric labels.
            capacity: Maximum tokens the bucket holds (burst size).
            refill_per_sec: Tokens added per second in steady state.
            backoff_s: Seconds to halve the refill rate after a 429.
            unlimited: If True, acquire() is a no-op fast path; useful
                for the local-Erigon entry which has no rate limit.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    @property
    def tokens_available(self) -> float:
        """Current token count. Refills lazily on read; the public
        accessor is here so the metrics exporter doesn't need to poke
        at private attributes."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    async def acquire(self) -> None:
        """Block until at least one token is available; debit it.

        Implementation contract:
          * ``unlimited=True`` short-circuits to immediate return.
          * Otherwise computes exact sleep time for the next token,
            releases the internal lock during sleep, retries on wake.
          * Cap individual sleeps to ~1s so cancellation latency stays
            bounded (mirrors FalconClient._TokenBucket.acquire).
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def try_acquire(self) -> bool:
        """Non-blocking variant. Used by the ProviderPool's fast-path
        round-robin scan to find a provider with a token ready right
        now without committing to a wait.

        Returns:
            True if a token was debited, False otherwise. Never blocks,
            never raises.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def penalise(self) -> None:
        """Apply the 429 backoff. Halve refill_per_sec for backoff_s
        seconds, then restore the base rate.

        Increments ``polybot_rpc_calls_total{provider, result='rate_limited'}``
        and logs a structured warning. Caller (RPCClient) decides
        whether to retry on the same provider or fail over."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    def stats(self) -> dict[str, Any]:
        """Snapshot for /metrics and operator inspection.

        Returns:
            ``{"provider": str, "tokens": float, "capacity": float,
              "refill_per_sec": float, "penalty_active": bool,
              "penalty_until": float}``
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
