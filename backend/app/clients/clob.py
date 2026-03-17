from typing import Any

import httpx


class ClobClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def fetch_trades(self, market: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        resp = await self._client.get("/trades", params={"market": market, "limit": limit, "offset": offset})
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def fetch_book_snapshot(self, token_id: str) -> dict[str, Any]:
        resp = await self._client.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
