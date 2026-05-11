"""
Unit tests for src/profiler/behavior_profiler.py

Pure helper functions are tested without mocking.
Async methods that touch DB are tested with mocked get_db context managers.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.profiler.behavior_profiler import (
    BehaviorProfiler,
    _compute_maturity,
    _default_profile,
    _infer_reason_codes,
    _reason_penalty_from_profile,
    _update_accuracy,
    _update_decision_learning,
    _update_decision_process,
    _update_dirichlet,
    _update_entry_patterns,
    _update_sizing,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(fetchrow_result=None, fetch_result=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.execute = AsyncMock()
    return conn


def _make_mock_get_db(conn):
    """Return an async context manager factory that yields conn."""

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _make_profiler():
    redis_mock = MagicMock()
    return BehaviorProfiler(redis_client=redis_mock)


# ---------------------------------------------------------------------------
# 1. _update_dirichlet — creates new category
# ---------------------------------------------------------------------------


def test_update_dirichlet_creates_category():
    profile = {}
    _update_dirichlet(profile, "crypto")
    assert "crypto" in profile["preferred_categories"]
    # Round 4 update: size-weighted semantics — prior 1.0 + observation
    # weight (1.0 when size_usdc is None per _size_weight fallback).
    assert profile["preferred_categories"]["crypto"]["alpha"][0] == 2.0


# ---------------------------------------------------------------------------
# 2. _update_dirichlet — increments existing category
# ---------------------------------------------------------------------------


def test_update_dirichlet_increments_existing():
    profile = {}
    _update_dirichlet(profile, "crypto")
    _update_dirichlet(profile, "crypto")
    # Round 4 update: size-weighted — prior 1.0 + 2× obs weight 1.0 = 3.0.
    assert profile["preferred_categories"]["crypto"]["alpha"][0] == 3.0


# ---------------------------------------------------------------------------
# 3. _update_sizing — EWMA converges toward repeated value
# ---------------------------------------------------------------------------


def test_update_sizing_ewma_convergence():
    profile = _default_profile()
    for _ in range(30):
        _update_sizing(profile, 1000.0)
    # After 30 updates with constant 1000, EWMA should be very close to 1000
    ewma = profile["sizing"]["ewma_size"]
    assert abs(ewma - 1000.0) < 10.0, f"EWMA {ewma} did not converge toward 1000"


# ---------------------------------------------------------------------------
# 4. _update_entry_patterns — contrarian rate ≈ 0.3 after 3/10 contrarian
# ---------------------------------------------------------------------------


def test_update_entry_patterns_contrarian():
    profile = _default_profile()
    for i in range(10):
        _update_entry_patterns(profile, is_contrarian=(i < 3))
    rate = profile["entry_patterns"]["contrarian_rate"]
    assert abs(rate - 0.3) < 0.05, f"contrarian_rate {rate} not close to 0.3"
    assert abs(profile["entry_patterns"]["momentum_rate"] - 0.7) < 0.05


# ---------------------------------------------------------------------------
# 5. _update_accuracy — win increments beta_a
# ---------------------------------------------------------------------------


def test_update_accuracy_win():
    profile = _default_profile()
    _update_accuracy(profile, "crypto", win=True)
    cat = profile["accuracy"]["by_category"]["crypto"]
    assert cat["wins"] == 1
    assert cat["beta_a"] == 2.0  # started at 1.0 + 1.0
    assert cat["losses"] == 0


# ---------------------------------------------------------------------------
# 6. _update_accuracy — loss increments beta_b
# ---------------------------------------------------------------------------


def test_update_accuracy_loss():
    profile = _default_profile()
    _update_accuracy(profile, "crypto", win=False)
    cat = profile["accuracy"]["by_category"]["crypto"]
    assert cat["losses"] == 1
    assert cat["beta_b"] == 2.0  # started at 1.0 + 1.0
    assert cat["wins"] == 0


# ---------------------------------------------------------------------------
# 7. _compute_maturity — scaling
# ---------------------------------------------------------------------------


def test_compute_maturity_scaling():
    result = _compute_maturity(50, 5)
    assert result == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 8. _compute_maturity — zero followers → 0.0
# ---------------------------------------------------------------------------


def test_compute_maturity_zero_followers():
    result = _compute_maturity(100, 0)
    assert result == 0.0


# ---------------------------------------------------------------------------
# 9. get_deviation_score — unknown category adds deviation
# ---------------------------------------------------------------------------


def test_get_deviation_score_unknown_category():
    profiler = _make_profiler()
    profile = _default_profile()
    # Give the profile a known category
    _update_dirichlet(profile, "crypto")

    trade = {"category": "sports", "size_usdc": 500, "is_contrarian": False}
    score = profiler.get_deviation_score(profile, trade)
    assert score > 0.0


# ---------------------------------------------------------------------------
# 10. get_deviation_score — oversized trade adds deviation
# ---------------------------------------------------------------------------


def test_get_deviation_score_large_size():
    profiler = _make_profiler()
    profile = _default_profile()
    profile["sizing"]["ewma_size"] = 100.0  # normal size = 100

    # Trade is 10x the EWMA size → ratio > 3 → high deviation
    trade = {"category": "unknown", "size_usdc": 1000.0, "is_contrarian": False}
    score = profiler.get_deviation_score(profile, trade)
    assert score > 0.0


def test_get_process_insights_penalizes_flip_and_burst():
    profiler = _make_profiler()
    profile = _default_profile()

    _update_decision_process(
        profile,
        {
            "market_id": "mkt-1",
            "side": "BUY",
            "size_usdc": 100.0,
            "category": "crypto",
            "time": "2026-04-02T10:00:00+00:00",
        },
    )

    insights = profiler.get_process_insights(
        profile,
        {
            "market_id": "mkt-1",
            "side": "SELL",
            "size_usdc": 120.0,
            "category": "crypto",
            "time": "2026-04-02T10:01:00+00:00",
        },
    )

    assert insights["flip_flag"] is True
    assert insights["process_score"] < 0.5


# ---------------------------------------------------------------------------
# Helpers for multi-call get_db mocking
# ---------------------------------------------------------------------------


def _make_get_db_sequence(*conns):
    """
    Return a mock for get_db that, when called, returns an async context
    manager yielding each successive conn.  Each call consumes the next conn.
    """
    conn_iter = iter(conns)

    def _get_db_mock():
        conn = next(conn_iter)

        @asynccontextmanager
        async def _ctx():
            yield conn

        return _ctx()

    return _get_db_mock


# ---------------------------------------------------------------------------
# 11. on_position_closed — calls conn.execute (INSERT ... ON CONFLICT)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_position_closed_saves_profile():
    profiler = _make_profiler()

    load_conn = _make_mock_conn(
        fetchrow_result={
            "profile_json": json.dumps(_default_profile()),
            "positions_resolved": 0,
            "trades_observed": 0,
        }
    )
    liquidity_conn = _make_mock_conn(fetchrow_result={"liquidity_score": 0.55})
    follower_conn = _make_mock_conn(fetchrow_result={"cnt": 3})
    save_conn = _make_mock_conn()

    get_db_mock = _make_get_db_sequence(load_conn, liquidity_conn, follower_conn, save_conn)

    with patch("src.profiler.behavior_profiler.get_db", get_db_mock):
        event = {
            "wallet_address": "0xabc",
            "pnl_usdc": "100.00",
            "category": "crypto",
            "size_usdc": "500.00",
            "is_contrarian": False,
            "close_time": "2026-04-02T10:00:00+00:00",
        }
        await profiler.on_position_closed(event)

    # Round 4 update: Phase 0 added a leader FK upsert before the profile
    # write, so save_conn.execute is called TWICE. Find the leader_profiles
    # call by SQL fragment.
    assert save_conn.execute.call_count >= 1
    profile_calls = [
        c for c in save_conn.execute.call_args_list
        if "leader_profiles" in c.args[0]
    ]
    assert profile_calls, "expected one execute() call targeting leader_profiles"
    assert "ON CONFLICT" in profile_calls[0].args[0]


# ---------------------------------------------------------------------------
# 12. on_position_closed — resolved count increments by 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_position_closed_increments_resolved():
    profiler = _make_profiler()

    existing_profile = _default_profile()
    load_conn = _make_mock_conn(
        fetchrow_result={
            "profile_json": json.dumps(existing_profile),
            "positions_resolved": 5,
            "trades_observed": 10,
        }
    )
    liquidity_conn = _make_mock_conn(fetchrow_result={"liquidity_score": 0.42})
    follower_conn = _make_mock_conn(fetchrow_result={"cnt": 2})
    save_conn = _make_mock_conn()

    get_db_mock = _make_get_db_sequence(load_conn, liquidity_conn, follower_conn, save_conn)

    with patch("src.profiler.behavior_profiler.get_db", get_db_mock):
        event = {
            "wallet_address": "0xdef",
            "pnl_usdc": "-50.00",
            "category": "politics",
            "size_usdc": "200.00",
            "is_contrarian": True,
            "close_time": "2026-04-02T11:00:00+00:00",
        }
        await profiler.on_position_closed(event)

    # Round 4 update: Phase 0 added a leader FK upsert; filter for the
    # leader_profiles call to read positions_resolved (index 4).
    profile_calls = [
        c for c in save_conn.execute.call_args_list
        if "leader_profiles" in c.args[0]
    ]
    assert profile_calls, "expected one execute() call targeting leader_profiles"
    call_args = profile_calls[0].args
    # Positional args: sql, wallet, profile_json, trades_observed, positions_resolved, maturity
    assert call_args[4] == 6


@pytest.mark.asyncio
async def test_on_position_closed_calls_error_model_update():
    error_model = MagicMock()
    error_model.update = AsyncMock()
    profiler = BehaviorProfiler(redis_client=MagicMock(), error_model=error_model)
    profile = _default_profile()

    profiler._load_profile = AsyncMock(return_value=(profile, 5, 10, 0.4))
    profiler._count_confirmed_followers = AsyncMock(return_value=3)
    profiler._build_error_trade_context = AsyncMock(
        return_value={
            "category": "crypto",
            "is_contrarian": True,
            "deviation_score": 0.3,
            "size_ratio": 1.2,
            "liquidity_score": 0.55,
        }
    )
    profiler._save_profile = AsyncMock()

    await profiler.on_position_closed(
        {
            "wallet_address": "0xerr",
            "market_id": "mkt-1",
            "pnl_usdc": "-12.5",
            "category": "crypto",
            "size_usdc": "120.0",
            "is_contrarian": True,
            "close_time": "2026-04-02T12:00:00+00:00",
        }
    )

    error_model.update.assert_awaited_once()
    payload = error_model.update.await_args.args[1]
    assert payload["pnl_usdc"] == -12.5
    assert payload["trade_context"]["deviation_score"] == 0.3


@pytest.mark.asyncio
async def test_rebuild_decision_learning_replays_closed_paper_trades():
    profiler = _make_profiler()
    profile = _default_profile()
    profile["accuracy"]["overall"] = 0.77

    profiler._fetch_closed_paper_trades = AsyncMock(
        return_value=[
            {
                "leader_wallet": "0xlearn",
                "market_id": "mkt-1",
                "token_id": "tok-1",
                "strategy": "follow",
                "entry_price": 0.42,
                "exit_price": 0.35,
                "size_usdc": 300.0,
                "pnl_usdc": -21.0,
                "confidence": 0.62,
                "close_reason": "stop_loss",
                "closed_at": datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
                "leader_context": {"trade_context": {"category": "crypto", "deviation_score": 0.7}},
                "category": "crypto",
                "liquidity_score": 0.3,
            },
            {
                "leader_wallet": "0xlearn",
                "market_id": "mkt-2",
                "token_id": "tok-2",
                "strategy": "fade",
                "entry_price": 0.61,
                "exit_price": 0.45,
                "size_usdc": 180.0,
                "pnl_usdc": 28.8,
                "confidence": 0.81,
                "close_reason": "take_profit",
                "closed_at": datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc),
                "leader_context": {
                    "trade_context": {
                        "category": "politics",
                        "p_error": 0.7,
                        "error_confidence": 0.82,
                    }
                },
                "category": "politics",
                "liquidity_score": 0.8,
            },
        ]
    )
    profiler._load_profile = AsyncMock(return_value=(profile, 12, 40, 0.55))
    profiler._count_confirmed_followers = AsyncMock(return_value=6)
    profiler._save_profile = AsyncMock()

    result = await profiler.rebuild_decision_learning()

    assert result == {"wallets": 1, "trades": 2}
    profiler._save_profile.assert_awaited_once()
    saved_profile = profiler._save_profile.await_args.kwargs["profile"]
    assert saved_profile["accuracy"]["overall"] == 0.77
    assert saved_profile["decision_learning"]["follow"]["losses"] == 1
    assert saved_profile["decision_learning"]["fade"]["wins"] == 1
    assert saved_profile["loss_analysis"]["recent_losses"][0]["action"] == "follow"


@pytest.mark.asyncio
async def test_rebuild_decision_learning_skips_invalid_market_resolved_samples():
    profiler = _make_profiler()
    profile = _default_profile()

    profiler._fetch_closed_paper_trades = AsyncMock(
        return_value=[
            {
                "leader_wallet": "0xlearn",
                "market_id": "mkt-bad",
                "token_id": "tok-bad",
                "strategy": "fade",
                "entry_price": 0.49,
                "exit_price": 0.49,
                "size_usdc": 150.0,
                "pnl_usdc": 0.0,
                "confidence": 0.74,
                "close_reason": "market_resolved",
                "opened_at": datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
                "closed_at": datetime(2026, 4, 2, 10, 1, tzinfo=timezone.utc),
                "leader_context": {"trade_context": {"trade_age_s": 1800, "live_candidate": False}},
                "category": "sports",
                "liquidity_score": 0.7,
            }
        ]
    )
    profiler._load_profile = AsyncMock(return_value=(profile, 12, 40, 0.55))
    profiler._count_confirmed_followers = AsyncMock(return_value=6)
    profiler._save_profile = AsyncMock()

    result = await profiler.rebuild_decision_learning()

    assert result == {"wallets": 1, "trades": 0}
    saved_profile = profiler._save_profile.await_args.kwargs["profile"]
    assert saved_profile["decision_learning"]["fade"]["wins"] == 0
    assert saved_profile["decision_learning"]["fade"]["losses"] == 0


@pytest.mark.asyncio
async def test_rebuild_order_process_replays_leader_orders():
    profiler = _make_profiler()
    profile = _default_profile()
    profile["accuracy"]["overall"] = 0.55

    profiler._fetch_leader_trades = AsyncMock(
        return_value=[
            {
                "leader_wallet": "0xflow",
                "market_id": "m1",
                "token_id": "t1",
                "side": "BUY",
                "size_usdc": 100.0,
                "time": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
                "category": "crypto",
            },
            {
                "leader_wallet": "0xflow",
                "market_id": "m1",
                "token_id": "t1",
                "side": "SELL",
                "size_usdc": 90.0,
                "time": datetime(2026, 4, 2, 9, 2, tzinfo=timezone.utc),
                "category": "crypto",
            },
        ]
    )
    profiler._load_profile = AsyncMock(return_value=(profile, 5, 10, 0.4))
    profiler._save_profile = AsyncMock()

    result = await profiler.rebuild_order_process()

    assert result == {"wallets": 1, "orders": 2}
    saved_profile = profiler._save_profile.await_args.kwargs["profile"]
    process = saved_profile["decision_process"]
    assert process["orders_seen"] == 2
    assert process["flip_rate"] > 0.0
    assert saved_profile["accuracy"]["overall"] == 0.55


def test_update_decision_learning_records_loss_reasons():
    profile = _default_profile()
    trade_context = {
        "category": "crypto",
        "deviation_score": 0.8,
        "size_ratio": 2.1,
        "liquidity_score": 0.2,
        "is_contrarian": True,
        "profile_maturity": 0.2,
        "confirmed_followers": 3,
        "p_error": 0.7,
    }
    reason_codes = _infer_reason_codes(
        profile=profile,
        action="follow",
        trade_context=trade_context,
        confidence=0.4,
        close_reason="stop_loss",
    )

    _update_decision_learning(
        profile=profile,
        action="follow",
        won=False,
        pnl_usdc=-23.5,
        confidence=0.4,
        reason_codes=reason_codes,
        market_id="mkt-1",
        close_reason="stop_loss",
        event_time="2026-04-02T10:00:00+00:00",
        trade_context=trade_context,
    )

    follow = profile["decision_learning"]["follow"]
    assert follow["losses"] == 1
    assert follow["beta_b"] == 2.0
    assert profile["loss_analysis"]["recent_losses"][0]["reason_codes"]
    assert "high_deviation" in follow["reason_stats"]


def test_reason_penalty_uses_historical_loss_rate():
    profile = _default_profile()
    reason_codes = ["high_deviation", "low_liquidity"]

    for _ in range(4):
        _update_decision_learning(
            profile=profile,
            action="fade",
            won=False,
            pnl_usdc=-10.0,
            confidence=0.7,
            reason_codes=reason_codes,
            market_id="mkt-2",
            close_reason="stop_loss",
            event_time="2026-04-02T11:00:00+00:00",
            trade_context={},
        )

    penalty = _reason_penalty_from_profile(profile, "fade", reason_codes)
    assert penalty > 0.0
