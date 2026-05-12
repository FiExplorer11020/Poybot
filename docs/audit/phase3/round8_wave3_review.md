# Round 8 — The Lens: Wave-3 Independent Review

> **Reviewer**: Wave-3 independent reviewer (separate session from R8 architect)
> **Date**: 2026-05-12
> **Repo state**: `main` @ `26ec6c2` (R8 originally tagged `v0.8.0` at `6ad40c5`)
> **Spec reference**: `docs/ROUND_8_STRATEGY_CLASSIFIER.md` §§ 1-10
> **Architect audit**: `docs/audit/phase3/round8_final_review.md` (PASS)
> **Exclusive scope edits**: `src/strategy_classifier/*` (excluding `features.py`),
> `tests/test_strategy_classifier/test_*_hardening.py` (new files),
> this document.

---

## 1. Top-line verdict

**PASS — code layer is impeccable for v1 with one latent column-reorder
bug fixed in this pass.**

The architect pass shipped R8 cleanly. This Wave-3 audit drilled into
the math, edge cases, and operator-cold-start paths and uncovered ONE
real bug (column reorder in `predict_proba` crashes when the trained
LightGBM model saw fewer than all 9 classes — a near-certain scenario
during the first weeks of the labelling sprint). Fixed in this pass,
covered by a hardening test, full suite unaffected.

44 new hardening tests now lock in the math, edge cases, and the bug
fix. Full suite stays green at 1,861 passed (well above the 1,608
guardrail).

The operator gates (hand-labelling sprint, A/B Sharpe, flipping the
runtime flag) are unchanged from the architect audit — out of scope
for code review.

---

## 2. Per-component verification matrix

| § Spec | Component                              | File                                            | Verdict          | Notes                                                               |
|--------|----------------------------------------|-------------------------------------------------|------------------|---------------------------------------------------------------------|
| 3.1    | LeaderFeatureExtractor (FORBIDDEN)     | `src/strategy_classifier/features.py`           | PASS (read-only) | 45 features (FEATURE_COUNT) matching spec § 3.1 A-I categories.    |
| 3.2    | StrategyLabelStore + κ                 | `src/strategy_classifier/labeling/label_store.py` | PASS           | κ math verified against sklearn.cohen_kappa_score (test_kappa_matches_sklearn). |
| 3.2    | Operator protocol doc                  | `src/strategy_classifier/labeling/labeling_protocol.md` | PASS     | Spec § 3.2 + § 7.A walked end-to-end. No changes.                   |
| 3.3    | StrategyClassifier + isotonic + dummy  | `src/strategy_classifier/model.py`              | PASS (after fix) | Column-reorder bug FIXED in this pass (n_classes < 9 case).        |
| 3.3    | STRATEGY_WEIGHTS defaults              | `src/strategy_classifier/model.py`              | PASS             | All 9 classes × {follow, fade, skip}, all non-negative, no NaNs.    |
| 3.4    | UnsupervisedStrategyExplorer           | `src/strategy_classifier/cluster.py`            | PASS             | K-means deterministic with seed; DBSCAN noise -1 surfaced. NaN-warning suppressed for the all-NaN-column path. |
| 3.5    | StrategyDriftDetector + JS divergence  | `src/strategy_classifier/drift.py`              | PASS             | JS(log_2) bounded [0,1] across Monte-Carlo. Cold-start exclusive boundary verified. |
| 3.6    | confidence_engine integration (FORBIDDEN) | `src/engine/confidence_engine.py`            | PASS (read-only) | Flag default OFF; no fingerprint = no-op; tested by `test_engine_integration.py`. |
| 4      | Migration 026 (labels + history)       | `docs/migrations/026_strategy_labels_and_history.sql` | PASS     | CHECK constraint matches STRATEGY_CLASSES exactly. Indexes optimal for hot paths. |
| 4      | Migration 027 (classification_json)    | `docs/migrations/027_leaders_classification_json.sql` | PASS     | Partial GIN index correctly scoped. No DB-side CHECK on JSONB (correct: hot-path UPDATE). |
| 5      | 10 Prometheus metrics (FORBIDDEN)      | `src/monitoring/metrics.py`                     | PASS (read-only) | All 10 metrics present, daemon wires the hot-traffic ones.          |
| -      | Daemon entrypoint                      | `src/strategy_classifier/daemon.py`             | PASS             | Graceful cancel verified. Resilient to feature-extractor failure (hardening test). |
| -      | Module-run entry                       | `src/strategy_classifier/__main__.py`           | PASS             | Defers to `daemon.main()`.                                          |
| -      | Package exports                        | `src/strategy_classifier/__init__.py`           | PASS             | Public surface matches spec; internal helpers not re-exported.       |

