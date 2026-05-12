"""
Tests for the Round 9 (The Web) volume_anticipation branch of
``src.engine.decision_router.DecisionRouter``.

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.4.

Coverage:
  1. Flag OFF → no volume_anticipation entry (byte-identical to pre-R9).
  2. Flag ON + threshold cleared → entry fires.
  3. Drift gate suppresses entries.
  4. Kelly sizing capped by MAX_POSITION_PCT.
  5. Threshold not cleared → no entry.
  6. Predictor missing → no entry (defensive).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine.confidence_engine import Decision
from src.engine.decision_router import (
    REDIS_DECISIONS_PAPER_CHANNEL,
    VOLUME_ANTICIPATION_ACTION,
    DecisionRouter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_redis() -> MagicMock:
    r = MagicMock()
    r.get = AsyncMock(return_value=None)
    r.publish = AsyncMock(return_value=1)
    return r


def _make_runtime_config(enabled: bool, threshold: float = 5000.0) -> MagicMock:
    cfg = MagicMock()

    async def _get(key: str):
        if key == "volume_anticipation_enabled":
            return bool(enabled)
        if key == "volume_anticipation_threshold_usdc":
            return float(threshold)
        return None

    cfg.get = AsyncMock(side_effect=_get)
    return cfg


def _make_predictor(total_volume_usdc: float, confidence: float = 0.8) -> MagicMock:
    p = MagicMock()
    p.forecast = AsyncMock(
        return_value={
            "total_volume_usdc": total_volume_usdc,
            "ci_low": total_volume_usdc * 0.5,
            "ci_high": total_volume_usdc * 1.5,
            "by_pool": {"directional": total_volume_usdc * 0.6,
                        "momentum": total_volume_usdc * 0.4},
            "time_distribution": {"0-5min": 1.0},
            "confidence": confidence,
        }
    )
    return p


def _make_drift_detector(drift: bool) -> MagicMock:
    d = MagicMock()
    rep = MagicMock()
    rep.drift_detected = bool(drift)
    d.evaluate = AsyncMock(return_value=rep)
    return d


def _signal_decision() -> Decision:
    return Decision(
        action="follow",
        leader_wallet="0xLEADER",
        market_id="0xMARKET",
        token_id="tok-1",
        size_usdc=100.0,
        kelly_fraction=0.02,
        thompson_follow=0.7,
        thompson_fade=0.3,
        confidence=0.75,
        reason="thompson_follow",
    )


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Quiet test-rig knobs so the router runs in a deterministic mode."""
    monkeypatch.setattr("src.engine.decision_router.settings.TRADING_MODE", "paper")
    monkeypatch.setattr("src.engine.decision_router.settings.MAX_POSITION_PCT", 0.02)
    monkeypatch.setattr("src.engine.decision_router.settings.MIN_POSITION_USDC", 50.0)


# ---------------------------------------------------------------------------
# 1. Flag OFF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_returns_none_and_publishes_nothing():
    """When volume_anticipation_enabled=False the method short-circuits
    BEFORE calling the predictor — true byte-identical pre-R9 behavior.
    """
    redis = _make_redis()
    predictor = _make_predictor(total_volume_usdc=100_000.0)
    cfg = _make_runtime_config(enabled=False)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=predictor,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
    )
    assert out is None
    predictor.forecast.assert_not_awaited()
    redis.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Flag ON + threshold cleared → entry fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_and_threshold_cleared_fires_entry():
    """Flag ON + forecast above threshold → maybe_emit publishes a
    volume_anticipation decision."""
    redis = _make_redis()
    predictor = _make_predictor(total_volume_usdc=20_000.0, confidence=0.9)
    cfg = _make_runtime_config(enabled=True, threshold=5_000.0)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=predictor,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
        market_depth_usdc=50_000.0,
    )
    assert out is not None
    assert out.routed_to_paper is True
    redis.publish.assert_awaited()
    # The published channel should be the paper channel.
    args, _ = redis.publish.await_args
    assert args[0] == REDIS_DECISIONS_PAPER_CHANNEL
    # The payload action is the volume_anticipation marker.
    import json

    payload = json.loads(args[1])
    assert payload["action"] == VOLUME_ANTICIPATION_ACTION


# ---------------------------------------------------------------------------
# 3. Drift gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_detected_suppresses_entry():
    """Drift detector flagging the leader → no entry, no publish."""
    redis = _make_redis()
    predictor = _make_predictor(total_volume_usdc=20_000.0)
    cfg = _make_runtime_config(enabled=True)
    drift = _make_drift_detector(drift=True)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=predictor,
        drift_detector=drift,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
    )
    assert out is None
    drift.evaluate.assert_awaited()
    redis.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Kelly sizing cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kelly_sizing_capped_by_max_position_pct(monkeypatch):
    """Even on an unbounded forecast (huge volume + tiny depth + high
    confidence), the emitted size cannot exceed MAX_POSITION_PCT × cap."""
    monkeypatch.setattr(
        "src.engine.decision_router.settings.MAX_POSITION_PCT", 0.02
    )
    redis = _make_redis()
    predictor = _make_predictor(
        total_volume_usdc=10_000_000.0, confidence=1.0
    )
    cfg = _make_runtime_config(enabled=True, threshold=5_000.0)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=predictor,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
        market_depth_usdc=1.0,  # tiny depth makes raw Kelly enormous
    )
    assert out is not None
    # Inspect the published payload — size_usdc must be <= 2% of 10_000.
    import json

    args, _ = redis.publish.await_args
    payload = json.loads(args[1])
    assert payload["size_usdc"] <= 0.02 * 10_000.0 + 1e-6


# ---------------------------------------------------------------------------
# 5. Threshold not cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_not_cleared_returns_none():
    """Forecast below threshold → no entry."""
    redis = _make_redis()
    predictor = _make_predictor(total_volume_usdc=100.0)  # below 5k threshold
    cfg = _make_runtime_config(enabled=True, threshold=5_000.0)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=predictor,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
    )
    assert out is None
    redis.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. Predictor missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_predictor_returns_none_even_with_flag_on():
    """Defensive path: router constructed without a predictor → no R9
    surface area, no crash."""
    redis = _make_redis()
    cfg = _make_runtime_config(enabled=True)
    router = DecisionRouter(
        redis_client=redis,
        volume_predictor=None,
        runtime_config=cfg,
    )
    out = await router.maybe_emit_volume_anticipation(
        signal_decision=_signal_decision(),
        current_capital=10_000.0,
    )
    assert out is None
    redis.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Existing route() behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_unchanged_when_no_r9_deps():
    """A DecisionRouter constructed without R9 deps must behave
    EXACTLY like the pre-R9 router on a normal route() call."""
    redis = _make_redis()
    router = DecisionRouter(redis_client=redis)  # no R9 args
    result = await router.route(_signal_decision())
    # Routed to paper (default TRADING_MODE).
    assert result.routed_to_paper is True
    redis.publish.assert_awaited()
