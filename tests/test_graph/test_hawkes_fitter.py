"""
Unit tests for src/graph/hawkes_fitter.py
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(fetch_results=None):
    conn = AsyncMock()
    if fetch_results is None:
        conn.fetch = AsyncMock(return_value=[])
    elif (
        isinstance(fetch_results, list)
        and len(fetch_results) > 0
        and isinstance(fetch_results[0], list)
    ):
        # Multiple sequential calls: side_effect
        conn.fetch = AsyncMock(side_effect=fetch_results)
    else:
        conn.fetch = AsyncMock(return_value=fetch_results)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return conn


def _make_mock_get_db(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _make_mock_get_db_sequence(conns):
    """Return a context manager factory that cycles through multiple connections."""
    call_count = {"n": 0}

    @asynccontextmanager
    async def _ctx():
        idx = min(call_count["n"], len(conns) - 1)
        call_count["n"] += 1
        yield conns[idx]

    return _ctx


# ---------------------------------------------------------------------------
# Tests for hawkes_log_likelihood
# ---------------------------------------------------------------------------


def test_hawkes_log_likelihood_positive():
    """Valid params + timestamps should return a finite float."""
    from src.graph.hawkes_fitter import hawkes_log_likelihood

    timestamps = np.array([0.0, 1.0, 2.5, 4.0, 6.0, 8.0, 10.0])
    window_end = 12.0
    params = np.array([0.1, 0.3, 1.0])

    result = hawkes_log_likelihood(params, timestamps, window_end)

    assert np.isfinite(result)
    assert isinstance(result, float)


def test_hawkes_log_likelihood_invalid_params_mu_zero():
    """mu = 0 should return 1e10."""
    from src.graph.hawkes_fitter import hawkes_log_likelihood

    timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    window_end = 5.0
    params = np.array([0.0, 0.3, 1.0])  # mu = 0

    result = hawkes_log_likelihood(params, timestamps, window_end)

    assert result == pytest.approx(1e10)


def test_hawkes_log_likelihood_invalid_params_negative_alpha():
    """alpha < 0 should return 1e10."""
    from src.graph.hawkes_fitter import hawkes_log_likelihood

    timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    window_end = 5.0
    params = np.array([0.1, -0.1, 1.0])  # alpha < 0

    result = hawkes_log_likelihood(params, timestamps, window_end)

    assert result == pytest.approx(1e10)


def test_hawkes_log_likelihood_invalid_params_negative_beta():
    """beta <= 0 should return 1e10."""
    from src.graph.hawkes_fitter import hawkes_log_likelihood

    timestamps = np.array([0.0, 1.0, 2.0])
    window_end = 3.0
    params = np.array([0.1, 0.3, 0.0])  # beta = 0

    result = hawkes_log_likelihood(params, timestamps, window_end)

    assert result == pytest.approx(1e10)


def test_hawkes_log_likelihood_empty_timestamps():
    """Empty timestamps should return 1e10."""
    from src.graph.hawkes_fitter import hawkes_log_likelihood

    timestamps = np.array([])
    window_end = 10.0
    params = np.array([0.1, 0.3, 1.0])

    result = hawkes_log_likelihood(params, timestamps, window_end)

    assert result == pytest.approx(1e10)


# ---------------------------------------------------------------------------
# Tests for HawkesFitter._fit
# ---------------------------------------------------------------------------


def test_fit_recovers_approximate_params():
    """
    Generate synthetic timestamps and verify fit returns a dict with expected keys
    and valid numeric values.
    """
    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()
    # Generate 20 events via inter-arrival exponential (simple Poisson approximation)
    rng = np.random.default_rng(seed=123)
    timestamps = np.cumsum(rng.exponential(10.0, 20))
    window_end = float(timestamps[-1]) + 1.0

    result = fitter._fit(timestamps, window_end)

    # Should return a 3-tuple (mu, alpha, beta)
    assert result is not None
    assert len(result) == 3
    mu, alpha, beta = result
    assert mu > 0
    assert alpha > 0
    assert beta > 0


@pytest.mark.asyncio
async def test_fit_recovers_alpha_mu_ratio_positive():
    """fit_edge returns dict with alpha_mu_ratio >= 0 for valid data."""

    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()

    # Generate 20 fake timestamps as datetime objects
    base_ts = 1_700_000_000.0

    # Patch datetime.timestamp behavior: make rows behave like asyncpg records with "time" column
    # Use simpler approach: mock rows as objects with a dict-like interface
    def make_time_rows(base, count, step):
        rows = []
        for i in range(count):
            rec = MagicMock()
            rec.__getitem__ = MagicMock(
                return_value=MagicMock(timestamp=MagicMock(return_value=base + i * step))
            )
            rows.append(rec)
        return rows

    # Build proper records using asyncpg-like dict access
    class FakeRecord:
        def __init__(self, ts):
            self._ts = ts

        def __getitem__(self, key):
            if key == "time":

                class FakeTime:
                    def __init__(self, t):
                        self._t = t

                    def timestamp(self):
                        return self._t

                return FakeTime(self._ts)

    leader_records = [FakeRecord(base_ts + i * 3600) for i in range(20)]
    follower_records = [FakeRecord(base_ts + i * 3600 + 60) for i in range(20)]

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[leader_records, follower_records])

    with patch("src.graph.hawkes_fitter.get_db", _make_mock_get_db(conn)):
        result = await fitter.fit_edge("0xleader", "0xfollower")

    assert result is not None
    assert "alpha_mu_ratio" in result
    assert result["alpha_mu_ratio"] >= 0
    assert "mu" in result
    assert "alpha" in result
    assert "beta" in result


# ---------------------------------------------------------------------------
# Tests for HawkesFitter.run_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_batch_updates_edges():
    """run_batch fetches edges and calls UPDATE on successful fit."""
    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()

    # Mock a confirmed edge row
    edge_row = MagicMock()
    edge_row.__getitem__ = MagicMock(
        side_effect=lambda k: {
            "leader_wallet": "0xleader",
            "follower_wallet": "0xfollower",
        }[k]
    )

    # First get_db call → fetch edges
    conn_edges = AsyncMock()
    conn_edges.fetch = AsyncMock(return_value=[edge_row])
    conn_edges.execute = AsyncMock()

    # Second get_db call → execute UPDATE
    conn_update = AsyncMock()
    conn_update.execute = AsyncMock()

    call_count = {"n": 0}

    @asynccontextmanager
    async def _cycling_ctx():
        conns = [conn_edges, conn_update]
        idx = min(call_count["n"], len(conns) - 1)
        call_count["n"] += 1
        yield conns[idx]

    fit_result = {
        "mu": 0.1,
        "alpha": 0.3,
        "beta": 1.0,
        "alpha_mu_ratio": 3.0,
    }

    with (
        patch("src.graph.hawkes_fitter.get_db", _cycling_ctx),
        patch.object(fitter, "fit_edge", AsyncMock(return_value=fit_result)),
    ):
        updated = await fitter.run_batch()

    assert updated == 1
    assert conn_update.execute.called
    execute_args = conn_update.execute.call_args[0]
    # First arg is SQL with UPDATE
    assert "UPDATE follower_edges" in execute_args[0]
    assert "hawkes_alpha_mu" in execute_args[0]
    # Second arg is the rounded alpha_mu_ratio
    assert float(execute_args[1]) == pytest.approx(3.0, abs=0.001)


@pytest.mark.asyncio
async def test_fit_edge_insufficient_data():
    """fit_edge returns None when fewer than 5 timestamps returned from DB."""
    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()

    base_ts = 1_700_000_000.0

    class FakeRecord:
        def __init__(self, ts):
            self._ts = ts

        def __getitem__(self, key):
            if key == "time":

                class FakeTime:
                    def __init__(self, t):
                        self._t = t

                    def timestamp(self):
                        return self._t

                return FakeTime(self._ts)

    # Only 3 records for each wallet — below the 5-record minimum
    short_records = [FakeRecord(base_ts + i * 100) for i in range(3)]

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=short_records)

    with patch("src.graph.hawkes_fitter.get_db", _make_mock_get_db(conn)):
        result = await fitter.fit_edge("0xleader", "0xfollower")

    assert result is None


@pytest.mark.asyncio
async def test_run_batch_returns_zero_on_db_error():
    """run_batch returns 0 when DB call raises an exception."""
    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=Exception("DB connection refused"))

    with patch("src.graph.hawkes_fitter.get_db", _make_mock_get_db(conn)):
        updated = await fitter.run_batch()

    assert updated == 0
