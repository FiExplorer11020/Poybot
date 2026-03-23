from typing import Any

import httpx

from app.clients.http_utils import request_with_retry


class GammaClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=5.0))

    async def fetch_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "active": str(active).lower()}
        if closed is not None:
            params["closed"] = str(closed).lower()

        async def _op() -> list[dict[str, Any]]:
            resp = await self._client.get("/markets", params=params)
            resp.raise_for_status()
            return resp.json()

        return await request_with_retry(_op)

    async def fetch_events(self, limit: int = 100, offset: int = 0, active: bool = True) -> list[dict[str, Any]]:
        params = {"limit": limit, "offset": offset, "active": str(active).lower()}

        async def _op() -> list[dict[str, Any]]:
            resp = await self._client.get("/events", params=params)
            resp.raise_for_status()
            return resp.json()

        return await request_with_retry(_op)

    async def close(self) -> None:
        await self._client.aclose()
