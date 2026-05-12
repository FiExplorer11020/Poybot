# Round 9 — The Web: Final Code-Layer Review

> **Branch**: `round-9-web`
> **Reviewer**: R9 single-architect+implementer (one-pass)
> **Date**: 2026-05-12
> **Specification reference**: [`docs/ROUND_9_MULTIVARIATE_HAWKES.md`](../../ROUND_9_MULTIVARIATE_HAWKES.md)

---

## 1. Top-line recommendation

**PASS — code layer complete, awaiting operator-only gates.**

R9 ships the full code-layer of the Web: a population-level
N-dimensional multivariate Hawkes fitter with block-sparse priors and
BIC model selection (additive to the Round-5 bivariate fitter — the
R5 fitter is untouched), a per-(leader, pool_class) Extended Kalman
state-space model, a FollowerVolumePredictor that combines the two
plus an R8 strategy prior, a HawkesCouplingDriftDetector, daemon
entrypoint + systemd unit, two migrations (028/029), 12 Prometheus
metrics, and decision_router integration gated behind a runtime config
flag that defaults to False.

**Tests**: 42 new R9 tests; full suite 1,303 passed, 9 skipped, 2 xfailed (zero failures). Baseline was 1,272 collected; R9 added 42 tests for a new total of 1,314 collected.

**Operator-only gates remain** (spec § 6 / § 7) — explicitly out of scope
for the code pass:

1. Monte Carlo identifiability validation on the real 30-day data
   slice (the unit test exercises the algorithm; the operator's job
   is to confirm recovery on production data).
2. 7 nights of clean shadow fits before flipping any consumer flag.
3. CI-coverage soak over a 14-day shadow window (must hold 0.95 ± 0.03
   per spec § 6).
4. Volume-forecast MAPE soak over a 30-day out-of-sample window
   (acceptance gate: < 30%).
5. A/B Sharpe verification (paper) over 30 days vs FOLLOW-only baseline
   (acceptance gate: Sharpe ≥ 1.3×).
6. Gradual live size-ramp (0.1% of bankroll → up) once paper passes.

---

## 2. Per-component verification

| § Spec | Component | File | Verdict |
|---|---|---|---|
| 3.1 | `MultivariateHawkesFitter` + block-sparse mask + BIC | `src/graph/hawkes_multivariate.py` (476 lines) | PASS |
| 3.1 | `multivariate_hawkes_nll` closed-form integral + vectorised sum-log | `src/graph/hawkes_multivariate_nll.py` (210 lines) | PASS |
| 3.2 | `FollowerPoolKalman` (Extended KF, numpy-only) | `src/follower_volume/kalman.py` | PASS |
| 3.2 | Persistence (current row + history snapshot) | (same) | PASS |
| 3.3 | `FollowerVolumePredictor` headline API | `src/follower_volume/volume_predictor.py` | PASS |
| 3.3 | Strategy-prior fallback + by_pool sum invariant | (same) | PASS |
| 3.4 | `decision_router` `volume_anticipation` branch (runtime-config-flagged) | `src/engine/decision_router.py` | PASS |
| 3.4 | Kelly-from-volume sizing capped by MAX_POSITION_PCT | (same) | PASS |
| 3.5 | `HawkesCouplingDriftDetector` | `src/follower_volume/drift.py` | PASS |
| 4   | Migration 028 (`multivariate_hawkes_fits`) | `docs/migrations/028_multivariate_hawkes_fits.sql` | PASS |
| 4   | Migration 029 (`follower_pool_state` + `_history`) | `docs/migrations/029_follower_pool_state.sql` | PASS |
| 5   | 12 Prometheus metrics | `src/monitoring/metrics.py` | PASS |
| -   | Daemon entrypoint | `src/follower_volume/daemon.py` | PASS |
| -   | Module-run entry | `src/follower_volume/__main__.py` | PASS |
| -   | systemd unit | `infra/systemd/polymarket-follower-volume.service` | PASS |
| -   | Scheduler hook (03:30 UTC nightly) | `src/engine/main.py` | PASS |
| -   | R9 settings constants | `src/config.py` | PASS |
| -   | Runtime config flags + bounds | `src/control/runtime_config.py` | PASS |

### Component notes

