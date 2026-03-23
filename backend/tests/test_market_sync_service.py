import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Market
from app.services.market_sync_service import MarketSyncService


class FakeGammaClient:
    async def fetch_events(self, limit: int, offset: int, active: bool = True):
        return [{"id": "e1", "title": "Election", "slug": "election-2028", "active": True}]

    async def fetch_markets(self, limit: int, offset: int, active: bool = True):
        return [
            {
                "id": "m1",
                "conditionId": "cond-1",
                "events": [{"id": "e1"}],
                "question": "Will X win?",
                "outcomes": "[\"Yes\", \"No\"]",
                "clobTokenIds": "[\"t_yes\", \"t_no\"]",
                "active": True,
                "resolved": False,
            }
        ]


@pytest.mark.anyio
async def test_market_sync_service(session_factory) -> None:
    async with session_factory() as session:
        service = MarketSyncService(session, FakeGammaClient())
        result = await service.sync_metadata(page_size=10, pages=1)

        assert result["events_synced"] == 1
        assert result["markets_synced"] == 1

        market = await session.scalar(
            select(Market).options(selectinload(Market.tokens)).where(Market.id == "m1")
        )
        assert market is not None
        assert market.event_id == "e1"
        assert market.condition_id == "cond-1"
        assert market.outcomes == ["Yes", "No"]
        assert sorted(token.id for token in market.tokens) == ["t_no", "t_yes"]
