from dataclasses import dataclass

from app.clients.gamma import GammaClient


@dataclass
class MarketUniverse:
    market_id: str
    market_title: str
    token_ids: list[str]


class UniverseBuilder:
    """Builds a tradable market universe for all active Polymarket tickers.

    This structure is intentionally separated so we can plug additional filters
    (liquidity, spread, resolution horizon, strategy allow-lists) without changing
    websocket ingestion or API layers.
    """

    def __init__(self, gamma_client: GammaClient) -> None:
        self.gamma_client = gamma_client

    async def fetch_active_universe(self, page_size: int = 500, max_pages: int = 20) -> list[MarketUniverse]:
        universe: list[MarketUniverse] = []
        for page in range(max_pages):
            markets = await self.gamma_client.fetch_markets(limit=page_size, offset=page * page_size, active=True)
            if not markets:
                break
            for market in markets:
                token_ids = [str(token) for token in market.get("clobTokenIds", [])]
                if not token_ids:
                    continue
                universe.append(
                    MarketUniverse(
                        market_id=str(market.get("id")),
                        market_title=market.get("question") or market.get("title") or "Unknown market",
                        token_ids=token_ids,
                    )
                )
        return universe