---

## 3. Spec § 6 acceptance criteria checklist

| Criterion                                                              | Code-layer status | Operator status         |
|-----------------------------------------------------------------------|-------------------|-------------------------|
| Cohen's κ ≥ 0.7 between two labellers on 20-wallet validation set     | PASS (math)       | PENDING (sprint)        |
| Held-out val accuracy ≥ 75 % overall, ≥ 60 % minority classes         | N/A (no model)    | PENDING (training)      |
| Per-class Brier score ≤ 0.15                                          | N/A (no model)    | PENDING (training)      |
| Confidence-engine A/B Sharpe ≥ 1.2× baseline                          | N/A (no live data)| PENDING (paper backtest)|
| Drift detector fires for ≥ 5 wallets in 90-day backwards validation   | N/A (no model)    | PENDING (backtest)      |
| **JS divergence math correctness (Wave-3 extra)**                     | **PASS**          | -                       |
| **Cohen's κ math correctness (Wave-3 extra)**                         | **PASS** (vs sklearn) | -                   |
| **Column-reorder bug after partial-class training (Wave-3 extra)**    | **PASS** (fixed)  | -                       |

---

## 4. Findings

### 4.1 In-scope findings + fixes applied

#### F1. **BUG: predict_proba crashes when LightGBM was trained on a STRICT SUBSET of STRATEGY_CLASSES.** (severity: HIGH)

`src/strategy_classifier/model.py:259-261` (pre-fix):

```python
if self._lgb_classes is not None and tuple(self._lgb_classes) != STRATEGY_CLASSES:
    col_idx = [self._lgb_classes.index(c) for c in STRATEGY_CLASSES]
    probs = probs[:, col_idx]
```

When the labelled training set does NOT contain every class (a near-
certain scenario for the first weeks of the spec § 7.A labelling
sprint — info_leak and arb_3way are rare), `self._lgb_classes` is a
tuple of the K' < 9 classes actually seen. Calling `.index(c)` for a
class NOT in that tuple raises `ValueError: tuple.index(x): x not in
tuple`, breaking every single prediction.

The architect tests covered the 9-class case but not the partial-class
case. Hardening test `test_lightgbm_columns_aligned_to_spec_order`
in `tests/test_strategy_classifier/test_model_hardening.py` reproduces
the failure.

**Fix applied** (`src/strategy_classifier/model.py:255-280`):

```python
if self._lgb_classes is not None and tuple(self._lgb_classes) != STRATEGY_CLASSES:
    aligned = np.zeros((probs.shape[0], k), dtype=float)
    lgb_idx = {c: i for i, c in enumerate(self._lgb_classes)}
    for j, cls in enumerate(STRATEGY_CLASSES):
        src = lgb_idx.get(cls)
        if src is not None:
            aligned[:, j] = probs[:, src]
    probs = aligned
```

Missing-class columns are zero-filled (probability 0 for unseen classes,
which is mathematically correct — the model never saw them). The
defensive renormalisation downstream still ensures rows sum to 1 when
mass is on the seen classes. Verified by the new test.

#### F2. **NIT: noisy RuntimeWarning in `cluster.py` when a feature column is entirely NaN.** (severity: LOW)

