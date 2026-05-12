"""Wave-3 hardening tests for :mod:`src.microstructure.wallet_signature` — R11.

Cover edge cases the baseline suite doesn't:

  * **Empty universe → clean no-op** (re-asserted under a different
    fixture path).
  * **Tier filter actually used** (re-asserted with multiple tiers).
  * **min_orders gate is per-wallet** — a wallet just below the floor is
    skipped, neighbours above are upserted.
  * **Pure-cancel sentinel is finite, not +inf** — re-asserted with
    different cancel counts.
  * **iceberg_score / spoof_score bounded in [0, 1]** — the proxy
    formula must never overflow.
  * **Failure on a single wallet doesn't poison the rest** — one
    fetchrow raises, the batch still upserts the others.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.microstructure.wallet_signature import WalletSignatureBatch


@pytest.fixture
def asof_ts():
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _conn(
    tier_wallets: list[str],
    per_wallet_row: dict[str, dict | Exception],
):
    """Mock asyncpg conn. ``per_wallet_row[w]`` is either a dict or an
    Exception (raised on fetchrow for that wallet)."""
    conn = AsyncMock()

    async def _fetch(sql, *args):
        if "FROM wallet_universe" in sql:
            return [{"wallet_address": w} for w in tier_wallets]
        return []

    async def _fetchrow(sql, *args):
        if "FROM clob_book_events" in sql or "WITH ev AS" in sql:
            wallet = args[0]
            val = per_wallet_row.get(wallet)
            if isinstance(val, Exception):
                raise val
            return val
        return None

    conn.fetch = _fetch
    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()
    return conn


# --------------------------------------------------------------------------- #
# 1. Empty universe                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_universe_returns_zero_and_does_no_upserts(asof_ts):
    conn = _conn(tier_wallets=[], per_wallet_row={})
    batch = WalletSignatureBatch()
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    assert n == 0
    conn.execute.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. Tier-filter coverage                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tier_filter_supports_multiple_tiers(asof_ts):
    """Pass tier_filter=(0, 1, 2) — the batch must pass the same list
    into the wallet_universe query verbatim."""
    captured = []

    conn = AsyncMock()

    async def _fetch(sql, *args):
        captured.append((sql, args))
        return []

    conn.fetch = _fetch
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    batch = WalletSignatureBatch(tier_filter=(0, 1, 2))
    await batch.run(asof_ts=asof_ts, conn=conn)
    wallet_universe_calls = [
        (s, a) for s, a in captured if "wallet_universe" in s
    ]
    assert len(wallet_universe_calls) == 1
    _sql, args = wallet_universe_calls[0]
    assert args[0] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 3. min_orders gate is per-wallet                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_min_orders_per_wallet_gate(asof_ts):
    """One wallet above the gate, one below — only the first upserts."""
    rows = {
        "0xa": {"n_orders": 200, "n_cancels": 50, "n_fills": 50, "p50": 10.0, "p99": 60.0},
        "0xb": {"n_orders": 49, "n_cancels": 1, "n_fills": 48, "p50": 1.0, "p99": 5.0},
    }
    conn = _conn(["0xa", "0xb"], rows)
    batch = WalletSignatureBatch(min_orders=50)
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    assert n == 1
    assert conn.execute.await_count == 1
    # Verify the upserted wallet is 0xa.
    args = conn.execute.await_args.args
    assert args[1] == "0xa"


# --------------------------------------------------------------------------- #
# 4. Pure cancels — different counts, all finite                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n_cancels", [50, 100, 9999])
@pytest.mark.asyncio
async def test_pure_cancel_sentinel_is_finite(asof_ts, n_cancels):
    rows = {
        "0xp": {
            "n_orders": n_cancels,
            "n_cancels": n_cancels,
            "n_fills": 0,
            "p50": None,
            "p99": None,
        }
    }
    conn = _conn(["0xp"], rows)
    batch = WalletSignatureBatch(min_orders=10)
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    assert n == 1
    c2f = conn.execute.await_args.args[3]
    assert isinstance(c2f, float)
    assert math.isfinite(c2f)
    assert c2f == float(n_cancels)


# --------------------------------------------------------------------------- #
# 5. iceberg / spoof score bounded in [0, 1]                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proxy_scores_bounded_zero_one(asof_ts):
    """Even with extreme cancel ratios, the proxy formula must clamp at
    1.0 — the DB column is NUMERIC(8,4) and the R8 classifier expects
    a [0, 1] feature."""
    rows = {
        "0xx": {
            "n_orders": 100,
            "n_cancels": 1000,  # extreme: more cancels than orders
            "n_fills": 5,
            "p50": 1.0,
            "p99": 60.0,
        }
    }
    conn = _conn(["0xx"], rows)
    batch = WalletSignatureBatch(min_orders=10)
    await batch.run(asof_ts=asof_ts, conn=conn)
    args = conn.execute.await_args.args
    iceberg_score = args[4]
    spoof_score = args[5]
    assert 0.0 <= iceberg_score <= 1.0
    assert 0.0 <= spoof_score <= 1.0


# --------------------------------------------------------------------------- #
# 6. One failing wallet doesn't poison the rest                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_per_wallet_isolation_on_derive_failure(asof_ts):
    """fetchrow raises for one wallet → the batch logs and continues
    with the others."""
    rows = {
        "0xa": {"n_orders": 100, "n_cancels": 50, "n_fills": 50, "p50": 1.0, "p99": 5.0},
        "0xb": RuntimeError("simulated DB hiccup"),
        "0xc": {"n_orders": 100, "n_cancels": 20, "n_fills": 80, "p50": 1.0, "p99": 5.0},
    }
    conn = _conn(["0xa", "0xb", "0xc"], rows)
    batch = WalletSignatureBatch(min_orders=50)
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    # 0xa + 0xc upserted; 0xb skipped.
    assert n == 2
    assert conn.execute.await_count == 2


# --------------------------------------------------------------------------- #
# 7. Naive datetime upgraded to UTC                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_naive_asof_upgraded_to_utc():
    """The batch must accept a naive datetime and treat it as UTC —
    asyncpg requires tz-aware values for TIMESTAMPTZ columns."""
    naive = datetime(2026, 5, 12, 10, 0, 0)
    rows = {"0xa": {"n_orders": 100, "n_cancels": 10, "n_fills": 90, "p50": 1.0, "p99": 5.0}}
    conn = _conn(["0xa"], rows)
    batch = WalletSignatureBatch(min_orders=10)
    n = await batch.run(asof_ts=naive, conn=conn)
    assert n == 1
    # The rollup_at passed to the upsert must be tz-aware.
    rollup_at_arg = conn.execute.await_args.args[2]
    assert rollup_at_arg.tzinfo is not None
