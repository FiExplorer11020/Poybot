"""Tests for ``src/monitoring/coverage_reconciler.py``.

Invariants under test:

* ``reconcile_window`` with an empty ``trades_observed`` returns
  zero counts and EMITS NO ratios (divide-by-zero guard).
* ``reconcile_window`` with onchain=100, api_market=95, api_wallet=98
  produces ratios 0.95 and 0.98 and emits the gauges.
* ``run_periodic`` ticks at the configured interval and exits cleanly
  on ``asyncio.CancelledError``.
* ``find_missed_trades`` correctly counts trades in source A but not
  in source B, comparing via the natural-key tuple.
* Window boundaries are half-open: a trade at exactly ``window_start``
  IS included; a trade at exactly ``window_end`` is NOT.
* The trailing buffer in ``run_periodic`` ensures a trade with
  ``time = now`` is NOT in this cycle's window.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.monitoring import coverage_reconciler as cr
from src.monitoring.coverage_reconciler import (
    SOURCE_API_MARKET,
    SOURCE_API_WALLET,
    SOURCE_ONCHAIN,
    SOURCE_WEBSOCKET,
    CoverageReconciler,
)


# --------------------------------------------------------------------------- #
# Helpers — synthetic trades_observed rows and a fake asyncpg Connection      #
# --------------------------------------------------------------------------- #


def _trade(
    source: str,
    *,
    wallet: str = "0xabc",
    market: str = "mkt-1",
    time: datetime | None = None,
    side: str = "buy",
    price: float = 0.5,
    size_usdc: float = 100.0,
) -> dict:
    """Build a synthetic trades_observed row."""
    if time is None:
        time = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "source": source,
        "wallet_address": wallet,
        "market_id": market,
        "time": time,
        "side": side,
        "price": price,
        "size_usdc": size_usdc,
    }


def _natural_key(row: dict) -> tuple:
    return (
        row["wallet_address"],
        row["market_id"],
        row["time"],
        row["side"],
        row["price"],
        row["size_usdc"],
    )


def _make_conn(rows: list[dict]) -> MagicMock:
    """Build an asyncpg connection mock whose ``fetch`` / ``fetchval``
    answers questions about an in-memory list of synthetic
    trades_observed rows.

    The reconciler issues exactly two SQL shapes:
      * ``SELECT source, COUNT(*) ... GROUP BY source`` — handled by
        looking for ``"GROUP BY source"`` in the SQL.
      * ``SELECT COUNT(*) FROM (SELECT ... EXCEPT SELECT ...)`` —
        handled by looking for ``"EXCEPT"``.
    """
    conn = MagicMock()

    async def fake_fetch(sql, *params):
        if "GROUP BY source" in sql:
            window_start, window_end = params
            counts: dict[str, int] = {}
            for r in rows:
                if window_start <= r["time"] < window_end:
                    counts[r["source"]] = counts.get(r["source"], 0) + 1
            return [{"source": s, "n": n} for s, n in counts.items()]
        raise AssertionError(f"Unexpected fetch SQL: {sql!r}")

    async def fake_fetchval(sql, *params):
        if "EXCEPT" in sql:
            source_a, window_start, window_end, source_b = params
            keys_a = {
                _natural_key(r)
                for r in rows
                if r["source"] == source_a and window_start <= r["time"] < window_end
            }
            keys_b = {
                _natural_key(r)
                for r in rows
                if r["source"] == source_b and window_start <= r["time"] < window_end
            }
            return len(keys_a - keys_b)
        raise AssertionError(f"Unexpected fetchval SQL: {sql!r}")

    conn.fetch.side_effect = fake_fetch
    conn.fetchval.side_effect = fake_fetchval
    return conn


def _patch_get_db(conn: MagicMock):
    """Patch the ``get_db`` symbol imported into the reconciler module."""

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch("src.monitoring.coverage_reconciler.get_db", fake_get_db)


@pytest.fixture
def reconciler() -> CoverageReconciler:
    # Small window so run_periodic tests don't sleep forever.
    return CoverageReconciler(window_s=1, alert_threshold=0.95, trailing_buffer_s=0)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset the coverage gauges + counters between tests so set/inc
    assertions don't see leakage from a previous test."""
    from src.monitoring.metrics import coverage_disagreement_total, coverage_ratio

    coverage_ratio.clear()
    # Counters can't be cleared per-label without poking internals; we
    # snapshot the current value at test start and assert deltas.
    coverage_disagreement_total.clear()
    yield


# --------------------------------------------------------------------------- #
# reconcile_window                                                             #
# --------------------------------------------------------------------------- #


async def test_reconcile_window_empty_trades_observed_skips_ratio_emission(
    reconciler,
):
    """No trades at all → counts all zero, ratios dict empty, no metric set."""
    from src.monitoring.metrics import coverage_ratio

    conn = _make_conn(rows=[])
    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=300)

    with _patch_get_db(conn):
        result = await reconciler.reconcile_window(start, end)

    assert result["counts"] == {
        SOURCE_ONCHAIN: 0,
        SOURCE_API_MARKET: 0,
        SOURCE_API_WALLET: 0,
        SOURCE_WEBSOCKET: 0,
    }
    # Divide-by-zero guard: no ratios were emitted.
    assert result["ratios"] == {}
    # All disagreement pairs are 0 (no trades to disagree about).
    assert all(n == 0 for n in result["disagreements"].values())
    # And the gauge itself has no sample for these labels.
    samples = {
        sample.labels.get("source"): sample.value
        for metric in coverage_ratio.collect()
        for sample in metric.samples
        if sample.name == "polybot_coverage_ratio"
    }
    assert samples == {}


