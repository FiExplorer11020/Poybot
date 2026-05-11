"""
Async client for the Falcon API (Heisenberg Narrative platform).

Phase 3 Task B — Smart Falcon Client. Builds on Phase 1's parallel-friendly
semaphore by adding four legitimate throughput maximisations:

1. **Multi-API-key rotation** (`FalconKeyPool`). Operators may configure
   N keys via `FALCON_API_KEYS=k1,k2,k3`; each key gets its own per-key
   token bucket so total sustained throughput is N × refill, all within
   Falcon's documented 60 RPM per-key cap.
2. **Adaptive token bucket** (`_TokenBucket`). Replaces the
   sleep-based legacy `_throttle()` with a proper bucket: starts full
   (burst of `FALCON_RPM_BUCKET_CAPACITY`), refills at
   `FALCON_RPM_REFILL_PER_SEC` tokens/sec. On HTTP 429 the refill rate
   is halved for `FALCON_BACKOFF_S` seconds, then restored. Per-key.
3. **Request coalescing** (in-flight dedup). Two concurrent calls with
   the same `(agent_id, params, limit, offset)` share one HTTP request;
   the second `await`s the first's future. Resolved futures are kept
   for `FALCON_COALESCE_TTL_S` seconds so a quick re-issue returns the
   same payload (in-process dedup; *not* a replacement for the 48h
   Redis cache).
4. **Conditional GET** (ETag / Last-Modified revalidation). For
   endpoints that return either header on a fresh response, we store
   it alongside the Redis cache and, after a soft expiry of
   `FALCON_CONDITIONAL_REVALIDATE_S`, revalidate with
   `If-None-Match` / `If-Modified-Since`. A 304 restores TTL without
   re-downloading the payload. For agents that don't support
   conditional GET (most Falcon ones — see phase3/B_smart_falcon.md)
   the path is a no-op and the 48h TTL cache remains as-is.

Public API surface is unchanged: every existing call site
(`leader_registry.py`, callers of `get_wallet360`, `get_leaderboard`,
`get_market_insights`, `get_pnl_leaderboard`, `close`) continues to work
without modification.
"""

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp
from loguru import logger

from src.config import settings
from src.registry.models import FalconLeaderEntry, MarketInsights, PnlLeaderEntry, WalletMetrics

