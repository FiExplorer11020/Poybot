"""Wave-3 hardening tests for DoCalculusEngine.

Audit reference: docs/audit/phase3/round10_wave3_review.md.

Coverage gaps the original tests left open:

  1. do(leader=1) - do(leader=0) collapses to ~0 when leader_trade
     coefficient is zero (the news-confounding gate sentinel).
  2. do(leader=1) - do(leader=0) magnitude scales monotonically with
     the IV-adjusted coefficient.
  3. P(follower=1) under various marginal settings respects the
     parent-marginalisation rules.
  4. Counterfactual: evidence on a confounder (news_event=1) changes
     P(follower) even when leader_trade coefficient is zero.
  5. The 'p_no_news' path: when treatment is news_event (a parent of
     follower), the do() correctly marginalises across the other
     follower-parent (market_state) — DOCUMENTED LIMITATION of the MVP.
  6. Distribution normalisation invariant (probs sum to 1.0).
"""

from __future__ import annotations

import math

import pytest

from src.causal.do_calculus import DoCalculusEngine


class TestATESign:
    def test_zero_coefficient_collapses_intervention_difference(self):
        """The news-confounding gate sentinel: if b_L == 0 the gate
        should see do(leader=1) ≈ do(leader=0)."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=0.0)
        p1 = eng.do_intervention("leader_trade", 1, "follower_trade").p(1)
        p0 = eng.do_intervention("leader_trade", 0, "follower_trade").p(1)
        assert abs(p1 - p0) < 1e-6

    @pytest.mark.parametrize("coef", [0.5, 1.0, 2.0, 3.0])
    def test_positive_coefficient_monotone(self, coef):
        """For increasing positive b_L, do(leader=1) probability also
        increases monotonically (sigmoid is monotonic)."""
        results = []
        for c in [0.0, coef]:
            eng = DoCalculusEngine()
            eng.set_iv_adjusted_estimate(leader_trade_coefficient=c)
            p1 = eng.do_intervention(
                "leader_trade", 1, "follower_trade"
            ).p(1)
            results.append(p1)
        assert results[1] > results[0]

    def test_negative_coefficient_inverts_signal(self):
        """Negative coefficient -> do(leader=1) has LOWER P(follower=1)
        than do(leader=0). This is the case where causal inference says
        'this leader actually drives followers AWAY' — rare but the gate
        must report it cleanly."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=-2.0)
        p1 = eng.do_intervention("leader_trade", 1, "follower_trade").p(1)
        p0 = eng.do_intervention("leader_trade", 0, "follower_trade").p(1)
        assert p1 < p0


class TestParentMarginalisation:
    def test_uniform_marginals_default(self):
        """Fresh engine: all marginals at 0.5 produces midpoint P(follower)."""
        eng = DoCalculusEngine()
        p = eng.do_intervention("leader_trade", 1, "follower_trade").p(1)
        # With coef=0 and marginals all 0.5: sigmoid(0) * fraction + ...
        # Sigmoid(0) = 0.5 regardless of any single parent.
        assert abs(p - 0.5) < 1e-6

    def test_setting_news_marginal_propagates(self):
        """When news_marginal -> 0.0 and coef_news_follower > 0,
        P(follower) drops because the positive shift never activates."""
        eng = DoCalculusEngine()
        eng.set_observational_estimate("news_event", "follower_trade", 4.0)
        eng.set_marginal("news_event", 0.0)
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=0.0)
        p_no_news_marginal = eng.do_intervention(
            "leader_trade", 0, "follower_trade"
        ).p(1)
        # With news marginal = 0 and no leader effect, P(follower) ≈ sigmoid(0) = 0.5
        # marginalised over market_state.
        assert abs(p_no_news_marginal - 0.5) < 1e-2

        eng.set_marginal("news_event", 1.0)
        p_all_news = eng.do_intervention(
            "leader_trade", 0, "follower_trade"
        ).p(1)
        # With news marginal = 1.0 and large positive coef, P(follower) rises.
        assert p_all_news > 0.9


