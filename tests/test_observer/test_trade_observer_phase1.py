"""
Phase 1 Task O — HP-1 trade-observer hot-path tests.

Covers:
1. `_process_trade` enqueues onto `_write_queue` instead of writing inline.
2. `_writer_run_once()` flushes at TRADE_OBSERVER_BATCH_MAX (200) rows.
3. `_writer_run_once()` flushes at TRADE_OBSERVER_BATCH_FLUSH_MS (100 ms)
   even with fewer than 200 rows pending.
4. Queue-full backpressure: trade is dropped and `observer_queue_drops_total`
   increments.
5. ETag round-trip: server returns ETag → next call sends If-None-Match;
   server returns 304 → no records processed.
6. Batch transaction failure rolls back (no partial inserts).
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.trade_observer import (
    SOURCE_API_MARKET,
    TradeObserver,
    _TradeRecord,
)


_TRADE_TIME = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
_MARKET = "0xmarket1"
_TOKEN = "0xtoken1"
_LEADER = "0xleader1"


def _make_redis(duplicate: bool = False):
    r = AsyncMock()
    r.set = AsyncMock(return_value=None if duplicate else True)
    r.setex = AsyncMock()
    r.publish = AsyncMock()
    r.delete = AsyncMock()
    return r


def _make_observer(*, leader_wallets=None, leader_markets=None, duplicate=False):
    obs = TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=_make_redis(duplicate=duplicate),
        leader_wallets=leader_wallets or {_LEADER},
        leader_markets=leader_markets or set(),
    )
    return obs


def _make_writer_conn(*, all_inserted: bool = True):
    """Build a mock conn that the batched writer can drive end-to-end.

    Routes:
    - executemany(markets stub upsert)         → no-op
    - fetch(NULLIF(category) ANY)              → empty (all unknown)
    - fetch(INSERT trades_observed RETURNING)  → synthesizes one returned
                                                  row per VALUES tuple if
                                                  all_inserted=True, else
                                                  empty (all DB-deduped)
    - fetch(FROM leaders WHERE ANY)            → empty
    - fetchrow(FROM markets WHERE id=$1)       → None
    - execute(UPDATE trades_observed)          → no-op
    """
    conn = AsyncMock()
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)

    async def _fetch(sql, *args):
        if "INSERT INTO trades_observed" in sql:
            if not all_inserted:
                return []
            rows = []
            for i in range(0, len(args), 10):
                chunk = args[i : i + 10]
                if len(chunk) < 10:
                    break
                rows.append({
                    "id": 1,
                    "wallet_address": chunk[3],
                    "market_id": chunk[1],
                    "time": chunk[0],
                    "side": chunk[4],
                    "price": chunk[5],
                    "size_usdc": chunk[6],
                })
            return rows
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    return conn


def _mock_get_db(conn):
    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch("src.observer.trade_observer.get_db", fake_get_db)


# ---------------------------------------------------------------------------
# 1. _process_trade enqueues; never writes inline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_trade_enqueues_instead_of_writing_inline():
    obs = _make_observer()
    conn = _make_writer_conn()

    with _mock_get_db(conn):
        await obs._process_trade(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=_LEADER,
            side="BUY",
            price=Decimal("0.65"),
            size_usdc=Decimal("100.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
        )

    # Queue has exactly one record; no DB calls fired yet.
    assert obs._write_queue is not None
    assert obs._write_queue.qsize() == 1
    assert isinstance(obs._write_queue._queue[0], _TradeRecord)
    conn.executemany.assert_not_awaited()
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()
    assert obs.inserted_count == 0


# ---------------------------------------------------------------------------
# 2. Batch flushes at BATCH_MAX rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_flushes_at_batch_max(monkeypatch):
    """With BATCH_MAX records pre-queued, one `_writer_run_once` call
    drains the entire batch in a single multi-row INSERT.
    """
    from src.config import settings

    # Use the real TRADE_OBSERVER_BATCH_MAX from config (200). We
    # construct exactly that many records and verify they all come out
    # in one batch.
    batch_max = int(settings.TRADE_OBSERVER_BATCH_MAX)
    obs = _make_observer()
    conn = _make_writer_conn()

    # Pre-populate the queue directly to avoid 200 dedup roundtrips.
    obs._ensure_write_queue()
    for i in range(batch_max):
        rec = _TradeRecord(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=f"0xwallet{i:04d}",
            side="BUY",
            price=Decimal("0.50"),
            size_usdc=Decimal("10.00"),
            trade_time=_TRADE_TIME,
            source=SOURCE_API_MARKET,
            is_leader=False,
            dedup_key=f"k{i}",
            event_ts_s=0.0,
        )
        await obs._write_queue.put(rec)

    with _mock_get_db(conn):
        n_drained = await obs._writer_run_once()

    assert n_drained == batch_max
    # Exactly one multi-row INSERT call.
    insert_calls = [
        c for c in conn.fetch.call_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert len(insert_calls) == 1
    # The single INSERT carries batch_max × 10 params + 1 SQL string.
    assert len(insert_calls[0].args) == batch_max * 10 + 1
    assert obs.inserted_count == batch_max


# ---------------------------------------------------------------------------
# 3. Batch flushes at FLUSH_MS even with fewer than BATCH_MAX rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_flushes_at_flush_ms_with_partial_batch(monkeypatch):
    """Pre-queue fewer rows than BATCH_MAX. The writer must still flush
    once the FLUSH_MS deadline elapses, not block forever.
    """
    # Tighten the flush window for test speed.
    monkeypatch.setattr(
        "src.config.settings.TRADE_OBSERVER_BATCH_FLUSH_MS", 50
    )
    obs = _make_observer()
    conn = _make_writer_conn()

    obs._ensure_write_queue()
    for i in range(3):
        await obs._write_queue.put(_TradeRecord(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=f"0xw{i}",
            side="BUY",
            price=Decimal("0.50"),
            size_usdc=Decimal("1.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
            is_leader=False,
            dedup_key=f"k{i}",
            event_ts_s=0.0,
        ))

    with _mock_get_db(conn):
        # Should return after at most ~50 ms — bounded by the flush deadline.
        start = asyncio.get_event_loop().time()
        n = await asyncio.wait_for(obs._writer_run_once(), timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - start

    assert n == 3
    # Generous upper bound — the deadline is 50 ms, allow event-loop slack.
    assert elapsed < 0.5
    assert obs.inserted_count == 3


# ---------------------------------------------------------------------------
# 4. Queue-full backpressure: drop + observer_queue_drops_total increments.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_drops_trade(monkeypatch):
    """Cap the queue at 1, fill it, then submit a second trade. The second
    must be dropped (Redis dedup key cleared) and the counter must tick.
    """
    monkeypatch.setattr("src.config.settings.TRADE_OBSERVER_QUEUE_MAX", 1)
    # Tighten the put-timeout so the test doesn't sit for 1 s.
    monkeypatch.setattr("src.observer.trade_observer.QUEUE_PUT_TIMEOUT_S", 0.05)

    obs = _make_observer()

    # Patch the metric so we can assert on .inc() without a real registry.
    drop_metric = MagicMock()
    drop_metric.labels = MagicMock(return_value=drop_metric)
    drop_metric.inc = MagicMock()
    monkeypatch.setattr(
        "src.observer.trade_observer.observer_queue_drops_total", drop_metric
    )

    # First trade fills the queue.
    await obs._process_trade(
        market_id=_MARKET,
        token_id=_TOKEN,
        wallet_address=_LEADER,
        side="BUY",
        price=Decimal("0.65"),
        size_usdc=Decimal("100.00"),
        trade_time=_TRADE_TIME,
        source="websocket",
    )
    assert obs._write_queue.qsize() == 1

    # Second trade — same market/wallet but different time (so the Redis
    # dedup doesn't short-circuit it).
    await obs._process_trade(
        market_id=_MARKET,
        token_id=_TOKEN,
        wallet_address=_LEADER,
        side="BUY",
        price=Decimal("0.65"),
        size_usdc=Decimal("100.00"),
        trade_time=_TRADE_TIME.replace(second=1),
        source="websocket",
    )

    # Queue is still 1 (the second was dropped, not enqueued).
    assert obs._write_queue.qsize() == 1
    drop_metric.labels.assert_called_with(reason="queue_full")
    drop_metric.inc.assert_called_once()
    # Redis dedup key was cleared so a retry can succeed.
    obs._redis.delete.assert_awaited()


# ---------------------------------------------------------------------------
# 5. ETag round-trip: response with ETag → next call sends If-None-Match
#    → 304 → no records processed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_etag_round_trip():
    """Two-stage HTTP mock:
    - First call returns 200 + ETag header + 1 trade body.
    - Second call must include `If-None-Match: <etag>`; mock returns 304
      and the observer must skip body parsing entirely.
    """
    obs = _make_observer(leader_wallets={_LEADER})
    obs._leader_condition_ids.add(_MARKET)  # so the global sweep targets it

    # We capture each call's headers so we can assert the second sends
    # If-None-Match.
    captured_headers: list[dict | None] = []

    class _FakeResp:
        def __init__(self, status, body=None, etag=None):
            self.status = status
            self._body = body or []
            self.headers = {"ETag": etag} if etag else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return self._body

    call_idx = {"n": 0}

    def session_get(url, timeout=None, headers=None):
        captured_headers.append(headers)
        i = call_idx["n"]
        call_idx["n"] += 1
        if i == 0:
            return _FakeResp(
                200,
                body=[{
                    "proxyWallet": _LEADER,
                    "conditionId": _MARKET,
                    "asset": _TOKEN,
                    "side": "BUY",
                    "price": "0.6",
                    "size": "100",
                    "timestamp": 1700000000,
                }],
                etag='"abc123"',
            )
        return _FakeResp(304)

    fake_session = MagicMock()
    fake_session.get = MagicMock(side_effect=session_get)

    # First call — should send no conditional header, capture ETag.
    await obs._backfill_market_activity(fake_session)
    assert captured_headers[0] is None
    assert obs._last_etag == '"abc123"'

    # Second call — should send If-None-Match with the captured ETag,
    # receive 304, skip body.
    n_processed = await obs._backfill_market_activity(fake_session)
    assert captured_headers[1] is not None
    assert captured_headers[1]["If-None-Match"] == '"abc123"'
    assert n_processed == 0


# ---------------------------------------------------------------------------
# 6. Batch transaction failure rolls back (no partial inserts).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_failure_rolls_back():
    """If `_insert_batch_atomic` raises a non-UniqueViolation exception,
    the batch is abandoned: no records are committed, dedup keys are
    cleared so retries can succeed, and `inserted_count` is unchanged.
    """
    obs = _make_observer()
    conn = _make_writer_conn()

    # Force the multi-row INSERT to raise (simulating a generic DB failure).
    async def boom_fetch(sql, *args):
        if "INSERT INTO trades_observed" in sql:
            raise RuntimeError("simulated DB failure")
        return []

    conn.fetch = AsyncMock(side_effect=boom_fetch)

    obs._ensure_write_queue()
    for i in range(3):
        await obs._write_queue.put(_TradeRecord(
            market_id=_MARKET,
            token_id=_TOKEN,
            wallet_address=f"0xw{i}",
            side="BUY",
            price=Decimal("0.50"),
            size_usdc=Decimal("1.00"),
            trade_time=_TRADE_TIME,
            source="websocket",
            is_leader=False,
            dedup_key=f"k{i}",
            event_ts_s=0.0,
        ))

    with _mock_get_db(conn):
        await obs._writer_run_once()

    # Batch failed — no inserts counted, no Redis publishes.
    assert obs.inserted_count == 0
    obs._redis.publish.assert_not_awaited()
    # Dedup keys for all three records were cleared so retries can land.
    assert obs._redis.delete.await_count == 3
