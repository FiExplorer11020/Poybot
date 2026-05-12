"""Round 12 — Kalshi REST client (spec § 4.1).

Authenticated read-only. The aiohttp session is injected by the daemon
so tests can mock it.

What we DON'T implement:
  * Trade execution. Spec § 7 risk row 4 — we only read.
  * The full Kalshi schema. We model just the fields the position
    aggregator needs: market metadata + wallet positions + recent trade
    history. Adding fields is mechanical.

Acquiring the API key itself is operator-deliverable (free, rate-limited
— spec § 7). Tests run against mocked aiohttp.
"""
from __future__ import annotations

from typing import Any

from src.config import settings
from src.cross_market._http_base import VenueClient


class KalshiClient(VenueClient):
    """Read-only Kalshi REST client.

    Methods are kept narrow + obvious — each returns the parsed JSON
    payload (a dict / list of dicts) or an empty list/None on failure.
    The aggregator translates these to the unified
    :class:`cross_market_positions` row shape.
    """

    venue: str = "kalshi"

    def __init__(
        self,
        http_session: Any,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        token = api_key if api_key is not None else settings.KALSHI_API_KEY
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        super().__init__(
            http_session,
            base_url=base_url or settings.KALSHI_BASE_URL,
            bucket_capacity=settings.CROSS_MARKET_BUCKET_CAPACITY,
            bucket_refill_per_sec=settings.CROSS_MARKET_BUCKET_REFILL_PER_SEC,
            timeout_s=settings.CROSS_MARKET_HTTP_TIMEOUT_S,
            default_headers=headers,
        )

    async def fetch_market(self, market_id: str) -> dict[str, Any] | None:
        """GET /markets/<id>. Returns the parsed JSON or None on error.

        The Kalshi v2 schema wraps the market under a top-level
        ``market`` key; we unwrap when present.
        """
        resp = await self._get(f"/markets/{market_id}")
        if resp.status != 200 or not isinstance(resp.json_payload, dict):
            return None
        return resp.json_payload.get("market") or resp.json_payload

    async def fetch_wallet_positions(
        self, account: str
    ) -> list[dict[str, Any]]:
        """Positions for ``account``. Spec § 4.1 — read-only; the
        operator's API key controls which accounts are visible. The
        wallet resolver injects this account string from
        :class:`cross_market_operators`.

        Returns a list of position dicts (possibly empty). Schema fields
        used by the aggregator: ``ticker``, ``market_id``,
        ``position`` (positive = YES, negative = NO), ``last_price``,
        ``volume`` (USD), ``created_time``, ``closed_time``.
        """
        # Kalshi's positions endpoint is paginated; we request the
        # default page and trust the operator's daemon cadence to cover
        # tail entries on subsequent polls.
        resp = await self._get(
            "/portfolio/positions",
            params={"account_id": account, "limit": 200},
        )
        if resp.status != 200 or not isinstance(resp.json_payload, dict):
            return []
        positions = resp.json_payload.get("market_positions")
        if isinstance(positions, list):
            return positions
        return []

    async def stream_trades(
        self, market_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Recent trade events for a small set of markets. We don't open
        a websocket here — the aggregator cadence (hourly) is well
        within the REST page's window so polling is the simpler path.

        Returns a list of trade dicts; empty on failure.
        """
        if not market_ids:
            return []
        # Kalshi /trades returns recent trades across all markets;
        # we filter post-fetch.
        resp = await self._get(
            "/markets/trades",
            params={"limit": 1000},
        )
        if resp.status != 200 or not isinstance(resp.json_payload, dict):
            return []
        all_trades = resp.json_payload.get("trades") or []
        if not isinstance(all_trades, list):
            return []
        wanted = set(market_ids)
        return [
            t for t in all_trades
            if isinstance(t, dict)
            and (str(t.get("ticker") or "") in wanted
                 or str(t.get("market_id") or "") in wanted)
        ]


__all__ = ["KalshiClient"]
