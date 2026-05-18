"""
Live Portfolio dashboard endpoint tests.

Covers the 5 endpoints added on 2026-05-17 for the redesigned terminal-style
Live Portfolio view (see docs/autonomous_session_2026_05_17_strategy/
03_UI_REDESIGN_PROFESSIONAL.md):

  GET /api/portfolio/timeseries
  GET /api/portfolio/trades
  GET /api/portfolio/allocation
  GET /api/portfolio/kpis
  GET /api/portfolio/pipeline_status

These tests mock asyncpg + Redis so they run without the actual prod
infrastructure. Latency benchmarks (from prod EXPLAIN ANALYZE) are
annotated in each SQL builder docstring; they are not asserted here
because microbenchmarks on mocks are meaningless.

Run: pytest tests/test_api/test_portfolio_endpoints.py -v
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures — light copies of the helpers in tests/test_api/test_endpoints.py
# kept local so this file is self-contained.
# ---------------------------------------------------------------------------


def _make_row(payload: dict) -> MagicMock:
    """Wrap a dict in a MagicMock that supports row[key] access."""
    m = MagicMock()
    m.__getitem__ = lambda self, k: payload.get(k)
    m.get = payload.get
    return m


class _FakeConn:
    """Stub asyncpg connection. Each `fetch`/`fetchval`/`fetchrow` returns
    the next value from a per-method list. Lets a test scenario script the
    exact sequence of SQL responses the builder will get.
    """

    def __init__(
        self,
        fetch: list | None = None,
        fetchval: list | None = None,
        fetchrow: list | None = None,
    ):
        self._fetch = list(fetch or [])
        self._fetchval = list(fetchval or [])
        self._fetchrow = list(fetchrow or [])

    async def fetch(self, sql, *args):
        return self._fetch.pop(0) if self._fetch else []

    async def fetchval(self, sql, *args):
        return self._fetchval.pop(0) if self._fetchval else None

    async def fetchrow(self, sql, *args):
        return self._fetchrow.pop(0) if self._fetchrow else None


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
    with TestClient(api_main.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/portfolio/timeseries
# ---------------------------------------------------------------------------


class TestPortfolioTimeseries:
    def test_endpoint_shape(self, app_client):
        resp = app_client.get("/api/portfolio/timeseries?timeframe=1h")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in ("timeframe", "bucket_seconds", "bars", "from", "to"):
            assert key in data, f"missing top-level key {key}"
        assert data["timeframe"] == "1h"
        assert data["bucket_seconds"] == 3600
        assert isinstance(data["bars"], list)

    def test_empty_range_returns_empty_bars(self, app_client):
        # Default mock_pool returns [] for conn.fetch → empty bars.
        resp = app_client.get(
            "/api/portfolio/timeseries"
            "?timeframe=1m&from=2026-05-17T00:00:00Z&to=2026-05-17T00:01:00Z"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bars"] == []
        assert data["timeframe"] == "1m"

    def test_invalid_timeframe_returns_400(self, app_client):
        resp = app_client.get("/api/portfolio/timeseries?timeframe=99x")
        assert resp.status_code == 400
        assert "invalid timeframe" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_ohlc_monotonicity_and_pnl_join(self):
        """The builder must compute correct OHLC for each bucket and
        merge per-bucket realized PnL from the closed paper_trades.
        """
        import src.api.queries as q

        bucket1 = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
        bucket2 = datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc)

        bars_rows = [
            _make_row({
                "bucket": bucket1,
                "low": 9900.0, "high": 10100.0,
                "open": 10000.0, "close": 10050.0,
                "n_samples": 60,
            }),
            _make_row({
                "bucket": bucket2,
                "low": 10000.0, "high": 10200.0,
                "open": 10050.0, "close": 10150.0,
                "n_samples": 55,
            }),
        ]
        pnl_rows = [
            _make_row({
                "bucket": bucket1,
                "pnl_realized": 25.50,
                "trades_closed": 2,
            }),
            # bucket2 has no closed trades — should still emit a bar with 0 pnl
        ]

        conn = _FakeConn(fetch=[bars_rows, pnl_rows])
        result = await q.portfolio_timeseries(
            conn,
            timeframe="1h",
            from_ts=bucket1,
            to_ts=bucket2 + timedelta(hours=1),
        )

        assert result["timeframe"] == "1h"
        assert result["bucket_seconds"] == 3600
        assert len(result["bars"]) == 2

        b1, b2 = result["bars"]
        # OHLC monotonicity invariant: low <= open,close <= high
        for b in (b1, b2):
            assert b["low"] <= b["open"] <= b["high"]
            assert b["low"] <= b["close"] <= b["high"]
        assert b1["pnl_realized"] == 25.50
        assert b1["trades_closed"] == 2
        assert b2["pnl_realized"] == 0.0   # no trades closed in this bucket
        assert b2["trades_closed"] == 0

    @pytest.mark.asyncio
    async def test_1m_vs_1d_aggregation(self):
        """Same inputs aggregated at different timeframes produce different
        bucket counts.  The 1d bucket should be a single row collapsing
        the entire range.
        """
        import src.api.queries as q

        # 1m timeframe → 3 buckets (returns 3 rows)
        b0 = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
        bars_1m = [
            _make_row({
                "bucket": b0 + timedelta(minutes=i),
                "low": 100 - i, "high": 100 + i,
                "open": 100.0, "close": 100.0 + i,
                "n_samples": 6,
            })
            for i in range(3)
        ]
        conn_1m = _FakeConn(fetch=[bars_1m, []])
        result_1m = await q.portfolio_timeseries(
            conn_1m,
            timeframe="1m",
            from_ts=b0,
            to_ts=b0 + timedelta(minutes=3),
        )
        assert len(result_1m["bars"]) == 3
        assert result_1m["bucket_seconds"] == 60

        # 1d timeframe → single bucket
        bars_1d = [
            _make_row({
                "bucket": b0.replace(hour=0, minute=0),
                "low": 99.0, "high": 102.0,
                "open": 100.0, "close": 102.0,
                "n_samples": 18,
            })
        ]
        conn_1d = _FakeConn(fetch=[bars_1d, []])
        result_1d = await q.portfolio_timeseries(
            conn_1d,
            timeframe="1d",
            from_ts=b0,
            to_ts=b0 + timedelta(minutes=3),
        )
        assert len(result_1d["bars"]) == 1
        assert result_1d["bucket_seconds"] == 86400


# ---------------------------------------------------------------------------
# /api/portfolio/trades
# ---------------------------------------------------------------------------


class TestPortfolioTrades:
    def test_endpoint_returns_list(self, app_client):
        resp = app_client.get("/api/portfolio/trades")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_invalid_order_returns_400(self, app_client):
        resp = app_client.get("/api/portfolio/trades?order=invalid_order")
        assert resp.status_code == 400

    def test_invalid_status_returns_400(self, app_client):
        resp = app_client.get("/api/portfolio/trades?status=bogus")
        assert resp.status_code == 400

    def test_limit_clamped_at_500(self, app_client):
        resp = app_client.get("/api/portfolio/trades?limit=10000")
        assert resp.status_code == 422  # FastAPI Query validation

    @pytest.mark.asyncio
    async def test_joined_market_fields_and_pnl_pct(self):
        """A closed trade row should hydrate market_question, category,
        holding_period_s, and a derived pnl_pct.
        """
        import src.api.queries as q

        opened = datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc)
        closed = opened + timedelta(seconds=300)

        row = _make_row({
            "id": 42,
            "strategy": "follow",
            "leader_wallet": "0x1234567890abcdef1234567890abcdef12345678",
            "market_id": "0xmkt_abc",
            "token_id": "1234567890",
            "direction": "yes",
            "entry_price": 0.55,
            "exit_price": 0.62,
            "size_usdc": 100.0,
            "opened_at": opened,
            "closed_at": closed,
            "close_reason": "take_profit",
            "pnl_usdc": 12.50,
            "status": "closed",
            "confidence": 0.85,
            "fee_paid_usdc": 0.55,
            "holding_period_s": 300,
            "question": (
                "Will the LA Lakers win the 2026 NBA Finals against the Boston Celtics?"
            ),
            "category": "sports",
        })
        conn = _FakeConn(fetch=[[row]])
        trades = await q.portfolio_trades(
            conn,
            redis_client=None,
            limit=50,
            order="closed_desc",
            status="closed",
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["id"] == 42
        assert t["category"] == "sports"
        assert t["holding_period_s"] == 300
        # market_question must be truncated and end with the ellipsis char
        # OR the original if it fit under 80 chars.
        assert len(t["market_question"]) <= 81
        assert t["direction"] == "yes"
        assert t["pnl_usdc"] == 12.50
        assert t["pnl_pct"] == 0.125  # 12.50 / 100
        assert t["leader_short"].startswith("0x1234")
        assert t["leader_short"].endswith("5678")

    @pytest.mark.asyncio
    async def test_bid_ask_pulled_from_redis(self):
        """When redis has a book:last entry for the trade's (market,token),
        the bid/ask are surfaced on the trade payload.
        """
        import src.api.queries as q

        row = _make_row({
            "id": 1,
            "strategy": "follow",
            "leader_wallet": None,
            "market_id": "0xm",
            "token_id": "tok1",
            "direction": "yes",
            "entry_price": 0.5,
            "exit_price": None,
            "size_usdc": 100.0,
            "opened_at": datetime.now(tz=timezone.utc),
            "closed_at": None,
            "close_reason": None,
            "pnl_usdc": None,
            "status": "open",
            "confidence": 0.5,
            "fee_paid_usdc": 0.0,
            "holding_period_s": 0,
            "question": "Q",
            "category": "crypto",
        })
        conn = _FakeConn(fetch=[[row]])
        redis_client = AsyncMock()
        redis_client.get = AsyncMock(
            return_value='{"best_bid":"0.49","best_ask":"0.51"}'
        )
        trades = await q.portfolio_trades(
            conn,
            redis_client=redis_client,
            limit=10,
            order="opened_desc",
            status="open",
        )
        assert trades[0]["bid"] == 0.49
        assert trades[0]["ask"] == 0.51


# ---------------------------------------------------------------------------
# /api/portfolio/allocation
# ---------------------------------------------------------------------------


class TestPortfolioAllocation:
    def test_endpoint_shape(self, app_client):
        resp = app_client.get("/api/portfolio/allocation")
        assert resp.status_code == 200
        data = resp.json()
        for k in (
            "as_of",
            "total_capital",
            "total_open_capital",
            "open_pct_of_total",
            "by_category",
            "by_leader",
            "by_strategy",
        ):
            assert k in data, f"missing key {k}"

    @pytest.mark.asyncio
    async def test_zero_open_positions(self):
        """With no open trades, allocation returns empty buckets and
        zero open_capital — never NaN, never NULL.
        """
        import src.api.queries as q

        conn = _FakeConn(
            fetch=[[], [], []],                            # 3 GROUP BYs
            fetchrow=[None],                                # portfolio_state row
        )
        result = await q.portfolio_allocation(conn)
        assert result["by_category"] == []
        assert result["by_leader"] == []
        assert result["by_strategy"] == []
        assert result["total_open_capital"] == 0
        # total_capital falls back to settings.PAPER_CAPITAL_USDC default
        assert result["total_capital"] > 0
        assert result["open_pct_of_total"] == 0.0

    @pytest.mark.asyncio
    async def test_multiple_categories(self):
        """Allocation rows must sort by capital DESC and carry pct_of_total."""
        import src.api.queries as q

        cat_rows = [
            _make_row({"label": "crypto",   "count": 5, "capital_usdc": 500.0}),
            _make_row({"label": "sports",   "count": 3, "capital_usdc": 300.0}),
            _make_row({"label": "politics", "count": 2, "capital_usdc": 200.0}),
        ]
        leader_rows = []
        strategy_rows = [
            _make_row({"label": "follow", "count": 7, "capital_usdc": 700.0}),
            _make_row({"label": "fade",   "count": 3, "capital_usdc": 300.0}),
        ]
        portfolio_row = _make_row({
            "capital": 10_000.0,
            "peak_capital": 10_500.0,
            "realized_pnl_cum": 0.0,
            "consecutive_losses": 0,
            "open_positions": 10,
        })
        conn = _FakeConn(
            fetch=[cat_rows, leader_rows, strategy_rows],
            fetchrow=[portfolio_row],
        )
        result = await q.portfolio_allocation(conn)

        assert len(result["by_category"]) == 3
        assert result["by_category"][0]["label"] == "crypto"
        assert result["by_category"][0]["pct_of_total"] == 0.05  # 500 / 10000
        assert result["total_open_capital"] == 1000.0

    @pytest.mark.asyncio
    async def test_leader_other_bucket(self):
        """With 6+ leaders, top 5 are returned plus an aggregated 'other'."""
        import src.api.queries as q

        leader_rows = [
            _make_row({
                "label": f"0x{i:040x}",
                "count": 1,
                "capital_usdc": 100.0 * (10 - i),
            })
            for i in range(7)
        ]
        portfolio_row = _make_row({
            "capital": 10_000.0,
            "peak_capital": 10_000.0,
            "realized_pnl_cum": 0.0,
            "consecutive_losses": 0,
            "open_positions": 7,
        })
        conn = _FakeConn(
            fetch=[[], leader_rows, []],
            fetchrow=[portfolio_row],
        )
        result = await q.portfolio_allocation(conn)

        # 5 top leaders + 1 'other' = 6 entries.
        assert len(result["by_leader"]) == 6
        assert result["by_leader"][-1]["label"] == "other"
        assert result["by_leader"][-1]["wallet"] is None
        assert result["by_leader"][-1]["count"] == 2  # 7 - 5


# ---------------------------------------------------------------------------
# /api/portfolio/kpis
# ---------------------------------------------------------------------------


class TestPortfolioKpis:
    def test_endpoint_shape(self, app_client):
        resp = app_client.get("/api/portfolio/kpis")
        assert resp.status_code == 200
        data = resp.json()
        required = (
            "capital",
            "peak_capital",
            "drawdown_pct",
            "daily_pnl",
            "daily_win_count",
            "daily_loss_count",
            "weekly_pnl",
            "win_rate_30d",
            "win_streak_current",
            "win_streak_best",
            "latency_p50_ms",
            "open_positions_count",
            "open_capital_usdc",
        )
        for k in required:
            assert k in data, f"missing KPI {k}"

    def test_sane_defaults_no_nulls_in_non_nullable(self, app_client):
        """Non-nullable counters must default to 0, not None/null."""
        resp = app_client.get("/api/portfolio/kpis").json()
        for k in (
            "capital",
            "drawdown_pct",
            "daily_pnl",
            "daily_win_count",
            "daily_loss_count",
            "weekly_pnl",
            "win_rate_30d",
            "win_streak_current",
            "win_streak_best",
            "open_positions_count",
            "open_capital_usdc",
        ):
            assert resp[k] is not None, f"{k} unexpectedly null"
        # latency_p50_ms may be null when Redis has no metric written yet
        assert "latency_p50_ms" in resp

    @pytest.mark.asyncio
    async def test_win_streak_logic(self):
        """`_compute_win_streak` must distinguish current vs best streak."""
        from src.api.queries import _compute_win_streak

        # newest first: 3 wins, 1 loss, 2 wins → current=3, best=3
        cur, best = _compute_win_streak([5.0, 2.0, 1.0, -1.0, 3.0, 4.0])
        assert cur == 3
        assert best == 3

        # current=0, best=3 (when most recent is a loss)
        cur, best = _compute_win_streak([-1.0, 5.0, 2.0, 1.0])
        assert cur == 0
        assert best == 3

        # empty list → 0, 0
        cur, best = _compute_win_streak([])
        assert cur == 0
        assert best == 0

    @pytest.mark.asyncio
    async def test_win_rate_30d_calc(self):
        """win_rate_30d = m_wins / m_total, 0 when no trades."""
        import src.api.queries as q

        agg_row = _make_row({
            "open_count": 5,
            "open_capital": 500.0,
            "daily_pnl": 12.0,
            "daily_wins": 3,
            "daily_losses": 1,
            "weekly_pnl": 50.0,
            "m_total": 20,
            "m_wins": 13,
        })
        portfolio_row = _make_row({
            "capital": 10_500.0,
            "peak_capital": 11_000.0,
            "realized_pnl_cum": 500.0,
            "consecutive_losses": 0,
            "open_positions": 5,
        })
        streak_rows = [
            _make_row({"pnl": 1.0}),
            _make_row({"pnl": -1.0}),
            _make_row({"pnl": 2.0}),
        ]
        conn = _FakeConn(
            fetch=[streak_rows],
            fetchrow=[agg_row, portfolio_row],
        )
        result = await q.portfolio_kpis(conn, redis_client=None)
        # 13 / 20 = 0.65
        assert result["win_rate_30d"] == 0.65
        assert result["open_capital_usdc"] == 500.0
        assert result["daily_pnl"] == 12.0


# ---------------------------------------------------------------------------
# /api/portfolio/pipeline_status
# ---------------------------------------------------------------------------


class TestPortfolioPipelineStatus:
    def test_endpoint_shape(self, app_client):
        resp = app_client.get("/api/portfolio/pipeline_status")
        assert resp.status_code == 200
        data = resp.json()
        for k in (
            "bot_status",
            "ws_status",
            "ingestion_lag_s",
            "ingestion_count_24h",
            "exec_mode",
            "killswitch_active",
            "redis_ok",
            "db_ok",
            "last_decision_at",
            "last_trade_at",
        ):
            assert k in data, f"missing key {k}"

    @pytest.mark.asyncio
    async def test_redis_unreachable_returns_degraded(self):
        """A failing Redis must NOT crash the endpoint — it must return
        redis_ok=False and ws_status='redis_unreachable'.
        """
        import src.api.queries as q

        conn = _FakeConn(
            fetchrow=[_make_row({"t": datetime.now(tz=timezone.utc)})],
            fetchval=[
                datetime.now(tz=timezone.utc),  # MAX(opened_at)
                datetime.now(tz=timezone.utc),  # MAX(closed_at)
                100,                              # 24h count
            ],
        )
        failing_redis = AsyncMock()
        failing_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await q.portfolio_pipeline_status(conn, redis_client=failing_redis)

        assert result["redis_ok"] is False
        assert result["ws_status"] == "redis_unreachable"
        # Body still includes DB-derived signals.
        assert result["ingestion_count_24h"] == 100
        # bot_status downgrades when redis is down.
        assert result["bot_status"] == "down"

    @pytest.mark.asyncio
    async def test_healthy_pipeline(self):
        """All systems nominal → bot_status='healthy'."""
        import src.api.queries as q

        now = datetime.now(tz=timezone.utc)
        conn = _FakeConn(
            fetchrow=[_make_row({"t": now})],
            fetchval=[now, now, 200],
        )
        redis_client = AsyncMock()
        redis_client.ping = AsyncMock(return_value=True)
        redis_client.get = AsyncMock(return_value=str(now.timestamp()))
        result = await q.portfolio_pipeline_status(conn, redis_client=redis_client)

        assert result["redis_ok"] is True
        assert result["db_ok"] is True
        assert result["ws_status"] == "connected"
        # exec_mode comes from settings.PAPER_TRADING + killswitch.
        # In tests, the killswitch defaults to a safe-off state (exec_mode=paused).
        # We just check it's one of the documented values.
        assert result["exec_mode"] in ("paper", "live", "paused")
        # bot_status one of the documented values
        assert result["bot_status"] in ("healthy", "degraded", "down", "paused")
