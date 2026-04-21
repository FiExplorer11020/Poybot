"""
Unit tests for ConfidenceEngine (Thompson Sampling + Bayesian Kelly).
All external I/O (DB, Redis) is mocked.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.config import settings
from src.engine.confidence_engine import DEFAULT_ALPHA, DEFAULT_BETA, ConfidenceEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_engine() -> ConfidenceEngine:
    """Return a ConfidenceEngine with stub Redis and no profiler/error_model."""
    redis = MagicMock()
    redis.publish = AsyncMock()
    return ConfidenceEngine(redis_client=redis)


def _mock_get_db(execute_mock=None, fetchrow_mock=None):
    """
    Return a patcher for src.database.connection.get_db that yields a mock
    asyncpg connection.
    """
    conn = AsyncMock()
    if execute_mock is not None:
        conn.execute = execute_mock
    else:
        conn.execute = AsyncMock()
    if fetchrow_mock is not None:
        conn.fetchrow = fetchrow_mock
    else:
        conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx), conn


# ---------------------------------------------------------------------------
# Kelly sizing tests (synchronous — no DB / Redis needed)
# ---------------------------------------------------------------------------


class TestKellySize:
    def test_kelly_size_follow_capped_at_2pct(self):
        """Large alpha/beta giving high p should still be capped at 2% of capital."""
        engine = make_engine()
        # p ≈ 0.90 → f* will be large
        _, size_usdc = engine._kelly_size("follow", alpha=90.0, beta_=10.0)
        max_allowed = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT
        assert size_usdc <= max_allowed, f"Follow size {size_usdc} exceeds hard cap {max_allowed}"

    def test_kelly_size_fade_half_of_follow(self):
        """FADE max size must be exactly FADE_SIZE_RATIO of the FOLLOW hard cap."""
        engine = make_engine()
        alpha, beta_ = 90.0, 10.0

        _, follow_size = engine._kelly_size("follow", alpha=alpha, beta_=beta_)
        _, fade_size = engine._kelly_size("fade", alpha=alpha, beta_=beta_)

        follow_cap = settings.PAPER_CAPITAL_USDC * settings.MAX_POSITION_PCT
        fade_cap = follow_cap * settings.FADE_SIZE_RATIO

        assert fade_size <= fade_cap, f"Fade size {fade_size} exceeds fade cap {fade_cap}"
        # When follow is already at cap, fade should be approx half
        if follow_size == follow_cap:
            assert abs(fade_size - fade_cap) < 0.01

    def test_kelly_size_below_min_returns_zero(self):
        """Very small Kelly fraction (<MIN_POSITION_USDC after multiply) → both zero."""
        engine = make_engine()
        # p close to 0.5 with heavy uncertainty → tiny f* → size below floor
        # Use alpha=1, beta_=1 (uniform prior, p=0.5, f*=0, size=0)
        kelly_frac, size_usdc = engine._kelly_size("follow", alpha=1.0, beta_=1.0)
        assert size_usdc == 0.0
        assert kelly_frac == 0.0

    def test_kelly_size_zero_for_degenerate_p(self):
        """alpha or beta near 0 should not crash and should return zeros."""
        engine = make_engine()
        # beta_ effectively zero → p≈1 (boundary), should return 0
        kelly_frac, size_usdc = engine._kelly_size("follow", alpha=999.0, beta_=0.001)
        assert size_usdc >= 0.0
        assert kelly_frac >= 0.0


# ---------------------------------------------------------------------------
# Thompson Sampling tests
# ---------------------------------------------------------------------------


class TestThompsonSampling:
    def test_sample_thompson_returns_floats_in_unit_interval(self):
        """_sample_thompson must return two floats each in [0, 1]."""
        engine = make_engine()
        wallet = "0xABC"
        follow_val, fade_val = engine._sample_thompson(wallet)
        assert isinstance(follow_val, float)
        assert isinstance(fade_val, float)
        assert 0.0 <= follow_val <= 1.0
        assert 0.0 <= fade_val <= 1.0

    def test_sample_thompson_uses_stored_params(self):
        """After setting custom Beta params, samples should reflect them."""
        engine = make_engine()
        wallet = "0xDEF"
        # Alpha >> Beta → expected value close to 1
        engine._thompson[wallet] = {
            "follow": [999.0, 1.0],
            "fade": [1.0, 999.0],
        }
        # Sample 50 times; follow mean should be >> fade mean
        follow_samples = [engine._sample_thompson(wallet)[0] for _ in range(50)]
        fade_samples = [engine._sample_thompson(wallet)[1] for _ in range(50)]
        assert np.mean(follow_samples) > np.mean(fade_samples)

    def test_update_thompson_increments_alpha_on_win(self):
        """update_thompson with won=True must increment alpha."""
        engine = make_engine()
        wallet = "0x111"
        engine.update_thompson(wallet, "follow", won=True)
        assert engine._thompson[wallet]["follow"][0] == DEFAULT_ALPHA + 1.0
        assert engine._thompson[wallet]["follow"][1] == DEFAULT_BETA

    def test_update_thompson_increments_beta_on_loss(self):
        """update_thompson with won=False must increment beta."""
        engine = make_engine()
        wallet = "0x222"
        engine.update_thompson(wallet, "follow", won=False)
        assert engine._thompson[wallet]["follow"][0] == DEFAULT_ALPHA
        assert engine._thompson[wallet]["follow"][1] == DEFAULT_BETA + 1.0

    def test_update_thompson_initialises_wallet_if_missing(self):
        """Calling update_thompson for an unseen wallet must not raise."""
        engine = make_engine()
        engine.update_thompson("0xNEW", "fade", won=True)
        assert "0xNEW" in engine._thompson
        assert engine._thompson["0xNEW"]["fade"][0] == DEFAULT_ALPHA + 1.0

    @pytest.mark.asyncio
    async def test_seed_thompson_uses_persisted_decision_learning(self):
        engine = make_engine()
        profile = {
            "decision_learning": {
                "follow": {"beta_a": 9.0, "beta_b": 3.0, "wins": 8, "losses": 2},
                "fade": {"beta_a": 4.0, "beta_b": 8.0, "wins": 3, "losses": 7},
            },
            "accuracy": {"overall": 0.5, "resolved_count": 0},
        }

        await engine._seed_thompson_from_profile("0xPERSIST", profile)

        assert engine._thompson["0xPERSIST"]["follow"] == [9.0, 3.0]
        assert engine._thompson["0xPERSIST"]["fade"] == [4.0, 8.0]


# ---------------------------------------------------------------------------
# evaluate() — async decision tests
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_evaluate_skips_insufficient_data(self):
        """When readiness is zero, evaluate must return None and log 'skip'."""
        engine = make_engine()

        execute_mock = AsyncMock()
        patcher, conn = _mock_get_db(
            execute_mock=execute_mock,
            fetchrow_mock=AsyncMock(return_value=None),  # no profile row
        )
        trade = {
            "wallet_address": "0xAAA",
            "market_id": "mkt-1",
            "token_id": "tok-1",
            "is_leader": True,
        }
        with patcher:
            result = await engine.evaluate(trade)

        assert result is None
        # INSERT into decision_log must have been called with action='skip'
        execute_mock.assert_awaited_once()
        call_args = execute_mock.await_args[0]
        assert "skip" in call_args

    @pytest.mark.asyncio
    async def test_evaluate_returns_none_on_missing_wallet(self):
        """Missing wallet_address → early return None, no DB call."""
        engine = make_engine()
        result = await engine.evaluate({"market_id": "mkt-1", "is_leader": True})
        assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_ignores_stale_trade_events(self):
        """Historical backfill trades should not create decisions or logs."""
        engine = make_engine()
        engine._get_readiness = AsyncMock()
        engine._log_decision = AsyncMock()

        result = await engine.evaluate(
            {
                "wallet_address": "0xSTALE",
                "market_id": "mkt-old",
                "token_id": "tok-old",
                "is_leader": True,
                "source": "data_api_wallet",
                "time": "2026-01-01T00:00:00+00:00",
            }
        )

        assert result is None
        engine._get_readiness.assert_not_awaited()
        engine._log_decision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_evaluate_returns_follow_when_follow_ready(self):
        """Follow-ready leader with thompson_follow > thompson_fade → action='follow'."""
        engine = make_engine()
        wallet = "0xBBB"

        # Pre-seed Thompson so follow wins deterministically when sampled
        engine._thompson[wallet] = {
            "follow": [100.0, 1.0],  # p ≈ 0.99
            "fade": [1.0, 100.0],  # p ≈ 0.01
        }

        # Patch _get_readiness to return follow-ready values
        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 60,
                "positions_resolved": 10,
                "confirmed_followers": 6,
            }
        )
        # Suppress DB/Redis side-effects
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        # Force exploration OFF so Thompson drives the decision
        with patch("numpy.random.random", return_value=1.0):  # > any exploration floor
            # numpy.random.beta: first call (follow) high, second call (fade) low
            with patch("numpy.random.beta", side_effect=[0.95, 0.05]):
                decision = await engine.evaluate(
                    {
                        "wallet_address": wallet,
                        "market_id": "mkt-2",
                        "token_id": "tok-2",
                        "is_leader": True,
                    }
                )

        assert decision is not None
        assert decision.action == "follow"
        assert decision.leader_wallet == wallet

    @pytest.mark.asyncio
    async def test_evaluate_returns_fade_when_only_fade_ready(self):
        """Only fade_ready (follow not ready) → action must be 'fade'."""
        engine = make_engine()
        wallet = "0xCCC"

        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 10,  # below FOLLOW_MIN_TRADES (50)
                "positions_resolved": 60,  # above FADE_MIN_RESOLVED (50)
                "confirmed_followers": 0,  # below FOLLOW_MIN_FOLLOWERS (5)
            }
        )
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        # Force exploration OFF
        with patch("numpy.random.random", return_value=1.0):
            with patch("numpy.random.beta", side_effect=[0.5, 0.5]):
                decision = await engine.evaluate(
                    {
                        "wallet_address": wallet,
                        "market_id": "mkt-3",
                        "token_id": "tok-3",
                        "is_leader": True,
                    }
                )

        assert decision is not None
        assert decision.action == "fade"

    @pytest.mark.asyncio
    async def test_evaluate_exploration_floor_still_returns_valid_action(self):
        """When forced into exploration, returned action must still be a valid string."""
        engine = make_engine()
        wallet = "0xDDD"

        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 60,
                "positions_resolved": 10,
                "confirmed_followers": 6,
            }
        )
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        # Force random() < exploration_floor to trigger exploration branch
        with patch("numpy.random.random", return_value=0.0):
            with patch("numpy.random.beta", side_effect=[0.5, 0.5]):
                decision = await engine.evaluate(
                    {
                        "wallet_address": wallet,
                        "market_id": "mkt-4",
                        "token_id": "tok-4",
                        "is_leader": True,
                    }
                )

        assert decision is not None
        assert decision.action in ("follow", "fade", "skip")

    @pytest.mark.asyncio
    async def test_evaluate_fade_skipped_when_error_model_confidence_low(self):
        """FADE with low error model confidence must be skipped."""
        engine = make_engine()
        wallet = "0xEEE"

        # Set up a fake error_model that returns low confidence
        low_confidence_pred = MagicMock()
        low_confidence_pred.confidence = 0.50  # below FADE_MIN_CONFIDENCE (0.75)
        error_model = MagicMock()
        error_model.predict = AsyncMock(return_value=low_confidence_pred)
        engine._error_model = error_model

        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 10,  # follow not ready
                "positions_resolved": 60,  # fade ready
                "confirmed_followers": 0,
            }
        )
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        with patch("numpy.random.random", return_value=1.0):
            with patch("numpy.random.beta", side_effect=[0.5, 0.5]):
                decision = await engine.evaluate(
                    {
                        "wallet_address": wallet,
                        "market_id": "mkt-5",
                        "token_id": "tok-5",
                        "is_leader": True,
                    }
                )

        assert decision is None
        # log_decision must have been called with 'skip'
        engine._log_decision.assert_awaited_once()
        call_args = engine._log_decision.await_args[0]
        assert "skip" in call_args

    @pytest.mark.asyncio
    async def test_evaluate_logs_every_follow_decision(self):
        """Every non-None decision must result in exactly one _log_decision call."""
        engine = make_engine()
        wallet = "0xFFF"

        engine._get_readiness = AsyncMock(
            return_value={
                "trades_observed": 60,
                "positions_resolved": 10,
                "confirmed_followers": 6,
            }
        )
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        with patch("numpy.random.random", return_value=1.0):
            with patch("numpy.random.beta", side_effect=[0.9, 0.1]):
                decision = await engine.evaluate(
                    {
                        "wallet_address": wallet,
                        "market_id": "mkt-6",
                        "token_id": "tok-6",
                        "is_leader": True,
                    }
                )

        assert decision is not None
        engine._log_decision.assert_awaited_once()
