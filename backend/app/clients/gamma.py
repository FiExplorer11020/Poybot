from typing import Any

import httpx


class GammaClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def fetch_markets(self, limit: int = 100, offset: int = 0, active: bool = True) -> list[dict[str, Any]]:
        params = {"limit": limit, "offset": offset, "active": str(active).lower()}
        resp = await self._client.get("/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_events(self, limit: int = 100, offset: int = 0, active: bool = True) -> list[dict[str, Any]]:
        params = {"limit": limit, "offset": offset, "active": str(active).lower()}
        resp = await self._client.get("/events", params=params)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
