"""
API endpoint tests — mocked DB and Redis.
Run: pytest tests/test_api/ -v
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers — build a fake asyncpg connection
# ---------------------------------------------------------------------------


def make_conn(return_map: dict):
    """
    Return a mock asyncpg connection whose fetchval/fetchrow/fetch calls
    return values from return_map keyed by the first word of the SQL.
    """
    conn = AsyncMock()

    async def fetchval(sql, *args):
        key = sql.strip().split()[0].lower() + "_val"
        return return_map.get(key, return_map.get("default_val", 0))

    async def fetchrow(sql, *args):
        key = sql.strip().split()[0].lower() + "_row"
        return return_map.get(key, return_map.get("default_row", None))

    async def fetch(sql, *args):
        key = sql.strip().split()[0].lower() + "_list"
        return return_map.get(key, return_map.get("default_list", []))

    conn.fetchval = fetchval
    conn.fetchrow = fetchrow
    conn.fetch = fetch
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield AsyncMock(
            fetchval=AsyncMock(return_value=0),
            fetchrow=AsyncMock(return_value=None),
            fetch=AsyncMock(return_value=[]),
        )

    pool.acquire = acquire
    return pool


@pytest.fixture
def mock_redis():
    r = MagicMock()
    r.ping = AsyncMock(return_value=True)
    r.get = AsyncMock(return_value=None)
    r.hgetall = AsyncMock(return_value={})
    return r


@pytest.fixture
def app_client(mock_pool, mock_redis):
    """Patch pool and redis into the API module, return TestClient."""
    import src.api.main as api_main

    api_main._pool = mock_pool
    api_main._redis = mock_redis
    # Skip lifespan for unit tests
    with TestClient(api_main.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOverview:
    def test_returns_required_keys(self, app_client, mock_pool):
        resp = app_client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "total_pnl",
            "win_rate",
            "active_leaders",
            "open_positions",
            "pnl_series",
            "activity_feed",
            "health",
        ):
            assert key in data, f"Missing key: {key}"

    def test_pnl_is_numeric(self, app_client):
        resp = app_client.get("/api/overview")
        assert isinstance(resp.json()["total_pnl"], (int, float))

    def test_health_has_components(self, app_client):
        resp = app_client.get("/api/overview")
        h = resp.json()["health"]
        for k in ("db", "redis", "falcon", "websocket"):
            assert k in h


class TestLeaders:
    def test_returns_list(self, app_client):
        resp = app_client.get("/api/leaders")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_leader_row_shape(self, app_client, mock_pool):
        # Patch the pool to return one fake leader row
        fake_row = MagicMock()
        fake_row.__getitem__ = lambda self, k: {
            "wallet_address": "0xabc123",
            "falcon_score": 7.5,
            "classification_json": (
                '{"strategy":"directional","horizon":"swing","influence":"whale","copiable":true}'
            ),
            "excluded": False,
            "on_watchlist": True,
            "last_refresh": None,
            "exclude_reason": None,
            "profile_maturity": 0.4,
            "error_model_phase": 1,
            "trades_observed": 75,
            "positions_resolved": 30,
            "confirmed_followers": 2,
        }[k]

        import src.api.queries as q

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[fake_row])

        rows = await q.leaders(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["wallet_address"] == "0xabc123"
        assert r["strategy"] == "directional"
        assert r["trades_observed"] == 75


class TestLeaderDetail:
    def test_404_on_unknown_wallet(self, app_client):
        resp = app_client.get("/api/leaders/0xdeadbeef")
        assert resp.status_code == 404


class TestPositions:
    def test_returns_open_closed_stats(self, app_client):
        resp = app_client.get("/api/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert "open" in data
        assert "closed" in data
        assert "stats" in data
        assert "total_pnl" in data["stats"]

    def test_stats_keys(self, app_client):
        stats = app_client.get("/api/positions").json()["stats"]
        for k in ("total_pnl", "wins", "losses"):
            assert k in stats


class TestDecisions:
    def test_returns_list(self, app_client):
        resp = app_client.get("/api/decisions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_limit_param(self, app_client):
        resp = app_client.get("/api/decisions?limit=10&offset=0")
        assert resp.status_code == 200

    def test_limit_out_of_range(self, app_client):
        resp = app_client.get("/api/decisions?limit=0")
        assert resp.status_code == 422  # FastAPI validation error


class TestML:
    def test_returns_ml_summary(self, app_client):
        resp = app_client.get("/api/ml")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "leaders_with_process",
            "leaders_with_decision_learning",
            "follow",
            "fade",
            "top_loss_reasons",
        ):
            assert key in data


class TestNeuralReadiness:
    def test_returns_neural_readiness_contract(self, app_client):
        resp = app_client.get("/api/neural-readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) >= {"global", "tracks", "markets", "transitions"}
        assert "leader_swing" in data["tracks"]
        assert "micro_reactive" in data["tracks"]
        assert "data_accumulation_pct" in data["global"]["bars"]


class TestLiveSnapshotCache:
    @pytest.mark.asyncio
    async def test_reuses_recent_live_snapshot(self):
        import src.api.main as api_main

        api_main._live_snapshot_cache = {"data": None, "last_built": 0.0}

        overview = AsyncMock(return_value={"total_pnl": 12.0, "activity_feed": []})
        ml = AsyncMock(return_value={"leaders_with_process": 2})
        health = AsyncMock(
            return_value={"db": True, "redis": True, "falcon": True, "websocket": True}
        )

        with (
            patch.object(api_main, "_fetch_overview_snapshot", overview),
            patch.object(api_main, "_fetch_ml_snapshot", ml),
            patch.object(api_main, "_health_checks", health),
        ):
            first = await api_main._get_live_snapshot()
            second = await api_main._get_live_snapshot()

        assert first["total_pnl"] == 12.0
        assert first["ml"]["leaders_with_process"] == 2
        assert second["ml"]["leaders_with_process"] == 2
        assert overview.await_count == 1
        assert ml.await_count == 1
        assert health.await_count == 1


class TestDashboardHtml:
    def test_serves_html(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Polymarket Intelligence" in resp.text

    def test_contains_chartjs(self, app_client):
        resp = app_client.get("/")
        assert "chart.umd.min.js" in resp.text

    def test_contains_websocket_code(self, app_client):
        resp = app_client.get("/")
        assert "/ws/live" in resp.text

    def test_fetches_neural_readiness(self, app_client):
        resp = app_client.get("/")
        assert "/api/neural-readiness" in resp.text
        assert "neural-global-bars" in resp.text
        assert "neural-data-counts" in resp.text


# ---------------------------------------------------------------------------
# Query unit tests (no HTTP layer)
# ---------------------------------------------------------------------------


class TestQueriesOverview:
    @pytest.mark.asyncio
    async def test_overview_empty_db(self):
        """overview() on an empty DB returns zeros, not errors."""
        import src.api.queries as q

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        result = await q.overview(conn)
        assert result["total_pnl"] == 0.0
        assert result["active_leaders"] == 0
        assert result["pnl_series"] == []
        assert result["activity_feed"] == []

    @pytest.mark.asyncio
    async def test_overview_enriches_activity_feed(self):
        import src.api.queries as q

        conn = AsyncMock()
        now = datetime.now(tz=timezone.utc)
        conn.fetchval = AsyncMock(side_effect=[12.5, 3, 1, 42])
        conn.fetchrow = AsyncMock(side_effect=[{"win_rate": 0.5}, {"last_trade": now}])
        conn.fetch = AsyncMock(
            side_effect=[
                [{"day": now.date(), "pnl": 12.5}],
                [
                    {
                        "time": now,
                        "market_id": "0xmkt",
                        "wallet_address": "0xleader",
                        "side": "BUY",
                        "size_usdc": 88.0,
                        "is_leader": True,
                        "question": "Will BTC close above $100k this month?",
                        "category": "crypto",
                        "classification_json": json.dumps(
                            {
                                "strategy": "directional",
                                "horizon": "swing",
                                "influence": "whale",
                            }
                        ),
                        "on_watchlist": True,
                        "excluded": False,
                    }
                ],
            ]
        )

        result = await q.overview(conn)

        assert (
            result["activity_feed"][0]["market_question"]
            == "Will BTC close above $100k this month?"
        )
        assert result["activity_feed"][0]["market_category"] == "crypto"
        assert result["activity_feed"][0]["market_type"] == "crypto"
        assert result["activity_feed"][0]["wallet_type"] == "leader"
        assert result["activity_feed"][0]["wallet_strategy"] == "directional"
        assert result["activity_feed"][0]["wallet_status"] == "active"

    @pytest.mark.asyncio
    async def test_overview_marks_mapped_followers(self):
        import src.api.queries as q

        conn = AsyncMock()
        now = datetime.now(tz=timezone.utc)
        conn.fetchval = AsyncMock(side_effect=[0, 1, 0, 0, 10])
        conn.fetchrow = AsyncMock(side_effect=[{"win_rate": 0.0}, {"last_trade": now}])
        conn.fetch = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "time": now,
                        "market_id": "0xmkt",
                        "wallet_address": "0xfollower",
                        "side": "BUY",
                        "size_usdc": 88.0,
                        "is_leader": False,
                        "question": "Will BTC close above $100k this month?",
                        "category": "crypto",
                        "classification_json": None,
                        "on_watchlist": None,
                        "excluded": None,
                        "mapped_leader_wallet": "0xleader",
                        "mapped_follow_probability": 0.92,
                        "mapped_edge_count": 7,
                    }
                ],
            ]
        )

        result = await q.overview(conn)

        row = result["activity_feed"][0]
        assert row["wallet_type"] == "follower"
        assert row["wallet_status"] == "mapped"
        assert row["mapped_leader_wallet"] == "0xleader"
        assert row["mapped_follow_probability"] == 0.92


class TestQueriesDecisions:
    @pytest.mark.asyncio
    async def test_decisions_maps_fields(self):
        import src.api.queries as q

        fake = MagicMock()
        now = datetime.now(tz=timezone.utc)
        fake.__getitem__ = lambda self, k: {
            "id": 1,
            "time": now,
            "leader_wallet": "0xabc",
            "market_id": "0xmkt",
            "action": "follow",
            "thompson_follow": 0.7,
            "thompson_fade": 0.3,
            "kelly_fraction": 0.015,
            "confidence": 0.72,
            "reason": "exploration",
            "outcome": None,
            "question": "Will X happen?",
            "leader_context": json.dumps(
                {
                    "trade_context": {
                        "category": "crypto",
                        "reason_codes": ["high_deviation"],
                        "process_score": 0.41,
                        "p_error": 0.66,
                    },
                    "context_penalty": 0.22,
                }
            ),
        }[k]

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[fake])
        result = await q.decisions(conn, limit=10, offset=0)
        assert len(result) == 1
        assert result[0]["action"] == "follow"
        assert result[0]["question"] == "Will X happen?"
        assert result[0]["kelly_fraction"] == 0.015
        assert result[0]["ml_snapshot"]["category"] == "crypto"
        assert result[0]["ml_snapshot"]["reason_codes"] == ["high_deviation"]


class TestQueriesPositions:
    @pytest.mark.asyncio
    async def test_positions_filters_invalid_market_resolved_artifacts(self):
        import src.api.queries as q

        invalid_row = {
            "id": 1,
            "opened_at": datetime(2026, 4, 2, 17, 39, tzinfo=timezone.utc),
            "closed_at": datetime(2026, 4, 2, 17, 39, 54, tzinfo=timezone.utc),
            "market_id": "0xmkt",
            "token_id": "0xtoken",
            "direction": "no",
            "entry_price": 0.33,
            "exit_price": 0.33,
            "size_usdc": 50.0,
            "pnl_usdc": 0.0,
            "fee_paid_usdc": 0.0,
            "strategy": "fade",
            "leader_wallet": "0xleader",
            "confidence": 0.8,
            "status": "closed",
            "close_reason": "market_resolved",
            "leader_context": json.dumps(
                {"trade_context": {"live_candidate": False, "trade_age_s": 9999}}
            ),
            "age_s": 54,
            "question": "Will Joe Biden get Coronavirus before the election?",
            "category": "US-current-affairs",
            "fee_rate_pct": 0.0,
            "end_date": datetime(2020, 11, 3, 23, 0, tzinfo=timezone.utc),
        }
        valid_row = {
            "id": 2,
            "opened_at": datetime(2026, 4, 2, 18, 0, tzinfo=timezone.utc),
            "closed_at": datetime(2026, 4, 2, 18, 45, tzinfo=timezone.utc),
            "market_id": "0xvalid",
            "token_id": "0xtoken2",
            "direction": "yes",
            "entry_price": 0.55,
            "exit_price": 0.61,
            "size_usdc": 75.0,
            "pnl_usdc": 4.5,
            "fee_paid_usdc": 0.0,
            "strategy": "follow",
            "leader_wallet": "0xleader",
            "confidence": 0.9,
            "status": "closed",
            "close_reason": "take_profit",
            "leader_context": json.dumps(
                {"trade_context": {"live_candidate": True, "trade_age_s": 12}}
            ),
            "age_s": 2700,
            "question": "Will BTC close above $100k this month?",
            "category": "crypto",
            "fee_rate_pct": 0.0,
            "end_date": None,
        }

        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[[invalid_row, valid_row], []])

        result = await q.positions(conn)

        assert len(result["closed"]) == 1
        assert result["closed"][0]["id"] == 2
        assert result["stats"]["wins"] == 1
        assert result["stats"]["losses"] == 0


class TestQueriesMLSummary:
    @pytest.mark.asyncio
    async def test_ml_summary_aggregates_learning(self):
        import src.api.queries as q

        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "wallet_address": "0x1",
                    "error_model_phase": 2,
                    "profile_json": json.dumps(
                        {
                            "decision_process": {"orders_seen": 12, "process_score_ewma": 0.61},
                            "decision_learning": {
                                "follow": {
                                    "wins": 3,
                                    "losses": 1,
                                    "reason_stats": {
                                        "high_deviation": {"wins": 0, "losses": 1, "avg_pnl": -12.0}
                                    },
                                },
                                "fade": {
                                    "wins": 1,
                                    "losses": 2,
                                    "reason_stats": {
                                        "low_liquidity": {"wins": 0, "losses": 2, "avg_pnl": -8.0}
                                    },
                                },
                            },
                            "error_model_runtime": {"drift_alert": True},
                        }
                    ),
                },
                {
                    "wallet_address": "0x2",
                    "error_model_phase": 3,
                    "profile_json": json.dumps(
                        {
                            "decision_process": {"orders_seen": 5, "process_score_ewma": 0.41},
                            "decision_learning": {
                                "follow": {"wins": 2, "losses": 0, "reason_stats": {}},
                                "fade": {"wins": 0, "losses": 0, "reason_stats": {}},
                            },
                            "error_model_runtime": {"drift_alert": False},
                        }
                    ),
                },
            ]
        )

        result = await q.ml_summary(conn)

        assert result["leaders_with_process"] == 2
        assert result["leaders_with_decision_learning"] == 2
        assert result["drift_alerts"] == 1
        assert result["phase2_leaders"] == 1
        assert result["phase3_leaders"] == 1
        assert result["follow"]["samples"] == 6
        assert result["top_loss_reasons"]["fade"][0]["code"] == "low_liquidity"
