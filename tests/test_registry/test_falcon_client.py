"""Unit tests for FalconClient."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.registry.falcon_client import FalconAPIError, FalconClient
from src.registry.models import (
    FalconLeaderEntry,
    MarketInsights,
    PnlLeaderEntry,
    WalletMetrics,
)


def _make_client(redis=None) -> FalconClient:
    return FalconClient(
        api_key="test-key",
        api_url="https://falcon.example.com",
        redis_client=redis,
        cache_ttl_s=300,
        max_rpm=0,
    )


def _mock_response(status: int, data: dict) -> MagicMock:
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.raise_for_status = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestFalconClientQuery:
    def test_session_or_new_raises_when_key_missing(self):
        client = FalconClient(
            api_key="placeholder",
            api_url="https://falcon.example.com",
            redis_client=None,
        )
        client._api_key = ""
        with pytest.raises(FalconAPIError, match="FALCON_API_KEY"):
            client._session_or_new()

    @pytest.mark.asyncio
    async def test_query_returns_list_on_success(self):
        client = _make_client()
        data = {"data": {"results": [{"wallet": "0xabc", "h_score": "9.5"}]}}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(584, {}, limit=10)

        assert result == data["data"]["results"]

    @pytest.mark.asyncio
    async def test_query_returns_data_results_when_present(self):
        client = _make_client()
        data = {
            "data": {
                "results": [{"wallet_address": "0xabc", "falcon_score": 9.5}],
                "timestamp": "",
            }
        }
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(584, {}, limit=10)

        assert result == data["data"]["results"]

    @pytest.mark.asyncio
    async def test_query_uses_cache_on_hit(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=json.dumps([{"wallet_address": "0xcached"}]))
        client = _make_client(redis=redis)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            result = await client.query(584, {}, limit=10)
            mock_sess_fn.assert_not_called()

        assert result == [{"wallet_address": "0xcached"}]

    @pytest.mark.asyncio
    async def test_query_stores_result_in_cache(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        client = _make_client(redis=redis)

        data = {"results": [{"wallet_address": "0xnew"}]}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            await client.query(584, {}, limit=10)

        redis.set.assert_awaited_once()
        args = redis.set.call_args
        assert "falcon:584:" in args[0][0]

    @pytest.mark.asyncio
    async def test_query_sends_pagination_and_raw_formatter(self):
        client = _make_client()
        data = {"results": [{"wallet_address": "0xnew"}]}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            await client.query(584, {}, limit=7, offset=3)

        body = session.post.call_args.kwargs["json"]
        assert body["pagination"] == {"limit": 7, "offset": 3}
        assert body["formatter_config"] == {"format_type": "raw"}

    @pytest.mark.asyncio
    async def test_query_uses_minimum_api_page_size_but_slices_result(self):
        client = _make_client()
        data = {"data": {"results": [{"wallet_address": "0x1"}, {"wallet_address": "0x2"}]}}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(581, {"proxy_wallet": "0xabc"}, limit=1)

        body = session.post.call_args.kwargs["json"]
        assert body["pagination"] == {"limit": 5, "offset": 0}
        assert result == [{"wallet_address": "0x1"}]

    @pytest.mark.asyncio
    async def test_query_retries_on_429(self):
        client = _make_client()
        resp_429 = _mock_response(429, {})
        resp_200 = _mock_response(200, {"results": [{"wallet_address": "0xok"}]})

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return resp_429
            return resp_200

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(side_effect=side_effect)
            mock_sess_fn.return_value = session

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.query(584, {})

        assert call_count == 3
        assert result == [{"wallet_address": "0xok"}]

    @pytest.mark.asyncio
    async def test_query_raises_after_max_retries(self):
        client = _make_client()
        resp_500 = _mock_response(500, {})

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp_500)
            mock_sess_fn.return_value = session

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(FalconAPIError):
                    await client.query(584, {})

    @pytest.mark.asyncio
    async def test_get_leaderboard_parses_entries(self):
        client = _make_client()
        raw = [
            {"wallet": "0xa", "h_score": "8.0", "extra_field": "ignored"},
            {"wallet": "0xb", "h_score": "6.5"},
        ]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            entries = await client.get_leaderboard()

        assert len(entries) == 2
        assert all(isinstance(e, FalconLeaderEntry) for e in entries)
        assert entries[0].falcon_score == 8.0

    @pytest.mark.asyncio
    async def test_get_leaderboard_uses_documented_filters(self):
        client = _make_client()
        with patch.object(client, "query", new=AsyncMock(return_value=[])) as mock_query:
            await client.get_leaderboard(limit=25)

        mock_query.assert_awaited_once_with(
            584,
            {
                "min_win_rate_15d": "0.45",
                "max_win_rate_15d": "0.92",
                "min_roi_15d": "0",
                "min_pnl_15d": "0",
                "min_total_trades_15d": "30",
                "max_total_trades_15d": "5000",
                "sort_by": "roi",
            },
            limit=25,
        )

    @pytest.mark.asyncio
    async def test_get_wallet360_returns_none_on_empty(self):
        client = _make_client()
        with patch.object(client, "query", new=AsyncMock(return_value=[])):
            result = await client.get_wallet360("0xabc")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_wallet360_returns_metrics(self):
        client = _make_client()
        raw = [
            {
                "wallet_address": "0xabc",
                "total_volume_usdc": 50000,
                "avg_trade_duration_s": 3600,
            }
        ]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            result = await client.get_wallet360("0xabc")
        assert result is not None
        assert isinstance(result, WalletMetrics)
        assert result.wallet_address == "0xabc"

    @pytest.mark.asyncio
    async def test_get_wallet360_uses_proxy_wallet_contract(self):
        client = _make_client()
        with patch.object(client, "query", new=AsyncMock(return_value=[])) as mock_query:
            await client.get_wallet360("0xabc")

        mock_query.assert_awaited_once_with(
            581,
            {"proxy_wallet": "0xabc", "window_days": "15"},
            limit=1,
        )

    @pytest.mark.asyncio
    async def test_get_pnl_leaderboard_parses_entries(self):
        client = _make_client()
        raw = [
            {"address": "0xa", "total_pnl": "1500.0"},
            {"address": "0xb", "total_pnl": "800.0", "extra": "ignored"},
        ]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            entries = await client.get_pnl_leaderboard()

        assert len(entries) == 2
        assert all(isinstance(e, PnlLeaderEntry) for e in entries)
        assert entries[0].profit == 1500.0

    @pytest.mark.asyncio
    async def test_get_pnl_leaderboard_uses_documented_params(self):
        client = _make_client()
        with patch.object(client, "query", new=AsyncMock(return_value=[])) as mock_query:
            await client.get_pnl_leaderboard(limit=15)

        mock_query.assert_awaited_once_with(
            579,
            {"wallet_address": "ALL", "leaderboard_period": "7d"},
            limit=15,
        )

    @pytest.mark.asyncio
    async def test_get_leaderboard_skips_invalid_rows(self):
        client = _make_client()
        raw = [
            {"h_score": 8.0},  # missing wallet → invalid
            {"wallet": "0xb", "h_score": "6.5"},
        ]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            entries = await client.get_leaderboard()
        assert len(entries) == 1
        assert entries[0].wallet_address == "0xb"

    @pytest.mark.asyncio
    async def test_query_no_redis_skips_cache(self):
        client = _make_client(redis=None)
        data = {"results": [{"wallet_address": "0xa"}]}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(584, {})

        assert result == data["results"]

    @pytest.mark.asyncio
    async def test_query_returns_list_when_response_is_list(self):
        client = _make_client()
        # Some Falcon endpoints return a bare list instead of {"results": [...]}
        data = [{"wallet_address": "0xa", "falcon_score": 7.0}]
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(584, {})

        assert result == data

    @pytest.mark.asyncio
    async def test_get_market_insights_returns_score_from_agent_575(self):
        """Phase 0 Task C / audit MG-3: get_market_insights must call
        agent 575 with the condition_id and return the normalized
        liquidity_score from the response."""
        client = _make_client()
        raw = [{"condition_id": "0xmkt", "liquidity_score": 0.62}]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)) as mock_query:
            result = await client.get_market_insights("0xmkt")
        assert isinstance(result, MarketInsights)
        assert result.liquidity_score == 0.62
        mock_query.assert_awaited_once_with(575, {"condition_id": "0xmkt"}, limit=1)

    @pytest.mark.asyncio
    async def test_get_market_insights_falls_back_to_slug(self):
        """When condition_id returns no rows, agent 575 is retried with
        market_slug — mirrors the 574 fallback pattern in sync_markets."""
        client = _make_client()

        async def fake_query(agent_id, params, limit=100, offset=0):
            if params.get("condition_id"):
                return []
            if params.get("market_slug"):
                return [{"liquidity_score": 0.31}]
            return []

        with patch.object(client, "query", new=AsyncMock(side_effect=fake_query)):
            result = await client.get_market_insights("some-slug")
        assert result is not None
        assert result.liquidity_score == 0.31

    @pytest.mark.asyncio
    async def test_get_market_insights_returns_none_on_empty(self):
        client = _make_client()
        with patch.object(client, "query", new=AsyncMock(return_value=[])):
            result = await client.get_market_insights("0xmkt")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_market_insights_returns_none_on_falcon_error(self):
        """Transient Falcon failures must not break sync_markets — the
        caller is expected to fall back to agent 574's `liquidity`."""
        client = _make_client()
        with patch.object(
            client, "query", new=AsyncMock(side_effect=FalconAPIError("575 down"))
        ):
            result = await client.get_market_insights("0xmkt")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_market_insights_clamps_score_to_unit_interval(self):
        """USD-denominated depth payloads must be squashed to [0,1] so
        `_build_features` slot [4] stays in range."""
        client = _make_client()
        raw = [{"condition_id": "0xmkt", "liquidity_score": 1_000_000.0}]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            result = await client.get_market_insights("0xmkt")
        assert result is not None
        assert 0.0 <= result.liquidity_score <= 1.0

    @pytest.mark.asyncio
    async def test_get_market_insights_clamps_negative_score(self):
        client = _make_client()
        raw = [{"condition_id": "0xmkt", "liquidity_score": -5.0}]
        with patch.object(client, "query", new=AsyncMock(return_value=raw)):
            result = await client.get_market_insights("0xmkt")
        assert result is not None
        assert result.liquidity_score == 0.0

    @pytest.mark.asyncio
    async def test_close_closes_session(self):
        client = _make_client()
        mock_session = AsyncMock()
        mock_session.closed = False
        client._session = mock_session
        await client.close()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_noop_when_no_session(self):
        client = _make_client()
        client._session = None
        # Should not raise
        await client.close()

    @pytest.mark.asyncio
    async def test_cache_key_is_deterministic(self):
        client = _make_client()
        k1 = client._cache_key(584, {"a": 1}, 10, 0)
        k2 = client._cache_key(584, {"a": 1}, 10, 0)
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_cache_key_differs_by_params(self):
        client = _make_client()
        k1 = client._cache_key(584, {"a": 1}, 10, 0)
        k2 = client._cache_key(584, {"a": 2}, 10, 0)
        assert k1 != k2

    @pytest.mark.asyncio
    async def test_redis_cache_read_failure_falls_through(self):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=Exception("redis down"))
        client = _make_client(redis=redis)

        data = {"results": [{"wallet_address": "0xa"}]}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            result = await client.query(584, {})

        assert result == data["results"]

    @pytest.mark.asyncio
    async def test_redis_cache_write_failure_does_not_raise(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(side_effect=Exception("write failed"))
        client = _make_client(redis=redis)

        data = {"results": [{"wallet_address": "0xa"}]}
        resp = _mock_response(200, data)

        with patch.object(client, "_session_or_new") as mock_sess_fn:
            session = MagicMock()
            session.post = MagicMock(return_value=resp)
            mock_sess_fn.return_value = session
            # Should not raise despite cache write failure
            result = await client.query(584, {})

        assert result == data["results"]
