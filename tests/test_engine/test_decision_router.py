"""
Tests for src/engine/decision_router.py.

The DecisionRouter takes a Decision and publishes it onto the
appropriate Redis channel(s) based on TRADING_MODE (env) + a Redis
override. The live channel is additionally gated by a confidence /
size / market-allowlist filter.

We mock the Redis client and inspect which channels were published to.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine.confidence_engine import Decision
from src.engine.decision_router import (
    REDIS_DECISIONS_LIVE_CHANNEL,
    REDIS_DECISIONS_PAPER_CHANNEL,
    DecisionRouter,
    RoutingResult,
    TradingMode,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _make_redis(*, override: str | None = None, get_raises: bool = False,
                publish_raises_on: tuple[str, ...] = ()) -> MagicMock:
    """A fake redis client. `override` is what `get(...)` returns;
    `get_raises` makes `get` blow up to test the env fallback;
    `publish_raises_on` is a tuple of channels that should fail."""
    r = MagicMock()
    if get_raises:
        r.get = AsyncMock(side_effect=RuntimeError("redis_down"))
    else:
        r.get = AsyncMock(return_value=override)

    async def _publish(channel, payload):
        if channel in publish_raises_on:
            raise RuntimeError(f"publish failed on {channel}")
        return 1

    r.publish = AsyncMock(side_effect=_publish)
    return r


def _decision(**overrides) -> Decision:
    base = dict(
        action="follow",
        leader_wallet="0xLeader",
        market_id="0xMarket",
        token_id="tok-1",
        size_usdc=100.0,
        kelly_fraction=0.02,
        thompson_follow=0.7,
        thompson_fade=0.3,
        confidence=0.8,
        reason="thompson_follow",
    )
    base.update(overrides)
    return Decision(**base)


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Each test starts from a clean slate of routing-related settings."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.TRADING_MODE_OVERRIDE_KEY",
        "trading:mode_override",
    )
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_CONFIDENCE_MIN", 0.6)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_FILTER_SIZE_MIN_USDC", 10.0)
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST", "")


# --------------------------------------------------------------------------- #
# TradingMode.parse                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [
    ("paper", TradingMode.PAPER),
    (" LIVE ", TradingMode.LIVE),
    ("Dual", TradingMode.DUAL),
])
def test_trading_mode_parse_accepts_valid(raw, expected):
    assert TradingMode.parse(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "garbage", "shadow"])
def test_trading_mode_parse_rejects_invalid(raw):
    assert TradingMode.parse(raw) is None


# --------------------------------------------------------------------------- #
# Mode resolution                                                              #
# --------------------------------------------------------------------------- #


async def test_mode_uses_redis_override_when_set(monkeypatch):
    """Redis override takes precedence over env."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis(override="dual")
    router = DecisionRouter(redis)
    mode = await router._active_mode()
    assert mode == TradingMode.DUAL


async def test_mode_falls_back_to_env_when_redis_returns_none(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    redis = _make_redis(override=None)
    router = DecisionRouter(redis)
    assert await router._active_mode() == TradingMode.LIVE


async def test_mode_falls_back_to_env_when_redis_get_fails(monkeypatch):
    """If Redis is down, we still want a deterministic mode."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis(get_raises=True)
    router = DecisionRouter(redis)
    assert await router._active_mode() == TradingMode.PAPER


async def test_mode_invalid_redis_value_falls_back_to_env(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    redis = _make_redis(override="banana")
    router = DecisionRouter(redis)
    assert await router._active_mode() == TradingMode.LIVE


async def test_mode_invalid_env_falls_back_to_paper(monkeypatch):
    """Defensive: if TRADING_MODE is misconfigured, go to paper, never live."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "garbage")
    redis = _make_redis(override=None)
    router = DecisionRouter(redis)
    assert await router._active_mode() == TradingMode.PAPER


async def test_mode_decodes_bytes_from_redis():
    """Some Redis clients return bytes; we should decode them."""
    redis = _make_redis(override=b"live")
    router = DecisionRouter(redis)
    assert await router._active_mode() == TradingMode.LIVE


# --------------------------------------------------------------------------- #
# Routing — paper mode                                                         #
# --------------------------------------------------------------------------- #


async def test_paper_mode_publishes_to_paper_only(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision())

    assert isinstance(res, RoutingResult)
    assert res.routed_to_paper is True
    assert res.routed_to_live is False
    assert res.mode == TradingMode.PAPER

    channels = [call.args[0] for call in redis.publish.await_args_list]
    assert channels == [REDIS_DECISIONS_PAPER_CHANNEL]


# --------------------------------------------------------------------------- #
# Routing — live mode                                                          #
# --------------------------------------------------------------------------- #


async def test_live_mode_publishes_to_live_only_when_filter_passes(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(confidence=0.9, size_usdc=100.0))
    assert res.routed_to_paper is False
    assert res.routed_to_live is True
    channels = [c.args[0] for c in redis.publish.await_args_list]
    assert channels == [REDIS_DECISIONS_LIVE_CHANNEL]


async def test_live_mode_blocks_low_confidence(monkeypatch):
    """confidence below LIVE_FILTER_CONFIDENCE_MIN -> dropped from live."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.LIVE_FILTER_CONFIDENCE_MIN", 0.7
    )
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(confidence=0.5))
    assert res.routed_to_live is False
    redis.publish.assert_not_called()


async def test_live_mode_blocks_small_size(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.LIVE_FILTER_SIZE_MIN_USDC", 50.0
    )
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(size_usdc=20.0))
    assert res.routed_to_live is False
    redis.publish.assert_not_called()


async def test_live_mode_allowlist_blocks_unknown_market(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST",
        "0xAllowed1, 0xAllowed2",
    )
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(market_id="0xOtherMarket"))
    assert res.routed_to_live is False
    redis.publish.assert_not_called()


async def test_live_mode_allowlist_passes_listed_market(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST",
        "0xAllowed1, 0xAllowed2",
    )
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(market_id="0xAllowed1"))
    assert res.routed_to_live is True


async def test_live_mode_empty_allowlist_means_no_filter(monkeypatch):
    """Default LIVE_MARKET_ALLOWLIST is empty -> all markets allowed."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "live")
    monkeypatch.setattr("src.engine.decision_router.settings.LIVE_MARKET_ALLOWLIST", "")
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(market_id="0xRandom"))
    assert res.routed_to_live is True


