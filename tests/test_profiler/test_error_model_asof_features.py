"""
Integration tests for error_model._fetch_training_data ↔ feature_store
— Phase 3 Round 2 Agent Y.

These tests prove that the LEAKAGE Phase 0 Task C flagged is now closed:
the training pipeline reads `liquidity_score` from
`market_features_history` at `pr.open_time`, not the AS-OF-NOW value
of `markets.liquidity_score`. The legacy fallback path (live row +
metric bump) is also exercised.

Mocking strategy mirrors `tests/test_profiler/test_error_model.py`:
a single AsyncMock conn whose `fetch` calls return the three datasets
the SUT requests in order — positions, observed trades, follower
edges — followed by the new as-of-features dataset that the feature
store helper queries.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.profiler.error_model import ErrorModel


def _make_get_db(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _position_row(
    market_id="m1",
    open_time=None,
    pnl=-15.0,
    liquidity_score=0.5,
):
    """A 'positions_reconstructed JOIN markets' row as returned by the
    main fetch in `_fetch_training_data`."""
    return {
        "market_id": market_id,
        "token_id": f"tok_{market_id}",
        "direction": "yes",
        "open_time": open_time,
        "close_time": open_time,
        "entry_price": 0.41,
        "size_usdc": 220.0,
        "pnl_usdc": pnl,
        "category": "crypto",
        "liquidity_score": liquidity_score,  # The live `markets.liquidity_score`.
        "avg_recent_price": 0.55,
    }


def _asof_row(idx, in_market_id, in_asof, captured_at, liquidity_score):
    """A `get_market_features_asof_batch` row — same shape as the
    LATERAL JOIN result."""
    return {
        "idx": idx,
        "in_market_id": in_market_id,
        "in_asof": in_asof,
        "captured_at": captured_at,
        "liquidity_score": liquidity_score,
        "volume_24h": 1000.0,
        "category": "crypto",
        "fee_rate_pct": 0.01,
        "source": "falcon_575",
        "extra_json": None,
    }


# ─── 1. With explicit history rows, asof value wins over live value ──────────


@pytest.mark.asyncio
async def test_training_data_uses_asof_history_value_not_current():
    """The asof history row says liquidity_score=0.20 at the time of
    the trade. The live `markets.liquidity_score` says 0.90. The
    training feature MUST be 0.20."""
    open_time = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)

    positions = [_position_row(market_id="m1", open_time=open_time, liquidity_score=0.9)]
    observed_trades: list[dict] = []
    follower_edges: list[dict] = []
    asof_rows = [
        _asof_row(
            idx=0,
            in_market_id="m1",
            in_asof=open_time,
            captured_at=earlier,
            liquidity_score=0.20,  # AS-OF VALUE — what we should pick up.
        )
    ]

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[positions, observed_trades, follower_edges, asof_rows]
    )

    model = ErrorModel()
    with patch("src.profiler.error_model.get_db", _make_get_db(conn)):
        data = await model._fetch_training_data("0xtrain", phase=2)

    assert data is not None
    assert len(data["X"]) == 1
    # _build_features slot [4] is liquidity_score
    assert data["X"][0][4] == pytest.approx(0.20)


# ─── 2. Fallback path: no history row → live value + metric bump ─────────────


@pytest.mark.asyncio
async def test_training_data_falls_back_to_live_when_no_asof_history():
    """For positions older than the dual-write start, there's no
    history row at-or-before `pr.open_time`. The training path must
    fall back to the live `markets.liquidity_score` and bump the
    `feature_store_lookups_total{result='fallback_live'}` counter."""
    open_time = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)

    positions = [_position_row(market_id="m1", open_time=open_time, liquidity_score=0.75)]
    observed_trades: list[dict] = []
    follower_edges: list[dict] = []
    # The LATERAL JOIN returns a NULL row for each input that has no
    # qualifying history.
    asof_rows = [
        _asof_row(
            idx=0,
            in_market_id="m1",
            in_asof=open_time,
            captured_at=None,
            liquidity_score=None,
        )
    ]

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[positions, observed_trades, follower_edges, asof_rows]
    )

    model = ErrorModel()

    # Spy on the fallback counter helper so we can prove the rate is tracked.
    with patch(
        "src.profiler.error_model.record_fallback_live"
    ) as record_spy, patch(
        "src.profiler.error_model.get_db", _make_get_db(conn)
    ):
        data = await model._fetch_training_data("0xtrain", phase=2)

    assert data is not None
    assert len(data["X"]) == 1
    # The live value flows through.
    assert data["X"][0][4] == pytest.approx(0.75)
    # And the fallback metric is bumped with N=1 (one position fell back).
    record_spy.assert_called_once_with(1)


# ─── 3. Empty history table — every position falls back; rate = 100% ─────────


@pytest.mark.asyncio
async def test_training_data_empty_history_table_uses_full_fallback():
    """If `market_features_history` is empty (immediately after deploy
    of migration 016, before the first sync_markets cycle runs), the
    LATERAL JOIN produces a NULL row for every input. Training must
    still succeed and the fallback metric must reflect 100%."""
    open_time_a = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    open_time_b = datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc)

    positions = [
        _position_row(market_id="m1", open_time=open_time_a, liquidity_score=0.55),
        _position_row(market_id="m2", open_time=open_time_b, liquidity_score=0.65),
    ]
    observed_trades: list[dict] = []
    follower_edges: list[dict] = []
    # Empty history → both positions get a NULL-row LATERAL result.
    asof_rows = [
        _asof_row(
            idx=0, in_market_id="m1", in_asof=open_time_a,
            captured_at=None, liquidity_score=None,
        ),
        _asof_row(
            idx=1, in_market_id="m2", in_asof=open_time_b,
            captured_at=None, liquidity_score=None,
        ),
    ]

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[positions, observed_trades, follower_edges, asof_rows]
    )

    model = ErrorModel()
    with patch(
        "src.profiler.error_model.record_fallback_live"
    ) as record_spy, patch(
        "src.profiler.error_model.get_db", _make_get_db(conn)
    ):
        data = await model._fetch_training_data("0xtrain", phase=2)

    assert data is not None
    assert len(data["X"]) == 2
    # Each row uses its own live fallback value.
    assert data["X"][0][4] == pytest.approx(0.55)
    assert data["X"][1][4] == pytest.approx(0.65)
    # Fallback counter bumped with N=2 — 100% fallback rate for this run.
    record_spy.assert_called_once_with(2)
