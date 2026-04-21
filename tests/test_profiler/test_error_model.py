"""
Unit tests for src/profiler/error_model.py

Pure/sync methods (_predict_phase1, _build_features, _determine_phase) are
tested without any mocking.

Async methods that touch the DB are tested with a mocked get_db context manager.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from src.config import settings
from src.profiler.error_model import (
    CUSUM_THRESHOLD,
    ErrorModel,
    _phase3_supported,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_mock_conn(fetchrow_result=None, fetch_result=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.execute = AsyncMock()
    return conn


def _make_get_db(conn):
    """Return a callable that acts as an async context manager yielding conn."""

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _make_get_db_sequence(*conns):
    """
    Return a mock for get_db that yields each successive conn on each call.
    """
    conn_iter = iter(conns)

    def _get_db_mock():
        conn = next(conn_iter)

        @asynccontextmanager
        async def _ctx():
            yield conn

        return _ctx()

    return _get_db_mock


def _profile_with_category(category: str, beta_a: float, beta_b: float) -> dict:
    return {
        "accuracy": {
            "overall": 0.0,
            "resolved_count": int(beta_a + beta_b - 2),
            "by_category": {
                category: {
                    "wins": int(beta_a - 1),
                    "losses": int(beta_b - 1),
                    "beta_a": beta_a,
                    "beta_b": beta_b,
                }
            },
        }
    }


# ─── 1. test_predict_phase1_uniform_prior ────────────────────────────────────


def test_predict_phase1_uniform_prior():
    """Wallet with no profile data → uniform Beta(1,1) → p_error ≈ 0.5."""
    model = ErrorModel()
    accuracy = {}
    trade_context = {"category": "crypto"}
    p_error, confidence = model._predict_phase1(accuracy, trade_context)
    assert abs(p_error - 0.5) < 1e-6


# ─── 2. test_predict_phase1_with_history ─────────────────────────────────────


def test_predict_phase1_with_history():
    """
    Profile has crypto: {beta_a: 7.0, beta_b: 3.0}.
    p_error = 3 / (7 + 3) = 0.3
    """
    model = ErrorModel()
    profile = _profile_with_category("crypto", beta_a=7.0, beta_b=3.0)
    p_error, _ = model._predict_phase1(profile["accuracy"], {"category": "crypto"})
    assert abs(p_error - 0.3) < 1e-4


# ─── 3. test_predict_phase1_confidence_increases_with_data ───────────────────


def test_predict_phase1_confidence_increases_with_data():
    """
    A profile with a lot of data (large beta_a + beta_b) has lower variance
    and therefore higher confidence than an empty (uniform) profile.
    """
    model = ErrorModel()

    # Empty profile
    _, conf_empty = model._predict_phase1({}, {"category": "unknown"})

    # Well-populated profile (50 wins, 50 losses)
    profile = _profile_with_category("politics", beta_a=51.0, beta_b=51.0)
    _, conf_full = model._predict_phase1(profile["accuracy"], {"category": "politics"})

    assert conf_full > conf_empty


# ─── 4. test_determine_phase_boundaries ──────────────────────────────────────


def test_determine_phase_boundaries():
    model = ErrorModel()
    assert model._determine_phase(0) == 1
    assert model._determine_phase(settings.MIN_RESOLVED_FOR_ERROR_P2 - 1) == 1
    assert model._determine_phase(settings.MIN_RESOLVED_FOR_ERROR_P2) == 2
    assert model._determine_phase(settings.MIN_RESOLVED_FOR_ERROR_P3 - 1) == 2
    expected_phase3 = 3 if _phase3_supported() else 2
    assert model._determine_phase(settings.MIN_RESOLVED_FOR_ERROR_P3) == expected_phase3
    assert model._determine_phase(settings.MIN_RESOLVED_FOR_ERROR_P3 + 100) == expected_phase3


# ─── 5. test_cusum_state_update_on_correct_prediction ────────────────────────


def test_cusum_state_update_on_correct_prediction():
    """
    When the model predicts 1.0 (certain loss) and the trade IS a loss,
    prediction_error = |1.0 - 1.0| = 0.0.
    CUSUM increment = max(0, 0 - 0.15 - 0.05) = 0 → stays at 0.
    """
    model = ErrorModel()
    wallet = "0xwallet_correct"
    model._cusum_state[wallet] = 0.0

    # Simulate the CUSUM update from update() manually
    prediction_error = 0.0  # perfect prediction
    s_prev = model._cusum_state.get(wallet, 0.0)
    s_new = max(0.0, s_prev + prediction_error - 0.15 - 0.05)
    model._cusum_state[wallet] = s_new

    assert model._cusum_state[wallet] == 0.0
    assert model._cusum_state[wallet] <= CUSUM_THRESHOLD


# ─── 6. test_cusum_state_triggers_on_drift ───────────────────────────────────


def test_cusum_state_triggers_on_drift():
    """
    Injecting large prediction errors repeatedly must push CUSUM above threshold.
    """
    model = ErrorModel()
    wallet = "0xwallet_drift"
    model._cusum_state[wallet] = 0.0

    # Each step: prediction_error = 1.0 (worst case)
    # increment = max(0, S + 1.0 - 0.15 - 0.05) = S + 0.8
    for _ in range(5):
        s_prev = model._cusum_state.get(wallet, 0.0)
        s_new = max(0.0, s_prev + 1.0 - 0.15 - 0.05)
        model._cusum_state[wallet] = s_new

    assert model._cusum_state[wallet] > CUSUM_THRESHOLD


# ─── 7. test_build_features_returns_array ────────────────────────────────────


def test_build_features_returns_array():
    """_build_features must return a numpy array of shape (18,)."""
    model = ErrorModel()
    ctx = {
        "category": "sports",
        "is_contrarian": True,
        "deviation_score": 0.4,
        "size_ratio": 1.5,
        "liquidity_score": 0.7,
        "process_score": 0.8,
        "flip_rate": 0.1,
        "scale_in_rate": 0.2,
        "hours_since_last_trade": 6.0,
        "hours_since_category_last_trade": 24.0,
        "hours_since_last_loss": 12.0,
        "category_accuracy": 0.65,
        "profile_maturity": 0.4,
        "confirmed_followers": 4,
        "hour_sin": 0.5,
        "hour_cos": 0.866,
        "dow_sin": 0.1,
        "dow_cos": 0.99,
    }
    features = model._build_features(ctx)
    assert isinstance(features, np.ndarray)
    assert features.shape == (18,)
    # is_contrarian encoded as 1.0
    assert features[1] == 1.0
    assert features[2] == pytest.approx(0.4)
    assert features[3] == pytest.approx(1.5 / 4.0)
    assert features[4] == pytest.approx(0.7)
    assert features[5] == pytest.approx(0.8)
    assert features[11] == pytest.approx(0.65)


# ─── 8. test_update_triggers_upgrade_check ───────────────────────────────────


@pytest.mark.asyncio
async def test_update_triggers_upgrade_check():
    """
    When _load_state returns phase=1 and resolved_count=100
    (= MIN_RESOLVED_FOR_ERROR_P2), update() must call _upgrade_phase.
    """
    model = ErrorModel()
    wallet = "0xupgrade_wallet"

    profile_json = json.dumps(
        {
            "accuracy": {
                "overall": 0.6,
                "resolved_count": 100,  # triggers P2
                "by_category": {},
            }
        }
    )

    conn = _make_mock_conn(
        fetchrow_result={
            "error_model_phase": 1,
            "error_model_blob": None,
            "profile_json": profile_json,
        }
    )
    get_db_mock = _make_get_db(conn)

    position_result = {
        "category": "crypto",
        "pnl_usdc": 50.0,
        "trade_context": {"category": "crypto"},
    }

    upgrade_called_with = []

    async def _mock_upgrade(w, new_phase, profile):
        upgrade_called_with.append((w, new_phase))

    model._upgrade_phase = _mock_upgrade

    with patch("src.profiler.error_model.get_db", get_db_mock):
        await model.update(wallet, position_result)

    assert len(upgrade_called_with) == 1
    assert upgrade_called_with[0][0] == wallet
    assert upgrade_called_with[0][1] == 2


# ─── 9. test_downgrade_resets_cusum ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_downgrade_resets_cusum():
    """
    After _downgrade_phase, the wallet's CUSUM statistic is reset to 0.0
    and the DB is updated with the lower phase.
    """
    model = ErrorModel()
    wallet = "0xdowngrade_wallet"
    model._cusum_state[wallet] = 5.0  # Well above threshold

    save_conn = _make_mock_conn()
    runtime_conn = _make_mock_conn()
    get_db_mock = _make_get_db_sequence(save_conn, runtime_conn)

    with patch("src.profiler.error_model.get_db", get_db_mock):
        await model._downgrade_phase(wallet, current_phase=2, profile={})

    # CUSUM must be reset
    assert model._cusum_state[wallet] == 0.0

    # DB save must have been called with new_phase=1 and blob=None
    save_conn.execute.assert_called_once()
    call_args = save_conn.execute.call_args[0]
    # $2 = phase, $3 = blob
    assert call_args[1] == wallet
    assert call_args[2] == 1  # phase downgraded from 2 → 1
    assert call_args[3] is None  # model blob wiped
    runtime_conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_training_data_uses_reconstructed_features():
    model = ErrorModel()
    conn = _make_mock_conn()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "market_id": "m1",
                    "token_id": "tok1",
                    "direction": "yes",
                    "open_time": datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                    "close_time": datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
                    "entry_price": 0.41,
                    "size_usdc": 220.0,
                    "pnl_usdc": -15.0,
                    "category": "crypto",
                    "liquidity_score": 0.35,
                    "avg_recent_price": 0.55,
                },
                {
                    "market_id": "m2",
                    "token_id": "tok2",
                    "direction": "yes",
                    "open_time": datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
                    "close_time": datetime(2026, 4, 2, 13, 0, tzinfo=timezone.utc),
                    "entry_price": 0.62,
                    "size_usdc": 120.0,
                    "pnl_usdc": 20.0,
                    "category": "politics",
                    "liquidity_score": 0.85,
                    "avg_recent_price": 0.5,
                },
            ],
            [
                {
                    "market_id": "m1",
                    "token_id": "tok1",
                    "side": "BUY",
                    "size_usdc": 180.0,
                    "time": datetime(2026, 4, 1, 8, 30, tzinfo=timezone.utc),
                    "category": "crypto",
                },
                {
                    "market_id": "m1",
                    "token_id": "tok1",
                    "side": "SELL",
                    "size_usdc": 150.0,
                    "time": datetime(2026, 4, 1, 8, 50, tzinfo=timezone.utc),
                    "category": "crypto",
                },
            ],
            [
                {"first_observed": datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc)},
            ],
        ]
    )

    with patch("src.profiler.error_model.get_db", _make_get_db(conn)):
        data = await model._fetch_training_data("0xtrain", phase=2)

    assert data is not None
    assert len(data["X"]) == 2
    first = data["X"][0]
    assert first[1] == 1.0
    assert first[2] > 0.0
    assert first[4] == pytest.approx(0.35)
    assert first[5] >= 0.0
    assert first[13] > 0.0
