"""Round 12 — Manifold Markets REST client (spec § 4.1).

Manifold is play-money — no auth needed for read-only access. Per spec
§ 4.1 it's the "farm league" signal: traders on Manifold often appear
on Polymarket later.

The Manifold API ships a clean public surface at /v0/users/<username>/bets
+ /v0/markets/<id>. We model just what the position aggregator + wallet
resolver need.
"""
from __future__ import annotations

from typing import Any

from src.config import settings
from src.cross_market._http_base import VenueClient


class ManifoldClient(VenueClient):
    """Read-only Manifold REST client. No API key."""

    venue: str = "manifold"

    def __init__(
        self,
        http_session: Any,
        *,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            http_session,
            base_url=base_url or settings.MANIFOLD_BASE_URL,
            bucket_capacity=settings.CROSS_MARKET_BUCKET_CAPACITY,
            bucket_refill_per_sec=settings.CROSS_MARKET_BUCKET_REFILL_PER_SEC,
            timeout_s=settings.CROSS_MARKET_HTTP_TIMEOUT_S,
        )

    async def fetch_market(self, market_id: str) -> dict[str, Any] | None:
        """GET /market/<id>. Returns the parsed market or None.

        Note: Manifold's slug shape (e.g. "will-the-fed-hike-rates")
        works in the URL path; numeric ids work too. The caller is
        responsible for passing whatever the operator stored.
        """
        resp = await self._get(f"/market/{market_id}")
        if resp.status != 200 or not isinstance(resp.json_payload, dict):
            return None
        return resp.json_payload

    async def fetch_wallet_positions(
        self, handle: str
    ) -> list[dict[str, Any]]:
        """Returns the user's recent bets (Manifold's positions
        primitive). Schema fields the aggregator reads:
        ``contractId`` (market_id), ``outcome`` ('YES'/'NO'),
        ``amount`` (mana — we convert in the aggregator if needed),
        ``createdTime``, ``isFilled``, ``isCancelled``.
        """
        resp = await self._get(f"/bets", params={"username": handle, "limit": 100})
        if resp.status != 200:
            return []
        data = resp.json_payload
        if isinstance(data, list):
            return data
        return []

    async def stream_trades(
        self, market_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Manifold doesn't expose a per-market trade stream in the
        public API. We approximate by sweeping recent bets across the
        requested markets via /bets?contractId=<id>.
        """
        out: list[dict[str, Any]] = []
        for market_id in market_ids:
            resp = await self._get(
                "/bets",
                params={"contractId": market_id, "limit": 50},
            )
            if resp.status != 200 or not isinstance(resp.json_payload, list):
                continue
            out.extend(resp.json_payload)
        return out


__all__ = ["ManifoldClient"]
