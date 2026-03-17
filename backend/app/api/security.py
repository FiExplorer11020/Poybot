from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, WebSocket

from app.core.settings import get_settings

settings = get_settings()


class InMemoryRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True


rate_limiter = InMemoryRateLimiter(
    max_requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
)


def require_api_token(x_api_token: str | None = Header(default=None)) -> None:
    if not settings.api_auth_token:
        return
    if x_api_token != settings.api_auth_token:
        raise HTTPException(status_code=401, detail="invalid API token")


def require_ws_token(websocket: WebSocket) -> None:
    if not settings.live_ws_token:
        return
    ws_token = websocket.query_params.get("token") or websocket.headers.get("x-api-token")
    if ws_token != settings.live_ws_token:
        raise HTTPException(status_code=401, detail="invalid websocket token")


def rate_limit_key_from_request(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit_key_from_websocket(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "unknown"
