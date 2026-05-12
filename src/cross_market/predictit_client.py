"""Round 12 — PredictIt market-data client (spec § 4.1).

PredictIt is US-regulated and exposes ONLY public market prices +
per-market aggregates — no individual positions. We model this honestly:
``fetch_wallet_positions`` is a no-op surface that returns [] (PredictIt
doesn't allow this read), so the aggregator can iterate every venue with
the same interface but PredictIt-derived rows are market-level
aggregates only.
"""
from __future__ import annotations

from typing import Any

from src.config import settings
from src.cross_market._http_base import VenueClient


class PredictItClient(VenueClient):
    """Read-only PredictIt market-data client. Public; no key."""

    venue: str = "predictit"

    def __init__(
        self,
        http_session: Any,
        *,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            http_session,
            base_url=base_url or settings.PREDICTIT_BASE_URL,
            bucket_capacity=settings.CROSS_MARKET_BUCKET_CAPACITY,
            bucket_refill_per_sec=settings.CROSS_MARKET_BUCKET_REFILL_PER_SEC,
            timeout_s=settings.CROSS_MARKET_HTTP_TIMEOUT_S,
        )

    async def fetch_market(self, market_id: str) -> dict[str, Any] | None:
        """GET /markets/<id>. Returns the parsed market dict.

        PredictIt's response wraps multiple "contracts" (event branches)
        under one market id — we surface the dict as-is.
        """
        resp = await self._get(f"/markets/{market_id}")
        if resp.status != 200 or not isinstance(resp.json_payload, dict):
            return None
        return resp.json_payload

    async def fetch_wallet_positions(
        self, account: str  # noqa: ARG002 — interface parity
    ) -> list[dict[str, Any]]:
        """PredictIt does NOT expose individual positions to public
        readers (spec § 4.1). Returns [] always.

        The aggregator handles this gracefully — the operator's position
        rows for PredictIt are derived from the market-level aggregates
        (volume, share-distribution) rather than per-wallet snapshots.
        """
        return []

    async def stream_trades(
        self, market_ids: list[str]
    ) -> list[dict[str, Any]]:
        """PredictIt's public API exposes market-level price snapshots
        only — there's no per-trade endpoint. We expose the current
        market state (one entry per requested market_id) as a
        trade-shaped placeholder so the aggregator can iterate uniformly.
        """
        out: list[dict[str, Any]] = []
        for market_id in market_ids:
            market = await self.fetch_market(market_id)
            if market is None:
                continue
            # PredictIt's "Contracts" array carries the per-side
            # prices; we surface each as a market-state snapshot.
            for contract in market.get("Contracts", []) or []:
                out.append(
                    {
                        "market_id": market_id,
                        "contract_id": contract.get("ID"),
                        "name": contract.get("Name"),
                        "last_trade_price": contract.get("LastTradePrice"),
                        "best_buy_yes_cost": contract.get("BestBuyYesCost"),
                        "best_buy_no_cost": contract.get("BestBuyNoCost"),
                        "best_sell_yes_cost": contract.get("BestSellYesCost"),
                        "best_sell_no_cost": contract.get("BestSellNoCost"),
                    }
                )
        return out


__all__ = ["PredictItClient"]
