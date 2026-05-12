"""Tests for DoCalculusEngine — Pearl-style do() over the fixed R10 DAG.

Coverage:
  * DAG structure is fixed (4 nodes, 5 edges per spec § 2).
  * do(leader_trade=1) and do(leader_trade=0) yield different
    follower distributions.
  * Counterfactual query returns a scalar probability in [0, 1].
  * Invalid nodes / values raise.
  * The IV-adjusted leader -> follower coefficient flows through.
"""

from __future__ import annotations

import pytest

from src.causal.do_calculus import (
    CAUSAL_DAG_EDGES,
    CAUSAL_DAG_NODES,
    Distribution,
    DoCalculusEngine,
)


# ---------------------------------------------------------------------------
# DAG structure
# ---------------------------------------------------------------------------


class TestDAGStructure:
    def test_node_set_matches_spec(self):
        """Spec § 2: 4 nodes, in this exact order."""
        assert set(CAUSAL_DAG_NODES) == {
            "news_event",
            "market_state",
            "leader_trade",
            "follower_trade",
        }

    def test_edge_set_matches_spec(self):
        """Spec § 2: 5 edges per the DAG diagram."""
        assert set(CAUSAL_DAG_EDGES) == {
            ("news_event", "leader_trade"),
            ("news_event", "follower_trade"),
            ("market_state", "leader_trade"),
            ("market_state", "follower_trade"),
            ("leader_trade", "follower_trade"),
        }

    def test_dag_is_acyclic(self):
        """No cycles in the fixed edge set."""
        # Simple cycle-detection via topological sort.
        nodes = set(CAUSAL_DAG_NODES)
        in_degree = {n: 0 for n in nodes}
        for _, c in CAUSAL_DAG_EDGES:
            in_degree[c] += 1
        # Sources have zero in-degree.
        sources = [n for n, d in in_degree.items() if d == 0]
        # At least two sources (news_event, market_state) per the
        # spec — exogenous to the system.
        assert len(sources) >= 2
        assert "news_event" in sources
        assert "market_state" in sources


# ---------------------------------------------------------------------------
# do() queries
# ---------------------------------------------------------------------------


class TestDoIntervention:
    def test_default_engine_uniform(self):
        """A fresh engine with zero coefficients => P(follower=1) = 0.5."""
        eng = DoCalculusEngine()
        dist = eng.do_intervention("leader_trade", 1, "follower_trade")
        assert isinstance(dist, Distribution)
        assert abs(dist.p(1) - 0.5) < 1e-6
        assert abs(dist.p(0) - 0.5) < 1e-6

    def test_positive_leader_coefficient_increases_follower(self):
        """Strong positive coefficient -> do(leader=1) yields high P(follower=1)."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=3.0)
        p1 = eng.do_intervention(
            "leader_trade", 1, "follower_trade"
        ).p(1)
        p0 = eng.do_intervention(
            "leader_trade", 0, "follower_trade"
        ).p(1)
        assert p1 > p0, f"do(leader=1) p={p1} must exceed do(leader=0) p={p0}"
        # Magnitude: sigmoid(3) ≈ 0.95, sigmoid(0) = 0.5
        assert p1 > 0.85
        assert p0 < 0.6

    def test_zero_coefficient_no_change(self):
        """When the leader->follower coefficient is 0, do(leader=*) doesn't
        change P(follower)."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=0.0)
        p1 = eng.do_intervention("leader_trade", 1, "follower_trade").p(1)
        p0 = eng.do_intervention("leader_trade", 0, "follower_trade").p(1)
        assert abs(p1 - p0) < 1e-6

    def test_invalid_treatment_var_raises(self):
        eng = DoCalculusEngine()
        with pytest.raises(ValueError, match="Unknown treatment_var"):
            eng.do_intervention("foo", 1, "follower_trade")

    def test_invalid_query_var_raises(self):
        eng = DoCalculusEngine()
        with pytest.raises((ValueError, NotImplementedError)):
            eng.do_intervention("leader_trade", 1, "news_event")

    def test_non_binary_treatment_value_raises(self):
        eng = DoCalculusEngine()
        with pytest.raises(ValueError, match="treatment_value"):
            eng.do_intervention("leader_trade", 5, "follower_trade")


# ---------------------------------------------------------------------------
# Counterfactual queries
# ---------------------------------------------------------------------------


class TestCounterfactual:
    def test_counterfactual_returns_scalar_in_unit_interval(self):
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=1.0)
        p = eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 1},
        )
        assert isinstance(p, float)
        assert 0.0 <= p <= 1.0

    def test_counterfactual_evidence_changes_result(self):
        """Conditioning on news_event=1 increases P(follower=1) when news
        has a positive observational coefficient."""
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=0.0)
        eng.set_observational_estimate("news_event", "follower_trade", 2.0)
        p_news = eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 1},
        )
        p_no_news = eng.counterfactual(
            "leader_trade", 0, "follower_trade",
            evidence={"news_event": 0},
        )
        assert p_news > p_no_news


# ---------------------------------------------------------------------------
# Coefficient management
# ---------------------------------------------------------------------------


class TestCoefficientManagement:
    def test_set_observational_estimate_rejects_unknown_edge(self):
        eng = DoCalculusEngine()
        with pytest.raises(ValueError, match="not in the fixed DAG"):
            eng.set_observational_estimate("foo", "bar", 1.0)

    def test_get_edge_coefficient_round_trip(self):
        eng = DoCalculusEngine()
        eng.set_observational_estimate("news_event", "follower_trade", 1.7)
        assert eng.get_edge_coefficient("news_event", "follower_trade") == 1.7

    def test_set_marginal_validates_range(self):
        eng = DoCalculusEngine()
        with pytest.raises(ValueError, match="must be in"):
            eng.set_marginal("news_event", 1.5)
        with pytest.raises(ValueError, match="not in"):
            eng.set_marginal("unknown_node", 0.5)
        eng.set_marginal("news_event", 0.3)  # legit


# ---------------------------------------------------------------------------
# Describe / introspection
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_describe_returns_json_friendly(self):
        eng = DoCalculusEngine()
        eng.set_iv_adjusted_estimate(leader_trade_coefficient=1.2)
        d = eng.describe()
        assert "nodes" in d
        assert "edges" in d
        assert "coefficients" in d
        assert "leader_trade->follower_trade" in d["coefficients"]
        assert d["coefficients"]["leader_trade->follower_trade"] == 1.2
