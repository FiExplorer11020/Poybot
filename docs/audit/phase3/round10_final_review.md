# Round 10 — The Truth Test: Final Code-Layer Review

> **Branch**: `round-10-truth`
> **Reviewer**: R10 single-architect+implementer (one-pass)
> **Date**: 2026-05-12
> **Specification reference**: [`docs/ROUND_10_CAUSAL_INFERENCE.md`](../../ROUND_10_CAUSAL_INFERENCE.md)
> **Risk rating**: 5/5 (highest of any round — methodology audit gate REQUIRED before live)

---

## 1. Top-line recommendation

**PASS — code layer complete, awaiting methodology-audit gate.**

R10 ships the full code-layer of The Truth Test: an InstrumentRegistry
with five separate detector classes (NewsEventDetector +
FixtureNewsEventDetector for tests, OracleUpdateDetector,
RelatedMarketResolver, LeaderGasQuirkDetector, APIOutageWindowDetector),
a pure-numpy TwoStageLeastSquaresEstimator with bootstrap CI +
Wu-Hausman + first-stage F-stat, a Pearl-style DoCalculusEngine over
the FIXED 4-node DAG (MVP scope), a cold-tier-backed
CounterfactualReplayer, a nightly daemon with systemd unit, two
migrations (030/031), 10 Prometheus metrics, a confidence-engine R10
gate gated behind `causal_gating_enabled` (default OFF), and all
constants registered in `src/config.py` with validators.

**Tests**: 67 new R10 tests; full suite 1,370 passed, 9 skipped, 2
xfailed (zero failures). Baseline was 1,303 tests; R10 added 67
tests (matches the diff).

**CRITICAL gate before going live**: the spec § 6 risk row "Causal
inference math is harder than we think" mandates a 1-week external
causal-inference expert review of the methodology BEFORE flipping
`causal_gating_enabled=true`. The code layer ships clean code, the
math is sound, but the *application* (which instruments are valid
for this exact data, how to handle multiple-testing inflation across
hundreds of leader-pool pairs, whether the binary-logistic-link MVP
do-calculus is appropriate for the gate) is the hard part the
external review catches.

**Operator-only gates remain** (spec § 6 / § 7) — explicitly out of
scope for the code pass:

1. **NewsAPI subscription** + entity-recognition pipeline wiring. The
   `NewsEventDetector` class takes an injected `http_session` and a
   pluggable NER extractor; the operator wires both. The
   `FixtureNewsEventDetector` covers tests + smoke runs.
2. **External methodology review** — 1 week, causal-inference expert.
   The audit doc owner is OUR external reviewer; nothing in the code
   layer pre-empts their sign-off.
3. **Bonferroni / Benjamini-Hochberg multiple-testing correction**
   over the (leader, pool) grid. The daemon emits per-pair p-values;
   the correction is applied at the dashboard layer per spec § 6
   risk row "Multiple-testing inflation". The operator wires this
   when they're ready for the 80% Wu-Hausman acceptance criterion.
4. **80% Wu-Hausman p < 0.05** acceptance across all converged pairs
   — measured over a 30-day shadow window once the daemon has run
   nightly.
5. **60% CI-excludes-zero positively** acceptance per spec § 6 —
   same shadow-window measurement.
6. **A/B Sharpe + max-drawdown soak** over 60 days vs the R9-baseline
   to clear the spec § 6 final acceptance row.
