"""
Unit tests for src/graph/graph_engine.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(
    wallet: str,
    market_id: str = "market_1",
    side: str = "BUY",
    is_leader: bool = False,
    time: datetime | None = None,
) -> dict:
    if time is None:
        time = datetime.now(tz=timezone.utc)
    return {
        "wallet_address": wallet,
        "market_id": market_id,
        "side": side,
        "is_leader": is_leader,
        "time": time.isoformat(),
        "token_id": "token_1",
        "price": "0.55",
        "size_usdc": "100",
    }


def _make_mock_conn(fetchrow_result=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    return conn


def _make_mock_get_db(conn):
    """Return an async context manager that yields conn."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_trade_leader_triggers_follower_detection():
    """
    Follower BUY in buffer, then leader BUY later within window → _update_edge called.

    The window is [leader_time, leader_time + FOLLOWER_WINDOW_S].
    We put the follower trade into the buffer first (at base_time + 60s),
    then inject the leader trade (at base_time). The engine scans the buffer
    looking for trades AFTER the leader: delta = follower_time - leader_time = +60s → within window.
    """
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = _make_mock_conn(fetchrow_result=None)  # new edge

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Insert follower trade into buffer first (60s after the leader's trade time)
        follower_trade = _make_trade(
            wallet="0xfollower",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time + timedelta(seconds=60),
        )
        await engine.on_trade(follower_trade)

        # Now inject leader trade at base_time — triggers detection scanning buffer
        # follower_time - leader_time = +60s → within FOLLOWER_WINDOW_S (300s)
        leader_trade = _make_trade(
            wallet="0xleader",
            market_id="market_1",
            side="BUY",
            is_leader=True,
            time=base_time,
        )
        await engine.on_trade(leader_trade)

    # conn.execute should have been called (upsert for the detected follower edge)
    assert conn.execute.called


@pytest.mark.asyncio
async def test_on_trade_follower_after_leader_triggers_detection():
    """Normal live order: leader first, follower later within window."""
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = _make_mock_conn(fetchrow_result=None)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        leader_trade = _make_trade(
            wallet="0xleader",
            market_id="market_1",
            side="BUY",
            is_leader=True,
            time=base_time,
        )
        follower_trade = _make_trade(
            wallet="0xfollower",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time + timedelta(seconds=45),
        )

        await engine.on_trade(leader_trade)
        await engine.on_trade(follower_trade)

    assert conn.execute.called


@pytest.mark.asyncio
async def test_on_trade_follower_outside_window_ignored():
    """Follower trade after FOLLOWER_WINDOW_S+100s should not trigger edge update."""
    from src.config import settings
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = _make_mock_conn(fetchrow_result=None)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Follower trade is already in buffer (happened BEFORE the leader trade)
        # so delta = (follower_time - leader_time) will be negative → ignored
        early_follower = _make_trade(
            wallet="0xfollower",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time - timedelta(seconds=10),
        )
        await engine.on_trade(early_follower)

        # Leader trade now
        leader_trade = _make_trade(
            wallet="0xleader",
            market_id="market_1",
            side="BUY",
            is_leader=True,
            time=base_time,
        )
        await engine.on_trade(leader_trade)

        # Late follower — outside window
        late_follower = _make_trade(
            wallet="0xfollower_late",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time + timedelta(seconds=settings.FOLLOWER_WINDOW_S + 100),
        )
        await engine.on_trade(late_follower)

    # No execute call because no valid candidates within window
    assert not conn.execute.called


@pytest.mark.asyncio
async def test_on_trade_non_leader_no_detection():
    """Non-leader trade should not trigger follower detection."""
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = _make_mock_conn(fetchrow_result=None)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        non_leader = _make_trade(
            wallet="0xregular",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time,
        )
        await engine.on_trade(non_leader)

        follower_trade = _make_trade(
            wallet="0xfollower",
            market_id="market_1",
            side="BUY",
            is_leader=False,
            time=base_time + timedelta(seconds=30),
        )
        await engine.on_trade(follower_trade)

    assert not conn.execute.called


@pytest.mark.asyncio
async def test_on_trade_opposite_direction_updates_failure_signal():
    """Leader BUY, follower SELL should update the edge as a negative observation."""
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = _make_mock_conn(fetchrow_result=None)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        follower_trade = _make_trade(
            wallet="0xfollower",
            market_id="market_1",
            side="SELL",
            is_leader=False,
            time=base_time + timedelta(seconds=30),
        )
        await engine.on_trade(follower_trade)

        leader_trade = _make_trade(
            wallet="0xleader",
            market_id="market_1",
            side="BUY",
            is_leader=True,
            time=base_time - timedelta(seconds=15),
        )
        await engine.on_trade(leader_trade)
        await engine.on_trade(follower_trade)

    assert conn.execute.called
    execute_args = conn.execute.call_args[0]
    assert float(execute_args[6]) == pytest.approx(2.0, abs=0.001)


@pytest.mark.asyncio
async def test_beta_binomial_update_increments_alpha():
    """
    Existing edge row returned by fetchrow → same-direction co-occurrence
    should increment beta_a by 1.
    """
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    # Existing edge: beta_a=3.0, beta_b=1.0
    existing_row = {
        "co_occurrences": 3,
        "follow_beta_a": 3.0,
        "follow_beta_b": 1.0,
        "avg_delay_s": 30.0,
        "same_direction_rate": 0.75,
    }
    conn = _make_mock_conn(fetchrow_result=existing_row)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        await engine._update_edge(
            leader_wallet="0xleader",
            follower_wallet="0xfollower",
            delay_s=25.0,
            same_direction=True,
        )

    # Verify execute was called
    assert conn.execute.called
    execute_args = conn.execute.call_args[0]

    # Positional args layout: [0]=SQL, [1]=leader, [2]=follower, [3]=count,
    #                         [4]=follow_prob, [5]=beta_a, [6]=beta_b, [7]=delay, [8]=sdr
    # beta_a should be 4.0 (3.0 + 1.0), follow_probability = 4/(4+1) = 0.8
    new_count = execute_args[3]  # co_occurrences ($3)
    beta_a_arg = execute_args[5]  # follow_beta_a ($5)
    assert new_count == 4
    assert float(beta_a_arg) == pytest.approx(4.0, abs=0.001)


@pytest.mark.asyncio
async def test_get_followers_queries_db():
    """get_followers should call conn.fetch with correct leader_wallet."""
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        result = await engine.get_followers("0xleader_abc")

    assert conn.fetch.called
    fetch_args = conn.fetch.call_args[0]
    # Second argument is the wallet address
    assert fetch_args[1] == "0xleader_abc"
    assert result == []


@pytest.mark.asyncio
async def test_get_confirmed_edges_filters_correctly():
    """get_confirmed_edges should pass MIN_CO_OCCURRENCES and MIN_SAME_DIRECTION_RATE to DB."""
    from src.config import settings
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        await engine.get_confirmed_edges(min_confidence=0.6)

    assert conn.fetch.called
    fetch_args = conn.fetch.call_args[0]
    # $1 = MIN_CO_OCCURRENCES, $2 = MIN_SAME_DIRECTION_RATE, $3 = min_confidence
    assert fetch_args[1] == settings.MIN_CO_OCCURRENCES
    assert float(fetch_args[2]) == pytest.approx(settings.MIN_SAME_DIRECTION_RATE, abs=0.001)
    assert float(fetch_args[3]) == pytest.approx(0.6, abs=0.001)


@pytest.mark.asyncio
async def test_ewma_delay_update():
    """
    EWMA update: existing avg_delay=100s, new delay=50s.
    Expected: 0.94 * 100 + 0.06 * 50 = 97.0
    """
    from src.graph.graph_engine import GraphEngine

    redis_mock = MagicMock()
    engine = GraphEngine(redis_client=redis_mock)

    existing_row = {
        "co_occurrences": 5,
        "follow_beta_a": 3.0,
        "follow_beta_b": 2.0,
        "avg_delay_s": 100.0,
        "same_direction_rate": 0.6,
    }
    conn = _make_mock_conn(fetchrow_result=existing_row)

    with patch("src.graph.graph_engine.get_db", _make_mock_get_db(conn)):
        await engine._update_edge(
            leader_wallet="0xleader",
            follower_wallet="0xfollower",
            delay_s=50.0,
            same_direction=True,
        )

    assert conn.execute.called
    execute_args = conn.execute.call_args[0]
    # avg_delay_s is positional arg 7:
    # leader, follower, count, prob, a, b, delay, sdr
    new_avg_delay = execute_args[7]
    expected = 0.94 * 100.0 + 0.06 * 50.0  # = 97.0
    assert float(new_avg_delay) == pytest.approx(expected, abs=0.01)