async def test_reconcile_window_canonical_ratios(reconciler):
    """onchain=100, api_market=95, api_wallet=98 → ratios 0.95 / 0.98."""
    from src.monitoring.metrics import coverage_ratio

    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=300)
    # Build 100 onchain trades, 95 matching api_market, 98 matching api_wallet.
    # Use distinct natural keys per trade so the EXCEPT counts come
    # out correctly.
    rows: list[dict] = []
    for i in range(100):
        t = start + timedelta(seconds=i)
        rows.append(_trade(SOURCE_ONCHAIN, wallet=f"0x{i:040x}", time=t))
    for i in range(95):
        t = start + timedelta(seconds=i)
        rows.append(_trade(SOURCE_API_MARKET, wallet=f"0x{i:040x}", time=t))
    for i in range(98):
        t = start + timedelta(seconds=i)
        rows.append(_trade(SOURCE_API_WALLET, wallet=f"0x{i:040x}", time=t))

    conn = _make_conn(rows=rows)
    with _patch_get_db(conn):
        result = await reconciler.reconcile_window(start, end)

    assert result["counts"][SOURCE_ONCHAIN] == 100
    assert result["counts"][SOURCE_API_MARKET] == 95
    assert result["counts"][SOURCE_API_WALLET] == 98
    assert result["ratios"][SOURCE_API_MARKET] == pytest.approx(0.95)
    assert result["ratios"][SOURCE_API_WALLET] == pytest.approx(0.98)
    assert result["ratios"][SOURCE_WEBSOCKET] == pytest.approx(0.0)

    # The gauges should be set on the registry.
    samples = {
        sample.labels.get("source"): sample.value
        for metric in coverage_ratio.collect()
        for sample in metric.samples
        if sample.name == "polybot_coverage_ratio"
    }
    assert samples[SOURCE_API_MARKET] == pytest.approx(0.95)
    assert samples[SOURCE_API_WALLET] == pytest.approx(0.98)


async def test_reconcile_window_disagreements_emit_counter(reconciler):
    """Trades present in api_market but not onchain → disagreement counter."""
    from src.monitoring.metrics import coverage_disagreement_total

    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=300)
    # 10 onchain, only 8 of them also seen on api_market.
    rows: list[dict] = []
    for i in range(10):
        t = start + timedelta(seconds=i)
        rows.append(_trade(SOURCE_ONCHAIN, wallet=f"0x{i:040x}", time=t))
    for i in range(8):
        t = start + timedelta(seconds=i)
        rows.append(_trade(SOURCE_API_MARKET, wallet=f"0x{i:040x}", time=t))

    conn = _make_conn(rows=rows)
    with _patch_get_db(conn):
        result = await reconciler.reconcile_window(start, end)

    # 2 onchain trades NOT seen by api_market.
    assert result["disagreements"][(SOURCE_ONCHAIN, SOURCE_API_MARKET)] == 2
    # api_market saw no extra trades beyond onchain.
    assert result["disagreements"][(SOURCE_API_MARKET, SOURCE_ONCHAIN)] == 0

    samples = {
        (s.labels.get("primary"), s.labels.get("missed_by")): s.value
        for metric in coverage_disagreement_total.collect()
        for s in metric.samples
        if s.name == "polybot_coverage_disagreement_total"
    }
    assert samples[(SOURCE_ONCHAIN, SOURCE_API_MARKET)] == 2.0


async def test_reconcile_window_half_open_interval(reconciler):
    """Trade exactly at window_start is included; trade at window_end is not."""
    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    rows = [
        # Exactly at window_start → included.
        _trade(SOURCE_ONCHAIN, wallet="0xstart", time=start),
        # Exactly at window_end → EXCLUDED (half-open).
        _trade(SOURCE_ONCHAIN, wallet="0xend", time=end),
        # Just before window_end → included.
        _trade(
            SOURCE_ONCHAIN,
            wallet="0xinside",
            time=end - timedelta(microseconds=1),
        ),
    ]
    conn = _make_conn(rows=rows)
    with _patch_get_db(conn):
        result = await reconciler.reconcile_window(start, end)
    # 2 included (start + just-before-end), 1 excluded (exactly at end).
    assert result["counts"][SOURCE_ONCHAIN] == 2


# --------------------------------------------------------------------------- #
# find_missed_trades                                                           #
# --------------------------------------------------------------------------- #


