"""
Unit tests for database connection pool and dataclass models.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import (
    LeaderEvent,
    LeaderScore,
    Market,
    OrderbookSnapshot,
    PaperTrade,
    Trade,
    VolumeSpike,
    Wallet,
    WalletCluster,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(**kwargs):
    """Build a dict-like mock that supports item access."""
    m = MagicMock()
    m.__getitem__ = lambda self, k: kwargs[k]
    return m


NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Connection pool tests
# ---------------------------------------------------------------------------


class TestConnectionPool:
    @pytest.mark.asyncio
    async def test_initialize_pool_success(self):
        mock_pool = MagicMock()
        with patch(
            "src.database.connection.asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)
        ):
            import src.database.connection as conn_module

            conn_module._pool = None
            await conn_module.initialize_pool("postgresql://test/test")
            assert conn_module._pool is mock_pool

    @pytest.mark.asyncio
    async def test_initialize_pool_retries_then_succeeds(self):
        mock_pool = AsyncMock()
        call_count = 0

        async def flaky_pool(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("not ready")
            return mock_pool

        with patch("src.database.connection.asyncpg.create_pool", side_effect=flaky_pool):
            with patch("src.database.connection.asyncio.sleep", new_callable=AsyncMock):
                import src.database.connection as conn_module

                conn_module._pool = None
                await conn_module.initialize_pool("postgresql://test/test")
                assert conn_module._pool is mock_pool
                assert call_count == 3

    @pytest.mark.asyncio
    async def test_close_pool(self):
        mock_pool = AsyncMock()
        import src.database.connection as conn_module

        conn_module._pool = mock_pool
        await conn_module.close_pool()
        mock_pool.close.assert_awaited_once()
        assert conn_module._pool is None

    @pytest.mark.asyncio
    async def test_get_db_yields_connection(self):
        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        import src.database.connection as conn_module

        conn_module._pool = mock_pool

        async with conn_module.get_db() as conn:
            assert conn is mock_conn

    @pytest.mark.asyncio
    async def test_get_db_raises_if_no_pool(self):
        import src.database.connection as conn_module

        conn_module._pool = None
        with pytest.raises(RuntimeError, match="not initialized"):
            async with conn_module.get_db():
                pass


# ---------------------------------------------------------------------------
# Model: Market
# ---------------------------------------------------------------------------


class TestMarketModel:
    def test_from_row(self):
        row = _record(
            market_id="mkt-001",
            question="Will X happen?",
            category="politics",
            token_yes="tok_yes",
            token_no="tok_no",
            end_date=NOW,
            last_price_yes=Decimal("0.65"),
            last_price_no=Decimal("0.35"),
            volume_24h=Decimal("10000"),
            active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        m = Market.from_row(row)
        assert m.market_id == "mkt-001"
        assert m.question == "Will X happen?"
        assert m.last_price_yes == Decimal("0.65")

    def test_to_dict_excludes_timestamps(self):
        m = Market(market_id="mkt-001", question="Q", created_at=NOW, updated_at=NOW)
        d = m.to_dict()
        assert "created_at" not in d
        assert "updated_at" not in d
        assert d["market_id"] == "mkt-001"


# ---------------------------------------------------------------------------
# Model: Wallet
# ---------------------------------------------------------------------------


class TestWalletModel:
    def test_from_row(self):
        row = _record(
            address="0xabc",
            first_seen=NOW,
            last_active=NOW,
            leaderboard_rank=42,
            leaderboard_pnl=Decimal("5000"),
            leaderboard_volume=Decimal("100000"),
            whale_flag=True,
            leader_score=Decimal("75.5"),
            leader_type="whale",
            on_watchlist=True,
        )
        w = Wallet.from_row(row)
        assert w.address == "0xabc"
        assert w.whale_flag is True
        assert w.leader_score == Decimal("75.5")


# ---------------------------------------------------------------------------
# Model: Trade
# ---------------------------------------------------------------------------


class TestTradeModel:
    def test_from_row(self):
        row = _record(
            time=NOW,
            market_id="mkt-001",
            token_id="tok_yes",
            price=Decimal("0.65"),
            size=Decimal("1000"),
            trade_id="t-001",
            wallet_address="0xabc",
            side="buy",
            fee_rate_bps=10,
            maker_order_id="mo-1",
            taker_order_id="to-1",
        )
        t = Trade.from_row(row)
        assert t.trade_id == "t-001"
        assert t.side == "buy"
        assert t.size == Decimal("1000")


# ---------------------------------------------------------------------------
# Model: OrderbookSnapshot
# ---------------------------------------------------------------------------


class TestOrderbookSnapshotModel:
    def test_from_row(self):
        row = _record(
            time=NOW,
            market_id="mkt-001",
            token_id="tok_yes",
            bids=[{"price": 0.64, "size": 500}],
            asks=[{"price": 0.66, "size": 300}],
            best_bid=Decimal("0.64"),
            best_ask=Decimal("0.66"),
            spread=Decimal("0.02"),
            mid_price=Decimal("0.65"),
        )
        s = OrderbookSnapshot.from_row(row)
        assert s.best_bid == Decimal("0.64")
        assert len(s.bids) == 1


# ---------------------------------------------------------------------------
# Model: VolumeSpike
# ---------------------------------------------------------------------------


class TestVolumeSpikeModel:
    def test_from_row(self):
        row = _record(
            time=NOW,
            market_id="mkt-001",
            token_id="tok_yes",
            volume_window_s=60,
            volume_spike=Decimal("5000"),
            volume_baseline=Decimal("1000"),
            z_score=Decimal("4.5"),
            attributed=False,
        )
        s = VolumeSpike.from_row(row)
        assert s.z_score == Decimal("4.5")
        assert s.attributed is False


# ---------------------------------------------------------------------------
# Model: LeaderEvent
# ---------------------------------------------------------------------------


class TestLeaderEventModel:
    def test_from_row(self):
        row = _record(
            id=1,
            time=NOW,
            market_id="mkt-001",
            initiator_wallet="0xabc",
            order_size=Decimal("10000"),
            induced_volume=Decimal("50000"),
            follower_count=12,
            delay_p50_ms=250,
            event_type="whale",
            spike_z_score=Decimal("5.1"),
        )
        e = LeaderEvent.from_row(row)
        assert e.event_type == "whale"
        assert e.follower_count == 12

    def test_to_dict_excludes_id(self):
        e = LeaderEvent(id=5, time=NOW, market_id="mkt-001", initiator_wallet="0x1")
        d = e.to_dict()
        assert "id" not in d


# ---------------------------------------------------------------------------
# Model: WalletCluster
# ---------------------------------------------------------------------------


class TestWalletClusterModel:
    def test_from_row(self):
        row = _record(
            id=1,
            detected_at=NOW,
            market_id="mkt-001",
            leader_wallet="0xabc",
            follower_wallets=["0x1", "0x2"],
            cluster_size=3,
            total_volume=Decimal("3000"),
            window_s=30,
            confidence=Decimal("0.92"),
        )
        c = WalletCluster.from_row(row)
        assert c.cluster_size == 3
        assert c.confidence == Decimal("0.92")


# ---------------------------------------------------------------------------
# Model: LeaderScore
# ---------------------------------------------------------------------------


class TestLeaderScoreModel:
    def test_from_row(self):
        row = _record(
            time=NOW,
            wallet_address="0xabc",
            score_total=Decimal("78.5"),
            score_volume_impact=Decimal("80"),
            score_frequency=Decimal("70"),
            score_follower_magnitude=Decimal("75"),
            score_repeatability=Decimal("85"),
            score_leaderboard=Decimal("100"),
            events_7d=14,
            induced_volume_7d=Decimal("200000"),
        )
        s = LeaderScore.from_row(row)
        assert s.score_total == Decimal("78.5")
        assert s.events_7d == 14


# ---------------------------------------------------------------------------
# Model: PaperTrade
# ---------------------------------------------------------------------------


class TestPaperTradeModel:
    def test_from_row(self):
        row = _record(
            id=1,
            opened_at=NOW,
            closed_at=None,
            market_id="mkt-001",
            token_id="tok_yes",
            direction="yes",
            entry_price=Decimal("0.65"),
            exit_price=None,
            size_usdc=Decimal("500"),
            pnl_usdc=None,
            signal_type="whale_follow",
            leader_wallet="0xabc",
            status="open",
            close_reason=None,
        )
        p = PaperTrade.from_row(row)
        assert p.direction == "yes"
        assert p.status == "open"

    def test_to_dict_excludes_id(self):
        p = PaperTrade(
            opened_at=NOW,
            market_id="mkt-001",
            token_id="tok_yes",
            direction="yes",
            entry_price=Decimal("0.65"),
            size_usdc=Decimal("500"),
            id=7,
        )
        d = p.to_dict()
        assert "id" not in d
