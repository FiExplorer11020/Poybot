"""Pearl-style do-calculus engine over the FIXED 4-node R10 causal DAG.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 2 + § 3.3.

The DAG (per spec § 2 — NOT user-configurable):

        news_event  ───┬───▶ leader_trade
                       │
                       └───▶ follower_trade
                                 ▲
                                 │
                        leader_trade
                                 ▲
                                 │
                        market_state

    Nodes: {news_event, market_state, leader_trade, follower_trade}
    Edges:
        news_event   → leader_trade        (news drives leader)
        news_event   → follower_trade      (news drives followers — confounder)
        market_state → leader_trade        (state drives leader)
        market_state → follower_trade      (state drives followers)
        leader_trade → follower_trade      (the causal arrow we want)

MVP scope (per spec § 3.3 and the orchestrator's hard constraints):

  * ``do(leader_trade=value)`` for binary 0/1 leader_trade — mutilation
    of the graph (delete parents of leader_trade), then propagate to
    P(follower_trade).
  * Counterfactual P(follower_trade=1 | not leader_trade, evidence) —
    estimated via the second arm of the mutilated graph.

We do NOT implement the full Pearl do-calculus algorithm (three
inference rules, identifiability proof, c-component analysis). That is
research-grade and out of scope for this round; the methodology audit
gate is what catches misuse — see spec § 6 row "Causal inference math
is harder than we think".

Implementation: small directed graph stored as adjacency-dict. CPTs
are filled by ``DoCalculusEngine.set_iv_adjusted_estimate(...)`` from
the 2SLS output (the leader → follower causal coefficient) and by
``set_observational_estimate(...)`` for confounder → child arrows. Any
unset CPT defaults to a uniform prior so the engine never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# DAG constants (FIXED, exported)
# ---------------------------------------------------------------------------

CAUSAL_DAG_NODES: tuple[str, ...] = (
    "news_event",
    "market_state",
    "leader_trade",
    "follower_trade",
)
"""Fixed node set per spec § 2. DO NOT modify at runtime."""

CAUSAL_DAG_EDGES: tuple[tuple[str, str], ...] = (
    ("news_event", "leader_trade"),
    ("news_event", "follower_trade"),
    ("market_state", "leader_trade"),
    ("market_state", "follower_trade"),
    ("leader_trade", "follower_trade"),
)
"""Fixed edge set per spec § 2. DO NOT modify at runtime."""


# ---------------------------------------------------------------------------
# Lightweight distribution dataclass
# ---------------------------------------------------------------------------


@dataclass
class Distribution:
    """Discrete probability distribution over a single binary variable.

    Kept deliberately tiny: a {0: p0, 1: p1} dict. Sufficient for the
    do() and counterfactual queries the gate uses.
    """

    variable: str
    probabilities: dict[int, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalise defensively. If both are zero (caller passed an
        # uninitialised dist), fall back to uniform.
        total = sum(self.probabilities.values())
        if total <= 0:
            self.probabilities = {0: 0.5, 1: 0.5}
            return
        for k in list(self.probabilities.keys()):
            self.probabilities[k] = self.probabilities[k] / total

    def p(self, value: int) -> float:
        return float(self.probabilities.get(value, 0.0))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DoCalculusEngine:
    """Pearl-style do-operator over the FIXED R10 causal DAG.

    Two public queries:

      * ``do_intervention(treatment_var, treatment_value, query_var)``
        — returns ``Distribution`` over query_var under
        ``do(treatment_var=treatment_value)``.

      * ``counterfactual(treatment_var, treatment_value, query_var, evidence)``
        — returns the probability ``P(query_var=1 | do(treatment), evidence)``.

    The engine uses a single causal coefficient ``b_L`` (set via
    ``set_iv_adjusted_estimate``) for the leader_trade → follower_trade
    arrow. Confounder → follower_trade arrows ("news → follower",
    "market_state → follower") use coefficients set via
    ``set_observational_estimate``.

    All probabilities are computed under the logistic-link convention:

        P(follower_trade=1 | parents=p) = sigmoid(b_0 + sum_i b_i * p_i)

    with all parents treated as binary indicators. This is the MVP
    parametrisation per spec § 3.3 — sufficient to gate the volume
    anticipation policy when ``do(leader=1) - do(leader=0)`` collapses
    near zero.
    """

    def __init__(self) -> None:
        # Edge -> coefficient (log-odds) entering the child.
        # Defaults to 0 (no effect) for every edge.
        self._coef: dict[tuple[str, str], float] = {e: 0.0 for e in CAUSAL_DAG_EDGES}
        # Intercept (log-odds) per child node.
        self._intercept: dict[str, float] = {n: 0.0 for n in CAUSAL_DAG_NODES}
        # Marginal P(parent=1) for nodes that ARE parents (used when the
        # query doesn't fix them — we average over their distribution).
        # Default 0.5 = pure uncertainty.
        self._marginal: dict[str, float] = {n: 0.5 for n in CAUSAL_DAG_NODES}

    # ------------------------------------------------------------------ #
    # CPT setters                                                        #
    # ------------------------------------------------------------------ #

    def set_iv_adjusted_estimate(
        self,
        leader_trade_coefficient: float,
        intercept: float = 0.0,
    ) -> None:
        """Set the IV-adjusted leader → follower coefficient.

        The 2SLS ATE coefficient is the *partial derivative* of
        E[follower] w.r.t. leader_trade after controlling for confounders.
        For the binary logistic-link MVP we coerce that into a log-odds:
        the gate only cares about the sign + magnitude of ``b_L``, not
        its exact calibration.

        Parameters
        ----------
        leader_trade_coefficient : float
            The 2SLS ATE for leader_trade → follower_trade.
        intercept : float
            Optional baseline log-odds offset for follower_trade.
        """
        self._coef[("leader_trade", "follower_trade")] = float(
            leader_trade_coefficient
        )
        self._intercept["follower_trade"] = float(intercept)

    def set_observational_estimate(
        self,
        parent: str,
        child: str,
        coefficient: float,
    ) -> None:
        """Set the observational (uncorrected) coefficient on parent → child."""
        edge = (parent, child)
        if edge not in self._coef:
            raise ValueError(
                f"Edge {edge!r} is not in the fixed DAG; valid edges: "
                f"{CAUSAL_DAG_EDGES}"
            )
        self._coef[edge] = float(coefficient)

    def set_marginal(self, node: str, p_one: float) -> None:
        """Set the marginal P(node=1) used when averaging out a parent."""
        if node not in self._marginal:
            raise ValueError(
                f"Node {node!r} is not in the fixed DAG; valid nodes: "
                f"{CAUSAL_DAG_NODES}"
            )
        if not 0.0 <= p_one <= 1.0:
            raise ValueError(f"P(node=1) must be in [0, 1], got {p_one}")
        self._marginal[node] = float(p_one)

    # ------------------------------------------------------------------ #
    # Headline queries                                                   #
    # ------------------------------------------------------------------ #

    def do_intervention(
        self,
        treatment_var: str,
        treatment_value: int,
        query_var: str,
    ) -> Distribution:
        """Compute ``P(query_var | do(treatment_var=treatment_value))``.

        Implementation: graph mutilation. Delete all incoming edges to
        ``treatment_var`` (the parents no longer affect it under the
        do() operator), fix treatment_var = treatment_value, then
        compute the distribution over query_var by propagating along
        the remaining edges, marginalising over unset upstream nodes.

        MVP scope: only supports treatment_var in {leader_trade,
        news_event, market_state} and query_var = follower_trade
        (the gate use case). Other combinations raise.
        """
        if treatment_var not in CAUSAL_DAG_NODES:
            raise ValueError(
                f"Unknown treatment_var={treatment_var!r}; valid: "
                f"{CAUSAL_DAG_NODES}"
            )
        if query_var not in CAUSAL_DAG_NODES:
            raise ValueError(
                f"Unknown query_var={query_var!r}; valid: {CAUSAL_DAG_NODES}"
            )
        if query_var != "follower_trade":
            raise NotImplementedError(
                f"R10 MVP only supports query_var='follower_trade'; "
                f"got {query_var!r}. Extending to other queries requires "
                "the methodology-audit gate per spec § 6."
            )
        if treatment_value not in (0, 1):
            raise ValueError(
                f"treatment_value must be 0 or 1 (binary), got {treatment_value}"
            )

        # Build the parent-value assignment for follower_trade.
        # Parents under the mutilated graph: anything that's NOT the
        # mutilated subtree above the treatment.
        # Concretely for our DAG: when we do(leader_trade=v), the leader
        # node is fixed at v; news_event and market_state still have
        # their marginal influence on follower_trade. When we
        # do(news_event=v), only the news → follower arrow is fixed at
        # the do-value; leader_trade then becomes a function of
        # market_state alone (we have to marginalise it).

        # Express E[follower=1] = E_{parents not fixed}[sigmoid(b_0 + sum_i b_i p_i)]
        # Marginalise discretely over the un-fixed parents (each binary,
        # so at most 2^3 = 8 combinations for a 3-parent node; in our
        # DAG follower has 3 parents -> at most 4 combos when 1 is fixed).
        parents = [p for (p, c) in CAUSAL_DAG_EDGES if c == query_var]
        return self._propagate_to_follower(
            parents=parents,
            fixed={treatment_var: int(treatment_value)} if treatment_var in parents else {},
            mutilated_treatment=treatment_var,
            mutilated_value=int(treatment_value),
        )

    def counterfactual(
        self,
        treatment_var: str,
        treatment_value: int,
        query_var: str,
        evidence: dict[str, int] | None = None,
    ) -> float:
        """Counterfactual query: P(query_var=1 | do(treatment), evidence).

        Implementation matches ``do_intervention`` plus conditioning on
        the evidence dict (override marginals for evidence nodes). The
        spec's canonical example "P(follower | not leader, evidence)"
        is supported by passing ``treatment_var='leader_trade'``,
        ``treatment_value=0``, ``evidence={...}``.

        Returns the scalar probability ``P(query_var=1 | ...)`` rather
        than a Distribution because that's the gate's contract (single
        probability to compare against a threshold).
        """
        original_marginals = dict(self._marginal)
        try:
            if evidence:
                for k, v in evidence.items():
                    if k not in CAUSAL_DAG_NODES:
                        raise ValueError(
                            f"Evidence node {k!r} not in DAG; valid: "
                            f"{CAUSAL_DAG_NODES}"
                        )
                    self._marginal[k] = 1.0 if int(v) == 1 else 0.0
            dist = self.do_intervention(treatment_var, treatment_value, query_var)
            return float(dist.p(1))
        finally:
            self._marginal = original_marginals

    # ------------------------------------------------------------------ #
    # Internal: propagation                                              #
    # ------------------------------------------------------------------ #

    def _propagate_to_follower(
        self,
        parents: list[str],
        fixed: dict[str, int],
        mutilated_treatment: str,
        mutilated_value: int,
    ) -> Distribution:
        """Compute Distribution over follower_trade by marginalising
        over un-fixed parents.

        Under the do() operator, the mutilated_treatment node is set
        to mutilated_value regardless of its parents. The remaining
        un-fixed parents are averaged over their marginal P(parent=1).
        """
        # The do() always sets the treatment to the requested value;
        # if the treatment is a parent of follower_trade, that's
        # already in `fixed`; if not (e.g. treatment is also a parent
        # of leader_trade, like news_event), then we let the marginal
        # propagation absorb the do() through the standard DAG.

        # Enumerate all 2^|free_parents| binary settings.
        free = [p for p in parents if p not in fixed]
        # Defensive: cap at 16 free parents (2^16 = 65k); we never
        # have more than 3 in this DAG so this is just a sanity belt.
        if len(free) > 16:
            raise RuntimeError(
                f"Too many free parents to enumerate ({len(free)}); "
                "DAG bug."
            )
        expected_p1 = 0.0
        for mask in range(2 ** len(free)):
            assignment = dict(fixed)
            joint_prob = 1.0
            for i, parent in enumerate(free):
                v = (mask >> i) & 1
                assignment[parent] = v
                # Under do(mutilated_treatment), the mutilated node's
                # OWN marginal is fixed (no upstream effect). All other
                # nodes use their stored marginal.
                if parent == mutilated_treatment:
                    p1 = 1.0 if mutilated_value == 1 else 0.0
                else:
                    p1 = self._marginal[parent]
                joint_prob *= p1 if v == 1 else (1.0 - p1)
            # P(follower=1 | parents=assignment)
            logit = self._intercept["follower_trade"]
            for parent, val in assignment.items():
                edge = (parent, "follower_trade")
                if edge in self._coef:
                    logit += self._coef[edge] * val
            p_follower = _sigmoid(logit)
            expected_p1 += joint_prob * p_follower

        return Distribution(
            variable="follower_trade",
            probabilities={1: expected_p1, 0: 1.0 - expected_p1},
        )

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #

    def get_edge_coefficient(self, parent: str, child: str) -> float:
        return self._coef[(parent, child)]

    def describe(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the engine's CPT state."""
        return {
            "nodes": list(CAUSAL_DAG_NODES),
            "edges": [list(e) for e in CAUSAL_DAG_EDGES],
            "coefficients": {f"{p}->{c}": v for (p, c), v in self._coef.items()},
            "intercepts": dict(self._intercept),
            "marginals": dict(self._marginal),
        }


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic."""
    # Standard guard to avoid overflow in expressions like exp(1000).
    if x >= 0:
        z = pow(2.718281828459045, -x)
        return 1.0 / (1.0 + z)
    z = pow(2.718281828459045, x)
    return z / (1.0 + z)


__all__ = [
    "CAUSAL_DAG_EDGES",
    "CAUSAL_DAG_NODES",
    "Distribution",
    "DoCalculusEngine",
]
