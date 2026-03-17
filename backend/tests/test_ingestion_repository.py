import pytest
from sqlalchemy import func, select

from app.models import Event, Market, MarketStatus, Trade
from app.repositories.ingestion_repository import IngestionRepository


@pytest.mark.anyio
async def test_trade_upsert_idempotent(session_factory) -> None:
    payload = {
        "id": "tr1",
        "token_id": "tok1",
        "side": "buy",
        "price": "0.52",
        "size": "14",
        "timestamp": "2025-01-01T00:00:00Z",
    }
    async with session_factory() as session:
        session.add(Event(id="e1", title="Event", active=True, resolved=False))
        session.add(
            Market(
                id="m1",
                event_id="e1",
                question="q",
                outcomes=["Yes", "No"],
                tags=["test"],
                status=MarketStatus.active,
                active=True,
                resolved=False,
            )
        )
        await session.commit()

        repo = IngestionRepository(session)
        await repo.upsert_trade(payload, market_id="m1")
        await repo.upsert_trade(payload, market_id="m1")
        await session.commit()

        total = await session.scalar(select(func.count()).select_from(Trade))
        assert total == 1
