"""Hardening tests for :mod:`src.calibration.decision_replay`.

Wave-3 reviewer additions:

* ``DecisionPrediction.from_decision_context`` extracts every nested
  context field correctly when ALL fields are present (positive case).
* ``from_decision_context`` survives when EVERY trade_context field is
  missing (the all-None case) without raising — every output field
  except predicted_at is None.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.calibration.decision_replay import DecisionPrediction


def test_from_decision_context_full_field_extraction():
    """Every nested field is correctly mapped when present."""
    decision = SimpleNamespace(
        thompson_follow=0.71,
        thompson_fade=0.22,
        trade_context={
            "wallet_strategy": "directional",
            "strategy_confidence": 0.88,
            "hawkes_alpha_mu": 1.42,  # top-level fallback path
            "volume_forecast": {
                "total_volume_usdc": 9500.0,
                "ci_low": 7800.0,
                "ci_high": 11200.0,
            },
            "causal_gate": {
                "ate": 1.6,
                "ci_low": 1.3,
                "ci_high": 1.9,
                "hawkes_alpha_mu": 1.55,  # nested takes precedence
            },
            "strategy_weights_applied": {
                "primary_strategy": "directional",
                "primary_strategy_confidence": 0.88,
            },
        },
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence == 0.71
    assert pred.fade_confidence == 0.22
    assert pred.strategy_class == "directional"
    assert pred.strategy_confidence == 0.88
    assert pred.hawkes_alpha_mu == 1.55  # nested causal_gate wins
    assert pred.volume_forecast_usdc == 9500.0
    assert pred.volume_forecast_ci_low == 7800.0
    assert pred.volume_forecast_ci_high == 11200.0
    assert pred.causal_ate == 1.6
    assert pred.causal_ate_ci_low == 1.3
    assert pred.causal_ate_ci_high == 1.9
    assert pred.predicted_at is not None


def test_from_decision_context_all_fields_missing_returns_predicted_at_only():
    """Every nested field absent → every output field except
    predicted_at is None. No exception."""
    decision = SimpleNamespace(
        thompson_follow=None,
        thompson_fade=None,
        trade_context={},
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence is None
    assert pred.fade_confidence is None
    assert pred.strategy_class is None
    assert pred.strategy_confidence is None
    assert pred.hawkes_alpha_mu is None
    assert pred.volume_forecast_usdc is None
    assert pred.volume_forecast_ci_low is None
    assert pred.volume_forecast_ci_high is None
    assert pred.causal_ate is None
    assert pred.causal_ate_ci_low is None
    assert pred.causal_ate_ci_high is None
    assert pred.predicted_at is not None


def test_from_decision_context_no_trade_context_attribute():
    """A Decision with no trade_context attr at all → all None except
    predicted_at. No AttributeError."""

    class _DecisionShape:
        thompson_follow = 0.5
        thompson_fade = 0.3
        # no trade_context attribute

    pred = DecisionPrediction.from_decision_context(_DecisionShape())
    assert pred.follow_confidence == 0.5
    assert pred.fade_confidence == 0.3
    assert pred.strategy_class is None
    assert pred.predicted_at is not None


def test_from_decision_context_handles_malformed_context_type():
    """If trade_context is not a dict (e.g. a list), the extractor
    treats it as empty rather than crashing."""
    decision = SimpleNamespace(
        thompson_follow=0.6,
        thompson_fade=0.4,
        trade_context=["this", "is", "not", "a", "dict"],
    )
    pred = DecisionPrediction.from_decision_context(decision)
    assert pred.follow_confidence == 0.6
    assert pred.strategy_class is None
    assert pred.causal_ate is None
