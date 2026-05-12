"""Round 10 (The Truth Test) — Causal inference layer.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md

This package layers causal inference (instrumental variables + 2SLS +
Pearl-style do-calculus + counterfactual replay) on top of the Hawkes
statistical association from R5/R9. The headline contract:

    When the IV-corrected ATE significantly differs from the Hawkes
    alpha/mu ratio, the confidence engine R10 gate downgrades follow
    confidence and BLOCKS volume_anticipation entries — i.e. we trade
    on causation, not correlation.

Module shape:

  * :mod:`src.causal.instruments`           — InstrumentRegistry +
        detectors (NewsEventDetector, OracleUpdateDetector,
        RelatedMarketResolver, LeaderGasQuirkDetector,
        APIOutageWindowDetector).
  * :mod:`src.causal.iv_estimator`          — TwoStageLeastSquaresEstimator
        (numpy + optional statsmodels). Bootstrap CI + Wu-Hausman +
        first-stage F-stat.
  * :mod:`src.causal.do_calculus`           — DoCalculusEngine
        (Pearl-style do() over the fixed 4-node DAG).
  * :mod:`src.causal.counterfactual_replay` — CounterfactualReplayer
        (cold-tier-backed what-if queries; 30-day replay < 5 min).
  * :mod:`src.causal.daemon`                — nightly 2SLS batch
        entrypoint for systemd.
  * :mod:`src.causal.__main__`              — ``python -m src.causal``.

The fixed causal DAG (spec § 2):

        ┌──────────────────┐
        │  News event /    │       (exogenous)
        │  Oracle update / │
        │  Related-market  │
        └────┬────────┬────┘
             │        │
        ┌────▼────┐ ┌─▼──────────┐
        │ Leader  │ │ Follower    │
        │ trades  │ │ trades      │
        └────┬────┘ └─▲──────────┘
             │        │
             └────────┘    (the causal arrow we want to estimate)

The DAG structure is FIXED — not user-configurable. Operators tune
which instruments are active (via the InstrumentRegistry detector
list), not the graph topology.
"""

from src.causal.counterfactual_replay import (
    CounterfactualReplayer,
    ReplayResult,
)
from src.causal.do_calculus import (
    CAUSAL_DAG_EDGES,
    CAUSAL_DAG_NODES,
    DoCalculusEngine,
)
from src.causal.instruments import (
    APIOutageWindowDetector,
    Detector,
    FixtureNewsEventDetector,
    InstrumentRegistry,
    InstrumentalEvent,
    LeaderGasQuirkDetector,
    NewsEventDetector,
    OracleUpdateDetector,
    RelatedMarketResolver,
)
from src.causal.iv_estimator import (
    IVEstimate,
    TwoStageLeastSquaresEstimator,
    first_stage_f_stat,
    wu_hausman_test,
)

__all__ = [
    # Instruments
    "APIOutageWindowDetector",
    "Detector",
    "FixtureNewsEventDetector",
    "InstrumentRegistry",
    "InstrumentalEvent",
    "LeaderGasQuirkDetector",
    "NewsEventDetector",
    "OracleUpdateDetector",
    "RelatedMarketResolver",
    # IV estimator
    "IVEstimate",
    "TwoStageLeastSquaresEstimator",
    "first_stage_f_stat",
    "wu_hausman_test",
    # Do-calculus
    "CAUSAL_DAG_EDGES",
    "CAUSAL_DAG_NODES",
    "DoCalculusEngine",
    # Counterfactual replay
    "CounterfactualReplayer",
    "ReplayResult",
]
