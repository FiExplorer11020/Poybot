import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.ingestion.universe import UniverseBuilder, UniverseFilter


class FakeGammaClient:
    def __init__(self, markets: list[dict]) -> None:
        self._markets = markets

    async def fetch_markets(self, limit: int, offset: int, active: bool = True):
        if offset > 0:
            return []
        return self._markets


def make_market(
    *,
    market_id: str = "m1",
    question: str = "Will BTC be above $100k tomorrow?",
    end_date_hours: float | None = 6,
    volume_24h: float = 5_000.0,
    best_bid: float = 0.46,
    best_ask: float = 0.52,
) -> dict:
    market = {
        "id": market_id,
        "question": question,
        "clobTokenIds": "[\"t_yes\", \"t_no\"]",
        "volume24hr": volume_24h,
        "bestBid": best_bid,
        "bestAsk": best_ask,
    }
    if end_date_hours is not None:
        market["endDate"] = (
            datetime.now(UTC) + timedelta(hours=end_date_hours)
        ).isoformat().replace("+00:00", "Z")
    return market


@pytest.mark.anyio
async def test_universe_builder_parses_stringified_token_ids() -> None:
    builder = UniverseBuilder(
        FakeGammaClient(
            [
                {
                    "id": "m1",
                    "question": "Will X win?",
                    "clobTokenIds": "[\"t_yes\", \"t_no\"]",
                }
            ]
        )
    )

    markets = await builder.fetch_active_universe(page_size=10, max_pages=2)

    assert len(markets) == 1
    assert markets[0].token_ids == ["t_yes", "t_no"]


@pytest.mark.anyio
async def test_universe_builder_keeps_market_that_passes_quality_filters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builder = UniverseBuilder(FakeGammaClient([make_market()]))

    with caplog.at_level(logging.DEBUG, logger="app.ingestion.universe"):
        markets = await builder.fetch_active_universe(filters=UniverseFilter())

    assert len(markets) == 1
    assert markets[0].market_id == "m1"
    assert "kept=1" in caplog.text


@pytest.mark.anyio
async def test_universe_builder_rejects_market_with_spread_above_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builder = UniverseBuilder(
        FakeGammaClient(
            [make_market(question="Will ETH rally today?", best_bid=0.40, best_ask=0.55)]
        )
    )

    with caplog.at_level(logging.DEBUG, logger="app.ingestion.universe"):
        markets = await builder.fetch_active_universe(filters=UniverseFilter())

    assert markets == []
    assert "'spread': 1" in caplog.text


@pytest.mark.anyio
async def test_universe_builder_rejects_market_with_denied_keyword(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builder = UniverseBuilder(
        FakeGammaClient([make_market(question="Will Bitcoin react to the election today?")])
    )

    with caplog.at_level(logging.DEBUG, logger="app.ingestion.universe"):
        markets = await builder.fetch_active_universe(filters=UniverseFilter())

    assert markets == []
    assert "'keyword_deny': 1" in caplog.text


@pytest.mark.anyio
async def test_universe_builder_rejects_market_without_end_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builder = UniverseBuilder(FakeGammaClient([make_market(end_date_hours=None)]))

    with caplog.at_level(logging.DEBUG, logger="app.ingestion.universe"):
        markets = await builder.fetch_active_universe(filters=UniverseFilter())

    assert markets == []
    assert "'missing_end_date': 1" in caplog.text