# --------------------------------------------------------------------------- #
# Routing — dual mode                                                          #
# --------------------------------------------------------------------------- #


async def test_dual_mode_publishes_to_both_when_filter_passes(monkeypatch):
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "dual")
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(confidence=0.8, size_usdc=100))
    assert res.routed_to_paper is True
    assert res.routed_to_live is True
    channels = sorted(c.args[0] for c in redis.publish.await_args_list)
    assert channels == sorted([
        REDIS_DECISIONS_PAPER_CHANNEL, REDIS_DECISIONS_LIVE_CHANNEL,
    ])


async def test_dual_mode_low_confidence_paper_only(monkeypatch):
    """In dual mode, paper still gets the decision even if live is filtered."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "dual")
    monkeypatch.setattr(
        "src.engine.decision_router.settings.LIVE_FILTER_CONFIDENCE_MIN", 0.9
    )
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(confidence=0.7))
    assert res.routed_to_paper is True
    assert res.routed_to_live is False
    channels = [c.args[0] for c in redis.publish.await_args_list]
    assert channels == [REDIS_DECISIONS_PAPER_CHANNEL]


# --------------------------------------------------------------------------- #
# Skip / failure paths                                                         #
# --------------------------------------------------------------------------- #


async def test_skip_action_routes_nowhere():
    redis = _make_redis()
    router = DecisionRouter(redis)
    res = await router.route(_decision(action="skip"))
    assert res.routed_to_paper is False
    assert res.routed_to_live is False
    assert res.skipped_reason == "action_is_skip"
    redis.publish.assert_not_called()


async def test_publish_failure_does_not_raise(monkeypatch):
    """A Redis publish error must not propagate up — would kill the
    upstream ConfidenceEngine subscribe loop."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis(publish_raises_on=(REDIS_DECISIONS_PAPER_CHANNEL,))
    router = DecisionRouter(redis)
    res = await router.route(_decision())
    # No exception bubbled up; routing reports a non-success.
    assert res.routed_to_paper is False
    assert res.skipped_reason == "no_channel_matched"


async def test_dual_mode_one_channel_fails_other_succeeds(monkeypatch):
    """Live publish fails -> paper still goes through."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "dual")
    redis = _make_redis(publish_raises_on=(REDIS_DECISIONS_LIVE_CHANNEL,))
    router = DecisionRouter(redis)
    res = await router.route(_decision())
    assert res.routed_to_paper is True
    assert res.routed_to_live is False


# --------------------------------------------------------------------------- #
# Payload integrity                                                            #
# --------------------------------------------------------------------------- #


async def test_payload_matches_legacy_emit_format(monkeypatch):
    """The router must publish the SAME JSON shape as the legacy
    ConfidenceEngine._emit() — otherwise PaperTrader / LiveTrader would
    break silently."""
    import json

    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis()
    router = DecisionRouter(redis)

    decision = _decision(
        trade_context={"market_question": "Q?", "wallet_type": "whale"},
        signal_audit={"accepted": True},
        context_penalty=0.05,
    )
    await router.route(decision)
    args = redis.publish.await_args.args
    assert args[0] == REDIS_DECISIONS_PAPER_CHANNEL
    body = json.loads(args[1])
    assert body["action"] == "follow"
    assert body["leader_wallet"] == "0xLeader"
    assert body["market_id"] == "0xMarket"
    assert body["market_question"] == "Q?"
    assert body["wallet_type"] == "whale"
    assert body["confidence"] == pytest.approx(0.8)
    assert body["context_penalty"] == pytest.approx(0.05)
    assert body["signal_audit"] == {"accepted": True}
    # trade_context preserved as-is.
    assert body["trade_context"]["market_question"] == "Q?"


async def test_payload_handles_missing_trade_context(monkeypatch):
    """Decision.trade_context is None -> payload still has empty dict
    fields, never raises on `.get`."""
    import json

    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    redis = _make_redis()
    router = DecisionRouter(redis)
    await router.route(_decision(trade_context=None))
    body = json.loads(redis.publish.await_args.args[1])
    assert body["trade_context"] == {}
    assert body["market_question"] is None
    assert body["wallet_type"] is None
