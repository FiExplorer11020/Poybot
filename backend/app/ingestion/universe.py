import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.clients.gamma import GammaClient
from app.utils.polymarket import parse_json_list_field

log = logging.getLogger(__name__)


@dataclass
class MarketUniverse:
    market_id: str
    market_title: str
    token_ids: list[str]
    expiry_ts: float | None = None


@dataclass
class UniverseFilter:
    min_volume_24h: float = 1000.0
    max_spread_pct: float = 0.08
    min_end_date_hours: float = 1.0
    max_end_date_hours: float = 24.0
    crypto_only: bool = True
    keyword_allow: list[str] = field(
        default_factory=lambda: ["BTC", "ETH", "SOL", "Bitcoin", "Ethereum", "Solana"]
    )
    keyword_deny: list[str] = field(default_factory=lambda: ["2028", "2027", "election"])


class UniverseBuilder:
    """Builds a tradable market universe for all active Polymarket tickers.

    This structure is intentionally separated so we can plug additional filters
    (liquidity, spread, resolution horizon, strategy allow-lists) without changing
    websocket ingestion or API layers.
    """

    def __init__(self, gamma_client: GammaClient) -> None:
        self.gamma_client = gamma_client

    async def fetch_active_universe(
        self,
        page_size: int = 500,
        max_pages: int = 20,
        filters: UniverseFilter | None = None,
    ) -> list[MarketUniverse]:
        universe: list[MarketUniverse] = []
        for page in range(max_pages):
            markets = await self.gamma_client.fetch_markets(
                limit=page_size,
                offset=page * page_size,
                active=True,
            )
            if not markets:
                break
            if filters is not None:
                markets = self._apply_filters(markets, filters)
            for market in markets:
                token_ids = [
                    str(token) for token in parse_json_list_field(market.get("clobTokenIds"))
                ]
                if not token_ids:
                    continue
                universe.append(
                    MarketUniverse(
                        market_id=str(market.get("id")),
                        market_title=(
                            market.get("question") or market.get("title") or "Unknown market"
                        ),
                        token_ids=token_ids,
                        expiry_ts=self._parse_expiry_ts(market),
                    )
                )
        return universe

    def _apply_filters(self, markets: list[dict], f: UniverseFilter) -> list[dict]:
        now = datetime.now(UTC)
        min_end_date = now + timedelta(hours=f.min_end_date_hours)
        max_end_date = now + timedelta(hours=f.max_end_date_hours)

        rejected = {
            "missing_end_date": 0,
            "end_date_window": 0,
            "keyword_allow": 0,
            "keyword_deny": 0,
            "volume_24h": 0,
            "spread": 0,
        }
        filtered: list[dict] = []

        for market in markets:
            end_date = self._parse_end_date(market)
            if end_date is None:
                rejected["missing_end_date"] += 1
                continue
            if end_date < min_end_date or end_date > max_end_date:
                rejected["end_date_window"] += 1
                continue

            question = str(market.get("question") or market.get("title") or "")
            question_lower = question.casefold()

            if f.crypto_only and f.keyword_allow:
                if not any(keyword.casefold() in question_lower for keyword in f.keyword_allow):
                    rejected["keyword_allow"] += 1
                    continue

            if f.keyword_deny and any(
                keyword.casefold() in question_lower for keyword in f.keyword_deny
            ):
                rejected["keyword_deny"] += 1
                continue

            volume_24h = self._extract_volume_24h(market)
            if volume_24h is None or volume_24h < f.min_volume_24h:
                rejected["volume_24h"] += 1
                continue

            spread_pct = self._extract_spread_pct(market)
            if spread_pct is None or spread_pct > f.max_spread_pct:
                rejected["spread"] += 1
                continue

            filtered.append(market)

        log.debug(
            "Universe filters applied: input=%s kept=%s rejected_by_filter=%s",
            len(markets),
            len(filtered),
            rejected,
        )
        return filtered

    @staticmethod
    def _parse_end_date(market: dict) -> datetime | None:
        end_date_raw = UniverseBuilder._first_present_value(
            market, "endDate", "endDateIso", "end_date_iso", "end_date"
        )
        if end_date_raw is None:
            return None
        try:
            end_date = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        return end_date

    @staticmethod
    def _parse_expiry_ts(market: dict) -> float | None:
        end_date = UniverseBuilder._parse_end_date(market)
        if end_date is None:
            return None
        return end_date.timestamp()

    @staticmethod
    def _extract_volume_24h(market: dict) -> float | None:
        raw_value = UniverseBuilder._first_present_value(
            market,
            "volume24hr",
            "volume24Hr",
            "volume24h",
            "volume24H",
            "volume_24h",
            "volume24hrClob",
            "volume24hClob",
        )
        return UniverseBuilder._coerce_float(raw_value)

    @staticmethod
    def _extract_spread_pct(market: dict) -> float | None:
        direct_spread = UniverseBuilder._coerce_float(
            UniverseBuilder._first_present_value(
                market,
                "spreadPct",
                "spread_pct",
                "spreadPercentage",
                "spread_percentage",
                "spread",
            )
        )
        if direct_spread is not None:
            return direct_spread / 100 if direct_spread > 1 else direct_spread

        best_bid = UniverseBuilder._coerce_float(
            UniverseBuilder._first_present_value(market, "bestBid", "best_bid")
        )
        best_ask = UniverseBuilder._coerce_float(
            UniverseBuilder._first_present_value(market, "bestAsk", "best_ask")
        )
        if best_bid is None or best_ask is None:
            return None
        return max(0.0, best_ask - best_bid)

    @staticmethod
    def _first_present_value(market: dict, *keys: str) -> object | None:
        for key in keys:
            if key in market and market.get(key) not in (None, ""):
                return market.get(key)
        return None

    @staticmethod
    def _coerce_float(value: object | None) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
