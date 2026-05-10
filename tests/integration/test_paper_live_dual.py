"""
End-to-end paper + live dual-routing integration tests (S2.8).

Validates that DecisionRouter routes through real Redis pubsub and that
both PaperTrader and LiveTrader receive + react to decisions correctly
under each TRADING_MODE.

Requirements (skipped if missing):
  * Local Docker Postgres (DATABASE_URL containing localhost) with
    migrations applied (paper_trades + live_trades tables).
  * Local Docker Redis (REDIS_URL).
  * LIVE_TRADING_DRY_RUN=true so LiveTrader never hits a real CLOB —
    it inserts `live_trades` rows in `shadow` status instead.

Run with:
    pytest tests/integration/test_paper_live_dual.py -m integration -v

Scenarios (mirror the fast suite, but against real services):
  1. Dual mode → 1 decision lands on BOTH paper_trades AND live_trades.
  2. Live filter blocks low-confidence in dual mode → only paper_trades row.
  3. Runtime override flips mode at runtime → no restart needed.
  4. Paper crash isolation → live row still inserted when paper raises.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Skip guard — only run against local Docker
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_IS_LOCAL = "localhost" in _DB_URL or "127.0.0.1" in _DB_URL
_SKIP_REASON = (
    "DATABASE_URL not set or does not point at localhost — "
    "integration tests require the local Docker stack"
)

pytestmark = pytest.mark.integration

skip_unless_local = pytest.mark.skipif(
    not _IS_LOCAL,
    reason=_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        # Wipe the override key from any prior run.
        await client.delete("trading:mode_override")
        yield client
    finally:
        await client.delete("trading:mode_override")
        await client.aclose()


@pytest.fixture
async def db_pool():
    """Initialize the shared asyncpg pool used by the traders, then
    tear it down. We do NOT truncate any production-looking tables —
    instead each test scopes its rows by a unique market_id and we
    clean up only those rows on teardown."""
    from src.config import settings
    from src.database.connection import close_pool, initialize_pool

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=1,
        max_size=4,
    )
    try:
        yield
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    """Make ABSOLUTELY sure the live trader never sends a real order."""
    monkeypatch.setenv("LIVE_TRADING_DRY_RUN", "true")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "")  # forces dry_run anyway
    # Reset any router settings that may have been mutated.
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.TRADING_MODE_OVERRIDE_KEY",
        "trading:mode_override",
    )
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_CONFIDENCE_MIN", 0.6)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_SIZE_MIN_USDC", 10.0)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_market_id() -> str:
    """Each test uses a unique market_id so it never collides with other
    runs and cleanup is precise."""
    return f"0xITEST_{uuid.uuid4().hex[:16]}"


def _decision_payload(
    *,
    market_id: str,
    confidence: float = 0.8,
    size_usdc: float = 100.0,
    action: str = "follow",
) -> dict:
    """Build the same payload shape DecisionRouter._build_payload emits.
    We bypass the router and publish directly when a test wants to
    exercise the trader subscribers in isolation; for full e2e routing
    we still call DecisionRouter.route()."""
    return {
        "action": action,
        "leader_wallet": "0xLeaderTest",
        "market_id": market_id,
        "market_question": "integration test?",
        "market_category": None,
        "market_type": None,
        "token_id": "tok-yes-itest",
        "size_usdc": size_usdc,
        "kelly_fraction": 0.02,
        "confidence": confidence,
        "thompson_follow": 0.7,
        "thompson_fade": 0.3,
        "reason": "integration_test",
        "wallet_type": None,
        "wallet_strategy": None,
        "wallet_horizon": None,
        "wallet_influence": None,
        "trade_context": {
            "market_question": "integration test?",
            "live_candidate": True,
            "trade_age_s": 1.0,
        },
        "context_penalty": 0.0,
        "strategy_track": "leader_swing",
        "economic_model_version": "1.0",
        "signal_audit": {"accepted": True, "reject_reason": None},
    }


async def _cleanup_market(market_id: str) -> None:
    """Remove every paper_trades / live_trades row tagged with the test's
    market_id. Safe even if no rows exist."""
    from src.database.connection import get_db

    async with get_db() as conn:
        await conn.execute("DELETE FROM paper_trades WHERE market_id = $1", market_id)
        await conn.execute("DELETE FROM live_trades WHERE market_id = $1", market_id)


async def _count_rows(table: str, market_id: str) -> int:
    from src.database.connection import get_db

    async with get_db() as conn:
        n = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE market_id = $1", market_id
        )
    return int(n or 0)


class CollectingSubscriber:
    """Minimal async subscriber that records what it sees on a channel.
    Lighter than the real traders (no DB writes) — used only for the
    crash-isolation test where we don't want full PaperTrader spinning."""

    def __init__(self, redis_client, channel: str, *, raise_on_recv: bool = False):
        self._redis = redis_client
        self._channel = channel
        self._raise_on_recv = raise_on_recv
        self.received: list[dict] = []
        self.errors = 0
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        self._ready.set()
        try:
            while not self._stop.is_set():
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.05
                )
                if msg is None:
                    continue
                if msg.get("type") != "message":
                    continue
                try:
                    payload = json.loads(msg["data"])
                except Exception:
                    continue
                if self._raise_on_recv:
                    self.errors += 1
                    raise RuntimeError("paper subscriber crashed (test scenario)")
                self.received.append(payload)
        finally:
            await pubsub.unsubscribe(self._channel)
            await pubsub.aclose()


