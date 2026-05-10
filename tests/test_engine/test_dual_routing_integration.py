"""
Fast integration tests for the DecisionRouter ↔ Redis pubsub layer (S2.8).

These tests validate end-to-end routing behavior — not just the router's
internal logic, but the actual round-trip through a (fake) Redis pubsub.
We use `fakeredis.aioredis.FakeRedis` so the suite stays in-process and
runs in CI without Docker.

What we DON'T test here:
  * PaperTrader / LiveTrader DB writes — covered by their own unit tests
    plus the e2e Docker suite (tests/integration/test_paper_live_dual.py).
  * Sizing, Kelly, signal_audit logic — covered upstream in
    test_confidence_engine.py.

What we DO test:
  1. Dual mode dispatch: a single decision lands on BOTH
     `decisions` and `decisions:live` channels.
  2. Live filter blocking: a low-confidence decision in dual mode
     reaches paper but NOT live.
  3. Runtime override: SET-ing the Redis override key flips routing
     mid-stream without restart.
  4. Crash isolation: a paper-side subscriber that raises does NOT
     prevent the live-side subscriber from receiving its message.

Pattern: instead of wiring real PaperTrader / LiveTrader (which would
drag in asyncpg + the whole DB layer), we install lightweight stub
subscriber tasks that mirror the real `_subscribe_loop` shape — they
listen on the production channel constants and collect decisions into a
list. This gives us a faithful pubsub round-trip without DB plumbing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import fakeredis.aioredis
import pytest

from src.engine.confidence_engine import Decision
from src.engine.decision_router import (
    REDIS_DECISIONS_LIVE_CHANNEL,
    REDIS_DECISIONS_PAPER_CHANNEL,
    DecisionRouter,
    TradingMode,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    """Real-protocol fake Redis with pubsub support."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
