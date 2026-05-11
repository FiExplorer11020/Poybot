"""
Unit tests for src/observer/orderbook_observer.py

The DB-touching paths are tested with a mocked ``get_db`` context manager
so the suite runs without a live Postgres. The pure aggregation helpers
(`_features_from_snapshot`, `_aggregate_snapshots`) are tested directly
without mocks — they're the core "rollup correctness" guarantee.

Coverage matrix:
  1. _features_from_snapshot: healthy / crossed / one-sided / zero-depth
  2. _aggregate_snapshots: mean / max / NULL-feature handling / bucket fork
  3. Rollup correctness: synthetic 60 s of snapshots → expected aggregates
  4. Idempotent rerun: same window twice writes same row count via upsert
  5. Lookback boundary: snapshot at t-69 included, at t-71 excluded
  6. Raw snapshot writer: assert the trade_observer source is the
     UNIQUE writer (Agent Z does NOT duplicate it) — guarded by
     inspecting the symbol surface.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.observer.orderbook_observer import (
    OrderBookObserver,
    _aggregate_snapshots,
    _features_from_snapshot,
    _truncate_to_minute,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_get_db(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _snapshot_row(
    *,
    market_id: str = "m1",
    token_id: str = "t1",
    observed_at: datetime,
    best_bid: float = 0.60,
    best_ask: float = 0.62,
    bid_size: float = 100.0,
    ask_size: float = 50.0,
) -> dict:
    """Build a raw book_quality_snapshots dict matching what asyncpg would
    return (depth_top_levels is a parsed dict because asyncpg auto-decodes
    JSONB; observed_at is a tz-aware datetime)."""
    return {
        "market_id": market_id,
        "token_id": token_id,
        "observed_at": observed_at,
        "best_bid": Decimal(str(best_bid)),
        "best_ask": Decimal(str(best_ask)),
        "depth_top_levels": {
            "bids": [{"price": str(best_bid), "size": str(bid_size)}],
            "asks": [{"price": str(best_ask), "size": str(ask_size)}],
        },
    }


# --------------------------------------------------------------------------- #
# 1. _features_from_snapshot — pure                                            #
# --------------------------------------------------------------------------- #


class TestFeaturesFromSnapshot:
    def test_healthy_book(self):
        bids = [{"price": "0.60", "size": "100"}]
        asks = [{"price": "0.62", "size": "50"}]
        di, sp, mp, md = _features_from_snapshot(bids, asks, 0.60, 0.62)
        # imbalance: (100 - 50) / 150 = 0.333...
        assert di == pytest.approx(1 / 3, abs=1e-9)
        # spread_bps: 0.02 / 0.61 * 10000
        assert sp == pytest.approx(0.02 / 0.61 * 10_000, abs=1e-6)
        # microprice: (0.60 * 50 + 0.62 * 100) / 150 = 92/150 = 0.61333..
        assert mp == pytest.approx(92.0 / 150.0, abs=1e-9)
        # midprice = 0.61, deviation = |0.61333.. - 0.61|
        assert md == pytest.approx(abs(92.0 / 150.0 - 0.61), abs=1e-9)

    def test_crossed_book_returns_none(self):
        di, sp, mp, md = _features_from_snapshot(
            [{"price": "0.65", "size": "10"}],
            [{"price": "0.60", "size": "10"}],
            0.65,
            0.60,
        )
        assert (di, sp, mp, md) == (None, None, None, None)

    def test_one_sided_book_returns_none(self):
        # No bid side
        di, sp, mp, md = _features_from_snapshot(
            [], [{"price": "0.62", "size": "10"}], None, 0.62
        )
        assert (di, sp, mp, md) == (None, None, None, None)

    def test_zero_depth_returns_none(self):
        di, sp, mp, md = _features_from_snapshot(
            [{"price": "0.60", "size": "0"}],
            [{"price": "0.62", "size": "0"}],
            0.60,
            0.62,
        )
        assert (di, sp, mp, md) == (None, None, None, None)

    def test_thin_ask_pushes_microprice_up(self):
        # Heavy bids, thin asks → microprice closer to ask
        bids = [{"price": "0.60", "size": "1000"}]
        asks = [{"price": "0.62", "size": "10"}]
        di, sp, mp, md = _features_from_snapshot(bids, asks, 0.60, 0.62)
        # imbalance ≈ +1 (bid-heavy)
        assert di == pytest.approx((1000 - 10) / 1010, abs=1e-9)
        # microprice should be > midprice (0.61) — thin ask pulls price up
        assert mp > 0.61

    def test_tuple_form_level(self):
        bids = [["0.60", "100"]]
        asks = [["0.62", "50"]]
        di, _, _, _ = _features_from_snapshot(bids, asks, 0.60, 0.62)
        assert di == pytest.approx(1 / 3, abs=1e-9)


# --------------------------------------------------------------------------- #
# 2. _aggregate_snapshots — pure                                               #
# --------------------------------------------------------------------------- #


class TestAggregateSnapshots:
    def test_single_bucket_mean_and_max(self):
        base = datetime(2026, 5, 10, 12, 30, 17, tzinfo=timezone.utc)
        # Three snapshots in the same minute, different imbalances
        rows = [
            _snapshot_row(observed_at=base, bid_size=100, ask_size=50),
            _snapshot_row(
                observed_at=base + timedelta(seconds=20), bid_size=200, ask_size=50
            ),
            _snapshot_row(
                observed_at=base + timedelta(seconds=40), bid_size=10, ask_size=90
            ),
        ]
        out = _aggregate_snapshots(rows)
        # All in the same minute bucket (12:30:00)
        assert len(out) == 1
        key = ("m1", "t1", datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc))
        row = out[key]
        assert row.n_snapshots == 3
        # imbalances: 1/3, 3/5, -8/10 → mean = (1/3 + 0.6 - 0.8) / 3
        expected_mean = (1 / 3 + 3 / 5 + -8 / 10) / 3
        assert row.depth_imbalance_mean == pytest.approx(expected_mean, abs=1e-9)
        # max-|imbalance| keeps sign of the most-extreme:  |-0.8| > |0.6| > |1/3|
        assert row.depth_imbalance_max == pytest.approx(-8 / 10, abs=1e-9)

    def test_split_buckets(self):
        a = datetime(2026, 5, 10, 12, 30, 30, tzinfo=timezone.utc)
        b = datetime(2026, 5, 10, 12, 31, 10, tzinfo=timezone.utc)
        rows = [_snapshot_row(observed_at=a), _snapshot_row(observed_at=b)]
        out = _aggregate_snapshots(rows)
        assert len(out) == 2
        for row in out.values():
            assert row.n_snapshots == 1

    def test_n_snapshots_counts_unusable(self):
        """A bucket with one unusable (crossed) snapshot and one healthy
        one still reports n_snapshots = 2 but means computed only over
        the healthy subset."""
        t = datetime(2026, 5, 10, 12, 30, 30, tzinfo=timezone.utc)
        good = _snapshot_row(observed_at=t)
        # Crossed book — same minute
        bad = _snapshot_row(
            observed_at=t + timedelta(seconds=5),
            best_bid=0.70,
            best_ask=0.65,
        )
        out = _aggregate_snapshots([good, bad])
        assert len(out) == 1
        row = next(iter(out.values()))
        assert row.n_snapshots == 2
        # imbalance_mean computed over 1 snapshot only
        assert row.depth_imbalance_mean == pytest.approx(1 / 3, abs=1e-9)

    def test_depth_top_levels_as_json_string(self):
        """asyncpg returns JSONB as dict, but a manual rerun or backfill
        may pass a string — make sure we tolerate it."""
        t = datetime(2026, 5, 10, 12, 30, 30, tzinfo=timezone.utc)
        row = _snapshot_row(observed_at=t)
        row["depth_top_levels"] = json.dumps(row["depth_top_levels"])
        out = _aggregate_snapshots([row])
        assert len(out) == 1


# --------------------------------------------------------------------------- #
# 3. Rollup correctness — full 60 s synthetic                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rollup_correctness_synthetic_minute():
    """Inject 60 raw snapshots spread across one minute with known features
    and verify the aggregated row matches within 1e-6."""
    base = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        # Alternate imbalances to make mean non-trivial
        bid_size = 100 if i % 2 == 0 else 50
        ask_size = 50 if i % 2 == 0 else 100
        rows.append(
            _snapshot_row(
                observed_at=base + timedelta(seconds=i),
                bid_size=bid_size,
                ask_size=ask_size,
            )
        )
    aggregates = _aggregate_snapshots(rows)
    assert len(aggregates) == 1
    row = next(iter(aggregates.values()))
    assert row.n_snapshots == 60
    # imbalance mean: 30 × (50/150) + 30 × (-50/150) divided by 60 → 0
    assert row.depth_imbalance_mean == pytest.approx(0.0, abs=1e-9)
    # max-|imbalance| has |value| = 50/150 = 1/3
    assert abs(row.depth_imbalance_max) == pytest.approx(1 / 3, abs=1e-9)
    # spread_bps is constant: 0.02 / 0.61 * 10000
    expected_sp = 0.02 / 0.61 * 10_000
    assert row.spread_bps_mean == pytest.approx(expected_sp, abs=1e-6)
    assert row.spread_bps_max == pytest.approx(expected_sp, abs=1e-6)


# --------------------------------------------------------------------------- #
# 4. Idempotent rerun                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_idempotent_rerun_same_window():
    """Running run_once twice over the same window writes the same number
    of rollup rows (no duplicates) — verified by counting executemany
    payloads and confirming both calls pass the same row count."""
    base = datetime(2026, 5, 10, 12, 30, 30, tzinfo=timezone.utc)
    rows = [
        _snapshot_row(observed_at=base + timedelta(seconds=i)) for i in range(0, 30, 5)
    ]

    fetch_conn = AsyncMock()
    fetch_conn.fetch = AsyncMock(return_value=rows)
    insert_conn = AsyncMock()
    insert_conn.executemany = AsyncMock()

    # get_db is called twice per run_once (fetch + upsert). We sequence
    # the same pair of mock conns for each invocation.
    sequence = iter([fetch_conn, insert_conn, fetch_conn, insert_conn])

    def _get_db_mock():
        return _make_get_db(next(sequence))()

    obs = OrderBookObserver(interval_s=60, lookback_s=70)
    now = base + timedelta(seconds=40)  # snapshots fall within lookback

    with patch("src.observer.orderbook_observer.get_db", side_effect=_get_db_mock):
        n1 = await obs.run_once(now=now)
        n2 = await obs.run_once(now=now)

    assert n1 == n2 == 1  # one (market, token, minute) bucket
    # executemany payloads must be identical (same PK + same data)
    payload1 = insert_conn.executemany.call_args_list[0].args[1]
    payload2 = insert_conn.executemany.call_args_list[1].args[1]
    assert payload1 == payload2


# --------------------------------------------------------------------------- #
# 5. Lookback boundary                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lookback_boundary_inclusive_at_t_minus_69():
    """Snapshot at t-69 s IS in the rollup; at t-71 s IS NOT.

    We verify by asserting the SQL bound passed to fetch().
    The actual filtering happens in SQL, so we check that the
    `window_start` parameter equals `now - 70 s` (default lookback).
    """
    now = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)

    fetch_conn = AsyncMock()
    fetch_conn.fetch = AsyncMock(return_value=[])

    def _get_db_mock():
        return _make_get_db(fetch_conn)()

    obs = OrderBookObserver(interval_s=60, lookback_s=70)
    with patch("src.observer.orderbook_observer.get_db", side_effect=_get_db_mock):
        await obs.run_once(now=now)

    # First fetch call: assert window_start = now - 70s
    call = fetch_conn.fetch.call_args_list[0]
    sql, window_start, window_end = call.args
    assert window_end == now
    assert window_start == now - timedelta(seconds=70)
    # The boundary: t-69 < window_end - lookback would be FALSE if window
    # is [now-70, now); t-71 < now-70 is TRUE so excluded. The SQL
    # `observed_at >= $1` makes this exact: 69 in, 71 out.


@pytest.mark.asyncio
async def test_lookback_aggregates_only_in_window():
    """End-to-end: feed three snapshots — at t-30, t-69, t-71 — through
    aggregator, verify the t-71 one drops out at the SQL layer (we
    simulate this by passing only the in-window snapshots into the mock
    fetch)."""
    now = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    # The SQL would filter t-71 out; our mock feeds only the in-window
    # rows so the aggregator sees 2 rows in one bucket.
    in_window = [
        _snapshot_row(observed_at=now - timedelta(seconds=30)),
        _snapshot_row(observed_at=now - timedelta(seconds=69)),
    ]

    fetch_conn = AsyncMock()
    fetch_conn.fetch = AsyncMock(return_value=in_window)
    insert_conn = AsyncMock()
    insert_conn.executemany = AsyncMock()
    sequence = iter([fetch_conn, insert_conn])

    def _get_db_mock():
        return _make_get_db(next(sequence))()

    obs = OrderBookObserver(interval_s=60, lookback_s=70)
    with patch("src.observer.orderbook_observer.get_db", side_effect=_get_db_mock):
        n = await obs.run_once(now=now)
    assert n == 2  # t-30 (12:29) and t-69 (12:28) are in DIFFERENT minutes


# --------------------------------------------------------------------------- #
# 6. Raw snapshot writer ownership                                             #
# --------------------------------------------------------------------------- #


def test_orderbook_observer_does_not_duplicate_raw_writer():
    """The audit spec says: 'If the existing trade observer ALREADY
    captures book_quality_snapshots, DO NOT duplicate the writer.'
    Guard against future regression by asserting that the orderbook
    observer module exposes no symbol resembling a raw-snapshot writer.
    """
    import src.observer.orderbook_observer as mod

    forbidden_names = {
        "persist_book_quality_snapshot",
        "_persist_book_quality_snapshot",
        "write_raw_snapshot",
        "ingest_book_update",
    }
    public = {n for n in dir(mod) if not n.startswith("__")}
    overlap = forbidden_names & public
    assert not overlap, (
        f"orderbook_observer.py must not duplicate the raw writer "
        f"already owned by trade_observer; offending names: {overlap}"
    )


# --------------------------------------------------------------------------- #
# Misc                                                                          #
# --------------------------------------------------------------------------- #


def test_truncate_to_minute_preserves_tz():
    ts = datetime(2026, 5, 10, 12, 30, 45, 123456, tzinfo=timezone.utc)
    out = _truncate_to_minute(ts)
    assert out == datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    assert out.tzinfo is not None


@pytest.mark.asyncio
async def test_run_once_empty_window_returns_zero():
    fetch_conn = AsyncMock()
    fetch_conn.fetch = AsyncMock(return_value=[])

    def _get_db_mock():
        return _make_get_db(fetch_conn)()

    obs = OrderBookObserver(interval_s=60, lookback_s=70)
    with patch("src.observer.orderbook_observer.get_db", side_effect=_get_db_mock):
        n = await obs.run_once()
    assert n == 0