`src/strategy_classifier/cluster.py:95` (pre-fix) emitted
`RuntimeWarning: All-NaN slice encountered` whenever `nanmedian` saw
an all-NaN column (legit cold-start scenario: feature G social fields
when R12 hasn't wired yet). The code handled the result correctly via
the `np.where(np.isnan(col_medians), 0.0, ...)` line below, but the
warning bled into test logs.

**Fix applied** (`src/strategy_classifier/cluster.py:93-101`): wrap
the `nanmedian` call in `warnings.catch_warnings()` with a targeted
filter. Behaviour unchanged; logs are cleaner.

#### F3. **STYLE: ruff auto-fixable imports** (severity: TRIVIAL)

`python -m ruff check src/strategy_classifier/ --select F401,I001 --fix`
removed three unused imports (typing.Any in cluster.py, STRATEGY_CLASSES
import in daemon.py, one blank line in __main__.py). No behavioural
change.

Remaining ruff warnings (`N803`/`N806` "X should be lowercase",
`E501` line-too-long) follow project ML/scikit conventions and are
NOT fixed — they would create inconsistency with the rest of the
codebase (X for feature matrix is universal in ML).

### 4.2 Cross-cutting findings (forbidden-files bugs)

**None found.** The four forbidden files
(`features.py`, `confidence_engine.py`, `runtime_config.py`,
`metrics.py`, `config.py`) were read end-to-end as part of the audit;
no math, edge-case, or correctness issues surfaced that warrant a
patch.

The architect audit's notes on these files match my reading:
- `features.py` correctly emits `np.nan` for unwired R9/R10/R11/R12
  slots; the model and cluster paths handle NaN correctly (LightGBM
  natively, cluster via median imputation).
- `confidence_engine.py`'s `_maybe_get_strategy_weights` is a clean
  guard — flag-off path is identity, missing-fingerprint path is
  identity, unknown-class path is identity.
- `runtime_config.py`'s BOOLEAN_KEYS coercion is correctly tested by
  the engine-integration suite.
- `metrics.py` defensive `try/except` around each declaration
  matches the R6/R7 pattern.

### 4.3 Operator-action-required findings

These remain unchanged from the architect audit. Repeated here for
completeness:

1. **Hand-labelling sprint (§ 7.A)** — 100 (wallet, window) labels
   stratified across 9 classes (≥ 5 per class).
2. **Cohen's κ measurement** on the 20-wallet validation pair —
   gate is κ ≥ 0.7.
3. **Model training** — operator runs the (TBD)
   `batch_labeler.ipynb` to fit LightGBM, save to
   `models/strategy_classifier.pkl`.
4. **Shadow-phase audit (§ 7.D)** — top-100 wallets manually
   reviewed for 1-2 weeks.
5. **A/B Sharpe verification (§ 7.E)** — 30-day paper backtest
   with the flag enabled on half decisions.
6. **Flipping `strategy_conditional_confidence_enabled=true`** —
   dashboard POST /api/risk/update.

---

## 5. Hardening tests added

All 44 hardening tests live in five new files. None touch DB I/O —
they all mock the connection.

| File                                            | Tests | Coverage                                                                                                |
|-------------------------------------------------|-------|---------------------------------------------------------------------------------------------------------|
| `tests/test_strategy_classifier/test_drift_hardening.py` | 13 | JS divergence numerical stability (very-skewed, zero bins, all-zero, negative inputs); JS bounded in [0,1] Monte-Carlo (5 seeds); reference value JS(U_9, e_0) ≈ 0.74 for tuning; cold-start boundary (exactly N samples evaluates, N-1 cold-starts); malformed baseline rows ignored. |
| `tests/test_strategy_classifier/test_label_store_hardening.py` | 7 | Cohen's κ on perfect-disagreement (κ=0); κ matches sklearn on mixed dataset; off-taxonomy labels skipped not crash; n_overlap=1 returns nan; `label_set_size` includes zero-classes; LabelRow rejects bad secondary_strategy; confidence=0.0 boundary accepted. |
| `tests/test_strategy_classifier/test_model_hardening.py` | 12 | `_lightgbm_available` returns bool, matches importlib; dummy classifier save/load round-trip; load nonexistent raises; predict_one 1D vs 2D identical; classification_json_patch values rounded to 4 dp; to_history_row schema matches migration 026; None drift_js_divergence preserved; **column-reorder bug fix verified** (subset-trained LightGBM aligns to spec order); STRATEGY_WEIGHTS all numeric/non-negative; structural_bot skip >= 5. |
| `tests/test_strategy_classifier/test_cluster_hardening.py` | 8 | K-means deterministic with seed (regression on random_state plumbing); all-NaN column imputed to 0; n < n_clusters_kmeans clamps k; single-sample dataset; min_size filter returns []; high-confidence filter returns []; sample_wallet_indices capped to 10; surface before fit raises. |
| `tests/test_strategy_classifier/test_daemon_hardening.py` | 4 | Wrong-shape feature vector silently skipped (does not crash pass); extractor that raises continues to next wallet; default model path falls back to dummy when file missing; `stop()` idempotent. |

---

## 6. Test counts

| Suite                                    | Before Wave-3 | After Wave-3 | Delta |
|------------------------------------------|---------------|--------------|-------|
| `tests/test_strategy_classifier/`        | 72 + 1 skip   | 116 + 1 skip | **+44 passing** |
| Full project test suite                  | 1,608 passed* | 1,861 passed | (other reviewers contribute the rest) |

\* The 1,608 figure is the prompt's stated guardrail. The 1,861 final
includes hardening contributions from the parallel Wave-3 reviewers
(R9-R13) and matches the post-Wave-3 expected state.

Zero failures, 9 pre-existing skips (R8's own LightGBM-fallback skip
when LightGBM IS installed), 2 pre-existing xfailed.

---

## 7. Math notes (subtle correctness observations)

These are observations the operator should know about, ranked by how
much they matter for live decisions:

1. **JS(log_2)(U_9, δ_0) ≈ 0.739** — the value computed by `js_divergence`
   when a wallet flips from a uniform prior to a delta on any single
   class. This sits comfortably above the default threshold of 0.3,
   so a wallet whose classification "decides" on a class (i.e., the
   typical post-cold-start path) will reliably trip the drift
   detector even before the rolling baseline has stabilised. The
   reference value is locked in by
   `test_js_uniform_vs_delta_9class_reference`.

2. **Cohen's κ when both labellers always pick the same class but
   different ones** — e.g., labeller A always picks "directional",
   labeller B always picks "momentum". The contingency matrix has
   all mass at `cm[0, 1]` (or wherever). Row marginal: 100 % in row
   0. Column marginal: 100 % in column 1. Expected agreement
   `p_e = 1 × 0 = 0` (not 1, because the row-and-column indices
   differ). Cohen's κ = (0 − 0) / (1 − 0) = 0. This is the correct
   convention and matches sklearn — verified by
   `test_perfect_disagreement_returns_zero`.

3. **`predict_proba` zero-fills missing classes** — after my fix in
   F1, a model trained on K' < 9 classes returns 0 in the columns
   for the unseen classes. The argmax over predict_proba then can
   NEVER predict an unseen class, even with the renormalisation —
   row_sums never include zero columns, so the seen classes get
   proportionally upweighted, never the unseen ones. This is the
   right semantics: if we never saw `arb_3way` in training, we
   shouldn't predict `arb_3way` at inference time. When the
   operator labels arb_3way wallets and retrains, this column gets
   non-zero contributions.

4. **Drift detector's `_load_baseline` filter is `< ceiling`, not `<=`.**
   The daemon evaluates drift BEFORE persisting the current row, so
   the strict-less filter is correct (the current row isn't yet in
   the table). If a future change moves persistence to happen first,
   the baseline would include the current row and the JS divergence
   would always be approximately the difference between the row
   and the average-including-itself — which is slightly smaller
   than the average-excluding-itself. Small but measurable bias;
   the current ordering is correct.

5. **`STRATEGY_WEIGHTS[structural_bot] = {follow: 0.0, fade: 0.0, skip: 10.0}`**
   acts as a hard veto on any wallet that slips through the
   `leaders.excluded=TRUE` gate. The 10× multiplier on `skip`
   guarantees argmax in confidence_engine resolves to SKIP unless
   another option's score exceeds 10× the baseline. This is defence
   in depth, not the primary exclusion mechanism — but it's the
   right safety net.

---

## 8. Summary

R8 was already at PASS quality after the architect pass. Wave-3
caught and fixed one latent bug (column-reorder crash on partial-class
training), added 44 hardening tests across math correctness and
edge cases, and silenced a noisy warning. The bug fix is small
(~15 LOC in `model.py`), surgical, and additively backwards-compatible
— the architect's existing tests still pass.

Working tree is dirty as instructed; no commit is made.
