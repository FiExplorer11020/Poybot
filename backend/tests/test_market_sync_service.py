import pytest

from app.services.market_sync_service import MarketSyncService


class FakeGammaClient:
    async def fetch_events(self, limit: int, offset: int, active: bool = True):
        return [{"id": "e1", "title": "Election", "slug": "election-2028", "active": True}]

    async def fetch_markets(self, limit: int, offset: int, active: bool = True):
        return [
            {
                "id": "m1",
                "eventId": "e1",
                "question": "Will X win?",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["t_yes", "t_no"],
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