7. **External validation** of each instrument's exogeneity assumption
   (the methodology audit's primary deliverable).
8. **Gradual flip of `causal_gating_enabled`** in shadow first
   (R10 gate runs but the downgrade is logged, not applied — operator
   wires a "shadow vs live diff" similar to R7's
   `mempool_shadow_vs_live_pnl_diff_usdc`).

---

## 2. Per-component verification

| § Spec | Component | File | Verdict |
|---|---|---|---|
| 3.1 | `InstrumentRegistry` + 5 detector classes | `src/causal/instruments.py` (~590 LOC) | PASS |
| 3.2 | `TwoStageLeastSquaresEstimator` + bootstrap CI + Wu-Hausman + F-stat | `src/causal/iv_estimator.py` (~400 LOC) | PASS |
| 3.3 | `DoCalculusEngine` (Pearl-style do() over fixed DAG, MVP scope) | `src/causal/do_calculus.py` (~360 LOC) | PASS |
| 3.4 | `CounterfactualReplayer` + cold-tier adapter | `src/causal/counterfactual_replay.py` (~310 LOC) | PASS |
| 3.5 | Confidence-engine R10 gate (`causal_gating_enabled`) | `src/engine/confidence_engine.py` (+157 LOC) | PASS |
| 4   | Migration 030 (`causal_estimates`) | `docs/migrations/030_causal_estimates.sql` | PASS |
| 4   | Migration 031 (`instrumental_events`) | `docs/migrations/031_instrumental_events.sql` | PASS |
| 5   | 10 Prometheus metrics | `src/monitoring/metrics.py` (+~100 LOC) | PASS |
| -   | Daemon entrypoint | `src/causal/daemon.py` (~450 LOC) | PASS |
| -   | Module-run entry | `src/causal/__main__.py` | PASS |
| -   | systemd unit | `infra/systemd/polymarket-causal.service` | PASS |
| -   | Scheduler hook (04:00 UTC nightly) | `src/engine/main.py` | PASS |
| -   | R10 settings constants + validators | `src/config.py` | PASS |
| -   | Runtime config flag + bound | `src/control/runtime_config.py` | PASS |

### Component notes

**iv_estimator.py** — pure numpy, 2SLS via two stacked
`numpy.linalg.lstsq` calls. The Wu-Hausman test is implemented from
scratch using the standard Hausman statistic + chi²(1) survival
function; we use `math.erfc(sqrt(h/2))` rather than `scipy.stats.chi2`
to keep the import surface tight. Bootstrap CI uses a non-parametric
percentile bootstrap with row resampling; configurable
`bootstrap_n` (default 1000, test fixture default 100). All math
runs in numpy; no statsmodels dependency was added (verified
present in the env but we chose hand-rolled implementation per the
hard constraint #1 — keeps the dependency surface minimal and the
methodology audit's code-review target smaller).

**Statsmodels-or-not decision**: statsmodels IS installed (v0.14.2)
but we deliberately did NOT use it. The 2SLS implementation here
is ~150 LOC of clean numpy; bringing in statsmodels for a single
function would add a heavyweight dependency to the audit target.
The methodology audit will be easier with the hand-rolled
implementation — every linear-algebra step is visible. If a future
operator wants to cross-check the numbers, they can do so via
statsmodels.regression.linear_model.IV2SLS without changing the
production path.

**do_calculus.py** — MVP scope per the hard constraints. The DAG is
FIXED at the 4 spec § 2 nodes; the adjacency dict is hard-coded in
two module-level constants (`CAUSAL_DAG_NODES`, `CAUSAL_DAG_EDGES`).
We support `do(treatment=v)` for treatment in {leader_trade,
news_event, market_state} and query_var = follower_trade only.
Other queries raise `NotImplementedError` per the orchestrator's
hard constraint #8. The do() implementation uses graph mutilation +
discrete marginalisation over 2^|free_parents| combinations (at
most 2^3 = 8 for this DAG — runtime is constant). CPTs default to
a logistic-link parametrisation: `P(follower=1 | parents=p) =
sigmoid(b_0 + sum_i b_i * p_i)`. This is sufficient for the gate
to compare `do(leader=1) - do(leader=0)` and identify the
news-confounding case (when this difference collapses near zero
while OLS suggests a strong correlation).

**MVP do-calculus scope reference** (re-stated for the methodology
review): we do NOT implement Pearl's three inference rules, the
identifiability proof, or c-component analysis. Those belong to
research-grade causal inference work and are explicitly out of
scope per the orchestrator's hard constraint #8. The MVP path
supports exactly the gate's use case: estimate
P(follower | do(leader=v)) under the IV-adjusted leader → follower
coefficient. Extending beyond requires the methodology-audit gate.

**networkx**: present in the env (v3.3) but unused in the
production path. We hand-rolled the DAG as a 2-tuple of constant
node + edge lists. Reason: the DAG is FIXED and 4 nodes large; the
networkx layer would obscure what is already a static structure.

**instruments.py** — five detector classes, each implementing a
common `Detector` ABC. Persistence is centralised in the
`InstrumentRegistry._persist()` method (writes to
`instrumental_events`). Each detector class has a single async
`detect(asof_ts)` method; detectors that raise are logged + skipped
without breaking the others (test
`test_registry_tolerates_failing_detector` enforces this contract).

The `NewsEventDetector` accepts an injected `http_session` and is
operator-deliverable (the actual NewsAPI integration + NER pipeline
lives outside this code-pass). `FixtureNewsEventDetector` is the
test-friendly counterpart that reads from a JSON file. The
`OracleUpdateDetector` takes an injected `src.rpc.client.RPCClient`
mock-friendly handle and reads logs via `eth_getLogs`. The remaining
three (`RelatedMarketResolver`, `LeaderGasQuirkDetector`,
`APIOutageWindowDetector`) are pure-SQL on existing R6/R7 tables.

**counterfactual_replay.py** — reads from the cold-tier via the
R6 `DuckDBResearchView` (per hard constraint #9, we do NOT extend
the R6 module; the replayer constructs the view lazily and falls
back gracefully when the cold tier hasn't been populated). Three
replay variants (`replay_with_classifier_override`,
`replay_with_policy_disabled`, `replay_with_event_shift`) each
return a `ReplayResult` dataclass with the spec § 3.4 fields:
`hypothetical_pnl_usdc`, `delta_vs_actual`, `decisions_changed`,
`wall_time_s`. The 30-day-replay-under-5-min acceptance gate
(spec § 3.4) is achievable in principle (DuckDB scans Parquet
with predicate pushdown) but cannot be exercised in the test
suite (no real cold tier) — the unit tests assert the shape
contracts; the operator verifies the wall-time gate on the
production VM.

**daemon.py** — mirrors `FollowerVolumeDaemon` (R9) in shape. Per
pass:
  1. Load (leader, pool_class) pairs from `multivariate_hawkes_fits`
     (R9 table; falls back to the `leaders` table if R9 hasn't been
     deployed yet).
  2. For each pair: load trade streams, build (L, F, Z, X) matrices
     via histogram binning + time-of-day controls, run the 2SLS
     estimator, write to `causal_estimates`.
  3. Emit per-pair metrics (`iv_estimates_total`, `iv_first_stage_f`,
     `iv_wu_hausman_p`, `causal_ate_vs_hawkes_disagreement`,
     `causal_ate_excludes_zero_count`).

The matrix-construction logic (`_build_matrices`) is the place where
the methodology audit should spend most of its time — most
causal-inference mistakes hide in how you bin the event streams and
choose exogenous controls. We use 300-second bins (matching
`FOLLOWER_WINDOW_S`) and time-of-day sin/cos as exogenous controls.

**confidence_engine.py** — additive integration:
  * New method `_maybe_apply_causal_gate(wallet, trade_context)`
    returns either None (no-op, flag off OR DB error) or a dict
    `{result, follow_multiplier, ate, ci_low, ci_high, pool_class}`.
  * Called once per `evaluate()`, right after `_sample_thompson()`
    and BEFORE the R8 strategy weighting. The multiplier is applied
    to `thompson_follow` (NOT `thompson_fade` — the spec is explicit
    about gating the follow channel; FADE has its own gating).
  * The gate emits `confidence_engine_causal_gates_total{result}` on
    every consultation (defensive try/except keeps the import path
    happy on stripped envs).
  * **Regression-proof**: when `causal_gating_enabled=False`
    (default), the gate returns None unconditionally and the
    multiplier never gets applied. The test
    `test_flag_off_returns_none` enforces this byte-identical
    contract.
  * **DB-failure path**: when the DB read raises, we return None
    (fail-open, not fail-closed). Rationale documented in the
    method docstring: a transient DB outage should not silently
    degrade every signal. The gate is opt-in; failing-open
    preserves the pre-R10 behavior under infra issues.

---

## 3. Monte Carlo IV recovery (the load-bearing numerics)

Using the test fixture's confounded DGP (`gamma=0.8`, `delta=1.2`,
`beta=1.5`, n=5000, seed=42):

| Quantity | Value | Truth | Check |
|---|---|---|---|
| **2SLS ATE** | 1.4313 | 1.5 | rel_err 4.58% (PASS, gate < 5%) |
| **OLS coef** | 1.9625 | 1.5 | biased upward as expected (PASS) |
| **95% bootstrap CI** | [1.3633, 1.5091] | brackets 1.5 | PASS |
| **First-stage F** | 540.53 | > 10 | PASS (strong instruments) |
| **Wu-Hausman p** | 1.79e-14 | < 0.05 | PASS (correctly rejects OLS) |
| **Convergence** | converged | converged | PASS |

No-confounder control DGP (`gamma=0`, `delta=0`):

| Quantity | Value | Check |
|---|---|---|
| **2SLS ATE** | 1.4615 | recovers truth |
| **OLS coef** | 1.4642 | agrees with 2SLS |
| **Wu-Hausman p** | 0.9506 | correctly fails to reject OLS |

Both rows ship as automated tests
(`tests/test_causal/test_iv_estimator.py`). The 5% relative error
gate is enforced.

### Methodology caveats (per spec § 6)

The math is sound; the application has THREE places I flagged for
the external methodology audit:

1. **Time-of-day exogenous controls only**: `_build_matrices` adds
   sin(hour) and cos(hour) as controls. Real causal-inference work
   would also control for day-of-week, market-category dummies, and
   recent volatility. We deliberately ship the minimal control set
   so the audit gets a clear baseline before adding complexity.

2. **Bin width = 300 s (5 min)** is what we used for the histogram
   binning. The methodology audit should sweep over {60, 300, 900} s
   to confirm the ATE is robust to binning choice. If it isn't,
   the instrument's exogeneity assumption is leaking through the
   binning — a classic "garden of forking paths" error.

3. **Multiple-testing inflation**: the daemon estimates ATE for
   every (leader, pool) pair — easily ~800 hypotheses over the
   full top-200 leaders × 4 pool classes. We do NOT apply Bonferroni
   or BH correction in the production gate. The dashboard panel
   (operator-deliverable) is where the correction lives; the gate
   reads the corrected q-value once the dashboard surfaces it. See
   spec § 6 risk row.

---

## 4. Tests

Files under `tests/test_causal/` and `tests/test_engine/`:

| File | New tests |
|---|---|
| `test_causal/test_iv_estimator.py` | 19 (Monte Carlo recovery, Wu-Hausman significance, bootstrap CI, weak-instrument flagging, edge cases) |
| `test_causal/test_do_calculus.py` | 15 (DAG structure, do() queries, counterfactual, coefficient management) |
| `test_causal/test_instruments.py` | 16 (each detector + registry orchestration + failing detector tolerance) |
| `test_causal/test_counterfactual_replay.py` | 6 (each replay variant + cold-tier-missing + wall-time + dataclass shape) |
| `test_causal/test_daemon.py` | 3 (stop idempotent, empty-pair pass, start-then-stop) |
| `test_engine/test_confidence_causal_gate.py` | 8 (flag off regression, flag on each evidence shape, missing data, DB failure) |
| **Total new** | **67** |

Full suite: **1,370 passed**, 9 skipped, 2 xfailed. Baseline was
1,303 (post-R9 merge); R10 added 67 tests, net new exactly 67.

Engine/graph/follower_volume regression: 246 passed, 2 xfailed
(zero failures). No pre-R10 test broken.

---

## 5. Migrations

| Migration | Tables | Notes |
|---|---|---|
| 030 | `causal_estimates` | PK (leader_wallet, pool_class, estimated_at). Hawkes vs IV side-by-side fields per spec § 4. Indexes for "latest per pair" and "converged-only". |
| 031 | `instrumental_events` | PK on event_id (BIGSERIAL). 5 indexes on (time DESC) and (type, time DESC). VARCHAR(2000) for affected_market_ids; longer lists go in payload_json. |

Both follow the project's `BEGIN ... COMMIT` + `IF NOT EXISTS`
pattern (matches migrations 028/029 from R9). Rollback paths
documented in the SQL trailers (DROP TABLE CASCADE).

---

## 6. Prometheus metrics (10)

Spec § 5 contracts, all defensively declared in
`src/monitoring/metrics.py`:

| Metric | Type | Owner |
|---|---|---|
| `polybot_iv_estimates_total` | Counter (result) | daemon.py |
| `polybot_iv_first_stage_f` | Histogram | daemon.py |
| `polybot_iv_wu_hausman_p` | Histogram | daemon.py |
| `polybot_causal_ate_vs_hawkes_disagreement` | Gauge | daemon.py |
| `polybot_causal_ate_excludes_zero_count` | Counter (leader) | daemon.py |
| `polybot_instruments_detected_total` | Counter (event_type) | instruments.py (wire-ready) |
| `polybot_instrumental_event_lag_seconds` | Histogram (event_type) | instruments.py (wire-ready) |
| `polybot_counterfactual_replays_total` | Counter (kind) | counterfactual_replay.py (wire-ready) |
| `polybot_counterfactual_replay_duration_seconds` | Histogram | counterfactual_replay.py (wire-ready) |
| `polybot_confidence_engine_causal_gates_total` | Counter (result) | confidence_engine.py |

"wire-ready" = declared in metrics.py, ready for the runtime path to
`inc()` / `observe()`. The five `iv_*` and `causal_*` metrics are
exercised on every nightly fit pass via the daemon; the gate
counter is incremented on every `_maybe_apply_causal_gate` call.

---

## 7. Decisions reviewers should know

1. **Statsmodels NOT used** (it IS installed). Pure numpy
   implementation chosen so the methodology audit has a minimal
   code-review target. statsmodels.regression.linear_model.IV2SLS
   is the canonical cross-check; operators can sanity-check the
   numbers offline if needed.

2. **DAG structure is FIXED** per spec § 2 and hard constraint #7.
   The 4 nodes + 5 edges are module-level constants in
   `src.causal.do_calculus`. Operator-tuneable knobs are the
   *coefficients* (set via `set_iv_adjusted_estimate` /
   `set_observational_estimate`) and the marginals — the topology
   is not negotiable from outside the methodology audit.

3. **MVP do-calculus scope** per hard constraint #8. We support
   `do(treatment_var, treatment_value, follower_trade)` and the
   counterfactual `P(follower | do(treatment), evidence)`. Pearl's
   full three-rule machinery is OUT OF SCOPE for the production
   path. The audit doc flags this as a place future research can
   extend (and where misuse can hide if not careful).

4. **Bin width = 300 s** for the daemon's (L, F, Z) matrix
   construction. Configurable via `CausalDaemon(bin_seconds=...)`;
   defaults to `settings.CAUSAL_BIN_SECONDS = 300`.

5. **Bootstrap n = 1000 production, 100 in tests**. Configurable via
   `TwoStageLeastSquaresEstimator(bootstrap_n=...)`. The test fixture
   default of 100 makes the heaviest test (`test_recover_known_coefficient`)
   run in ~0.5 s; the production daemon uses 1000 which is the spec
   § 3.2 default.

6. **Failing-open on DB error** for the confidence gate. When the
   `causal_estimates` read raises, the gate returns None and the
   pre-R10 behavior takes over. Rationale: a transient DB outage
   should not silently downgrade every signal. The methodology
   audit may want to revisit this trade-off (fail-closed is the
   alternative — downgrade every signal until the DB is back).

7. **`pool_class` defaults to `'all_followers'`** when the leader's
   `wallet_strategy` field is missing from `trade_context`. This
   mirrors the R9 daemon's graceful-degradation pool name; the
   `causal_estimates` table also uses 'all_followers' when R8
   classification hasn't run.

8. **Counterfactual-replay performance unmeasured in tests**. The
   30-day-replay < 5 min acceptance gate (spec § 3.4) requires a
   populated cold tier to verify; the test suite asserts the
   shape contracts only. Operator verification is mandatory before
   advertising the replay performance.

9. **Five instrument types ship, two are operator-deliverable**:
   * `news` (via NewsEventDetector + NewsAPI) — operator wires
     NewsAPI subscription + NER pipeline; FixtureNewsEventDetector
     covers tests.
   * `oracle_update` (via OracleUpdateDetector + RPC) — operator
     wires the oracle contract address.
   * `news` from `related_market` source — pure SQL, ships now.
   * `gas_quirk` — pure SQL on mempool_observations, ships now.
   * `api_outage` — pure SQL on coverage_reconciler-equivalent
     trades_observed bucketing, ships now.
   The "funding event" instrument from spec § 2.1 is NOT
   implemented — it's a future operator-deliverable. The
   `event_type='funding'` value is reserved in the migration for
   forward compatibility.

10. **The R10 daemon depends on R9 fits**. `_load_pairs` reads from
    `multivariate_hawkes_fits` (R9 table); the daemon graceful-
    degrades to a `leaders`-table fallback if R9 hasn't run. This
    matches the spec § 7 dependency ordering: deploy R9 first, let
    it run 7 nights, then deploy R10.

---

## 8. The CRITICAL methodology-audit gate

Per spec § 6 risk row "Causal inference math is harder than we
think — severity High":

> The math is sound; the **application** is hard. Plan in a 1-week
> external-reviewer pass on the methodology before deploying.

Before flipping `causal_gating_enabled=true` in production, the
operator MUST:

1. Hire / contract a causal-inference expert for ~1 week.
2. Hand them this audit doc + the spec + `src/causal/*` source.
3. Have them validate (in order):
   - Each instrument's exogeneity assumption against this exact
     Polymarket data (the spec § 2.1 reasoning is plausible but
     not proven for our specific leaders).
   - The DGP assumption underlying the 2SLS recovery test (the
     test simulates a linear DGP; real Polymarket data has
     non-linearities).
   - The choice of exogenous controls (`time_of_day` sin/cos only;
     no day-of-week, no market-category dummies, no recent
     volatility).
   - Multiple-testing correction policy across the (leader, pool)
     grid.
   - The MVP do-calculus scope vs the full Pearl algorithm.
4. Address feedback OR document the disagreement explicitly.
5. THEN flip the flag in shadow mode (log gate decisions but don't
   apply them) for 30 days.
6. THEN run the A/B Sharpe + max-drawdown soak (60 days) per spec
   § 6 acceptance criterion.
7. THEN consider the R10 gate "live".

The flag default of False is the spec's explicit safety position.
Do NOT change the default without methodology audit sign-off.

---

## 9. Files delivered

### New files

* `src/causal/__init__.py` (101 LOC)
* `src/causal/__main__.py` (15 LOC)
* `src/causal/iv_estimator.py` (374 LOC)
* `src/causal/iv_diagnostics.py` (164 LOC) — split out per the
  500-LOC project limit; holds `first_stage_f_stat`, `wu_hausman_test`,
  and shared OLS helpers.
* `src/causal/do_calculus.py` (396 LOC)
* `src/causal/instruments_base.py` (84 LOC) — `Detector` ABC +
  `InstrumentalEvent` dataclass (shared base types).
* `src/causal/instruments.py` (413 LOC) — News + Oracle detectors +
  `InstrumentRegistry`.
* `src/causal/instruments_sql.py` (306 LOC) — pure-SQL detectors:
  RelatedMarket, LeaderGasQuirk, APIOutage.
* `src/causal/counterfactual_replay.py` (350 LOC)
* `src/causal/daemon.py` (497 LOC)
* `src/causal/daemon_matrices.py` (104 LOC) — bin/build helper for
  the daemon's matrix construction; split out per the 500-LOC limit
  AND so the methodology audit reviewer has a clean small target
  for the most error-prone part of the application.
* `docs/migrations/030_causal_estimates.sql`
* `docs/migrations/031_instrumental_events.sql`
* `infra/systemd/polymarket-causal.service`
* `tests/test_causal/__init__.py`
* `tests/test_causal/test_iv_estimator.py` (19 tests)
* `tests/test_causal/test_do_calculus.py` (15 tests)
* `tests/test_causal/test_instruments.py` (16 tests)
* `tests/test_causal/test_counterfactual_replay.py` (6 tests)
* `tests/test_causal/test_daemon.py` (3 tests)
* `tests/test_engine/test_confidence_causal_gate.py` (8 tests)
* `docs/audit/phase3/round10_final_review.md` (this file)

### Modified files (surgical edits, additive only)

* `src/engine/confidence_engine.py` — added
  `_maybe_apply_causal_gate()` + `_inc_causal_gate_metric()`; wired
  the gate call after Thompson sampling, before R8 strategy
  weighting. Net +157 LOC.
* `src/engine/main.py` — registered `causal_nightly` cron at
  `CAUSAL_DAEMON_BATCH_HOUR_UTC` (04:00 UTC default). Graceful
  degrade if the daemon module is unimportable.
* `src/control/runtime_config.py` — registered
  `causal_gating_enabled` in `ALLOWED_KEYS`, `BOUNDS`,
  `BOOLEAN_KEYS`, and `_defaults_from_settings`.
* `src/monitoring/metrics.py` — appended R10 metric block (10
  metrics, all defensively declared).
* `src/config.py` — added 6 R10 constants + 4 validators.
* `infra/systemd/README.md` — added `polymarket-causal.service`
  row; bumped total memory budget; documented the systemd-or-cron
  alternative deployment.

---

## 10. North-star check

> Round 10 makes the bot trade on causation, not correlation —
> instrumental variables + do-calculus + counterfactual replay tell
> us when a confirmed Hawkes edge is real and when it's news
> leaking through both leader and followers, so the
> volume_anticipation policy stops firing on chimeras.

Code layer delivers this. Operator-only gates remain (chiefly the
methodology audit). The gate stays OFF until those gates close.
