import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db_session
from app.db.base import Base
from app.main import app
from app.models import Event, Market, MarketStatus, Token, TopOfBook


def test_backtest_route_runs_with_sqlite_fixture() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(
                Event(
                    id="e1",
                    title="Event",
                    active=False,
                    resolved=True,
                    ends_at=start + timedelta(hours=1),
                )
            )
            session.add(
                Market(
                    id="m1",
                    event_id="e1",
                    question="Will BTC finish up?",
                    outcomes=["Yes", "No"],
                    tags=["test"],
                    status=MarketStatus.resolved,
                    active=False,
                    resolved=True,
                )
            )
            session.add_all(
                [
                    Token(id="yes-1", market_id="m1", outcome="Yes"),
                    Token(id="no-1", market_id="m1", outcome="No"),
                ]
            )
            session.add_all(
                [
                    TopOfBook(
                        market_id="m1",
                        token_id="yes-1",
                        best_bid=Decimal("0.42"),
                        best_ask=Decimal("0.44"),
                        mid_price=Decimal("0.43"),
                        spread=Decimal("0.02"),
                        observed_at=start + timedelta(minutes=1),
                    ),
                    TopOfBook(
                        market_id="m1",
                        token_id="yes-1",
                        best_bid=Decimal("0.47"),
                        best_ask=Decimal("0.49"),
                        mid_price=Decimal("0.48"),
                        spread=Decimal("0.02"),
                        observed_at=start + timedelta(minutes=15),
                    ),
                    TopOfBook(
                        market_id="m1",
                        token_id="yes-1",
                        best_bid=Decimal("0.52"),
                        best_ask=Decimal("0.54"),
                        mid_price=Decimal("0.53"),
                        spread=Decimal("0.02"),
                        observed_at=start + timedelta(minutes=30),
                    ),
                    TopOfBook(
                        market_id="m1",
                        token_id="yes-1",
                        best_bid=Decimal("0.95"),
                        best_ask=Decimal("0.99"),
                        mid_price=Decimal("0.97"),
                        spread=Decimal("0.04"),
                        observed_at=start + timedelta(minutes=70),
                    ),
                    TopOfBook(
                        market_id="m1",
                        token_id="no-1",
                        best_bid=Decimal("0.55"),
                        best_ask=Decimal("0.57"),
                        mid_price=Decimal("0.56"),
                        spread=Decimal("0.02"),
                        observed_at=start + timedelta(minutes=1),
                    ),
                    TopOfBook(
                        market_id="m1",
                        token_id="no-1",
                        best_bid=Decimal("0.03"),
                        best_ask=Decimal("0.05"),
                        mid_price=Decimal("0.04"),
                        spread=Decimal("0.02"),
                        observed_at=start + timedelta(minutes=70),
                    ),
                ]
            )
            await session.commit()

    asyncio.run(seed())

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/backtest",
                json={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "initial_equity": 1000.0,
                    "strategy": "adaptive",
                    "fee_bps": 8.0,
                    "risk_cfg": {
                        "allocation_mode": "manual",
                        "manual_notional_amount": 100.0,
                        "min_observations": 1,
                        "min_signal_strength": 0.0,
                        "base_entry_threshold": 0.0,
                        "spread_cap": 0.10,
                        "cooldown_seconds": 3600,
                    },
                },
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_trades"] >= 1
    assert payload["winning_trades"] >= 1
    assert payload["total_pnl"] > 0
    assert payload["equity_curve"]
    assert payload["trades"][0]["side"] == "BUY_YES"