async def _wait_for(condition, timeout: float = 3.0, interval: float = 0.05) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if condition():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


# ---------------------------------------------------------------------------
# Scenario 1 — Dual mode: 1 decision → both DB tables get a row
# ---------------------------------------------------------------------------


@skip_unless_local
@pytest.mark.asyncio
async def test_dual_mode_routes_to_both_tables(redis_client, db_pool) -> None:
    """In dual mode, a passing decision creates rows in BOTH
    paper_trades and live_trades (live in `shadow` status because of
    LIVE_TRADING_DRY_RUN)."""
    from src.engine.decision_router import (
        REDIS_DECISIONS_LIVE_CHANNEL,
        REDIS_DECISIONS_PAPER_CHANNEL,
        DecisionRouter,
    )
    from src.engine.live_trader import LiveTrader
    from src.engine.paper_trader import PaperTrader

    market_id = _unique_market_id()
    try:
        await redis_client.set("trading:mode_override", "dual")

        paper = PaperTrader(redis_client=redis_client)
        live = LiveTrader(redis_client=redis_client)
        # Don't run full start() — we only want the subscriber loops.
        # Bypass the monitor loops because they hit get_midpoint, etc.
        paper._running = True
        live._running = True
        paper_task = asyncio.create_task(paper._subscribe_loop())
        live_task = asyncio.create_task(live._subscribe_loop())
        await asyncio.sleep(0.2)  # let subscriptions settle

        router = DecisionRouter(redis_client=redis_client)
        # Build a Decision-shaped object; the router only reads its attrs.
        from src.engine.confidence_engine import Decision

        decision = Decision(
            action="follow",
            leader_wallet="0xLeaderTest",
            market_id=market_id,
            token_id="tok-yes-itest",
            size_usdc=100.0,
            kelly_fraction=0.02,
            thompson_follow=0.7,
            thompson_fade=0.3,
            confidence=0.8,
            reason="integration_test",
            trade_context={
                "market_question": "integration test?",
                "live_candidate": True,
                "trade_age_s": 1.0,
            },
            signal_audit={"accepted": True, "reject_reason": None},
        )
        result = await router.route(decision)
        assert result.routed_to_paper is True
        assert result.routed_to_live is True

        # Wait for both rows to materialize.
        got_both = await _wait_for(
            lambda: asyncio.run_coroutine_threadsafe is not None  # noop guard
        )
        # Poll the DB instead of using sync helpers in lambda.
        for _ in range(60):
            paper_n = await _count_rows("paper_trades", market_id)
            live_n = await _count_rows("live_trades", market_id)
            if paper_n >= 1 and live_n >= 1:
                break
            await asyncio.sleep(0.1)

        assert paper_n >= 1, f"paper_trades should have a row for {market_id}"
        assert live_n >= 1, f"live_trades should have a row for {market_id}"

        # Live row must be in `shadow` status (dry-run).
        from src.database.connection import get_db

        async with get_db() as conn:
            status = await conn.fetchval(
                "SELECT status FROM live_trades WHERE market_id = $1 LIMIT 1",
                market_id,
            )
        assert status == "shadow", f"Expected live status='shadow', got {status!r}"

        paper.stop_event_set = True
        await paper.stop()
        await live.stop()
        for t in (paper_task, live_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await _cleanup_market(market_id)


# ---------------------------------------------------------------------------
# Scenario 2 — Live filter blocks low-confidence in dual mode
# ---------------------------------------------------------------------------


@skip_unless_local
@pytest.mark.asyncio
async def test_dual_mode_live_filter_blocks_low_confidence(redis_client, db_pool) -> None:
    """In dual mode with confidence < LIVE_FILTER_CONFIDENCE_MIN, paper
    must get a row but live must NOT."""
    from src.engine.confidence_engine import Decision
    from src.engine.decision_router import DecisionRouter
    from src.engine.live_trader import LiveTrader
    from src.engine.paper_trader import PaperTrader

    market_id = _unique_market_id()
    try:
        await redis_client.set("trading:mode_override", "dual")

        paper = PaperTrader(redis_client=redis_client)
        live = LiveTrader(redis_client=redis_client)
        paper._running = True
        live._running = True
        paper_task = asyncio.create_task(paper._subscribe_loop())
        live_task = asyncio.create_task(live._subscribe_loop())
        await asyncio.sleep(0.2)

        router = DecisionRouter(redis_client=redis_client)
        decision = Decision(
            action="follow",
            leader_wallet="0xLeaderTest",
            market_id=market_id,
            token_id="tok-yes-itest",
            size_usdc=100.0,
            kelly_fraction=0.02,
            thompson_follow=0.5,
            thompson_fade=0.5,
            confidence=0.3,  # < 0.6 filter
            reason="integration_test",
            trade_context={
                "market_question": "integration test?",
                "live_candidate": True,
                "trade_age_s": 1.0,
            },
            signal_audit={"accepted": True, "reject_reason": None},
        )
        result = await router.route(decision)
        assert result.routed_to_paper is True
        assert result.routed_to_live is False  # filter blocked

        # Paper should land a row; live should stay empty.
        paper_n = 0
        for _ in range(60):
            paper_n = await _count_rows("paper_trades", market_id)
            if paper_n >= 1:
                break
            await asyncio.sleep(0.1)
        # Give live a beat to definitely not insert.
        await asyncio.sleep(0.5)
        live_n = await _count_rows("live_trades", market_id)

        assert paper_n >= 1, "Paper should have inserted a row"
        assert live_n == 0, (
            f"Live should NOT have inserted a row (filter), found {live_n}"
        )

        await paper.stop()
        await live.stop()
        for t in (paper_task, live_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await _cleanup_market(market_id)


# ---------------------------------------------------------------------------
# Scenario 3 — Runtime override flips mode without restart
# ---------------------------------------------------------------------------


@skip_unless_local
@pytest.mark.asyncio
async def test_runtime_override_flips_mode_hot(redis_client, db_pool) -> None:
    """SET-ing the Redis override key changes routing on the very next
    decision — no router restart, no env change."""
    from src.engine.confidence_engine import Decision
    from src.engine.decision_router import DecisionRouter

    market_paper = _unique_market_id()
    market_live = _unique_market_id()
    try:
        from src.engine.live_trader import LiveTrader
        from src.engine.paper_trader import PaperTrader

        paper = PaperTrader(redis_client=redis_client)
        live = LiveTrader(redis_client=redis_client)
        paper._running = True
        live._running = True
        paper_task = asyncio.create_task(paper._subscribe_loop())
        live_task = asyncio.create_task(live._subscribe_loop())
        await asyncio.sleep(0.2)

        router = DecisionRouter(redis_client=redis_client)

        # Phase A — no override, env default = paper. Only paper_trades fires.
        decision_a = Decision(
            action="follow",
            leader_wallet="0xLeaderTest",
            market_id=market_paper,
            token_id="tok-yes-itest",
            size_usdc=100.0,
            kelly_fraction=0.02,
            thompson_follow=0.7,
            thompson_fade=0.3,
            confidence=0.8,
            reason="integration_test",
            trade_context={
                "market_question": "integration test?",
                "live_candidate": True,
                "trade_age_s": 1.0,
            },
            signal_audit={"accepted": True, "reject_reason": None},
        )
        await router.route(decision_a)
        for _ in range(60):
            if await _count_rows("paper_trades", market_paper) >= 1:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        assert await _count_rows("paper_trades", market_paper) >= 1
        assert await _count_rows("live_trades", market_paper) == 0

        # Phase B — flip to "live". Only live_trades should fire.
        await redis_client.set("trading:mode_override", "live")
        decision_b = Decision(
            action="follow",
            leader_wallet="0xLeaderTest",
            market_id=market_live,
            token_id="tok-yes-itest",
            size_usdc=100.0,
            kelly_fraction=0.02,
            thompson_follow=0.7,
            thompson_fade=0.3,
            confidence=0.8,
            reason="integration_test",
            trade_context={
                "market_question": "integration test?",
                "live_candidate": True,
                "trade_age_s": 1.0,
            },
            signal_audit={"accepted": True, "reject_reason": None},
        )
        await router.route(decision_b)
        for _ in range(60):
            if await _count_rows("live_trades", market_live) >= 1:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        assert await _count_rows("paper_trades", market_live) == 0, (
            "Paper should NOT have received the live-only decision"
        )
        assert await _count_rows("live_trades", market_live) >= 1, (
            "Live should have received the decision after override flip"
        )

        await paper.stop()
        await live.stop()
        for t in (paper_task, live_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await _cleanup_market(market_paper)
        await _cleanup_market(market_live)


# ---------------------------------------------------------------------------
# Scenario 4 — Paper crash isolation: live still gets its message
# ---------------------------------------------------------------------------


@skip_unless_local
@pytest.mark.asyncio
async def test_paper_crash_does_not_block_live(redis_client, db_pool) -> None:
    """If the paper subscriber crashes mid-handler, the live subscriber
    must still receive and process the same decision in dual mode.
    For this scenario we use a CollectingSubscriber on paper (so we
    can deliberately raise) and a real LiveTrader to verify the
    live_trades row is written."""
    from src.engine.confidence_engine import Decision
    from src.engine.decision_router import (
        REDIS_DECISIONS_PAPER_CHANNEL,
        DecisionRouter,
    )
    from src.engine.live_trader import LiveTrader

    market_id = _unique_market_id()
    try:
        await redis_client.set("trading:mode_override", "dual")

        # Crashing paper stub.
        paper_stub = CollectingSubscriber(
            redis_client, REDIS_DECISIONS_PAPER_CHANNEL, raise_on_recv=True
        )
        await paper_stub.start()

        # Real LiveTrader.
        live = LiveTrader(redis_client=redis_client)
        live._running = True
        live_task = asyncio.create_task(live._subscribe_loop())
        await asyncio.sleep(0.2)

        router = DecisionRouter(redis_client=redis_client)
        decision = Decision(
            action="follow",
            leader_wallet="0xLeaderTest",
            market_id=market_id,
            token_id="tok-yes-itest",
            size_usdc=100.0,
            kelly_fraction=0.02,
            thompson_follow=0.7,
            thompson_fade=0.3,
            confidence=0.8,
            reason="integration_test",
            trade_context={
                "market_question": "integration test?",
                "live_candidate": True,
                "trade_age_s": 1.0,
            },
            signal_audit={"accepted": True, "reject_reason": None},
        )
        result = await router.route(decision)
        assert result.routed_to_paper is True
        assert result.routed_to_live is True

        # Wait for live row.
        for _ in range(60):
            if await _count_rows("live_trades", market_id) >= 1:
                break
            await asyncio.sleep(0.1)
        live_n = await _count_rows("live_trades", market_id)
        assert live_n >= 1, (
            "Live trader must still insert its row even if paper subscriber crashed"
        )
        # Paper stub raised — collect-list should be empty, error count = 1.
        assert paper_stub.errors == 1, (
            f"Paper stub should have raised once, got {paper_stub.errors} errors"
        )
        assert paper_stub.received == []

        await paper_stub.stop()
        await live.stop()
        live_task.cancel()
        try:
            await live_task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await _cleanup_market(market_id)
