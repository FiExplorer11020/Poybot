"""Round 10 (The Truth Test) test package.

Coverage targets the 4 spec components:
  * test_instruments.py            — InstrumentRegistry + detectors
  * test_iv_estimator.py           — TwoStageLeastSquaresEstimator (Monte Carlo)
  * test_do_calculus.py            — DoCalculusEngine
  * test_counterfactual_replay.py  — CounterfactualReplayer (cold-tier shape)
  * test_daemon.py                 — CausalDaemon lifecycle

The IV estimator Monte Carlo test is the load-bearing numerics
deliverable — it verifies the 2SLS estimator recovers a known causal
coefficient within tolerance under a known confounder + valid
instrument. See the test docstring for the simulation contract.
"""
