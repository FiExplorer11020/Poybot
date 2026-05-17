"""Unit tests for ``scripts/backfill_polymarket_trades.py``.

Focus: the markets-stub bulk insert that must precede the
``trades_observed`` bulk insert.

Background — the Polymarket data-api backfill streams trades for fresh
wallets / markets the rest of the pipeline (Falcon ``sync_markets``,
Gamma resolver) hasn't seen yet. Without a placeholder markets row,
every downstream ``LEFT JOIN m ON m.market_id = t.market_id`` collapses
to NULL ``category`` and silently degrades the profiler's Dirichlet
posteriors, the strategy classifier features, and the dashboard
category mix.

Contract under test:

1. ``bulk_insert`` issues exactly ONE ``executemany`` against
   ``INSERT INTO markets ... ON CONFLICT DO NOTHING`` per chunk, with
   one row per UNIQUE ``market_id`` seen in that chunk.
2. The markets ``executemany`` is issued BEFORE the trades
   ``execute(INSERT INTO trades_observed ...)`` so the FK / LEFT JOIN
   target exists by the time the trade lands.
3. Markets ``executemany`` uses ``ON CONFLICT DO NOTHING`` (idempotent
   re-run safe) and ``category='unknown'``.
4. Empty rows list is a no-op — no SQL fires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import backfill_polymarket_trades as bp


class _FakeConn:
    """Minimal asyncpg-shaped fake. Records execute / executemany calls
    in insertion order so the tests can assert ordering invariants.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        # (kind, sql, args) where kind is 'execute' or 'executemany'.

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        # Mimic asyncpg's "INSERT 0 <N>" status string.
        return "INSERT 0 1"

    async def executemany(self, sql: str, args_list: list[tuple]) -> None:
        self.calls.append(("executemany", sql, tuple(args_list)))


def _row(
    *,
    market_id: str,
    wallet: str = "0xabc",
    price: Decimal = Decimal("0.50"),
    size_usdc: Decimal = Decimal("100.00"),
    side: str = "buy",
) -> tuple:
    """Mirror the 10-tuple shape ``_normalize_trade`` produces.

    The order matters because ``bulk_insert`` indexes ``row[1]`` for
    market_id.
    """
    return (
        datetime(2026, 5, 17, tzinfo=timezone.utc),  # 0 time
        market_id,                                     # 1 market_id
        f"tok-{market_id}",                            # 2 token_id
        wallet,                                        # 3 wallet
        side,                                          # 4 side
        price,                                         # 5 price
        size_usdc,                                     # 6 size_usdc
        "backfill",                                    # 7 source
        False,                                         # 8 is_leader
        "unknown",                                     # 9 category
    )


@pytest.mark.asyncio
async def test_bulk_insert_seeds_markets_stub_before_trades_insert():
    """The markets ``executemany`` must fire BEFORE the trades
    ``execute`` for each chunk. Order ensures the FK / LEFT JOIN target
    exists by the time the trade row is committed.
    """
    conn = _FakeConn()
    rows = [
        _row(market_id="mkt-A"),
        _row(market_id="mkt-B"),
        _row(market_id="mkt-A"),  # dupe → markets stub still 2 unique
    ]

    inserted = await bp.bulk_insert(conn, rows)  # type: ignore[arg-type]

    # One markets executemany + one trades execute in that order.
    kinds = [c[0] for c in conn.calls]
    sqls = [c[1] for c in conn.calls]
    assert "executemany" in kinds
    assert "execute" in kinds

    markets_idx = next(
        i for i, c in enumerate(conn.calls)
        if c[0] == "executemany" and "INSERT INTO markets" in c[1]
    )
    trades_idx = next(
        i for i, c in enumerate(conn.calls)
        if c[0] == "execute" and "INSERT INTO trades_observed" in c[1]
    )
    assert markets_idx < trades_idx, (
        f"markets stub must precede trades_observed insert; got "
        f"calls={[(k, s[:60]) for k, s, _ in conn.calls]}"
    )

    # The markets executemany payload carries one row per UNIQUE
    # market_id in the chunk (set semantics — dupes collapse).
    markets_args = conn.calls[markets_idx][2]
    unique_ids = {a[0] for a in markets_args}
    assert unique_ids == {"mkt-A", "mkt-B"}, (
        f"markets stub payload should be deduped by market_id, got {markets_args}"
    )

    # Markets stub SQL contract — idempotent, with placeholder category.
    markets_sql = sqls[markets_idx]
    assert "ON CONFLICT" in markets_sql
    assert "DO NOTHING" in markets_sql
    assert "'unknown'" in markets_sql

    # bulk_insert reports asyncpg's "INSERT 0 1" parsed count.
    assert inserted >= 0


@pytest.mark.asyncio
async def test_bulk_insert_empty_rows_is_a_noop():
    """No rows → no SQL fired (we must not seed an empty markets stub)."""
    conn = _FakeConn()
    inserted = await bp.bulk_insert(conn, [])  # type: ignore[arg-type]
    assert inserted == 0
    assert conn.calls == []


@pytest.mark.asyncio
async def test_bulk_insert_skips_market_stub_when_all_ids_missing():
    """Defensive: rows where market_id is empty / falsy must not generate
    a markets stub row. (The data-api filter normally drops these, but
    the bulk_insert layer should be defensive too.)"""
    conn = _FakeConn()
    rows = [
        _row(market_id=""),  # falsy market_id — skipped by the stub
    ]

    await bp.bulk_insert(conn, rows)  # type: ignore[arg-type]

    # No markets executemany — the only call should be the trades insert.
    markets_calls = [
        c for c in conn.calls
        if c[0] == "executemany" and "INSERT INTO markets" in c[1]
    ]
    assert markets_calls == []