def _reset_routing_settings(monkeypatch):
    """Bring the routing settings back to a known baseline before each test —
    the env defaults from .env.example."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.TRADING_MODE_OVERRIDE_KEY",
        "trading:mode_override",
    )
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_CONFIDENCE_MIN", 0.6)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_SIZE_MIN_USDC", 10.0)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST", "")


def _decision(**overrides) -> Decision:
    base = dict(
        action="follow",
        leader_wallet="0xLeader",
        market_id="0xMarket123",
        token_id="tok-yes",
        size_usdc=100.0,
        kelly_fraction=0.02,
        thompson_follow=0.7,
        thompson_fade=0.3,
        confidence=0.8,
        reason="thompson_follow",
        trade_context={"market_question": "test?"},
    )
    base.update(overrides)
    return Decision(**base)


# --------------------------------------------------------------------------- #
# Stub subscriber — mimics the trader's _subscribe_loop shape                  #
# --------------------------------------------------------------------------- #


class StubSubscriber:
    """Mirrors PaperTrader/LiveTrader._subscribe_loop without the DB tail.
    Collects every decoded payload it receives. Optionally raises in the
    handler to test crash isolation."""

    def __init__(self, redis_client, channel: str, *, raise_on_recv: bool = False):
        self._redis = redis_client
        self._channel = channel
        self._raise_on_recv = raise_on_recv
        self.received: list[dict] = []
        self.handler_errors = 0
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        # Wait until we've actually subscribed before the test publishes.
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
                    self.handler_errors += 1
                    raise RuntimeError("stub paper trader exploded")
                self.received.append(payload)
        finally:
            await pubsub.unsubscribe(self._channel)
            await pubsub.aclose()


async def _wait_for(condition, timeout: float = 1.0, interval: float = 0.01) -> bool:
    """Poll until condition() is truthy or timeout. Returns True if hit."""
    elapsed = 0.0
    while elapsed < timeout:
        if condition():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


# --------------------------------------------------------------------------- #
# Scenario 1 — Dual mode: 1 decision → both channels                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dual_mode_routes_to_paper_and_live(redis_client) -> None:
    """In dual mode, a non-skip decision passing the live filter must
    land on BOTH the paper and live channels."""
    # Override mode → dual.
    await redis_client.set("trading:mode_override", "dual")

    paper_sub = StubSubscriber(redis_client, REDIS_DECISIONS_PAPER_CHANNEL)
    live_sub = StubSubscriber(redis_client, REDIS_DECISIONS_LIVE_CHANNEL)
    await paper_sub.start()
    await live_sub.start()

    try:
        router = DecisionRouter(redis_client=redis_client)
        result = await router.route(_decision())

        assert result.mode == TradingMode.DUAL
        assert result.routed_to_paper is True
        assert result.routed_to_live is True

        # Wait for both subscribers to actually receive the payload.
        got_both = await _wait_for(
            lambda: len(paper_sub.received) == 1 and len(live_sub.received) == 1
        )
        assert got_both, (
            f"Expected 1 paper + 1 live payload, "
            f"got paper={len(paper_sub.received)}, live={len(live_sub.received)}"
        )
        # Same decision delivered on both channels.
        assert paper_sub.received[0]["market_id"] == "0xMarket123"
        assert live_sub.received[0]["market_id"] == "0xMarket123"
        assert paper_sub.received[0]["action"] == "follow"
        assert live_sub.received[0]["action"] == "follow"
    finally:
        await paper_sub.stop()
        await live_sub.stop()


# --------------------------------------------------------------------------- #
# Scenario 2 — Live filter blocks low-confidence in dual mode                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dual_mode_live_filter_blocks_low_confidence(redis_client) -> None:
    """In dual mode, a sub-threshold confidence decision must reach paper
    but NOT live — paper is unfiltered, live is filtered."""
    await redis_client.set("trading:mode_override", "dual")

    paper_sub = StubSubscriber(redis_client, REDIS_DECISIONS_PAPER_CHANNEL)
    live_sub = StubSubscriber(redis_client, REDIS_DECISIONS_LIVE_CHANNEL)
    await paper_sub.start()
    await live_sub.start()

    try:
        router = DecisionRouter(redis_client=redis_client)
        # confidence 0.3 < LIVE_FILTER_CONFIDENCE_MIN (0.6)
        result = await router.route(_decision(confidence=0.3))

        assert result.mode == TradingMode.DUAL
        assert result.routed_to_paper is True
        assert result.routed_to_live is False  # filter blocked

        got_paper = await _wait_for(lambda: len(paper_sub.received) == 1)
        assert got_paper, "Paper should have received the decision"
        # Give live a beat to NOT receive it.
        await asyncio.sleep(0.1)
        assert len(live_sub.received) == 0, (
            f"Live filter should have blocked low-confidence decision, "
            f"but live got {live_sub.received}"
        )
    finally:
        await paper_sub.stop()
        await live_sub.stop()


# --------------------------------------------------------------------------- #
# Scenario 3 — Runtime override hot-swap                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_runtime_override_flips_mode_without_restart(redis_client) -> None:
    """Flipping the Redis override key changes routing on the very next
    decision — no router restart, no env change."""
    paper_sub = StubSubscriber(redis_client, REDIS_DECISIONS_PAPER_CHANNEL)
    live_sub = StubSubscriber(redis_client, REDIS_DECISIONS_LIVE_CHANNEL)
    await paper_sub.start()
    await live_sub.start()

    try:
        router = DecisionRouter(redis_client=redis_client)

        # Phase A — no override, env default = "paper". Only paper should fire.
        await router.route(_decision(market_id="0xMarketA"))
        await _wait_for(lambda: len(paper_sub.received) == 1)
        await asyncio.sleep(0.05)
        assert len(paper_sub.received) == 1
        assert len(live_sub.received) == 0
        assert paper_sub.received[0]["market_id"] == "0xMarketA"

        # Phase B — flip override to "live". Only live should fire.
        await redis_client.set("trading:mode_override", "live")
        await router.route(_decision(market_id="0xMarketB"))
        await _wait_for(lambda: len(live_sub.received) == 1)
        await asyncio.sleep(0.05)
        # Paper still has only the first message — Phase B did not echo to it.
        assert len(paper_sub.received) == 1, (
            "Paper should not have received the live-mode decision"
        )
        assert len(live_sub.received) == 1
        assert live_sub.received[0]["market_id"] == "0xMarketB"

        # Phase C — flip to "dual". Both should fire on the next decision.
        await redis_client.set("trading:mode_override", "dual")
        await router.route(_decision(market_id="0xMarketC"))
        await _wait_for(
            lambda: len(paper_sub.received) == 2 and len(live_sub.received) == 2
        )
        assert len(paper_sub.received) == 2
        assert len(live_sub.received) == 2
        assert paper_sub.received[1]["market_id"] == "0xMarketC"
        assert live_sub.received[1]["market_id"] == "0xMarketC"
    finally:
        await paper_sub.stop()
        await live_sub.stop()


# --------------------------------------------------------------------------- #
# Scenario 4 — Paper crash isolation                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_paper_subscriber_crash_does_not_block_live(redis_client) -> None:
    """If the paper-side subscriber raises while handling a decision in
    dual mode, the live-side subscriber must still receive its copy.
    This is the property the router cares about: one side's failure
    cannot starve the other."""
    await redis_client.set("trading:mode_override", "dual")

    # Paper subscriber will raise on the very first message it gets.
    paper_sub = StubSubscriber(
        redis_client, REDIS_DECISIONS_PAPER_CHANNEL, raise_on_recv=True
    )
    live_sub = StubSubscriber(redis_client, REDIS_DECISIONS_LIVE_CHANNEL)
    await paper_sub.start()
    await live_sub.start()

    try:
        router = DecisionRouter(redis_client=redis_client)
        result = await router.route(_decision(market_id="0xCrashTest"))

        # Both publishes succeeded from the router's perspective — the
        # paper subscriber crashed downstream.
        assert result.routed_to_paper is True
        assert result.routed_to_live is True

        got_live = await _wait_for(lambda: len(live_sub.received) == 1)
        assert got_live, (
            "Live subscriber must still receive its message even after "
            "paper subscriber crashed"
        )
        # Paper raised but did NOT collect the payload before it died.
        assert paper_sub.handler_errors == 1
        assert len(paper_sub.received) == 0

        # Sanity: live really got the right decision.
        assert live_sub.received[0]["market_id"] == "0xCrashTest"
    finally:
        await paper_sub.stop()
        await live_sub.stop()


# --------------------------------------------------------------------------- #
# Bonus — skip decisions never reach either channel                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skip_action_never_routes(redis_client) -> None:
    """A `skip` decision is recorded upstream for telemetry but never
    published — neither paper nor live should ever see it, regardless
    of mode."""
    await redis_client.set("trading:mode_override", "dual")

    paper_sub = StubSubscriber(redis_client, REDIS_DECISIONS_PAPER_CHANNEL)
    live_sub = StubSubscriber(redis_client, REDIS_DECISIONS_LIVE_CHANNEL)
    await paper_sub.start()
    await live_sub.start()

    try:
        router = DecisionRouter(redis_client=redis_client)
        result = await router.route(_decision(action="skip"))

        assert result.routed_to_paper is False
        assert result.routed_to_live is False
        assert result.skipped_reason == "action_is_skip"

        # Give both subscribers time to confirm nothing arrived.
        await asyncio.sleep(0.1)
        assert paper_sub.received == []
        assert live_sub.received == []
    finally:
        await paper_sub.stop()
        await live_sub.stop()
