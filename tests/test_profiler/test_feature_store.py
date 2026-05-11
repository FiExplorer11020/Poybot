"""
Unit tests for src/profiler/feature_store.py — Phase 3 Round 2 Agent Y.

These tests exercise the as-of read API (single + batched) and the
dual-write contract introduced in LeaderRegistry.sync_markets, all
behind a mock asyncpg connection (the surface every other test in
this project uses).

The integration of the feature store with error_model._fetch_training_data
is covered in tests/test_profiler/test_error_model_asof_features.py.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.profiler.feature_store import (
    get_market_features_asof,
    get_market_features_asof_batch,
)
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry
from src.registry.models import MarketInsights


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _row(captured_at, liquidity_score, source="falcon_575", **extra):
    """Build a 'row-like' dict that mimics asyncpg.Record well enough for
    `_row_to_dict` to consume. asyncpg.Record supports `dict(record)`
    natively; a plain dict is dict-compatible by definition."""
    return {
        "market_id": extra.get("market_id", "0xmkt"),
        "captured_at": captured_at,
        "liquidity_score": liquidity_score,
        "volume_24h": extra.get("volume_24h", 1000.0),
        "category": extra.get("category", "crypto"),
        "fee_rate_pct": extra.get("fee_rate_pct", 0.02),
        "source": source,
        "extra_json": extra.get("extra_json"),
    }


def _make_conn(fetchrow_return=None, fetch_return=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    return conn


def _make_get_db(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


# ─── 1. Single-row read at exact captured_at returns that row ────────────────


@pytest.mark.asyncio
async def test_get_asof_returns_exact_row():
    captured = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    fake = _row(captured_at=captured, liquidity_score=0.42)
    conn = _make_conn(fetchrow_return=fake)

    result = await get_market_features_asof(conn, "0xmkt", captured)

    assert result is not None
    assert result["liquidity_score"] == 0.42
    assert result["captured_at"] == captured
    # Verify the SQL is the canonical at-or-before form.
    sql = conn.fetchrow.call_args[0][0]
    assert "captured_at <= $2" in sql
    assert "ORDER BY captured_at DESC" in sql
    assert "LIMIT 1" in sql


# ─── 2. Read at asof > captured_at returns the most-recent row ───────────────


@pytest.mark.asyncio
async def test_get_asof_returns_most_recent_before_asof():
    earlier = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    fake = _row(captured_at=earlier, liquidity_score=0.55)
    conn = _make_conn(fetchrow_return=fake)

    asof_ts = earlier + timedelta(days=3)
    result = await get_market_features_asof(conn, "0xmkt", asof_ts)

    assert result is not None
    # The mocked fetchrow returns the "most recent" row as-is — the
    # SQL `ORDER BY captured_at DESC LIMIT 1` enforces the ordering.
    assert result["captured_at"] == earlier
    assert result["liquidity_score"] == 0.55


# ─── 3. Read at asof < earliest captured_at returns None ─────────────────────


@pytest.mark.asyncio
async def test_get_asof_returns_none_when_no_row_before_asof():
    conn = _make_conn(fetchrow_return=None)  # nothing at-or-before

    asof_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = await get_market_features_asof(conn, "0xmkt", asof_ts)

    assert result is None


# ─── 4. Batched read of N queries returns dict of size N; nulls for missing ──


@pytest.mark.asyncio
async def test_batch_returns_dict_with_nulls_for_missing():
    t1 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 4, 3, 9, 0, tzinfo=timezone.utc)

    # The LATERAL JOIN returns one row per input index. For idx=1
    # (market m2 at t2) we simulate a "no history" hit by returning
    # NULL captured_at — the function then surfaces None for that key.
    rows = [
        {
            "idx": 0,
            "in_market_id": "m1",
            "in_asof": t1,
            "captured_at": t1,
            "liquidity_score": 0.4,
            "volume_24h": 100.0,
            "category": "crypto",
            "fee_rate_pct": 0.01,
            "source": "falcon_575",
            "extra_json": None,
        },
        {
            "idx": 1,
            "in_market_id": "m2",
            "in_asof": t2,
            "captured_at": None,  # LEFT JOIN miss
            "liquidity_score": None,
            "volume_24h": None,
            "category": None,
            "fee_rate_pct": None,
            "source": None,
            "extra_json": None,
        },
        {
            "idx": 2,
            "in_market_id": "m3",
            "in_asof": t3,
            "captured_at": t3,
            "liquidity_score": 0.8,
            "volume_24h": 200.0,
            "category": "politics",
            "fee_rate_pct": 0.02,
            "source": "falcon_575",
            "extra_json": None,
        },
    ]
    conn = _make_conn(fetch_return=rows)

    queries = [("m1", t1), ("m2", t2), ("m3", t3)]
    result = await get_market_features_asof_batch(conn, queries)

    assert len(result) == 3
    assert result[("m1", t1)] is not None
    assert result[("m1", t1)]["liquidity_score"] == 0.4
    assert result[("m2", t2)] is None  # LEFT JOIN miss
    assert result[("m3", t3)] is not None
    assert result[("m3", t3)]["liquidity_score"] == 0.8


@pytest.mark.asyncio
async def test_batch_empty_input_returns_empty_dict_without_db_call():
    conn = _make_conn()
    result = await get_market_features_asof_batch(conn, [])
    assert result == {}
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_uses_lateral_join_sql_shape():
    """The whole point of the batched variant is N+1 avoidance via
    LATERAL JOIN — assert the SQL shape."""
    t1 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    conn = _make_conn(fetch_return=[])  # body doesn't matter for this check

    await get_market_features_asof_batch(conn, [("m1", t1)])

    sql = conn.fetch.call_args[0][0]
    assert "LATERAL" in sql
    assert "ORDER BY captured_at DESC" in sql
    assert "LIMIT 1" in sql


# ─── 5. Dual-write: sync_markets writes both markets AND history ─────────────


@pytest.mark.asyncio
async def test_sync_markets_dual_writes_history_row():
    """Phase 3 Round 2 Agent Y: every sync_markets cycle must append a
    row to market_features_history alongside the markets UPSERT."""
    falcon = MagicMock(spec=FalconClient)
    falcon.query = AsyncMock(
        return_value=[
            {
                "question": "Q",
                "category": "crypto",
                "clob_token_ids": ["tok_yes", "tok_no"],
                "volume24hr": 1234.0,
                "liquidity": 0.5,
                "makerBaseFee": 0.02,
            }
        ]
    )
    falcon.get_market_insights = AsyncMock(
        return_value=MarketInsights(condition_id="0xmkt", liquidity_score=0.7)
    )
    registry = LeaderRegistry(falcon_client=falcon)

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"market_id": "0xmkt"}])
    conn.execute = AsyncMock()

    await registry.sync_markets(conn)

    # We expect at least 2 execute() calls: the markets UPSERT and the
    # market_features_history INSERT.
    assert conn.execute.await_count >= 2
    sqls = [call.args[0] for call in conn.execute.await_args_list]
    # Find the history INSERT.
    history_sqls = [s for s in sqls if "market_features_history" in s]
    assert len(history_sqls) == 1
    assert "INSERT INTO market_features_history" in history_sqls[0]

    # The args of the history INSERT carry the same values as the
    # markets UPSERT (market_id, liquidity_score, volume_24h, category,
    # fee_rate_pct, source).
    history_call = next(
        c for c in conn.execute.await_args_list
        if "market_features_history" in c.args[0]
    )
    args = history_call.args
    assert args[1] == "0xmkt"
    assert args[2] == 0.7  # 575-sourced score
    assert args[3] == 1234.0
    assert args[4] == "crypto"
    assert args[6] == "falcon_575"


@pytest.mark.asyncio
async def test_sync_markets_history_write_failure_does_not_abort_upsert():
    """A market_features_history INSERT failure must NOT abort the
    sync_markets cycle. We log-and-continue; the markets UPSERT has
    already succeeded by the time we reach the history INSERT."""
    falcon = MagicMock(spec=FalconClient)
    falcon.query = AsyncMock(
        return_value=[{"question": "Q", "category": "crypto", "liquidity": 0.5}]
    )
    falcon.get_market_insights = AsyncMock(return_value=None)
    registry = LeaderRegistry(falcon_client=falcon)

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"market_id": "0xmkt"}])

    # First execute() (markets upsert) succeeds; second (history insert) raises.
    async def _execute_side_effect(sql, *args):
        if "market_features_history" in sql:
            raise RuntimeError("simulated history insert failure")
        return None

    conn.execute = AsyncMock(side_effect=_execute_side_effect)

    # Must NOT raise — the count is still incremented because the
    # markets row was upserted successfully.
    count = await registry.sync_markets(conn)
    assert count == 1


# ─── 6. Round-trip: write a sequence, read at intermediate ts ────────────────


@pytest.mark.asyncio
async def test_get_asof_round_trip_returns_correct_row_by_time():
    """Simulates the production read path: given a sequence of refreshes
    over time, a read at an intermediate timestamp must return the
    most-recent row at-or-before that timestamp.

    We mock the DB layer here because this is a unit test — the
    semantic guarantee (ORDER BY captured_at DESC LIMIT 1) is what
    matters, and that's what the SQL string asserts.
    """
    captures = [
        datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
    ]
    scores = [0.3, 0.5, 0.7]

    # Build a fake table: list of rows keyed by (market_id, captured_at).
    table = [
        _row(captured_at=t, liquidity_score=s) for t, s in zip(captures, scores)
    ]

    async def _fetchrow(sql, market_id, asof_ts):
        # Replicate "most-recent at-or-before asof_ts" semantics.
        candidates = [r for r in table if r["captured_at"] <= asof_ts]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["captured_at"])

    conn = AsyncMock()
    conn.fetchrow = _fetchrow

    # Read at the second capture exactly → row 2.
    r = await get_market_features_asof(conn, "0xmkt", captures[1])
    assert r["liquidity_score"] == 0.5

    # Read between capture 2 and capture 3 → still row 2 (most recent at-or-before).
    r = await get_market_features_asof(
        conn, "0xmkt", captures[1] + timedelta(hours=6)
    )
    assert r["liquidity_score"] == 0.5

    # Read after capture 3 → row 3.
    r = await get_market_features_asof(
        conn, "0xmkt", captures[2] + timedelta(days=1)
    )
    assert r["liquidity_score"] == 0.7

    # Read BEFORE the first capture → None.
    r = await get_market_features_asof(
        conn, "0xmkt", captures[0] - timedelta(days=1)
    )
    assert r is None