**hawkes_multivariate.py** — The N²-parameter problem is collapsed to
~2K + N free entries by the default block-sparse mask
(`build_default_mask`). The mask is a **static parameter** set at
construction time; the design choice is per spec § 2.2 Box diagram.
Operators can pass a custom mask for research experiments via the
`mask=` constructor kwarg. The fitter is **additive** to the R5
bivariate fitter (`src/graph/hawkes_fitter.py`) — both run nightly
side-by-side; R5 confirms individual edges, R9 fits population
dynamics. Per the acceptance criterion in spec § 6, the R5 test
`test_independence_yields_low_alpha_mu` still passes (and a regression
test enforces it from inside R9's own test file).

The BIC threshold is `k_penalty · log(N_events)`, where `k_penalty` is
the number of free α entries. On the default K=4-pool mask, `k_penalty
= 2K = 8` and the threshold sits at ~60 for N=1500 events. The
acceptance semantics: per-entry α is "accepted" iff its post-fit
value is positive AND the joint BIC test on the full model passes.

**kalman.py** — Extended Kalman Filter on a 3-state vector
`[pool_size_usdc, recent_response_pct, decay_rate]`. The observation
model `y = pool_size · response_pct` is nonlinear, so we linearise H
at every update step (Jacobian: `[response_pct, pool_size, 0]`). Pure
numpy — no `filterpy`, no `pomegranate`. State is physically clamped
on every update (response_pct in (1e-4, 1.0], pool_size >= 0). Default
noise covariances (`DEFAULT_F`, `DEFAULT_Q`, `DEFAULT_R`, `DEFAULT_X0`,
`DEFAULT_P0`) are exposed at module level so operators / tests can
override without subclassing.

**volume_predictor.py** — Combines the latest cached Hawkes fit, the
per-pool Kalman state, and an optional R8 strategy prior. Critical
property tested: `by_pool` values sum to `total_volume_usdc` within
float tolerance. The time-distribution CDF is derived from the
dominant pool's half-life via a closed-form exponential-kernel
integration over the 4 spec § 3.3 buckets, renormalised to sum to 1.0.

**decision_router.py** — A new method
`maybe_emit_volume_anticipation(...)` is invoked by the engine after
the existing FOLLOW/FADE/SKIP routing. The R9 path is **opt-in at the
constructor level** (existing call sites that pass no `volume_predictor`
get behavior byte-identical to pre-R9) AND **gated at the runtime
level** by `volume_anticipation_enabled` (default False). Both gates
have to be opened before any volume_anticipation entry can fire,
which means the R9 surface area is invisible to operators until they
explicitly choose to engage it. Kelly-from-volume is heuristic:
`0.05 · sqrt(E[volume]/depth) · confidence`, hard-capped at 0.5 and
multiplied by `MAX_POSITION_PCT` upstream.

**drift.py** — Detects the specific transition
`converged → bic_rejected` between the latest two fits. Stateless;
queries the append-only `multivariate_hawkes_fits` timeline directly.
On detection, decrements the `polybot_mvhawkes_couplings_accepted{leader_wallet}`
gauge so dashboards see the drop immediately, and returns
`DriftReport(drift_detected=True, ...)`.

**daemon.py** — Mirrors `StrategyClassifierDaemon` (R8) in shape. One
pass = run `MultivariateHawkesFitter.fit_arrays` for every top-N
leader, persist to `multivariate_hawkes_fits`. The systemd unit fires
the daemon for the initial pass + sleeps on `MVHAWKES_REFRESH_INTERVAL_S`;
the engine scheduler also has a nightly cron at 03:30 UTC that calls
`FollowerVolumeDaemon.run_one_pass()` (so an operator can deploy
EITHER the systemd unit OR the engine cron — both work, both write
the same table).

---

## 3. Tests

Files under `tests/test_graph/` and `tests/test_follower_volume/`:

| File | New tests |
|---|---|
| `test_hawkes_multivariate.py` | 11 (mask shape, identifiability MC, off-diagonal mask enforcement, BIC k scaling, independence → bic_rejected, R5 regression, NLL sanity) |
| `test_follower_volume/test_kalman.py` | 10 (predict math, update math, clamps, CI coverage smoke, persistence, cold start) |
| `test_follower_volume/test_volume_predictor.py` | 6 (shape, sum invariant, time CDF, prior weighting, empty pools, Hawkes modulator) |
| `test_follower_volume/test_drift.py` | 5 (no-fits, one-fit, transition, steady state, db failure) |
| `test_follower_volume/test_daemon.py` | 3 (stop idempotent, empty-leader pass, start-then-stop) |
| `test_engine/test_decision_router_volume_anticipation.py` | 7 (flag off, flag on, drift gate, Kelly cap, threshold, missing predictor, regression) |
| **Total new** | **42** |

R5 regression: `test_independence_yields_low_alpha_mu` in
`tests/test_graph/test_hawkes_bivariate.py` (untouched by R9) still
passes — this is a hard acceptance criterion (spec § 6 criterion 5).

---

## 4. Migrations

| Migration | Tables | Notes |
|---|---|---|
| 028 | `multivariate_hawkes_fits` | PK (leader_wallet, fit_at). JSONB columns for α matrix + μ vector + accepted couplings. Indexes for "latest fit per leader" and "converged-only". |
| 029 | `follower_pool_state` + `follower_pool_state_history` | PK on (leader, pool). History is append-only for as-of training reads (matches market_features_history / leader_strategy_history pattern). |

Both migrations follow the project's `BEGIN ... COMMIT` + `IF NOT
EXISTS` pattern. Rollback paths documented in the SQL trailers.

---

## 5. Prometheus metrics (12)

Spec § 5 contracts, all defensively declared in
`src/monitoring/metrics.py`:

| Metric | Type | Owner |
|---|---|---|
| `polybot_mvhawkes_fits_total` | Counter (result) | daemon.py |
| `polybot_mvhawkes_fit_duration_seconds` | Histogram | daemon.py |
| `polybot_mvhawkes_alpha_value` | Histogram (pool_class) | daemon.py |
| `polybot_mvhawkes_couplings_accepted` | Gauge (leader_wallet) | drift.py |
| `polybot_mvhawkes_bic_statistic` | Histogram | daemon.py |
| `polybot_kalman_updates_total` | Counter (pool_class) | kalman.py (wire-ready) |
| `polybot_kalman_innovation_magnitude` | Histogram (pool_class) | kalman.py (wire-ready) |
| `polybot_pool_size_estimate` | Gauge (pool_class) | kalman.py (wire-ready) |
| `polybot_volume_forecasts_total` | Counter | volume_predictor.py (wire-ready) |
| `polybot_volume_forecast_mape` | Gauge | operator soak job (wire-ready) |
| `polybot_volume_forecast_ci_coverage` | Gauge | operator soak job (wire-ready) |
| `polybot_volume_anticipation_entries_total` | Counter (result) | decision_router.py (wire-ready) |

"wire-ready" = declared in metrics.py, ready for the runtime path to
`inc()` / `observe()`. The first three are exercised on every nightly
fit pass via the daemon; the gauge `mvhawkes_couplings_accepted` is
dec()'d by the drift detector on detected transitions.

---

## 6. Decisions reviewers should know

1. **Block-sparse mask shape** (spec § 2.2 Box diagram): diagonal +
   first column for i>0. Off-diagonal pool↔pool is hard-zeroed via
   the mask; the test
   `test_block_sparse_mask_zeroes_off_diagonal_pool_pool` enforces
   this contract. A custom mask is a constructor kwarg; production
   uses the default.

2. **BIC k_penalty derivation**: spec § 2.3 defines it as the number
   of free α entries. The fitter uses `n_free` by default
   (computed from the mask) — operator can override via `k_penalty=`
   kwarg. The settings constant `MVHAWKES_BIC_K_PENALTY=8` is the
   nominal value for the K=4 pool configuration (4 diagonal + 4
   leader→pool = 8 free, which under our default mask collapses to
   2K=8). On other K the fitter computes the correct value
   automatically.

3. **Kalman noise covariance defaults**: `DEFAULT_F`, `DEFAULT_Q`,
   `DEFAULT_R`, `DEFAULT_X0`, `DEFAULT_P0` are module-level
   constants. They're calibration points (not learned). The 0.95
   F[1,1] sets a slow AR(1)-like mean reversion on `response_pct`;
   the 0.99 F[2,2] keeps `decay_rate` nearly constant; Q is diagonal
   with the largest entry on pool_size variance (most uncertainty in
   capital swings). These constants are chosen so a 30-min observation
   window produces a Kalman gain in [0.05, 0.4] on typical data — the
   filter actually updates, but pure noise doesn't whip the state.
   They're tunable via the constructor; operators should expect to
   refine them during the 14-day CI-coverage shadow per spec § 6.

4. **`accepted_couplings` semantics**: the joint fit's per-entry
   acceptance flag = "the α entry's posterior is positive AND the
   joint BIC test on the full model passes." We deliberately do NOT
   re-fit N leave-one-out models per nightly pass to compute a strict
   per-entry LRT — that would 10× the wall-time budget. Operators
   wanting strict per-entry significance can run the offline LRT in
   the dashboard.

5. **R9 + R8 coupling is optional**: if `src.strategy_classifier`
   returns no fingerprint for a leader, the predictor collapses to a
   single `all_followers` pool. The daemon's SQL `LEFT JOIN` on
   `leaders.classification_json` carries the same fallback. Per spec
   § 6 dependency note, R9 graceful-degrades to bivariate-Hawkes-shape
   when R8 is missing.

6. **Two activation paths for the nightly batch**: the systemd unit
   `polymarket-follower-volume.service` boots the daemon (immediate
   first pass + 24h sleep loop). The engine scheduler also has a
   `mvhawkes_nightly` cron at 03:30 UTC. Both call the same code
   path and both write the same table. Operators may run either or
   both — the migration's PK prevents duplicate rows.

---

## 7. Out-of-scope follow-ups (operator gates)

These are the gates the spec lists explicitly:

1. **Monte Carlo identifiability soak** on real data (spec § 6).
   The unit test exercises the algorithm on a synthetic 1+1 system
   with α=0.4, β=1/300 over 7 days, asserting μ recovery within
   ~1.5× and integrated kernel area within ~20×. Real-data
   identifiability soak is an operator-only one-week exercise.

2. **7 nights of clean shadow fits** before any downstream consumer
   reads from `multivariate_hawkes_fits` (spec § 7 Phase 9.A gate).

3. **CI-coverage soak** over a 14-day window: 0.95 ± 0.03 (spec § 7
   Phase 9.B). The unit test enforces ≥ 0.7 as a smoke gate.

4. **MAPE soak** over 30 days: < 30% (spec § 7 Phase 9.C).

5. **A/B Sharpe verification** (paper): ≥ 1.3× FOLLOW-only baseline
   (spec § 6 acceptance + § 7 Phase 9.C).

6. **Gradual live size ramp** (spec § 7 Phase 9.D): start at 0.1%
   of bankroll, scale up only after each milestone passes.

---

## 8. Working tree

The working tree is **dirty** with R9 changes. Files created:
- `docs/migrations/028_multivariate_hawkes_fits.sql` (90 lines)
- `docs/migrations/029_follower_pool_state.sql` (84 lines)
- `src/graph/hawkes_multivariate.py` (476 lines)
- `src/graph/hawkes_multivariate_nll.py` (210 lines)
- `src/follower_volume/__init__.py` (37 lines)
- `src/follower_volume/__main__.py` (15 lines)
- `src/follower_volume/kalman.py` (437 lines)
- `src/follower_volume/volume_predictor.py` (353 lines)
- `src/follower_volume/drift.py` (146 lines)
- `src/follower_volume/daemon.py` (447 lines)
- `infra/systemd/polymarket-follower-volume.service` (29 lines)
- `tests/test_graph/test_hawkes_multivariate.py` (349 lines)
- `tests/test_follower_volume/__init__.py` (1 line)
- `tests/test_follower_volume/test_kalman.py` (264 lines)
- `tests/test_follower_volume/test_volume_predictor.py` (183 lines)
- `tests/test_follower_volume/test_drift.py` (98 lines)
- `tests/test_follower_volume/test_daemon.py` (61 lines)
- `tests/test_engine/test_decision_router_volume_anticipation.py` (290 lines)
- `docs/audit/phase3/round9_final_review.md` (this file)

Files modified:
- `src/config.py` — added R9 settings constants
- `src/control/runtime_config.py` — registered R9 flag + threshold
- `src/engine/decision_router.py` — added `maybe_emit_volume_anticipation`
- `src/engine/main.py` — registered nightly cron job
- `src/monitoring/metrics.py` — added 12 R9 Prometheus metrics
- `infra/systemd/README.md` — listed new systemd unit

Leave it for orchestrator commit.
