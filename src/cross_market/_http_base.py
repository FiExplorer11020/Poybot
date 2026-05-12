"""Shared cross-market HTTP plumbing — adaptive token bucket + metric
helpers.

Mirrors the FalconClient pattern (spec § 4.1 — "rate limit handling
reuses the FalconClient adaptive token bucket pattern"). We don't reuse
:class:`FalconClient` directly because it's coupled to Falcon's request
shape; instead we re-implement the bucket against the same primitives
so every venue client can carry its own per-venue limits.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger


# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        crossmarket_api_calls_total,
        crossmarket_api_latency_seconds,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def observe(self, *_a, **_kw):
            return None

    crossmarket_api_calls_total = _NoOp()  # type: ignore[assignment]
    crossmarket_api_latency_seconds = _NoOp()  # type: ignore[assignment]


class _TokenBucket:
    """Async-safe adaptive token bucket. Bucket starts full; tokens
    refill at ``refill_per_sec`` up to ``capacity``. ``acquire`` blocks
    until a token is available.
    """

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._refill = float(refill_per_sec)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill_tokens(self) -> None:
        now = time.monotonic()
        delta = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + delta * self._refill)
        self._last_refill = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill_tokens()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / max(self._refill, 1e-6)
                await asyncio.sleep(wait_s)


@dataclass
class HTTPResponse:
    """Tiny wrapper around the per-venue request result. We don't expose
    aiohttp internals — keeps tests simple."""

    status: int
    json_payload: Any
    elapsed_s: float


class VenueClient:
    """Common surface for Kalshi / Manifold / PredictIt clients.

    Subclasses set ``venue`` + ``_base_url`` and call :meth:`_get` /
    :meth:`_post`. All metric instrumentation lives here so the per-venue
    files stay short.
    """

    venue: str = "venue"

    def __init__(
        self,
        http_session: Any,
        *,
        base_url: str,
        bucket_capacity: int = 30,
        bucket_refill_per_sec: float = 1.0,
        timeout_s: float = 8.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._http = http_session
        self._base_url = base_url.rstrip("/")
        self._bucket = _TokenBucket(bucket_capacity, bucket_refill_per_sec)
        self._timeout_s = float(timeout_s)
        self._default_headers: dict[str, str] = dict(default_headers or {})
        self.last_successful_call: float = 0.0

    def _record(self, result: str, elapsed_s: float) -> None:
        try:
            crossmarket_api_calls_total.labels(
                venue=self.venue, result=result
            ).inc()
            crossmarket_api_latency_seconds.labels(venue=self.venue).observe(
                elapsed_s
            )
        except Exception:  # pragma: no cover
            pass
        if result == "ok":
            self.last_successful_call = time.time()

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> HTTPResponse:
        await self._bucket.acquire()
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        t0 = time.perf_counter()
        try:
            async with self._http.get(
                url,
                params=params,
                headers=self._default_headers,
                timeout=self._timeout_s,
            ) as resp:
                elapsed = time.perf_counter() - t0
                if resp.status == 429:
                    self._record("rate_limited", elapsed)
                    return HTTPResponse(
                        status=429, json_payload=None, elapsed_s=elapsed
                    )
                if resp.status >= 400:
                    self._record("error", elapsed)
                    return HTTPResponse(
                        status=resp.status, json_payload=None, elapsed_s=elapsed
                    )
                try:
                    payload = await resp.json()
                except Exception:
                    payload = None
                self._record("ok", elapsed)
                return HTTPResponse(
                    status=resp.status,
                    json_payload=payload,
                    elapsed_s=elapsed,
                )
        except asyncio.TimeoutError:
            self._record("timeout", time.perf_counter() - t0)
            return HTTPResponse(status=0, json_payload=None, elapsed_s=0.0)
        except Exception as exc:
            self._record("error", time.perf_counter() - t0)
            logger.debug(
                f"{type(self).__name__}: GET {path} failed: {exc}"
            )
            return HTTPResponse(status=0, json_payload=None, elapsed_s=0.0)


__all__ = ["HTTPResponse", "VenueClient", "_TokenBucket"]
