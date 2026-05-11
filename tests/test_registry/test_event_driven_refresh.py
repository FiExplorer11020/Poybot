"""
Phase 3 Round 1 (Agent A) — Event-driven Falcon refresh tests.

The user-facing problem this code path addresses is the 30-min pauses
that map exactly to `FALCON_REFRESH_INTERVAL_S=1800`. Lowering the
timer wastes Falcon quota; instead we keep the 1800 s timer as a
FLOOR and add an event-driven `refresh_wallet(wallet, reason=...)` API
that the trade observer + the event bridge can invoke.

Covers:
1. Single-wallet targeted refresh produces a UPSERT into `leaders`.
2. Concurrent duplicate calls for the same wallet coalesce into ONE
   Falcon round-trip.
3. Cooldown gate — second call within EVENT_REFRESH_COOLDOWN_S is
   skipped.
4. Daily Falcon budget exhaustion — Nth call returns False and emits
   the `budget_exhausted` counter.
5. falcon_no_data path — registers the leader as excluded.
6. refresh_now path — calls refresh_leaderboard + enrich_leaders.
7. LeaderEventBridge end-to-end: a `trades:observed` payload with an
   unknown wallet + size >= EVENT_REFRESH_MIN_USDC triggers
   refresh_wallet.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.registry.event_bridge import LeaderEventBridge
from src.registry.falcon_client import FalconAPIError, FalconClient
from src.registry.leader_registry import LeaderRegistry, _falcon_budget_key
from src.registry.models import LeaderClassification, WalletMetrics


def _make_metrics(**overrides) -> WalletMetrics:
    base = {
        "wallet_address": "0xLeader",
        "total_trades": 100,
        "win_rate": 0.6,
        "avg_position_size": 250,
        "total_volume_usdc": 25_000,
        "total_pnl": 5_000,
        "days_active": 30,
        "avg_trade_duration_s": 3600,
        "avg_holding_period_days": 1.5,
        "h_score": 4.5,
    }
    base.update(overrides)
    return WalletMetrics.model_validate(base)


@pytest.fixture
def fake_redis():
    r = AsyncMock()
    store: dict[str, str] = {}

    async def _incr(key):
        store[key] = str(int(store.get(key, "0")) + 1)
        return int(store[key])

    async def _decr(key):
        store[key] = str(int(store.get(key, "0")) - 1)
        return int(store[key])

    async def _expire(key, ttl):
        return True

    r.incr = AsyncMock(side_effect=_incr)
    r.decr = AsyncMock(side_effect=_decr)
    r.expire = AsyncMock(side_effect=_expire)
    r._store = store
    return r


def _make_registry(redis_client=None) -> tuple[LeaderRegistry, MagicMock]:
    falcon = MagicMock(spec=FalconClient)
    falcon.query = AsyncMock(return_value=[])
    falcon.get_leaderboard = AsyncMock(return_value=[])
    falcon.get_wallet360 = AsyncMock()
    falcon.get_pnl_leaderboard = AsyncMock(return_value=[])
    falcon.get_market_insights = AsyncMock(return_value=None)
    registry = LeaderRegistry(falcon_client=falcon, redis_client=redis_client)
    return registry, falcon


def _mock_get_db():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return fake_get_db, conn


# ---------------------------------------------------------------------------
# 1. Single-wallet refresh — full happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_wallet_upserts_on_success(fake_redis):
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.return_value = _make_metrics()
    fake_get_db, conn = _mock_get_db()
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    assert ok is True
    falcon.get_wallet360.assert_awaited_once_with("0xLeader")
    # leaders UPSERT was issued.
    assert conn.execute.await_count == 1
    sql, *args = conn.execute.await_args[0]
    assert "INSERT INTO leaders" in sql
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_refresh_wallet_handles_falcon_error(fake_redis):
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.side_effect = FalconAPIError("rate limit")
    fake_get_db, conn = _mock_get_db()
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    assert ok is False
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Coalescing duplicate concurrent calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_coalesce(fake_redis):
    """Five parallel refresh_wallet calls => ONE Falcon round-trip."""
    registry, falcon = _make_registry(redis_client=fake_redis)

    gate = asyncio.Event()
    completed = 0

    async def slow_get_wallet360(wallet):
        nonlocal completed
        await gate.wait()
        completed += 1
        return _make_metrics()

    falcon.get_wallet360.side_effect = slow_get_wallet360
    fake_get_db, _conn = _mock_get_db()
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        tasks = [
            asyncio.create_task(
                registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0.05)  # let them all reach the coalesce point
        gate.set()
        results = await asyncio.gather(*tasks)

    # Only ONE call hit Falcon.
    assert completed == 1
    # All five callers got True (the first did the work, others
    # observed the cached result).
    assert all(results)


# ---------------------------------------------------------------------------
# 3. Cooldown gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_wallet_cooldown_gate(fake_redis, monkeypatch):
    """Second call within EVENT_REFRESH_COOLDOWN_S returns False."""
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.return_value = _make_metrics()
    fake_get_db, _conn = _mock_get_db()
    # Long cooldown so the second call is guaranteed to be inside it.
    monkeypatch.setattr(settings, "EVENT_REFRESH_COOLDOWN_S", 3600)
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok1 = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
        ok2 = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    assert ok1 is True
    assert ok2 is False
    falcon.get_wallet360.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Daily budget exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_wallet_skips_when_budget_exhausted(fake_redis, monkeypatch):
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.return_value = _make_metrics()
    fake_get_db, _conn = _mock_get_db()
    monkeypatch.setattr(settings, "FALCON_DAILY_BUDGET", 2)
    monkeypatch.setattr(settings, "EVENT_REFRESH_COOLDOWN_S", 0)
    # Pre-fill the budget counter to its max.
    fake_redis._store[_falcon_budget_key()] = "2"
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    assert ok is False
    falcon.get_wallet360.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_wallet_consumes_budget_on_success(fake_redis, monkeypatch):
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.return_value = _make_metrics()
    fake_get_db, _conn = _mock_get_db()
    monkeypatch.setattr(settings, "FALCON_DAILY_BUDGET", 5)
    monkeypatch.setattr(settings, "EVENT_REFRESH_COOLDOWN_S", 0)
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    # One slot consumed.
    assert fake_redis._store[_falcon_budget_key()] == "1"


@pytest.mark.asyncio
async def test_refresh_wallet_no_redis_unbounded_budget():
    """No Redis => fail-open: budget never blocks the refresh."""
    registry, falcon = _make_registry(redis_client=None)
    falcon.get_wallet360.return_value = _make_metrics()
    fake_get_db, _conn = _mock_get_db()
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok = await registry.refresh_wallet("0xLeader", reason="user_command")
    assert ok is True


# ---------------------------------------------------------------------------
# 5. falcon_no_data path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_wallet_marks_falcon_no_data(fake_redis):
    """Falcon returns None => wallet is upserted as excluded."""
    registry, falcon = _make_registry(redis_client=fake_redis)
    falcon.get_wallet360.return_value = None
    fake_get_db, conn = _mock_get_db()
    with patch("src.registry.leader_registry.get_db", fake_get_db):
        ok = await registry.refresh_wallet("0xLeader", reason="ws_unknown_wallet")
    # No metrics -> we return True (the row was upserted as excluded).
    assert ok is True
    sql, *args = conn.execute.await_args[0]
    assert "falcon_no_data" in sql
    assert "excluded = TRUE" in sql or "excluded" in sql


# ---------------------------------------------------------------------------
# 6. refresh_now — full cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_now_runs_full_cycle(fake_redis):
    registry, falcon = _make_registry(redis_client=fake_redis)
    fake_get_db, _conn = _mock_get_db()
    with patch.object(registry, "refresh_leaderboard", AsyncMock(return_value=0)) as rl, \
         patch.object(registry, "enrich_leaders", AsyncMock(return_value=7)) as el, \
         patch.object(registry, "sync_markets", AsyncMock(return_value=0)) as sm, \
         patch("src.registry.leader_registry.get_db", fake_get_db):
        count = await registry.refresh_now(reason="user_command")
    assert count == 7
    rl.assert_awaited_once()
    el.assert_awaited_once()
    sm.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. LeaderEventBridge integration — qualifies and dispatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bridge_dispatches_on_high_notional(monkeypatch):
    monkeypatch.setattr(settings, "EVENT_REFRESH_MIN_USDC", 1_000.0)
    registry = AsyncMock()
    registry.refresh_wallet = AsyncMock(return_value=True)
    bridge = LeaderEventBridge(registry)
    # 5_000 USDC, unknown wallet => should dispatch.
    await bridge._on_trade(
        {"wallet_address": "0xWhale", "size_usdc": "5000"},
        "trades:observed",
    )
    registry.refresh_wallet.assert_awaited_once_with(
        "0xWhale", reason="ws_unknown_wallet"
    )


@pytest.mark.asyncio
async def test_event_bridge_dispatches_on_streak(monkeypatch):
    monkeypatch.setattr(settings, "EVENT_REFRESH_MIN_USDC", 10_000.0)
    monkeypatch.setattr(settings, "EVENT_REFRESH_UNKNOWN_TRADES", 3)
    registry = AsyncMock()
    registry.refresh_wallet = AsyncMock(return_value=True)
    bridge = LeaderEventBridge(registry)
    # 3 small trades from the same unknown wallet => 3rd triggers.
    for i in range(3):
        await bridge._on_trade(
            {"wallet_address": "0xUnknown", "size_usdc": "100"},
            "trades:observed",
        )
    registry.refresh_wallet.assert_awaited_once_with(
        "0xUnknown", reason="ws_unknown_wallet"
    )


@pytest.mark.asyncio
async def test_event_bridge_skips_known_wallets_below_threshold(monkeypatch):
    monkeypatch.setattr(settings, "EVENT_REFRESH_MIN_USDC", 10_000.0)
    monkeypatch.setattr(settings, "EVENT_REFRESH_UNKNOWN_TRADES", 5)
    registry = AsyncMock()
    registry.refresh_wallet = AsyncMock(return_value=True)
    bridge = LeaderEventBridge(registry)
    bridge.update_known_wallets({"0xKnown"})
    # Small trade by known wallet => no dispatch.
    for _ in range(20):
        await bridge._on_trade(
            {"wallet_address": "0xKnown", "size_usdc": "100"},
            "trades:observed",
        )
    registry.refresh_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_bridge_ignores_malformed_payloads():
    registry = AsyncMock()
    registry.refresh_wallet = AsyncMock(return_value=True)
    bridge = LeaderEventBridge(registry)
    await bridge._on_trade(None, "trades:observed")
    await bridge._on_trade({}, "trades:observed")
    await bridge._on_trade({"wallet_address": ""}, "trades:observed")
    await bridge._on_trade({"wallet_address": "0xX"}, "trades:observed")  # no size
    registry.refresh_wallet.assert_not_awaited()
