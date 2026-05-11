"""Tests for :mod:`src.mempool.wallet_index` — Round 7 Wave-2.

Covers:
  * Bloom membership semantics (``add`` + ``__contains__``).
  * Address normalisation: mixed-case checksummed addresses match the
    lowercase entry.
  * False-positive rate stays within the configured target for 2000
    entries (the production scale).
  * :meth:`refresh_from_universe` builds a fresh bloom from a mocked
    asyncpg cursor and atomically swaps state.
  * :meth:`run_refresh_loop` is cancellable cleanly.
  * :meth:`snapshot_addresses` returns a fresh list (no aliasing).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mempool import wallet_index as wi_module
from src.mempool.wallet_index import (
    DEFAULT_BLOOM_CAPACITY,
    DEFAULT_BLOOM_ERROR_RATE,
    WatchedWalletIndex,
    _Bloom,
)


# --------------------------------------------------------------------------- #
# 1. Bloom semantics                                                           #
# --------------------------------------------------------------------------- #


def test_add_and_contains_basic():
    index = WatchedWalletIndex()
    w = "0x" + "ab" * 20
    assert w not in index
    index.add(w)
    assert w in index


def test_contains_case_insensitive():
    """Mixed-case checksummed addresses must match the lowercase
    entry — wallet_universe stores lowercase, callers may pass either."""
    index = WatchedWalletIndex()
    lower = "0x" + "ab" * 20
    upper = "0X" + "AB" * 20
    index.add(lower)
    assert upper in index


def test_add_handles_missing_0x_prefix():
    """Both ``0xabc...`` and ``abc...`` forms normalise to the same
    bloom entry."""
    index = WatchedWalletIndex()
    index.add("ab" * 20)  # no 0x prefix
    assert ("0x" + "ab" * 20) in index


def test_add_is_idempotent():
    """Adding the same wallet twice doesn't grow the parallel set."""
    index = WatchedWalletIndex()
    w = "0x" + "cd" * 20
    index.add(w)
    index.add(w)
    assert len(index) == 1


def test_empty_string_ignored():
    index = WatchedWalletIndex()
    index.add("")
    assert "" not in index
    assert len(index) == 0


# --------------------------------------------------------------------------- #
# 2. False-positive rate                                                       #
# --------------------------------------------------------------------------- #


def test_false_positive_rate_within_target():
    """With 2000 entries at 1% target FP rate, a sample of 5000
    unrelated wallets must hit FP rate <= ~3% (allow 3x slack for
    finite-sample noise; the bloom is configured for capacity=4096
    so we have 2x headroom)."""
    index = WatchedWalletIndex(
        bloom_capacity=DEFAULT_BLOOM_CAPACITY,
        error_rate=DEFAULT_BLOOM_ERROR_RATE,
    )
    # Insert 2000 deterministic wallets.
    for i in range(2000):
        index.add(f"0x{i:040x}")
    # Probe 5000 unrelated wallets (range disjoint from insertions).
    fp = 0
    probes = 5000
    for i in range(10_000, 10_000 + probes):
        if f"0x{i:040x}" in index:
            fp += 1
    # 3% upper bound on FP rate — generous to absorb noise, tight
    # enough that a totally broken bloom (~50% FP) would fail.
    assert fp / probes < 0.03, f"fp_rate={fp/probes:.4f} (fp={fp})"


# --------------------------------------------------------------------------- #
# 3. refresh_from_universe                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_from_universe_builds_bloom(monkeypatch):
    """Mock the asyncpg cursor; verify the new bloom contains the
    returned rows and the OLD bloom is replaced."""
    index = WatchedWalletIndex()
    # Seed the OLD bloom with an entry that should NOT survive the
    # refresh (refresh is a rebuild, not an incremental update).
    old = "0x" + "00" * 19 + "01"
    index.add(old)
    assert old in index

    rows = [
        {"wallet_address": "0x" + "11" * 20},
        {"wallet_address": "0x" + "22" * 20},
        {"wallet_address": "0x" + "33" * 20},
    ]

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    @asynccontextmanager
    async def _fake_get_db():
        yield conn

    # Patch the import inside refresh_from_universe.
    fake_conn_module = MagicMock()
    fake_conn_module.get_db = _fake_get_db
    monkeypatch.setattr(
        "src.database.connection",
        fake_conn_module,
        raising=False,
    )
    # Patch the import inside the method.
    import sys

    monkeypatch.setitem(
        sys.modules,
        "src.database.connection",
        fake_conn_module,
    )

    n = await index.refresh_from_universe()
    assert n == 3
    for row in rows:
        assert row["wallet_address"] in index
    # The OLD bloom entry is gone.
    assert old not in index
    # The SQL query was issued.
    conn.fetch.assert_awaited_once()
    sql = conn.fetch.await_args.args[0]
    assert "wallet_universe" in sql
    assert "depth_tier" in sql


@pytest.mark.asyncio
async def test_refresh_from_universe_returns_zero_on_db_failure(monkeypatch):
    """A DB error must not crash the daemon — log + return 0."""
    index = WatchedWalletIndex()
    import sys

    fake_module = MagicMock()

    def _broken_get_db():
        raise RuntimeError("db unavailable")

    fake_module.get_db = _broken_get_db
    monkeypatch.setitem(sys.modules, "src.database.connection", fake_module)

    n = await index.refresh_from_universe()
    assert n == 0


# --------------------------------------------------------------------------- #
# 4. run_refresh_loop cancellation                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_refresh_loop_cancellable(monkeypatch):
    """The loop must exit cleanly on cancellation."""
    index = WatchedWalletIndex()

    # Stub refresh_from_universe so the loop doesn't try to talk to DB.
    async def _noop():
        return 0

    monkeypatch.setattr(index, "refresh_from_universe", _noop)

    task = asyncio.create_task(index.run_refresh_loop(interval_s=10))
    # Let it iterate at least once.
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# 5. snapshot_addresses                                                        #
# --------------------------------------------------------------------------- #


def test_snapshot_addresses_returns_fresh_list():
    """The subscription should be free to keep its own reference
    without seeing concurrent index mutation."""
    index = WatchedWalletIndex()
    index.add("0x" + "ab" * 20)
    snap = index.snapshot_addresses()
    assert len(snap) == 1
    # Mutate the index; the snapshot must be untouched.
    index.add("0x" + "cd" * 20)
    assert len(snap) == 1
    assert len(index.snapshot_addresses()) == 2


# --------------------------------------------------------------------------- #
# 6. Bloom internals                                                            #
# --------------------------------------------------------------------------- #


def test_bloom_sizing_matches_target():
    """The internal _Bloom uses optimal m / k formulas."""
    bloom = _Bloom(capacity=1000, error_rate=0.01)
    # 1000 entries at 1% FP → ~9.6 bits/entry → ~9585 bits, k ~= 7.
    # Allow slack: the exact rounding-up depends on math.ceil.
    assert 9000 <= bloom._m_bits <= 12_000
    assert 6 <= bloom._k <= 8
    # bytearray size is m_bits rounded up to a whole number of bytes.
    assert bloom.size_bytes == (bloom._m_bits + 7) // 8
