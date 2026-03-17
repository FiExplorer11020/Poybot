from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


async def request_with_retry(
    operation: Callable[[], Awaitable[T]],
    retries: int = 4,
    base_delay_s: float = 0.3,
    max_delay_s: float = 5.0,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await operation()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt == retries:
                break
            backoff = min(max_delay_s, base_delay_s * (2**attempt))
            jitter = random.uniform(0, backoff * 0.2)
            await asyncio.sleep(backoff + jitter)
    if last_exc is None:
        raise RuntimeError("retry operation failed without exception")
    raise last_exc