class TestCounterfactualEvidence:
    def test_confounder_evidence_shifts_probability(self):
        """Evidence{news_event=1} when news has POSITIVE observational
        coefficient lifts P(follower) even at zero leader effect.
        This proves the gate correctly identifies the news-confounding case."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=0.0)
        eng.set_observational_estimate("news_event", "follower_trade", 3.0)
        p_with = eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 1},
        )
        p_without = eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 0},
        )
        assert p_with > 0.9
        assert p_without < 0.6
        assert (p_with - p_without) > 0.3

    def test_evidence_restored_after_counterfactual(self):
        """Counterfactual must NOT mutate the engine's stored marginals."""
        eng = DoCalculusEngine()
        eng.set_marginal("news_event", 0.3)
        eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 1},
        )
        eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 0},
        )
        # Stored marginal still the original 0.3.
        d = eng.describe()
        assert d["marginals"]["news_event"] == pytest.approx(0.3)


class TestMVPLimitation:
    def test_do_news_does_not_propagate_through_leader(self):
        """DOCUMENTED LIMITATION (methodology audit deliverable).

        The MVP engine does NOT propagate do(news_event=v) through the
        news → leader edge. The leader_trade parent of follower_trade is
        treated with its stored marginal, regardless of whether the
        do() should have pushed leader_trade to a different value.

        This test PINS the current behaviour so the methodology auditor
        knows exactly where the MVP scope ends.
        """
        eng = DoCalculusEngine()
        # Strong news → follower coefficient.
        eng.set_observational_estimate("news_event", "follower_trade", 2.0)
        # Even stronger news → leader coefficient.
        eng.set_observational_estimate("news_event", "leader_trade", 5.0)
        # Strong leader → follower.
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=3.0)
        # leader_trade marginal stays at 0.5 (the engine's default).

        p_news = eng.do_intervention("news_event", 1, "follower_trade").p(1)
        p_no_news = eng.do_intervention("news_event", 0, "follower_trade").p(1)
        # Difference reflects the direct news → follower edge (coef=2)
        # but NOT the news → leader → follower indirect path.
        delta = p_news - p_no_news
        # Direct effect bound: sigmoid(2 + 3*0.5) - sigmoid(0 + 3*0.5) ≈
        # sigmoid(3.5) - sigmoid(1.5) ≈ 0.97 - 0.82 = 0.15 (marginalised
        # over market_state). The indirect path would have lifted p_news
        # by an additional sigmoid(2 + 3*1) - sigmoid(2 + 3*0) ≈ 0.18.
        # If the engine were a full do-calculus it would report ~0.33;
        # MVP reports ~0.15. The test pins that gap.
        assert 0.05 < delta < 0.30, (
            f"do(news=1) - do(news=0) = {delta:.3f}; MVP-scope direct "
            "effect only, indirect path through leader_trade is ignored."
        )


class TestDistributionInvariants:
    def test_distribution_probabilities_sum_to_one(self):
        """Distribution returned by do_intervention is normalised."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=2.5)
        dist = eng.do_intervention("leader_trade", 1, "follower_trade")
        s = dist.p(0) + dist.p(1)
        assert abs(s - 1.0) < 1e-9

    def test_sigmoid_handles_large_logit(self):
        """Engine internal _sigmoid must not overflow for large inputs."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=1000.0)
        p = eng.do_intervention("leader_trade", 1, "follower_trade").p(1)
        assert 0.0 <= p <= 1.0
        assert math.isfinite(p)


class TestNonImplementedQueries:
    def test_query_news_event_not_supported(self):
        """MVP scope: query_var must be follower_trade."""
        eng = DoCalculusEngine()
        with pytest.raises(NotImplementedError):
            eng.do_intervention("leader_trade", 1, "news_event")

    def test_query_market_state_not_supported(self):
        eng = DoCalculusEngine()
        with pytest.raises(NotImplementedError):
            eng.do_intervention("news_event", 1, "market_state")
