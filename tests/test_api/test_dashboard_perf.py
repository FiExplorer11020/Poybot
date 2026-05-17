"""
Dashboard performance regression tests — V1 audit Phase 3 (May 17, 2026).

Backstop for the 229s snapshot rebuild fix. Verifies:
1. The in-process `_cached_helper` actually returns cached results within
   TTL (one builder call) and rebuilds after TTL expiry (two builder calls).
2. `queries.recent_observed_trades` returns the BIGSERIAL `t.id` as `seq`,
   not a ROW_NUMBER() rank — required so PostgreSQL stops sorting the full
   580k-row trades_observed table on every snapshot rebuild.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.api.main as api_main
import src.api.queries as q


# --------------------------------------------------------------------------- #
# _cached_helper                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_helper_cache():
    """Each test starts with an empty helper cache + reset TTL overrides."""
    api_main._HELPER_CACHE.clear()
    yield
    api_main._HELPER_CACHE.clear()


@pytest.mark.asyncio
async def test_cached_helper_skips_rebuild_within_ttl(monkeypatch):
    """A second call to _cached_helper inside the TTL must reuse the cached
    value (builder fn invoked exactly once).
    """
    api_main._HELPER_CACHE_TTLS["__unit_test__"] = 10.0

    call_count = {"n": 0}

    async def builder():
        call_count["n"] += 1
        return {"data": call_count["n"]}

    # Freeze monotonic clock at t=1000.0 for both calls (well within 10s TTL).
    monkeypatch.setattr(api_main.time, "monotonic", lambda: 1000.0)

    first = await api_main._cached_helper("__unit_test__", builder)
    second = await api_main._cached_helper("__unit_test__", builder)

    assert call_count["n"] == 1, "builder called more than once within TTL"
    assert first == {"data": 1}
    assert second == {"data": 1}
    assert first is second  # Same object handed back


@pytest.mark.asyncio
async def test_cached_helper_rebuilds_after_ttl(monkeypatch):
    """A second call after the TTL window must trigger a fresh builder call."""
    api_main._HELPER_CACHE_TTLS["__unit_test__"] = 5.0

    call_count = {"n": 0}

    async def builder():
        call_count["n"] += 1
        return {"data": call_count["n"]}

    # First call at t=1000.0 — cache miss, builder runs, stamps at 1000.0.
    clock = {"now": 1000.0}
    monkeypatch.setattr(api_main.time, "monotonic", lambda: clock["now"])

    first = await api_main._cached_helper("__unit_test__", builder)
    assert call_count["n"] == 1
    assert first == {"data": 1}

    # Advance past TTL — second call must re-invoke the builder.
    clock["now"] = 1000.0 + 5.01

    second = await api_main._cached_helper("__unit_test__", builder)
    assert call_count["n"] == 2, "builder should have re-run after TTL expiry"
    assert second == {"data": 2}


# --------------------------------------------------------------------------- #
# recent_observed_trades — monotonic id, no ROW_NUMBER window                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recent_observed_trades_returns_monotonic_id():
    """The SQL must select `t.id AS seq` (BIGSERIAL PK) — verifies the
    ROW_NUMBER() window function was removed and that the returned rows
    carry the monotonically increasing identifier from the primary key.

    The mock asyncpg connection captures the SQL passed to `fetch` so we
    can assert the window function is gone in addition to checking the
    output shape.
    """
    captured = {"sql": None}

    # Three fake rows, newest first. `seq` here mimics `t.id` from Postgres
    # — strictly monotonic, gaps allowed (BIGSERIAL is not gap-free).
    now = datetime.now(timezone.utc)
    fake_rows = [
        {
            "seq": 100_003,
            "time": now,
            "market_id": "m1",
            "token_id": "tok1",
            "wallet_address": "0xaaa",
            "side": "buy",
            "price": 0.55,
            "size_usdc": 100.0,
            "is_leader": True,
            "question": "Will X happen?",
            "category": "politics",
        },
        {
            "seq": 100_002,
            "time": now - timedelta(seconds=10),
            "market_id": "m1",
            "token_id": "tok1",
            "wallet_address": "0xbbb",
            "side": "sell",
            "price": 0.52,
            "size_usdc": 50.0,
            "is_leader": False,
            "question": "Will X happen?",
            "category": "politics",
        },
        {
            "seq": 100_001,
            "time": now - timedelta(seconds=30),
            "market_id": "m2",
            "token_id": "tok2",
            "wallet_address": "0xccc",
            "side": "buy",
            "price": 0.31,
            "size_usdc": 25.0,
            "is_leader": True,
            "question": "Will Y happen?",
            "category": "crypto",
        },
    ]

    conn = MagicMock()

    async def fetch(sql, *args):
        captured["sql"] = sql
        return fake_rows

    conn.fetch = fetch

    rows = await q.recent_observed_trades(conn, limit=10)

    # 1. SQL no longer carries ROW_NUMBER() — the window function is what
    #    forced a full sort of trades_observed on every call.
    assert "ROW_NUMBER" not in (captured["sql"] or "")
    # 2. Instead, the BIGSERIAL primary key is aliased as seq.
    assert "t.id AS seq" in (captured["sql"] or "")

    # 3. Result shape — three trades, monotonic id field, id string carries
    #    the seq for stable row keying on the frontend.
    assert len(rows) == 3
    seqs = []
    for row in rows:
        # The composed id ends with f":{seq}".
        tail = row["id"].rsplit(":", 1)[-1]
        seqs.append(int(tail))

    # Newest row → highest BIGSERIAL id → comes first.
    assert seqs == [100_003, 100_002, 100_001]
    # Strict monotone decreasing (matches ORDER BY t.time DESC since BIGSERIAL
    # IDs grow with insertion order on append-only trade flow).
    assert all(seqs[i] > seqs[i + 1] for i in range(len(seqs) - 1))
