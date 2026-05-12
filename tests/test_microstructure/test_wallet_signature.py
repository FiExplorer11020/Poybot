"""Unit tests for :mod:`src.microstructure.wallet_signature` — Round 11.

Cover:
  * Nightly batch shape — emits a row per wallet that passes the
    min_orders gate, skips the others.
  * Tier-0/1 filtering — only those wallets pass through the universe
    query.
  * Pure-cancel wallets get a finite ratio sentinel rather than +inf.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.microstructure.wallet_signature import (
    WalletSignature,
    WalletSignatureBatch,
)


@pytest.fixture
def asof_ts():
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _conn_with_tier_wallets_and_signatures(
    tier_wallets: list[str],
    per_wallet_row: dict[str, dict],
):
    """Build a mock asyncpg conn that returns ``tier_wallets`` for the
    wallet_universe query and ``per_wallet_row[w]`` for each derive
    SELECT."""
    conn = AsyncMock()

    async def _fetch(sql: str, *args):
        if "FROM wallet_universe" in sql:
            return [{"wallet_address": w} for w in tier_wallets]
        return []

    async def _fetchrow(sql: str, *args):
        if "FROM clob_book_events" in sql or "WITH ev AS" in sql:
            wallet = args[0]
            return per_wallet_row.get(wallet)
        return None

    conn.fetch = _fetch
    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_batch_writes_one_row_per_eligible_wallet(asof_ts):
    wallets = ["0xa", "0xb", "0xc"]
    rows = {
        "0xa": {
            "n_orders": 200,
            "n_cancels": 150,
            "n_fills": 50,
            "p50": 30.0,
            "p99": 600.0,
        },
        "0xb": {
            # Below min_orders — should be skipped.
            "n_orders": 5,
            "n_cancels": 1,
            "n_fills": 4,
            "p50": 10.0,
            "p99": 60.0,
        },
        "0xc": {
            "n_orders": 100,
            "n_cancels": 80,
            "n_fills": 20,
            "p50": 45.0,
            "p99": 900.0,
        },
    }
    conn = _conn_with_tier_wallets_and_signatures(wallets, rows)
    batch = WalletSignatureBatch(min_orders=50)
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    # Wallets 0xa and 0xc pass the gate; 0xb skipped.
    assert n == 2
    # Two upsert calls.
    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_pure_cancels_get_finite_sentinel(asof_ts):
    rows = {
        "0xpure": {
            "n_orders": 100,
            "n_cancels": 100,
            "n_fills": 0,
            "p50": None,
            "p99": None,
        }
    }
    conn = _conn_with_tier_wallets_and_signatures(["0xpure"], rows)
    batch = WalletSignatureBatch(min_orders=50)
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    assert n == 1
    # Inspect the upsert payload — cancel_to_fill_ratio_30d must be
    # the finite sentinel (= n_cancels) not +inf.
    args = conn.execute.await_args.args
    # Positional args: sql, wallet, rollup_at, c2f, iceberg, spoof,
    #                  p50, p99, n_orders, n_fills
    c2f = args[3]
    assert c2f == 100.0


@pytest.mark.asyncio
async def test_tier_filter_used_in_query(asof_ts):
    """The tier filter must be passed to the wallet_universe query as
    an int[] parameter — verify by capturing the args."""
    captured = []
    conn = AsyncMock()

    async def _fetch(sql, *args):
        captured.append((sql, args))
        if "FROM wallet_universe" in sql:
            return []
        return []

    conn.fetch = _fetch
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    batch = WalletSignatureBatch(tier_filter=(0, 1))
    await batch.run(asof_ts=asof_ts, conn=conn)
    wallet_universe_calls = [
        (sql, args) for sql, args in captured if "wallet_universe" in sql
    ]
    assert len(wallet_universe_calls) == 1
    _sql, args = wallet_universe_calls[0]
    assert args[0] == [0, 1]


@pytest.mark.asyncio
async def test_empty_universe_is_clean_noop(asof_ts):
    """No tier-0/1 wallets → batch returns 0 without raising."""
    conn = _conn_with_tier_wallets_and_signatures([], {})
    batch = WalletSignatureBatch()
    n = await batch.run(asof_ts=asof_ts, conn=conn)
    assert n == 0
    conn.execute.assert_not_called()
