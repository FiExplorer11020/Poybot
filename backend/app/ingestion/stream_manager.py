from collections import defaultdict

from app.ingestion.universe import MarketUniverse


class StreamSubscriptionPlan:
    """Splits all active token ids into websocket subscription shards.

    This redesign is the base for scaling to all platform tickers while keeping
    websocket payload sizes controlled.
    """

    def __init__(self, chunk_size: int = 250) -> None:
        self.chunk_size = chunk_size

    def build(self, universe: list[MarketUniverse]) -> list[list[str]]:
        token_ids: list[str] = []
        for market in universe:
            token_ids.extend(market.token_ids)
        uniq = list(dict.fromkeys(token_ids))
        return [uniq[idx : idx + self.chunk_size] for idx in range(0, len(uniq), self.chunk_size)]

    def by_market(self, universe: list[MarketUniverse]) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = defaultdict(list)
        for market in universe:
            mapping[market.market_id] = market.token_ids
        return dict(mapping)
