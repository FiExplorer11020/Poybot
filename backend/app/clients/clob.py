from typing import Any

import httpx

from app.clients.http_utils import request_with_retry


class ClobClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=5.0))

    async def fetch_trades(self, market: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        async def _op() -> list[dict[str, Any]]:
            resp = await self._client.get("/trades", params={"market": market, "limit": limit, "offset": offset})
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload

        return await request_with_retry(_op)

    async def fetch_book_snapshot(self, token_id: str) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            resp = await self._client.get("/book", params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()

        return await request_with_retry(_op)


    async def place_order(self, payload: dict[str, Any], headers: dict[str, str] | None = None, endpoint: str = "/order") -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            resp = await self._client.post(endpoint, json=payload, headers=headers or {})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
            return {"data": data}

        return await request_with_retry(_op)

    async def close(self) -> None:
        await self._client.aclose()