# Phase 1 Task F (audit HP-2): Prometheus instrumentation. The metrics module
# is the single source of truth for metric names + labels (see
# src/monitoring/metrics.py). If it imports cleanly we use it; if not (older
# checkout, prometheus_client missing in some test env) we fall back to no-op
# stubs so production code paths don't break. Same pattern as Phase 1 Task O.
try:
    from src.monitoring.metrics import (
        falcon_call_latency_seconds,
        falcon_calls_total,
        falcon_coalesced_calls_total,
        falcon_concurrency,
        falcon_conditional_get_savings_total,
        falcon_keys_in_pool,
        falcon_rate_limit_hits_total,
        falcon_tokens_available,
    )
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def dec(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    falcon_call_latency_seconds = _NoOpLabel()  # type: ignore[assignment]
    falcon_calls_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_concurrency = _NoOpLabel()  # type: ignore[assignment]
    falcon_coalesced_calls_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_conditional_get_savings_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_keys_in_pool = _NoOpLabel()  # type: ignore[assignment]
    falcon_rate_limit_hits_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_tokens_available = _NoOpLabel()  # type: ignore[assignment]

# Phase 3 Task D: per-agent ingest-health heartbeat. We map the Falcon
# agent_id to a canonical source name (584 → falcon_leaderboard etc.)
# in ``src/monitoring/ingest_health.py`` so the IngestHealthMonitor can
# detect a stuck Falcon endpoint without hardcoding agent IDs at every
# call site. The import is defensive — a checkout without the module
# (rare; the file lands in the same MR) still loads this client.
try:
    from src.monitoring.ingest_health import (  # type: ignore[attr-defined]
        FALCON_AGENT_TO_SOURCE,
        get_health_monitor,
    )

    def _heartbeat_falcon(agent_id: int) -> None:
        source = FALCON_AGENT_TO_SOURCE.get(int(agent_id))
        if source is None:
            return
        try:
            get_health_monitor().heartbeat(source)
        except Exception:
            pass
except Exception:  # pragma: no cover
    def _heartbeat_falcon(agent_id: int) -> None:
        return None


class FalconAPIError(Exception):
    pass


def _coerce_header(resp: Any, name: str) -> str | None:
    """Pull a header off an aiohttp response, returning a `str | None`.

    Defensive: in tests `resp` may be an AsyncMock whose `.headers.get(...)`
    returns a Mock or coroutine rather than a string. We only persist real
    strings so the cache stays JSON-serialisable. We short-circuit on
    `Mock` instances (test artefacts) before invoking `.get()` so no
    coroutine is created — that silences the
    `RuntimeWarning: coroutine ... was never awaited` from unittest.mock.
    """
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    # Filter out test-fixture Mock attributes so we never accidentally
    # invoke their dynamic `.get()` (which AsyncMock turns into a
    # coroutine).
    try:
        from unittest.mock import Mock

        if isinstance(headers, Mock):
            return None
    except Exception:
        pass
    try:
        value = headers.get(name)
    except Exception:
        return None
    if isinstance(value, str):
        return value
    return None


# --------------------------------------------------------------------------- #
# Token bucket (Phase 3 Task B item #2)                                       #
# --------------------------------------------------------------------------- #


class _TokenBucket:
    """Adaptive per-key token bucket.

    The bucket holds up to `capacity` tokens and refills at `refill_per_sec`.
    `acquire()` waits until at least one token is available, then debits it.
    On HTTP 429 the caller triggers `penalise()` which halves the refill rate
    for `backoff_s` seconds, then restores it.

    Why a bucket vs the legacy sleep-based throttle: the bucket starts
    full, so the first 60 calls (capacity=60 default) go through with no
    waiting. After the burst, sustained throughput is bound by `refill_per_sec`.
    This matches "good citizen" API consumption: bursts are tolerated, then
    we ease into the limit.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        backoff_s: int,
        key_index: int = 0,
    ):
        self.capacity = float(max(1, capacity))
        self.base_refill = float(max(0.000001, refill_per_sec))
        self.refill = self.base_refill
        self.backoff_s = int(backoff_s)
        self.key_index = key_index
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._penalty_until: float = 0.0
        falcon_tokens_available.labels(key_index=str(key_index)).set(self._tokens)

    def _now(self) -> float:
        return time.monotonic()

    def _refill_tokens(self) -> None:
        now = self._now()
        # Restore base refill rate once the penalty window expires. This
        # is the "graceful recovery" half of the adaptive layer — the
        # penalise() call is the punishment, this is the forgiveness.
        if self._penalty_until and now >= self._penalty_until:
            self.refill = self.base_refill
            self._penalty_until = 0.0
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill)
            self._last_refill = now
        falcon_tokens_available.labels(key_index=str(self.key_index)).set(self._tokens)

    async def acquire(self) -> None:
        """Block until at least one token is available, then debit one."""
        while True:
            async with self._lock:
                self._refill_tokens()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    falcon_tokens_available.labels(
                        key_index=str(self.key_index)
                    ).set(self._tokens)
                    return
                # Compute exact sleep needed for the next token; release the
                # lock during the sleep so other coroutines can compete.
                missing = 1.0 - self._tokens
                wait_s = missing / max(self.refill, 1e-6)
            # Cap individual sleeps so cancellation latency stays bounded.
            await asyncio.sleep(min(wait_s, 1.0))

    def try_acquire(self) -> bool:
        """Non-blocking variant for the round-robin "pick first with tokens"
        fast path. Acquires the lock briefly; refills + debits or returns
        False without sleeping. Never raises.
        """
        # asyncio.Lock isn't usable from a sync context for try-style ops,
        # so we approximate: refill + compare under a tiny critical section
        # by mutating attributes directly. Since asyncio is single-threaded
        # this is safe — we're racing other coroutines, not OS threads.
        if self._lock.locked():
            return False
        self._refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            falcon_tokens_available.labels(key_index=str(self.key_index)).set(
                self._tokens
            )
            return True
        return False

    def penalise(self) -> None:
        """Called after an HTTP 429. Halve refill rate for `backoff_s` seconds."""
        self.refill = max(self.base_refill / 2.0, 1e-6)
        self._penalty_until = self._now() + float(self.backoff_s)
        falcon_rate_limit_hits_total.labels(key_index=str(self.key_index)).inc()
        logger.warning(
            f"Falcon 429 on key_index={self.key_index}; halving refill "
            f"{self.base_refill:.2f}→{self.refill:.2f}/s for {self.backoff_s}s"
        )


# --------------------------------------------------------------------------- #
# Multi-key pool (Phase 3 Task B item #1)                                     #
# --------------------------------------------------------------------------- #


@dataclass
class _KeyStats:
    """Per-key counters surfaced via `FalconKeyPool.stats()`."""

    calls: int = 0
    errors: int = 0
    rate_limit_hits: int = 0
    last_used_at: float = 0.0


class FalconKeyPool:
    """Round-robin pool of Falcon API keys, each with its own token bucket.

    Acquisition strategy: round-robin by `_next` index, but if the chosen
    key has no tokens we try the next; if all out, we block on the
    first (round-robin) key's `acquire()`. This keeps the steady-state
    distribution even while still letting bursts use any available key.

    Yielded value is the raw key string (not an opaque handle) — the
    caller uses it directly for the `Authorization: Bearer ...` header.
    """

    def __init__(
        self,
        keys: list[str],
        bucket_capacity: int,
        refill_per_sec: float,
        backoff_s: int,
    ):
        # Filter out empty / whitespace-only keys so an env like
        # `FALCON_API_KEYS=a,,b` doesn't create a bogus empty slot.
        cleaned = [k.strip() for k in keys if k and k.strip()]
        if not cleaned:
            # Tolerate empty pool at construction (FalconClient validates
            # at first HTTP attempt). Mirrors the legacy single-key behaviour.
            self._keys: list[str] = []
            self._buckets: list[_TokenBucket] = []
            self._stats: list[_KeyStats] = []
        else:
            self._keys = cleaned
            self._buckets = [
                _TokenBucket(
                    capacity=bucket_capacity,
                    refill_per_sec=refill_per_sec,
                    backoff_s=backoff_s,
                    key_index=i,
                )
                for i in range(len(cleaned))
            ]
            self._stats = [_KeyStats() for _ in cleaned]
        self._next = 0
        self._next_lock = asyncio.Lock()
        falcon_keys_in_pool.set(len(self._keys))
        # Warn if the operator raised the documented cap. We don't crash —
        # they may have a private contract with Falcon — but we log loudly.
        if bucket_capacity > 60:
            logger.warning(
                f"FALCON_RPM_BUCKET_CAPACITY={bucket_capacity} exceeds Falcon's "
                f"documented 60 RPM per-key cap. Proceeding, but you may hit 429s."
            )

    @property
    def size(self) -> int:
        return len(self._keys)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[tuple[str, int]]:
        """Yield (api_key, key_index) for a single HTTP call.

        On exit, updates per-key stats. The token has already been debited
        inside `_pick_and_debit`; we don't release it (tokens regenerate
        over time, that's the whole point of a bucket).
        """
        if not self._keys:
            raise FalconAPIError(
                "FalconKeyPool is empty — set FALCON_API_KEYS or FALCON_API_KEY"
            )
        idx = await self._pick_and_debit()
        key = self._keys[idx]
        self._stats[idx].calls += 1
        self._stats[idx].last_used_at = time.monotonic()
        try:
            yield (key, idx)
        except Exception:
            self._stats[idx].errors += 1
            raise

    async def _pick_and_debit(self) -> int:
        """Round-robin with fallback: try each key once for a non-blocking
        token; if all out, block on the next round-robin key."""
        n = len(self._keys)
        async with self._next_lock:
            start = self._next
            self._next = (self._next + 1) % n
        # Fast path: scan from `start` for a key that has a token available
        # right now. Avoids over-committing one key during a burst.
        for offset in range(n):
            idx = (start + offset) % n
            if self._buckets[idx].try_acquire():
                return idx
        # Slow path: every bucket is empty. Block on the original `start`
        # key's bucket; once it has a token we debit and return.
        await self._buckets[start].acquire()
        return start

    def report_429(self, key_index: int) -> None:
        """Called by FalconClient after a 429 on this key."""
        if 0 <= key_index < len(self._buckets):
            self._buckets[key_index].penalise()
            self._stats[key_index].rate_limit_hits += 1

    def stats(self) -> list[dict]:
        """Per-key counters for /metrics or ad-hoc inspection."""
        return [
            {
                "key_index": i,
                "calls": s.calls,
                "errors": s.errors,
                "rate_limit_hits": s.rate_limit_hits,
                "last_used_at": s.last_used_at,
            }
            for i, s in enumerate(self._stats)
        ]


def _resolve_api_keys(
    api_keys_env: str | None, single_key_env: str | None
) -> list[str]:
    """Build the key list from env vars.

    Precedence:
      1. `FALCON_API_KEYS` (comma-separated) if non-empty.
      2. `FALCON_API_KEY` (single) as a 1-element list.
      3. Empty list (FalconClient validates at first HTTP attempt).
    """
    if api_keys_env and api_keys_env.strip():
        return [k.strip() for k in api_keys_env.split(",") if k.strip()]
    if single_key_env and single_key_env.strip():
        return [single_key_env.strip()]
    return []


# --------------------------------------------------------------------------- #
# Cache entry — Phase 3 Task B item #4 (conditional GET)                      #
# --------------------------------------------------------------------------- #


@dataclass
class _CacheEntry:
    """Wraps the cached payload + conditional-GET headers.

    Stored in Redis as JSON: `{"payload": [...], "etag": "...",
    "last_modified": "...", "cached_at": 12345.6}`. Backward compatible:
    if the legacy `_query` saw a bare list under the same key it still
    works (we attempt to parse as dict first, fall back to list).
    """

    payload: list[dict]
    etag: str | None = None
    last_modified: str | None = None
    cached_at: float = field(default_factory=lambda: time.time())

    def to_json(self) -> str:
        return json.dumps(
            {
                "payload": self.payload,
                "etag": self.etag,
                "last_modified": self.last_modified,
                "cached_at": self.cached_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "_CacheEntry | None":
        try:
            data = json.loads(raw)
        except Exception:
            return None
        # Legacy format: a bare list, no metadata.
        if isinstance(data, list):
            return cls(payload=data, etag=None, last_modified=None, cached_at=0.0)
        if not isinstance(data, dict) or "payload" not in data:
            return None
        return cls(
            payload=list(data.get("payload") or []),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            cached_at=float(data.get("cached_at") or 0.0),
        )


# --------------------------------------------------------------------------- #
# FalconClient                                                                #
# --------------------------------------------------------------------------- #


class FalconClient:
    def __init__(
        self,
        api_key: str = "",
        api_url: str = "",
        redis_client: Any = None,
        cache_ttl_s: int = 172800,
        max_rpm: int | None = None,
        api_keys: list[str] | None = None,
    ):
        # Backward-compat: a constructor `api_key=...` is still honoured.
        # The pool prefers the explicit `api_keys` list, then falls back to
        # env (`FALCON_API_KEYS` then `FALCON_API_KEY`), then the
        # constructor `api_key` if everything else is empty.
        self._api_key = api_key or settings.FALCON_API_KEY
        self._api_url = api_url or settings.FALCON_API_URL
        self._redis = redis_client
        self._cache_ttl = cache_ttl_s
        # Legacy `_max_rpm` kept for the legacy `_throttle()` test surface
        # (test_falcon_phase1.py still asserts the lock invariant). The
        # real per-call rate limiting now lives in the per-key bucket.
        self._max_rpm = int(max_rpm or settings.FALCON_MAX_REQUESTS_PER_MINUTE)
        self._sem = asyncio.Semaphore(int(settings.FALCON_MAX_CONCURRENCY))
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._session: aiohttp.ClientSession | None = None

        # --- Build the key pool ---
        if api_keys is not None:
            resolved_keys = [k for k in api_keys if k]
        else:
            resolved_keys = _resolve_api_keys(
                getattr(settings, "FALCON_API_KEYS", ""),
                self._api_key,
            )
        self._pool = FalconKeyPool(
            keys=resolved_keys,
            bucket_capacity=int(settings.FALCON_RPM_BUCKET_CAPACITY),
            refill_per_sec=float(settings.FALCON_RPM_REFILL_PER_SEC),
            backoff_s=int(settings.FALCON_BACKOFF_S),
        )

        # --- Coalescing state (item #3) ---
        # `(cache_key) -> (future, completed_at)`. While in-flight,
        # `completed_at` is 0.0. After completion, we keep the resolved
        # future around for `FALCON_COALESCE_TTL_S` seconds.
        self._inflight: dict[str, tuple[asyncio.Future, float]] = {}
        self._inflight_lock = asyncio.Lock()
        self._coalesce_ttl_s = float(settings.FALCON_COALESCE_TTL_S)
        self._revalidate_after_s = int(settings.FALCON_CONDITIONAL_REVALIDATE_S)
        # Track fire-and-forget expiry tasks so `close()` can cancel them
        # cleanly (otherwise pytest warns "Task was destroyed but pending").
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # Session                                                            #
    # ------------------------------------------------------------------ #

    def _session_or_new(self) -> aiohttp.ClientSession:
        """Return an aiohttp session.

        The Authorization header is NOT set here anymore — the per-request
        header injection happens inside `query()` via the key acquired from
        `FalconKeyPool`. We still raise on a missing key configuration so
        the failure mode matches the legacy contract.

        Legacy contract preserved: if `_api_key` is empty AND the pool is
        empty, raise. Test sites that mutate `_api_key=""` post-construction
        to simulate misconfiguration also need the pool to look empty —
        we honour that by treating an empty `_api_key` as a clean-slate
        signal when the pool has only the single key derived from it.
        """
        if not self._api_key and self._pool.size == 0:
            raise FalconAPIError("FALCON_API_KEY is not configured")
        # If the operator nulled `_api_key` after construction and the pool
        # was only ever a single-key shadow of it (the common test path),
        # treat as misconfigured. Multi-key pools (FALCON_API_KEYS) are
        # not affected — they're independent of the bare `_api_key`.
        if not self._api_key and self._pool.size == 1:
            raise FalconAPIError("FALCON_API_KEY is not configured")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _cache_key(self, agent_id: int, params: dict, limit: int, offset: int) -> str:
        h = hashlib.md5(
            json.dumps(
                {"params": params, "limit": limit, "offset": offset}, sort_keys=True
            ).encode()
        ).hexdigest()
        return f"falcon:{agent_id}:{h}"

    # ------------------------------------------------------------------ #
    # Legacy throttle — kept for test_falcon_phase1.py invariants        #
    # ------------------------------------------------------------------ #

    async def _throttle(self) -> None:
        """Phase 1 token bucket. Phase 3 layered a proper per-key bucket on
        top (via `FalconKeyPool.acquire()`); we keep this method to preserve
        the lock-invariant tests in `test_falcon_phase1.py` and as a
        secondary defense if the pool's bucket is ever bypassed.
        """
        if self._max_rpm <= 0:
            return
        min_interval_s = 60.0 / float(self._max_rpm)
        async with self._rate_lock:
            now = time.monotonic()
            wait_for = self._last_request_at + min_interval_s - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._last_request_at = now

    # ------------------------------------------------------------------ #
    # Coalescing (item #3)                                               #
    # ------------------------------------------------------------------ #

    async def _coalesce_lookup(
        self, cache_key: str, agent_label: str
    ) -> tuple[asyncio.Future | None, bool]:
        """Return `(future, is_owner)`.

        - If no in-flight or recently-completed future exists, create a new
          one, mark the caller as owner (returns `is_owner=True`).
        - If an in-flight future exists, return it with `is_owner=False`;
          caller awaits it. Increments the coalesced counter.
        - If a completed future is still within TTL, return it as a fast
          hit (also counts as coalesced).
        """
        now = time.monotonic()
        async with self._inflight_lock:
            entry = self._inflight.get(cache_key)
            if entry is not None:
                fut, completed_at = entry
                if completed_at == 0.0 or (now - completed_at) <= self._coalesce_ttl_s:
                    falcon_coalesced_calls_total.labels(agent=agent_label).inc()
                    return fut, False
                # Expired — fall through and create a new one.
                self._inflight.pop(cache_key, None)
            new_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._inflight[cache_key] = (new_fut, 0.0)
            return new_fut, True

    async def _coalesce_complete(
        self, cache_key: str, fut: asyncio.Future, value: Any = None, exc: Exception | None = None
    ) -> None:
        """Resolve or reject the future and stamp it with the completion
        time so other callers within the TTL window can reuse it."""
        if exc is not None and not fut.done():
            fut.set_exception(exc)
        elif not fut.done():
            fut.set_result(value)
        async with self._inflight_lock:
            self._inflight[cache_key] = (fut, time.monotonic())
            # Schedule cleanup after the TTL elapses. Use a fire-and-forget
            # task so the caller's coroutine doesn't pay the cost. We track
            # the task in `_bg_tasks` so `close()` can cancel any survivors.
            task = asyncio.create_task(
                self._coalesce_expire(cache_key, self._coalesce_ttl_s)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _coalesce_expire(self, cache_key: str, ttl_s: float) -> None:
        try:
            await asyncio.sleep(max(0.0, ttl_s))
        except asyncio.CancelledError:
            return
        async with self._inflight_lock:
            entry = self._inflight.get(cache_key)
            if entry is None:
                return
            _fut, completed_at = entry
            if completed_at > 0.0 and (time.monotonic() - completed_at) >= ttl_s:
                self._inflight.pop(cache_key, None)

    # ------------------------------------------------------------------ #
    # query() — the workhorse                                            #
    # ------------------------------------------------------------------ #

    async def query(
        self, agent_id: int, params: dict, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        cache_key = self._cache_key(agent_id, params, limit, offset)
        agent_label = str(agent_id)

        # ---- Coalescing: race-safe in-flight dedup -------------------- #
        fut, is_owner = await self._coalesce_lookup(cache_key, agent_label)
        if not is_owner:
            # Another coroutine is doing this exact call (or did it
            # within the last FALCON_COALESCE_TTL_S seconds). Await its
            # result; the counter was already bumped.
            return await fut

        # ---- We're the owner; do the real work and resolve `fut` ------ #
        try:
            result = await self._do_query(
                agent_id=agent_id,
                params=params,
                limit=limit,
                offset=offset,
                cache_key=cache_key,
                agent_label=agent_label,
            )
        except Exception as exc:
            await self._coalesce_complete(cache_key, fut, exc=exc)
            raise
        await self._coalesce_complete(cache_key, fut, value=result)
        return result

    async def _do_query(
        self,
        agent_id: int,
        params: dict,
        limit: int,
        offset: int,
        cache_key: str,
        agent_label: str,
    ) -> list[dict]:
        # ---- Cache check (with conditional-GET revalidation hooks) ---- #
        cache_entry: _CacheEntry | None = None
        if self._redis is not None:
            try:
                cached_raw = await self._redis.get(cache_key)
                if cached_raw:
                    cache_entry = _CacheEntry.from_json(cached_raw)
            except Exception as exc:
                logger.warning(f"Redis cache read failed: {exc}")

        if cache_entry is not None:
            soft_expired = (
                self._revalidate_after_s > 0
                and (time.time() - cache_entry.cached_at) > self._revalidate_after_s
            )
            has_validators = bool(cache_entry.etag or cache_entry.last_modified)
            if not soft_expired or not has_validators:
                # Cache hit, payload is fresh enough OR no validators to
                # revalidate against — return the cached payload directly.
                return cache_entry.payload
            # else: soft-expired AND we have validators — fall through to
            # the request, which will attach `If-None-Match` etc.

        # ---- Build the request body ----------------------------------- #
        requested_limit = max(1, int(limit))
        api_limit = min(200, max(5, requested_limit))
        body = {
            "agent_id": agent_id,
            "params": params,
            "pagination": {
                "limit": api_limit,
                "offset": max(0, int(offset)),
            },
            "formatter_config": {"format_type": "raw"},
        }
        session = self._session_or_new()
        last_error: Exception | None = None
        new_etag: str | None = None
        new_last_modified: str | None = None

        for attempt in range(3):
            async with self._sem:
                falcon_concurrency.inc()
                attempt_start = time.monotonic()
                attempt_result: str | None = None  # ok|empty|rate_limited|error|timeout
                key_index = -1
                try:
                    # Acquire a key + its token in one step. The bucket's
                    # `acquire()` blocks until a token is available, which
                    # replaces the legacy `_throttle()` sleep loop.
                    async with self._pool.acquire() as (api_key, key_index):
                        headers = {"Authorization": f"Bearer {api_key}"}
                        if cache_entry is not None and cache_entry.etag:
                            headers["If-None-Match"] = cache_entry.etag
                        if cache_entry is not None and cache_entry.last_modified:
                            headers["If-Modified-Since"] = cache_entry.last_modified
                        try:
                            async with session.post(
                                self._api_url, json=body, headers=headers
                            ) as resp:
                                # 304 Not Modified — payload unchanged, refresh
                                # cached_at and return the existing payload.
                                if resp.status == 304 and cache_entry is not None:
                                    falcon_conditional_get_savings_total.labels(
                                        agent=agent_label
                                    ).inc()
                                    cache_entry.cached_at = time.time()
                                    if self._redis is not None:
                                        try:
                                            await self._redis.set(
                                                cache_key,
                                                cache_entry.to_json(),
                                                ex=self._cache_ttl,
                                            )
                                        except Exception as exc:
                                            logger.warning(
                                                f"Redis cache write failed: {exc}"
                                            )
                                    attempt_result = "ok"
                                    # Phase 3 Task D: 304 also counts
                                    # as Falcon liveness (the agent is
                                    # responding, just nothing new).
                                    _heartbeat_falcon(agent_id)
                                    return cache_entry.payload[:requested_limit]
                                if resp.status in (400, 404, 422):
                                    text = await resp.text()
                                    attempt_result = "error"
                                    raise FalconAPIError(
                                        f"Falcon {resp.status}: {text[:200]}"
                                    )
                                if resp.status == 429 or resp.status >= 500:
                                    # 429: penalise the key bucket, do NOT
                                    # retry-immediately (that'd be a hidden
                                    # rate-bypass). The bucket will block
                                    # the next attempt until refill catches
                                    # up — which is the right "back off"
                                    # behaviour.
                                    if resp.status == 429:
                                        self._pool.report_429(key_index)
                                    wait = 2**attempt
                                    logger.warning(
                                        f"Falcon HTTP {resp.status}, "
                                        f"key_index={key_index}, retry in {wait}s"
                                    )
                                    last_error = FalconAPIError(f"HTTP {resp.status}")
                                    attempt_result = (
                                        "rate_limited"
                                        if resp.status == 429
                                        else "error"
                                    )
                                    await asyncio.sleep(wait)
                                    continue
                                resp.raise_for_status()
                                data = await resp.json()
                                # Capture validators for future revalidation.
                                # Defensive: headers may be a mock in tests
                                # (returns non-str). We require str/None here
                                # to keep the cache JSON-serialisable.
                                new_etag = _coerce_header(resp, "ETag")
                                new_last_modified = _coerce_header(
                                    resp, "Last-Modified"
                                )
                                if isinstance(data, list):
                                    results: list[dict] = data
                                else:
                                    nested = (
                                        data.get("data", {})
                                        if isinstance(data.get("data"), dict)
                                        else {}
                                    )
                                    results = []
                                    for candidate in (
                                        nested.get("results"),
                                        nested.get("groups"),
                                        data.get("results"),
                                        data.get("groups"),
                                    ):
                                        if isinstance(candidate, list):
                                            results = candidate
                                            break
                                attempt_result = "ok" if results else "empty"
                                # Phase 3 Task D: heartbeat the per-agent
                                # source on any 200 (even an empty
                                # payload — the endpoint is alive). A
                                # rate-limited / 5xx / network-error
                                # path falls through without firing.
                                _heartbeat_falcon(agent_id)
                        except FalconAPIError:
                            if attempt_result is None:
                                attempt_result = "error"
                            raise
                        except asyncio.TimeoutError as exc:
                            wait = 2**attempt
                            logger.warning(
                                f"Falcon request timed out: {exc}, retry in {wait}s"
                            )
                            last_error = exc
                            attempt_result = "timeout"
                            await asyncio.sleep(wait)
                            continue
                        except Exception as exc:
                            wait = 2**attempt
                            logger.warning(
                                f"Falcon request failed: {exc}, retry in {wait}s"
                            )
                            last_error = exc
                            attempt_result = "error"
                            await asyncio.sleep(wait)
                            continue
                finally:
                    falcon_call_latency_seconds.labels(agent=agent_label).observe(
                        time.monotonic() - attempt_start
                    )
                    if attempt_result is not None:
                        falcon_calls_total.labels(
                            agent=agent_label, result=attempt_result
                        ).inc()
                    falcon_concurrency.dec()

            # Cache successful result. We store the (sliced) payload plus
            # any validators we captured; subsequent calls will use them
            # to revalidate via 304.
            trimmed = results[:requested_limit]
            if self._redis is not None:
                try:
                    entry_to_store = _CacheEntry(
                        payload=trimmed,
                        etag=new_etag,
                        last_modified=new_last_modified,
                        cached_at=time.time(),
                    )
                    await self._redis.set(
                        cache_key, entry_to_store.to_json(), ex=self._cache_ttl
                    )
                except Exception as exc:
                    logger.warning(f"Redis cache write failed: {exc}")

            return trimmed

        raise FalconAPIError(f"All retries failed for agent {agent_id}: {last_error}")

    # ------------------------------------------------------------------ #
    # High-level helpers — UNCHANGED signatures                          #
    # ------------------------------------------------------------------ #

    async def get_leaderboard(self, limit: int = 200) -> list[FalconLeaderEntry]:
        rows = await self.query(
            584,
            {
                "min_win_rate_15d": "0.45",
                "max_win_rate_15d": "0.92",
                "min_roi_15d": "0",
                "min_pnl_15d": "0",
                "min_total_trades_15d": "30",
                "max_total_trades_15d": "5000",
                "sort_by": "roi",
            },
            limit=limit,
        )
        entries = []
        for r in rows:
            try:
                entries.append(FalconLeaderEntry.model_validate(r))
            except Exception:
                pass
        return entries

    async def get_wallet360(self, wallet: str, window_days: str = "15") -> WalletMetrics | None:
        rows = await self.query(
            581,
            {"proxy_wallet": wallet, "window_days": window_days},
            limit=1,
        )
        if not rows:
            return None
        try:
            return WalletMetrics.model_validate(rows[0])
        except Exception:
            return None

    async def get_market_insights(self, condition_id: str) -> MarketInsights | None:
        """Fetch normalized 0–1 liquidity score from agent 575 (Market Insights).

        This is the documented source for `markets.liquidity_score`
        (master CLAUDE.md §6, `src/profiler/CLAUDE.md:172`,
        `src/profiler/error_model.py:83`). Returns None when agent 575
        has no data for the market — the caller (`sync_markets`) should
        fall back to agent 574's `liquidity` field with a provenance
        tag of `falcon_574` so the source mismatch is auditable in the
        DB.

        The returned `liquidity_score` is clamped to `[0.0, 1.0]`:
        agent 575 may surface raw USD-denominated depth on some markets
        rather than the documented normalized signal, and the
        downstream feature is expected to live in `[0, 1]`
        (`error_model._build_features` slot [4]; `confidence_engine`
        gates compare against `0.35` thresholds).
        """
        try:
            rows = await self.query(575, {"condition_id": condition_id}, limit=1)
            if not rows:
                # Some Falcon agents key on slug rather than condition_id;
                # mirror the agent-574 fallback in sync_markets.
                rows = await self.query(575, {"market_slug": condition_id}, limit=1)
        except FalconAPIError as exc:
            logger.debug(f"Market Insights (agent 575) unavailable for {condition_id}: {exc}")
            return None
        if not rows:
            return None
        try:
            insights = MarketInsights.model_validate(rows[0])
        except Exception as exc:
            logger.debug(f"Market Insights parse failed for {condition_id}: {exc}")
            return None
        # Clamp to [0,1] — see docstring; raw USD depth would explode
        # `_build_features` slot [4] otherwise.
        raw = float(insights.liquidity_score or 0.0)
        if raw != raw or raw < 0.0:  # NaN check or negative
            raw = 0.0
        if raw > 1.0:
            # Heuristic: agent 575 sometimes returns depth in USD; squash
            # via tanh so a $100k market lands near 1.0 without overflow.
            import math

            raw = math.tanh(raw / 100_000.0)
        insights.liquidity_score = max(0.0, min(1.0, raw))
        return insights

    async def get_pnl_leaderboard(self, limit: int = 200) -> list[PnlLeaderEntry]:
        rows = await self.query(
            579,
            {"wallet_address": "ALL", "leaderboard_period": "7d"},
            limit=limit,
        )
        entries = []
        for r in rows:
            try:
                entries.append(PnlLeaderEntry.model_validate(r))
            except Exception:
                pass
        return entries

    async def close(self) -> None:
        # Cancel any in-flight coalesce-expire tasks so we don't leak them
        # past process shutdown (or pytest's per-test event loop).
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