async def test_find_missed_trades_natural_key_comparison(reconciler):
    """Two trades with the same natural key on different sources are NOT
    a disagreement; a trade unique to source_a IS one."""
    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    t1 = start + timedelta(seconds=10)
    t2 = start + timedelta(seconds=20)
    rows = [
        # Shared (same natural key on both onchain + api_market) — not missed.
        _trade(SOURCE_ONCHAIN, wallet="0xshared", time=t1),
        _trade(SOURCE_API_MARKET, wallet="0xshared", time=t1),
        # Only on api_market — counted when source_a=api_market, source_b=onchain.
        _trade(SOURCE_API_MARKET, wallet="0xmarket-only", time=t2),
    ]
    conn = _make_conn(rows=rows)
    with _patch_get_db(conn):
        n = await reconciler.find_missed_trades(
            start, end, SOURCE_API_MARKET, SOURCE_ONCHAIN
        )
    assert n == 1


async def test_find_missed_trades_respects_window(reconciler):
    """Trades outside the window are never counted."""
    start = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    rows = [
        # Before window_start — excluded.
        _trade(SOURCE_API_MARKET, wallet="0xpast", time=start - timedelta(seconds=1)),
        # Inside window — counted (unique to api_market).
        _trade(SOURCE_API_MARKET, wallet="0xnow", time=start + timedelta(seconds=10)),
        # After window_end — excluded.
        _trade(SOURCE_API_MARKET, wallet="0xfuture", time=end + timedelta(seconds=1)),
    ]
    conn = _make_conn(rows=rows)
    with _patch_get_db(conn):
        n = await reconciler.find_missed_trades(
            start, end, SOURCE_API_MARKET, SOURCE_ONCHAIN
        )
    assert n == 1


# --------------------------------------------------------------------------- #
# run_periodic                                                                 #
# --------------------------------------------------------------------------- #


async def test_run_periodic_invokes_reconcile_window_and_exits_on_cancel(
    reconciler,
):
    """run_periodic ticks reconcile_window then exits cleanly on cancel."""
    calls: list[tuple[datetime, datetime]] = []

    async def fake_reconcile(start, end):
        calls.append((start, end))
        return {"window": (start, end), "counts": {}, "ratios": {}, "disagreements": {}}

    reconciler.reconcile_window = fake_reconcile  # type: ignore[method-assign]

    task = asyncio.create_task(reconciler.run_periodic())
    # Give the loop a moment to tick at least once. window_s=1 in
    # the fixture, but the FIRST call happens before the first sleep —
    # so a small wait is enough.
    await asyncio.sleep(0.05)
    task.cancel()
    # Must NOT re-raise CancelledError out of run_periodic.
    await task

    assert len(calls) >= 1
    # The window passed to reconcile_window must be a 1-second window
    # ending at (now - buffer). buffer=0 in the fixture, so it ends ≈ now.
    start, end = calls[0]
    assert (end - start) == timedelta(seconds=1)


async def test_run_periodic_trailing_buffer_excludes_trades_at_now(reconciler):
    """The trailing buffer means a trade at time=now is NOT in this
    cycle's window — it will be in the NEXT cycle once the buffer passes."""
    reconciler.trailing_buffer_s = 30
    reconciler.window_s = 60

    captured: list[tuple[datetime, datetime]] = []

    async def fake_reconcile(start, end):
        captured.append((start, end))
        return {"window": (start, end), "counts": {}, "ratios": {}, "disagreements": {}}

    reconciler.reconcile_window = fake_reconcile  # type: ignore[method-assign]
    task = asyncio.create_task(reconciler.run_periodic())
    await asyncio.sleep(0.05)
    task.cancel()
    await task

    assert captured, "run_periodic did not tick"
    start, end = captured[0]
    now = datetime.now(tz=timezone.utc)
    # window_end is now - 30s (give or take a tick).
    assert (now - end).total_seconds() >= 29
    # And window_start is 60s before window_end.
    assert (end - start) == timedelta(seconds=60)
    # Therefore a trade at time=now would fall OUTSIDE [start, end).
    assert now >= end


async def test_run_periodic_swallows_reconcile_exceptions(reconciler):
    """One bad cycle must NOT kill the loop."""
    call_count = {"n": 0}

    async def flaky_reconcile(start, end):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient DB failure")
        return {"window": (start, end), "counts": {}, "ratios": {}, "disagreements": {}}

    reconciler.reconcile_window = flaky_reconcile  # type: ignore[method-assign]
    task = asyncio.create_task(reconciler.run_periodic())
    # Wait long enough for at least two ticks (window_s=1 in fixture
    # — but the first sleep happens AFTER the first call, so we need
    # ~1s of wall time for the second tick).
    await asyncio.sleep(1.2)
    task.cancel()
    await task

    # Loop survived the first exception and ticked again.
    assert call_count["n"] >= 2


# --------------------------------------------------------------------------- #
# Constructor defaults                                                         #
# --------------------------------------------------------------------------- #


def test_constructor_pulls_defaults_from_settings():
    """No args → values read from settings."""
    from src.config import settings

    r = CoverageReconciler()
    assert r.window_s == settings.COVERAGE_RECONCILER_WINDOW_S
    assert r.alert_threshold == settings.COVERAGE_ALERT_THRESHOLD
    assert r.trailing_buffer_s == cr.DEFAULT_TRAILING_BUFFER_S
