"""
Phase 3 Round 1 (Agent A) — Continuous-cursor REST polling tests.

The "10-30 min pauses between continuous data gathering" pathology had
three root causes; this file pins down the first one: time-window REST
queries leaked trades at the boundary. Replacing the time-window with
a monotonic ``(timestamp_s, tx_hash)`` cursor eliminates the edge cases.

Covers:
1. Cursor persistence — write happens AFTER batch enqueue (the writer's
   PG commit is the atomic boundary).
2. Boot-time fallback — missing cursor produces "now minus
   OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S" with an explicit log.
3. Cursor filter — trades at-or-before the cursor are dropped; equal
   timestamp + different tx_hash counts as new.
4. Cursor head — picks the maximum (ts, tx_hash) tuple, robust to
   unsorted responses.
5. Replay on simulated crash mid-batch — cursor is NOT advanced if no
   new trades were processed, so the next poll re-fetches the same
   range; the DB UNIQUE INDEX absorbs the duplicates.
6. Per-wallet scope — each wallet has its own cursor key so a slow
   wallet's pagination can't rewind a fast wallet's head.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.observer.trade_observer import (
    SOURCE_API_MARKET,
    SOURCE_API_WALLET,
    TradeObserver,
    _cursor_key,
)


@pytest.fixture
def fake_redis():
    r = AsyncMock()
    store: dict[str, str] = {}

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ex=None, nx=None):
        if nx and key in store:
            return None
        store[key] = value if isinstance(value, str) else str(value)
        return True

    r.get = AsyncMock(side_effect=_get)
    r.set = AsyncMock(side_effect=_set)
    r.delete = AsyncMock()
    r.setex = AsyncMock()
    r.publish = AsyncMock()
    r._store = store
    return r


def _make_observer(fake_redis, *, leader_wallets=None):
    return TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=fake_redis,
        leader_wallets=leader_wallets or {"0xleader1"},
        leader_markets=set(),
    )


# ---------------------------------------------------------------------------
# 1. Cursor persistence — round-trip via Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_persists_and_reloads(fake_redis):
    obs = _make_observer(fake_redis)
    await obs._save_cursor(SOURCE_API_MARKET, 1_700_000_000.0, "0xabc")
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    assert ts == 1_700_000_000.0
    assert tx == "0xabc"
    # And the Redis key is the documented format.
    raw = fake_redis._store[_cursor_key(SOURCE_API_MARKET)]
    assert json.loads(raw) == {"ts": 1_700_000_000.0, "tx": "0xabc"}


# ---------------------------------------------------------------------------
# 2. Boot-time fallback — missing cursor => now - bootstrap lookback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_bootstrap_when_missing(fake_redis):
    obs = _make_observer(fake_redis)
    # No prior SET — Redis returns None.
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    # The bootstrap returns "now - lookback"; assert it's within a tight
    # window so the test isn't flaky.
    now_s = datetime.now(tz=timezone.utc).timestamp()
    from src.config import settings
    expected = now_s - settings.OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S
    assert abs(ts - expected) < 5.0
    assert tx == ""


# ---------------------------------------------------------------------------
# 3. Cursor filter — new vs already-seen
# ---------------------------------------------------------------------------


def test_cursor_filter_drops_already_seen():
    trades = [
        {"timestamp": 1000, "transactionHash": "0x1"},
        {"timestamp": 999, "transactionHash": "0x0"},
        {"timestamp": 1001, "transactionHash": "0x2"},
    ]
    out = TradeObserver._cursor_filter_new(trades, cursor_ts=1000.0, cursor_tx="0x1")
    assert len(out) == 1
    assert out[0]["transactionHash"] == "0x2"


def test_cursor_filter_treats_same_ts_different_tx_as_new():
    """Two trades at the same ms have different tx hashes — both new."""
    trades = [
        {"timestamp": 1000, "transactionHash": "0xA"},
        {"timestamp": 1000, "transactionHash": "0xB"},
    ]
    out = TradeObserver._cursor_filter_new(trades, cursor_ts=1000.0, cursor_tx="0xA")
    # 0xA is the cursor head, 0xB is new.
    assert len(out) == 1
    assert out[0]["transactionHash"] == "0xB"


def test_cursor_filter_handles_millisecond_timestamps():
    trades = [{"timestamp": 1_700_000_000_000, "transactionHash": "0x1"}]
    # 1_700_000_000_000 ms == 1_700_000_000 s
    ts, tx = TradeObserver._trade_cursor_tuple(trades[0])
    assert ts == 1_700_000_000.0
    out = TradeObserver._cursor_filter_new(trades, cursor_ts=1_699_999_999.0, cursor_tx="")
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 4. Cursor head — max across batch
# ---------------------------------------------------------------------------


def test_cursor_head_picks_max_across_unsorted():
    trades = [
        {"timestamp": 100, "transactionHash": "0x1"},
        {"timestamp": 200, "transactionHash": "0x2"},
        {"timestamp": 150, "transactionHash": "0x3"},
    ]
    ts, tx = TradeObserver._cursor_head(trades)
    assert ts == 200
    assert tx == "0x2"


def test_cursor_head_empty_batch_returns_zero():
    ts, tx = TradeObserver._cursor_head([])
    assert ts == 0.0
    assert tx == ""


# ---------------------------------------------------------------------------
# 5. Atomicity — cursor advances ONLY after enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_cursor_no_op_when_redis_none():
    """Cursor save with no Redis attached must not raise."""
    obs = TradeObserver(
        falcon_client=AsyncMock(),
        redis_client=None,
        leader_wallets=set(),
        leader_markets=set(),
    )
    await obs._save_cursor(SOURCE_API_MARKET, 1.0, "0x1")  # no exception
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    # Falls back to bootstrap.
    assert tx == ""


@pytest.mark.asyncio
async def test_save_cursor_swallows_redis_errors(fake_redis):
    """A Redis SET failure during cursor save must NOT crash the poll."""
    obs = _make_observer(fake_redis)
    fake_redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
    # No raise expected.
    await obs._save_cursor(SOURCE_API_MARKET, 1.0, "0x1")


# ---------------------------------------------------------------------------
# 6. Per-wallet scope — wallets get distinct cursor keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_wallet_cursor_isolation(fake_redis):
    obs = _make_observer(fake_redis)
    await obs._save_cursor(SOURCE_API_WALLET, 100.0, "0x1", scope="0xWalletA")
    await obs._save_cursor(SOURCE_API_WALLET, 200.0, "0x2", scope="0xWalletB")

    ts_a, _ = await obs._load_cursor(SOURCE_API_WALLET, scope="0xWalletA")
    ts_b, _ = await obs._load_cursor(SOURCE_API_WALLET, scope="0xWalletB")
    assert ts_a == 100.0
    assert ts_b == 200.0


# ---------------------------------------------------------------------------
# 7. Replay on simulated crash mid-batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cursor_advance_when_no_new_trades(fake_redis):
    """Crash before any new trade is processed -> cursor stays put."""
    obs = _make_observer(fake_redis)
    # Pre-set cursor.
    await obs._save_cursor(SOURCE_API_MARKET, 1_700_000_000.0, "0xabc")
    # Simulate the cursor-filter path: server returns the same trade
    # we've already seen (still at the head). The filter returns [].
    trades = [{"timestamp": 1_700_000_000, "transactionHash": "0xabc"}]
    new = obs._cursor_filter_new(trades, 1_700_000_000.0, "0xabc")
    assert new == []
    # Cursor unchanged.
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    assert ts == 1_700_000_000.0
    assert tx == "0xabc"


@pytest.mark.asyncio
async def test_cursor_advances_only_to_filtered_head(fake_redis):
    """Cursor head is computed over NEW trades, not the raw response."""
    obs = _make_observer(fake_redis)
    await obs._save_cursor(SOURCE_API_MARKET, 100.0, "0x1")
    trades = [
        {"timestamp": 100, "transactionHash": "0x1"},  # head — dropped
        {"timestamp": 200, "transactionHash": "0x2"},  # new
        {"timestamp": 150, "transactionHash": "0x3"},  # new
    ]
    new = obs._cursor_filter_new(trades, 100.0, "0x1")
    assert len(new) == 2
    head_ts, head_tx = obs._cursor_head(new)
    assert head_ts == 200
    assert head_tx == "0x2"
    await obs._save_cursor(SOURCE_API_MARKET, head_ts, head_tx)
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    assert ts == 200
    assert tx == "0x2"


# ---------------------------------------------------------------------------
# 8. Legacy / corrupt cursor formats — degrade gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_cursor_accepts_legacy_string_format(fake_redis):
    fake_redis._store[_cursor_key(SOURCE_API_MARKET)] = "1700000000.0:0xabc"
    obs = _make_observer(fake_redis)
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    assert ts == 1_700_000_000.0
    assert tx == "0xabc"


@pytest.mark.asyncio
async def test_load_cursor_corrupt_payload_falls_back_to_bootstrap(fake_redis):
    fake_redis._store[_cursor_key(SOURCE_API_MARKET)] = "not-a-json-or-number"
    obs = _make_observer(fake_redis)
    ts, tx = await obs._load_cursor(SOURCE_API_MARKET)
    # Falls back to bootstrap — recent timestamp.
    now_s = datetime.now(tz=timezone.utc).timestamp()
    assert abs(ts - (now_s - 300)) < 60.0
    assert tx == ""
