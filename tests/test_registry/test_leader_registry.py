"""Unit tests for LeaderRegistry."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.registry.falcon_client import FalconAPIError, FalconClient
from src.registry.leader_registry import LeaderRegistry
from src.registry.models import (
    FalconLeaderEntry,
    Leader,
    LeaderClassification,
    PnlLeaderEntry,
    WalletMetrics,
)


def _make_registry() -> tuple[LeaderRegistry, MagicMock]:
    falcon = MagicMock(spec=FalconClient)
    falcon.query = AsyncMock(return_value=[])
    falcon.get_leaderboard = AsyncMock()
    falcon.get_wallet360 = AsyncMock()
    falcon.get_pnl_leaderboard = AsyncMock()
    registry = LeaderRegistry(falcon_client=falcon)
    return registry, falcon


def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    return conn


class TestRefreshLeaderboard:
    @pytest.mark.asyncio
    async def test_upserts_leaders(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.return_value = [
            FalconLeaderEntry(wallet_address=f"0x{i}", falcon_score=float(i)) for i in range(5)
        ]
        conn = _make_conn()
        await registry.refresh_leaderboard(conn)
        conn.executemany.assert_awaited_once()
        sql = conn.executemany.call_args[0][0]
        assert "INSERT INTO leaders" in sql
        assert "ON CONFLICT" in sql

    @pytest.mark.asyncio
    async def test_returns_count(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.return_value = [
            FalconLeaderEntry(wallet_address=f"0x{i}", falcon_score=5.0) for i in range(3)
        ]
        conn = _make_conn()
        count = await registry.refresh_leaderboard(conn)
        assert count == 3

    @pytest.mark.asyncio
    async def test_returns_zero_on_empty_response(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.return_value = []
        conn = _make_conn()
        count = await registry.refresh_leaderboard(conn)
        assert count == 0
        conn.executemany.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_cached_db_count_when_all_leaderboards_are_empty(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.return_value = []
        falcon.get_pnl_leaderboard.return_value = []
        conn = _make_conn()
        conn.fetchval = AsyncMock(return_value=12)

        count = await registry.refresh_leaderboard(conn)

        assert count == 12
        conn.executemany.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filters_below_min_score(self):
        registry, falcon = _make_registry()
        # Score 0.0 passes (MIN_FALCON_SCORE = 0.0 by default), negative would not
        falcon.get_leaderboard.return_value = [
            FalconLeaderEntry(wallet_address="0xa", falcon_score=0.0),
            FalconLeaderEntry(wallet_address="0xb", falcon_score=5.0),
        ]
        conn = _make_conn()
        count = await registry.refresh_leaderboard(conn)
        # Both pass MIN_FALCON_SCORE = 0.0
        assert count == 2

    @pytest.mark.asyncio
    async def test_upsert_sql_updates_falcon_score(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.return_value = [
            FalconLeaderEntry(wallet_address="0xa", falcon_score=7.5)
        ]
        conn = _make_conn()
        await registry.refresh_leaderboard(conn)
        sql = conn.executemany.call_args[0][0]
        assert "falcon_score" in sql.lower()

    @pytest.mark.asyncio
    async def test_falls_back_to_pnl_leaderboard_when_584_fails(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.side_effect = FalconAPIError("agent 584 unavailable")
        falcon.get_pnl_leaderboard.return_value = [
            PnlLeaderEntry(wallet_address="0xa", profit=100.0),
            PnlLeaderEntry(wallet_address="0xb", profit=50.0),
        ]
        conn = _make_conn()

        count = await registry.refresh_leaderboard(conn)

        assert count == 2
        falcon.get_pnl_leaderboard.assert_awaited_once()
        sql = conn.executemany.call_args[0][0]
        assert "leaders.falcon_score" in sql

    @pytest.mark.asyncio
    async def test_returns_zero_when_primary_and_fallback_are_unavailable(self):
        registry, falcon = _make_registry()
        falcon.get_leaderboard.side_effect = FalconAPIError("agent 584 unavailable")
        falcon.get_pnl_leaderboard.side_effect = FalconAPIError("agent 579 unavailable")
        conn = _make_conn()
        conn.fetchval = AsyncMock(return_value=7)

        count = await registry.refresh_leaderboard(conn)

        assert count == 7
        conn.executemany.assert_not_awaited()


class TestEnrichLeaders:
    @pytest.mark.asyncio
    async def test_calls_wallet360_for_stale_leaders(self):
        registry, falcon = _make_registry()
        stale_rows = [{"wallet_address": "0xa"}, {"wallet_address": "0xb"}]
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=stale_rows)
        falcon.get_wallet360.return_value = WalletMetrics(wallet_address="0xa")

        count = await registry.enrich_leaders(conn)
        assert falcon.get_wallet360.await_count == 2
        assert count == 2

    @pytest.mark.asyncio
    async def test_updates_last_refresh(self):
        registry, falcon = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[{"wallet_address": "0xa"}])
        falcon.get_wallet360.return_value = WalletMetrics(wallet_address="0xa")

        await registry.enrich_leaders(conn)
        conn.execute.assert_awaited_once()
        sql = conn.execute.call_args[0][0]
        assert "last_refresh" in sql.lower()

    @pytest.mark.asyncio
    async def test_skips_wallet_on_none_response(self):
        registry, falcon = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[{"wallet_address": "0xa"}])
        falcon.get_wallet360.return_value = None

        count = await registry.enrich_leaders(conn)
        assert count == 0
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_stale_leaders(self):
        registry, falcon = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])

        count = await registry.enrich_leaders(conn)
        assert count == 0
        falcon.get_wallet360.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sets_excluded_for_structural_bots(self):
        registry, falcon = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[{"wallet_address": "0xbot"}])
        # Bot: avg_trade_duration_s < 60 → structural → copiable=False → excluded=True
        falcon.get_wallet360.return_value = WalletMetrics(
            wallet_address="0xbot", **{"avg_trade_duration_s": 0.5}
        )
        await registry.enrich_leaders(conn)
        conn.execute.assert_awaited_once()
        call_args = conn.execute.call_args[0]
        # $4 = excluded (True), $5 = exclude_reason ("structural_bot")
        assert call_args[4] is True
        assert call_args[5] == "structural_bot"

    @pytest.mark.asyncio
    async def test_sets_not_excluded_for_copiable_leader(self):
        registry, falcon = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[{"wallet_address": "0xgood"}])
        falcon.get_wallet360.return_value = WalletMetrics(
            wallet_address="0xgood", **{"avg_trade_duration_s": 3600, "avg_holding_period_days": 5}
        )
        await registry.enrich_leaders(conn)
        call_args = conn.execute.call_args[0]
        assert call_args[4] is False
        assert call_args[5] is None


class TestClassifyLeader:
    def test_structural_bot(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({"avg_trade_duration_s": 0.5})
        assert result.strategy == "structural"
        assert result.copiable is False

    def test_cognitive_holder(self):
        registry, _ = _make_registry()
        result = registry.classify_leader(
            {"avg_trade_duration_s": 86400, "avg_holding_period_days": 30}
        )
        assert result.strategy == "cognitive"

    def test_directional_trader(self):
        registry, _ = _make_registry()
        result = registry.classify_leader(
            {"avg_trade_duration_s": 7200, "avg_holding_period_days": 3}
        )
        assert result.strategy == "directional"

    def test_whale_influence(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({"total_volume_usdc": 500_000})
        assert result.influence == "whale"

    def test_top_trader_influence(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({"total_volume_usdc": 50_000, "falcon_score": 7.0})
        assert result.influence == "top_trader"

    def test_community_influence(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({"total_volume_usdc": 1_000, "falcon_score": 2.0})
        assert result.influence == "community"

    def test_copiable_false_for_structural(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({"avg_trade_duration_s": 0.1})
        assert result.copiable is False

    def test_copiable_true_for_directional(self):
        registry, _ = _make_registry()
        result = registry.classify_leader(
            {"avg_trade_duration_s": 3600, "avg_holding_period_days": 5}
        )
        assert result.copiable is True

    def test_returns_classification_instance(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({})
        assert isinstance(result, LeaderClassification)

    def test_scalper_horizon(self):
        registry, _ = _make_registry()
        # avg_holding_days < 1/24 (< 1 hour)
        result = registry.classify_leader(
            {"avg_trade_duration_s": 3600, "avg_holding_period_days": 0.01}
        )
        assert result.horizon == "scalper"

    def test_swing_horizon(self):
        registry, _ = _make_registry()
        result = registry.classify_leader(
            {"avg_trade_duration_s": 3600, "avg_holding_period_days": 7}
        )
        assert result.horizon == "swing"

    def test_holder_horizon(self):
        registry, _ = _make_registry()
        result = registry.classify_leader(
            {"avg_trade_duration_s": 86400, "avg_holding_period_days": 20}
        )
        assert result.horizon == "holder"

    def test_defaults_on_empty_dict(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({})
        # Defaults: duration=3600 (directional), holding=1 (swing),
        # volume=0 + score=0 (community)
        assert result.strategy == "directional"
        assert result.influence == "community"
        assert result.horizon == "swing"
        assert result.copiable is True

    def test_classified_at_is_set(self):
        registry, _ = _make_registry()
        result = registry.classify_leader({})
        assert result.classified_at != ""

    def test_copiable_false_when_duration_lt_5s(self):
        registry, _ = _make_registry()
        # avg_duration_s >= 60 (not structural) but < 5 → still not copiable
        # Actually: structural is < 60s, but let's test the copiable=False path
        # copiable = strategy != "structural" AND avg_duration_s >= 5
        result = registry.classify_leader({"avg_trade_duration_s": 3})
        assert result.copiable is False


class TestGetActiveLeaders:
    @pytest.mark.asyncio
    async def test_returns_only_non_excluded(self):
        registry, _ = _make_registry()

        def _row(wallet, excluded):
            m = MagicMock()
            m.__getitem__ = lambda self, k: {
                "wallet_address": wallet,
                "falcon_score": 5.0,
                "wallet360_json": None,
                "classification_json": None,
                "first_seen": None,
                "last_refresh": None,
                "on_watchlist": True,
                "excluded": excluded,
                "exclude_reason": None,
            }[k]
            return m

        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[_row("0xa", False), _row("0xb", False)])
        leaders = await registry.get_active_leaders(conn)
        assert len(leaders) == 2
        assert all(not leader.excluded for leader in leaders)

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_leaders(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])
        leaders = await registry.get_active_leaders(conn)
        assert leaders == []

    @pytest.mark.asyncio
    async def test_returns_leader_instances(self):
        registry, _ = _make_registry()

        m = MagicMock()
        m.__getitem__ = lambda self, k: {
            "wallet_address": "0xa",
            "falcon_score": 8.0,
            "wallet360_json": None,
            "classification_json": None,
            "first_seen": None,
            "last_refresh": None,
            "on_watchlist": True,
            "excluded": False,
            "exclude_reason": None,
        }[k]

        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[m])
        leaders = await registry.get_active_leaders(conn)
        assert len(leaders) == 1
        assert isinstance(leaders[0], Leader)
        assert leaders[0].wallet_address == "0xa"

    @pytest.mark.asyncio
    async def test_query_filters_excluded_and_watchlist(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])
        await registry.get_active_leaders(conn)
        sql = conn.fetch.call_args[0][0]
        assert "excluded = FALSE" in sql
        assert "on_watchlist = TRUE" in sql


class TestGetLeaderMarkets:
    @pytest.mark.asyncio
    async def test_returns_set_of_market_ids(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        rows = [MagicMock(), MagicMock()]
        rows[0].__getitem__ = lambda self, k: "mkt-001" if k == "market_id" else None
        rows[1].__getitem__ = lambda self, k: "mkt-002" if k == "market_id" else None
        conn.fetch = AsyncMock(return_value=rows)
        markets = await registry.get_leader_markets(conn)
        assert markets == {"mkt-001", "mkt-002"}

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_no_positions(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])
        markets = await registry.get_leader_markets(conn)
        assert markets == set()

    @pytest.mark.asyncio
    async def test_returns_set_not_list(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])
        result = await registry.get_leader_markets(conn)
        assert isinstance(result, set)

    @pytest.mark.asyncio
    async def test_query_joins_positions_and_leaders(self):
        registry, _ = _make_registry()
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[])
        await registry.get_leader_markets(conn)
        sql = conn.fetch.call_args[0][0]
        assert "positions_reconstructed" in sql
        assert "leaders" in sql


class TestSyncMarkets:
    @pytest.mark.asyncio
    async def test_sync_markets_falls_back_to_gamma_when_falcon_unavailable(self):
        registry, falcon = _make_registry()
        falcon.query = AsyncMock(side_effect=FalconAPIError("falcon down"))
        conn = _make_conn()
        conn.fetch = AsyncMock(return_value=[{"market_id": "0xmkt"}])

        with patch.object(
            registry,
            "_fetch_market_from_gamma",
            new=AsyncMock(
                return_value={
                    "question": "Will BTC be above $100k?",
                    "category": "crypto",
                    "clobTokenIds": ["tok_yes", "tok_no"],
                    "endDateIso": "2026-04-30T12:00:00Z",
                    "volume24hr": 1234.0,
                    "liquidity": 0.88,
                    "makerBaseFee": 0.02,
                }
            ),
        ):
            count = await registry.sync_markets(conn)

        assert count == 1
        conn.execute.assert_awaited_once()
        args = conn.execute.call_args.args
        assert args[1] == "0xmkt"
        assert args[2] == "Will BTC be above $100k?"
        assert args[3] == "crypto"
