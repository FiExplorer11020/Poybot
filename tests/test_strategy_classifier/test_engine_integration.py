"""Round 8 engine-integration tests.

These verify the runtime-gated strategy multiplier path in
:class:`src.engine.confidence_engine.ConfidenceEngine`:

* When ``strategy_conditional_confidence_enabled = False`` (default),
  behavior is unchanged.
* When True, weights are applied to Thompson samples.
* STRATEGY_WEIGHTS defaults match spec § 3.6.
* The defensive layers (no fingerprint → no-op, unknown class → no-op)
  hold.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine.confidence_engine import ConfidenceEngine
from src.strategy_classifier.model import STRATEGY_WEIGHTS


def make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return ConfidenceEngine(redis_client=redis)


def _mock_runtime_config(strategy_enabled: bool):
    """Return a patch object that swaps get_runtime_config."""
    cfg = MagicMock()
    cfg.effective = AsyncMock(return_value={
        "strategy_conditional_confidence_enabled": strategy_enabled,
    })

    def _get_runtime_config():
        return cfg

    return _get_runtime_config


def _mock_get_db_with_classification(classification_json):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "classification_json": classification_json,
    })

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx), conn


class TestRuntimeFlagGating:
    @pytest.mark.asyncio
    async def test_flag_disabled_returns_none(self):
        """Default (flag off) → _maybe_get_strategy_weights returns None."""
        engine = make_engine()
        with patch(
            "src.control.runtime_config.get_runtime_config",
            new=_mock_runtime_config(strategy_enabled=False),
        ):
            result = await engine._maybe_get_strategy_weights("0xabc")
        assert result is None

    @pytest.mark.asyncio
    async def test_flag_enabled_no_fingerprint_returns_none(self):
        """Flag ON but classification_json missing strategy_fingerprint
        → still no-op (safe fallback)."""
        engine = make_engine()
        # Empty classification_json
        patch_db, _ = _mock_get_db_with_classification({})
        with patch(
            "src.control.runtime_config.get_runtime_config",
            new=_mock_runtime_config(strategy_enabled=True),
        ), patch_db:
            result = await engine._maybe_get_strategy_weights("0xabc")
        assert result is None

    @pytest.mark.asyncio
    async def test_flag_enabled_with_fingerprint_returns_weights(self):
        """Flag ON + fingerprint with primary='directional' → returns
        STRATEGY_WEIGHTS['directional']."""
        engine = make_engine()
        cls_json = {
            "strategy_fingerprint": {
                "primary_strategy": "directional",
                "confidence": 0.85,
                "strategy_probs": {"directional": 0.85},
                "model_version": "sc.v1.0",
                "classified_at": "2026-05-12T00:00:00+00:00",
                "drift_detected": False,
            }
        }
        patch_db, _ = _mock_get_db_with_classification(cls_json)
        with patch(
            "src.control.runtime_config.get_runtime_config",
            new=_mock_runtime_config(strategy_enabled=True),
        ), patch_db:
            result = await engine._maybe_get_strategy_weights("0xabc")
        assert result is not None
        assert result["primary_strategy"] == "directional"
        assert result["follow"] == STRATEGY_WEIGHTS["directional"]["follow"]
        assert result["fade"] == STRATEGY_WEIGHTS["directional"]["fade"]
        assert result["skip"] == STRATEGY_WEIGHTS["directional"]["skip"]

    @pytest.mark.asyncio
    async def test_flag_enabled_with_unknown_primary_returns_none(self):
        """A forward-compat unknown strategy class doesn't crash — returns None."""
        engine = make_engine()
        cls_json = {
            "strategy_fingerprint": {
                "primary_strategy": "totally_new_class",
                "confidence": 0.5,
            }
        }
        patch_db, _ = _mock_get_db_with_classification(cls_json)
        with patch(
            "src.control.runtime_config.get_runtime_config",
            new=_mock_runtime_config(strategy_enabled=True),
        ), patch_db:
            result = await engine._maybe_get_strategy_weights("0xabc")
        assert result is None

    @pytest.mark.asyncio
    async def test_classification_json_as_string_decoded(self):
        """asyncpg sometimes returns JSONB as a str. We must json.loads it."""
        engine = make_engine()
        cls_json_str = json.dumps({
            "strategy_fingerprint": {
                "primary_strategy": "info_leak",
                "confidence": 0.9,
            }
        })
        patch_db, _ = _mock_get_db_with_classification(cls_json_str)
        with patch(
            "src.control.runtime_config.get_runtime_config",
            new=_mock_runtime_config(strategy_enabled=True),
        ), patch_db:
            result = await engine._maybe_get_strategy_weights("0xabc")
        assert result is not None
        assert result["primary_strategy"] == "info_leak"
        # info_leak FADE = 2.0 per spec § 3.6.
        assert result["fade"] == pytest.approx(2.0)


class TestStrategyWeightsSpecAlignment:
    """The defaults baked into STRATEGY_WEIGHTS must match the spec § 3.6
    commentary block. This test serves as the regression gate.
    """

    def test_directional_row(self):
        assert STRATEGY_WEIGHTS["directional"] == {
            "follow": 1.5, "fade": 0.5, "skip": 1.0,
        }

    def test_momentum_row(self):
        assert STRATEGY_WEIGHTS["momentum"] == {
            "follow": 1.0, "fade": 1.0, "skip": 1.2,
        }

    def test_contrarian_row(self):
        assert STRATEGY_WEIGHTS["contrarian"] == {
            "follow": 1.2, "fade": 0.8, "skip": 1.0,
        }

    def test_info_leak_row(self):
        # Spec § 3.6: info_leak → FADE 2.0
        assert STRATEGY_WEIGHTS["info_leak"]["follow"] == 0.5
        assert STRATEGY_WEIGHTS["info_leak"]["fade"] == 2.0

    def test_structural_bot_row(self):
        # Spec § 3.6: structural_bot is excluded — FOLLOW=FADE=0, SKIP=∞-ish
        assert STRATEGY_WEIGHTS["structural_bot"]["follow"] == 0.0
        assert STRATEGY_WEIGHTS["structural_bot"]["fade"] == 0.0
        assert STRATEGY_WEIGHTS["structural_bot"]["skip"] >= 10.0


class TestRuntimeConfigBooleanCoercion:
    """The bool gate must survive every common input format."""

    @pytest.mark.asyncio
    async def test_set_overrides_accepts_true_string(self):
        from src.control.runtime_config import RuntimeConfig

        # Use a mock Redis so we don't need a live server.
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.publish = AsyncMock()
        cfg = RuntimeConfig(redis_client=redis)
        result = await cfg.set_overrides({
            "strategy_conditional_confidence_enabled": "true",
        })
        assert result["strategy_conditional_confidence_enabled"] is True

    @pytest.mark.asyncio
    async def test_set_overrides_accepts_bool(self):
        from src.control.runtime_config import RuntimeConfig

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.publish = AsyncMock()
        cfg = RuntimeConfig(redis_client=redis)
        result = await cfg.set_overrides({
            "strategy_conditional_confidence_enabled": False,
        })
        assert result["strategy_conditional_confidence_enabled"] is False

    @pytest.mark.asyncio
    async def test_default_is_false(self):
        from src.control.runtime_config import RuntimeConfig

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        cfg = RuntimeConfig(redis_client=redis)
        effective = await cfg.effective()
        assert effective["strategy_conditional_confidence_enabled"] is False
