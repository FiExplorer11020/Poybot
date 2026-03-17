from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.analytics.market_analytics import MarketAnalyticsService
from app.models import Event, Market, MarketStatus, TopOfBook, Trade


@pytest.mark.asyncio
async def test_market_summary(session_factory) -> None:
    async with session_factory() as session:
        session.add(Event(id="e1", title="Event", active=True, resolved=False))
        session.add(
            Market(
                id="m1",
                event_id="e1",
                question="q",
                outcomes=["Yes", "No"],
                tags=[],
                status=MarketStatus.active,
                active=True,
                resolved=False,
            )
        )
        session.add(
            TopOfBook(
                market_id="m1",
                token_id="t1",
                best_bid=Decimal("0.49"),
                best_ask=Decimal("0.51"),
                mid_price=Decimal("0.50"),
                spread=Decimal("0.02"),
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            Trade(
                id="tr1",
                market_id="m1",
                token_id="t1",
                side="buy",
                price=Decimal("0.50"),
                size=Decimal("123"),
                traded_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

        svc = MarketAnalyticsService(session)
        summary = await svc.market_summary("m1")
        assert summary["implied_probability"] == 0.5
        assert summary["volume_24h"] == Decimal("123")
        assert summary["consistency_flag"] is True
