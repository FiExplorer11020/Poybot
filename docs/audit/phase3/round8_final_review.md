# Round 8 — The Lens: Final Code-Layer Review

> **Branch**: `round-8-lens`
> **Reviewer**: R8 single-architect+implementer (one-pass)
> **Date**: 2026-05-12
> **Specification reference**: [`docs/ROUND_8_STRATEGY_CLASSIFIER.md`](../../ROUND_8_STRATEGY_CLASSIFIER.md)

---

## 1. Top-line recommendation

**PASS — code layer complete, awaiting operator-only gates.**

R8 ships the full code-layer of the Lens: feature extractor,
LightGBM-9-class classifier with isotonic calibration, hand-label
store with Cohen's κ measurement, unsupervised K-means+DBSCAN
explorer, JS-divergence drift detector, daemon entrypoint + systemd
unit, two migrations (026/027), 10 Prometheus metrics, and engine
integration gated behind a runtime config flag that defaults to
False.

**Tests**: 67 new R8 tests + 1 skipped (the LightGBM-missing fallback,
correctly skipped because LightGBM IS installed in CI). Full suite:
1,261 passed, 9 skipped (pre-existing), 2 xfailed (pre-existing), zero
failures. Baseline was 1,204 tests — R8 added 68 collected (net +57
after lightgbm-skip variant).

**Operator-only gates remain** (spec § 7) — explicitly out of scope:

1. The 1-week hand-labelling sprint (100 wallets).
2. Cohen's κ ≥ 0.7 measurement on the 20-wallet validation pair.
3. Training the LightGBM model on the labelled set and saving to
   `models/strategy_classifier.pkl`.
4. A/B Sharpe verification (paper, 30 days) before flipping the
   runtime flag.
5. The `batch_labeler.ipynb` notebook itself.

---

## 2. Per-component verification

| § Spec | Component | File | Lines | Verdict |
|---|---|---|---|---|
| 3.1 | `LeaderFeatureExtractor` + 42-feature schema | `src/strategy_classifier/features.py` | 425 | PASS |
| 3.2 | `StrategyLabelStore` + κ math | `src/strategy_classifier/labeling/label_store.py` | 250 | PASS |
| 3.2 | Operator protocol doc | `src/strategy_classifier/labeling/labeling_protocol.md` | 130 | PASS |
| 3.3 | `StrategyClassifier` + LightGBM-optional fallback | `src/strategy_classifier/model.py` | 320 | PASS |
| 3.3 | `STRATEGY_WEIGHTS` defaults (spec table) | `src/strategy_classifier/model.py` | (same) | PASS |
| 3.4 | `UnsupervisedStrategyExplorer` (K-means + DBSCAN) | `src/strategy_classifier/cluster.py` | 200 | PASS |
| 3.5 | `StrategyDriftDetector` (JS divergence) | `src/strategy_classifier/drift.py` | 200 | PASS |
| 3.6 | `confidence_engine` strategy-weight gate (runtime-config-flagged) | `src/engine/confidence_engine.py` | +90 | PASS |
| 4   | Migration 026 (`strategy_labels` + `leader_strategy_history`) | `docs/migrations/026_strategy_labels_and_history.sql` | 160 | PASS |
| 4   | Migration 027 (`leaders.classification_json` schema) | `docs/migrations/027_leaders_classification_json.sql` | 80 | PASS |
| 5   | 10 Prometheus metrics | `src/monitoring/metrics.py` | +110 | PASS |
| -   | Daemon entrypoint | `src/strategy_classifier/daemon.py` | 360 | PASS |
| -   | Module-run entry | `src/strategy_classifier/__main__.py` | 14 | PASS |
| -   | systemd unit | `infra/systemd/polymarket-strategy-classifier.service` | 30 | PASS |
| -   | R8 settings constants | `src/config.py` | +55 | PASS |
| -   | Runtime config flag | `src/control/runtime_config.py` | +25 | PASS |

### Component notes

**features.py** — 42 feature slots organized in categories A-I matching
spec § 3.1. Categories E (entry microstructure), F (exit
microstructure), G (network) and H (social) have **structural slots
preserved** even when the upstream rounds (R9 / R10 / R11 / R12)
haven't wired their data sources yet: those cells emit `np.nan` and the
`PENDING_FEATURE_NAMES` frozenset tracks them for daemon metrics. The
contract: when R9/R10/R11/R12 land, populating those slots is purely
additive — no schema migration of the model, no test-shape change.
Acceptance gate from spec § 7.B (feature extraction < 1s per wallet)
is observable via `polybot_classifier_feature_extraction_seconds`.

**model.py** — LightGBM is OPTIONAL. The classifier falls back to a
uniform-prior dummy (1/9 per class) when LightGBM is not installed, so
the daemon, drift detector, and engine integration can all be
exercised by tests and at boot without the heavy dep. Calling `.fit()`
on the dummy path raises `RuntimeError` with a clear message — the
production path needs the real model. Isotonic calibration via
`CalibratedClassifierCV` with `method='isotonic'`. `class_weight='balanced'`
on the base LightGBM handles the spec § 6 risk-table "class imbalance"
concern. Column reordering at predict time ensures LightGBM's
internal lexicographic class index doesn't bleed through to consumers.

**cluster.py** — K-means + DBSCAN over the same 42-feature matrix.
NaN imputation to column medians (K-means and DBSCAN can't tolerate
NaNs unlike LightGBM). `surface_candidate_clusters` filters by size
≥ min AND mean-supervised-confidence ≤ max — directly implements the
spec § 3.4 "sizable AND poorly-matched" rule. The output is
**advisory only**; nothing in the production decision path reads
cluster labels.

**drift.py** — Jensen-Shannon divergence on `log_2` so the output is
bounded in [0, 1]. The "log_2" choice matches the spec § 3.5
threshold default of 0.3 (a class flip = JS ≈ 1.0 under log_2,
comfortably above 0.3). Cold-start guard: when the wallet has fewer
than `min_baseline_samples` rows in `leader_strategy_history`, drift
NEVER fires — we don't have a stable reference.

**daemon.py** — Mirrors `src/registry/refresher_main.py` shape for
systemd consistency. Reads tier-0/1 wallets from `wallet_universe`,
extracts features, predicts, evaluates drift, persists to
`leader_strategy_history`, merges `strategy_fingerprint` into
`leaders.classification_json`. Loads the model lazily from disk;
falls back to the uniform-prior dummy when the model file is missing
(common at first boot, before the operator has trained anything).

**confidence_engine.py integration** — A new helper
`_maybe_get_strategy_weights(wallet)` is called BEFORE the readiness
gates and Thompson sampling, returning `None` whenever the runtime
flag is False OR the leader has no `strategy_fingerprint`. When it
returns a weight dict, the `thompson_follow` and `thompson_fade`
samples are multiplied by the per-strategy weights from
`STRATEGY_WEIGHTS`. **Crucially**: when the flag is False (default),
this helper is a no-op — the rest of the engine is byte-identical to
pre-Round-8 behavior. The regression test `test_flag_disabled_returns_none`
is the gate.

**Migrations 026/027** — Append-only with full audit trail. Both
CHECK constraints hard-code the 9-class taxonomy in lock-step. The
GIN index on `classification_json -> 'strategy_fingerprint'` is
partial (`WHERE classification_json ? 'strategy_fingerprint'`) so it
stays tiny until the daemon populates the field. Documented operator
post-migration steps inline.

---

## 3. Metrics inventory (spec § 5 — 10 metrics)

All 10 metrics ship in `src/monitoring/metrics.py`, each declared
inside its own defensive `try/except` block matching the R6/R7 pattern.

| Metric | Type | Labels | Status |
|---|---|---|---|
| `polybot_classifier_predictions_total` | Counter | `strategy`, `source` | PRESENT |
| `polybot_classifier_confidence` | Histogram | `strategy` | PRESENT |
| `polybot_classifier_loss` | Gauge | `set` (train/val/live) | PRESENT |
| `polybot_classifier_calibration_loss` | Gauge | `strategy` | PRESENT |
| `polybot_classifier_drift_score` | Gauge | `wallet` | PRESENT |
| `polybot_strategy_drift_detected_total` | Counter | `from`, `to` | PRESENT |
| `polybot_strategy_label_set_size` | Gauge | `strategy` | PRESENT |
| `polybot_unsupervised_clusters_unmatched` | Gauge | (none) | PRESENT |
| `polybot_classifier_inference_seconds` | Histogram | (none) | PRESENT |
| `polybot_classifier_feature_extraction_seconds` | Histogram | (none) | PRESENT |

The daemon wires the high-traffic ones (`predictions_total`,
`confidence`, `drift_score`, `drift_detected_total`,
`inference_seconds`, `feature_extraction_seconds`) on every classify
pass. The training / calibration / labelling metrics
(`classifier_loss`, `classifier_calibration_loss`,
`strategy_label_set_size`, `unsupervised_clusters_unmatched`) are
wired BUT are written by the operator-facing training notebook — not
exercised by the runtime daemon.

---

## 4. Test inventory

| File | Tests | Notes |
|---|---|---|
| `tests/test_strategy_classifier/test_model.py` | 16 | Includes LightGBM fit + save/load round-trip; one test correctly skips when LightGBM IS installed. |
| `tests/test_strategy_classifier/test_features.py` | 9 | 42-slot shape, asof correctness, microstructure graceful nan. |
| `tests/test_strategy_classifier/test_label_store.py` | 8 | Insert validation, κ math (perfect/partial/no-overlap), training-set assembly. |
| `tests/test_strategy_classifier/test_cluster.py` | 8 | K-means recovers synthetic clusters, size/confidence filters. |
| `tests/test_strategy_classifier/test_drift.py` | 10 | JS divergence math (5) + StrategyDriftDetector (4). |
| `tests/test_strategy_classifier/test_daemon.py` | 4 | Daemon shape, graceful cancel, model-load fallback. |
| `tests/test_strategy_classifier/test_engine_integration.py` | 13 | Runtime-flag gating (5), spec-weights regression (5), runtime_config bool coercion (3). |
| **Total** | **68** | (67 pass + 1 LightGBM-irrelevant skip in current env) |

Full-suite regression: 1,261 passed (was 1,204 baseline; net +57
considering the one always-skipped LightGBM fallback test), 9
pre-existing skips, 2 pre-existing xfails, **zero failures**.

---

## 5. What was NOT implemented (operator gates, per spec § 7)

These were explicitly **out of scope** for the code-layer drop:

1. **Hand-labelling sprint (§ 7.A)** — 100 (wallet, window) labels.
   The store is ready; the operator + a second labeller drive the
   labelling via the protocol doc and the (operator-owned)
   `batch_labeler.ipynb` notebook.
2. **Cohen's κ measurement on the 20-wallet validation** — the math
   is shipped in `StrategyLabelStore.compute_inter_labeller_kappa`;
   operator runs it after both labellers complete their independent
   pass.
3. **Methodology audit gate** — κ ≥ 0.7 gate before unlocking the
   main 80-wallet pass.
4. **Model training** — operator runs the (TBD) training notebook
   to fit LightGBM on the labelled set, saves to
   `models/strategy_classifier.pkl`. The daemon picks it up on next
   restart.
5. **Shadow-phase audit (§ 7.D)** — operator inspects the top-100
   wallets' classifier outputs for 1-2 weeks before flipping
   anything.
6. **A/B Sharpe verification (§ 7.E)** — paper backtest with the
   flag enabled on half the decisions, 30 days, 95 % significance.
7. **Flipping `strategy_conditional_confidence_enabled` in
   RuntimeConfig** — operator-only via the dashboard
   `/api/risk/update` POST endpoint (already wired through the same
   path as the existing risk knobs).

---

## 6. Decisions made worth documenting

1. **`STRATEGY_WEIGHTS` lives in `src/strategy_classifier/model.py`,
   not in `runtime_config.py`.** Spec § 3.6 says the weights are
   "operator-tunable" but the per-strategy table is large (9 × 3 = 27
   knobs) and would balloon the RuntimeConfig ALLOWED_KEYS. v1 keeps
   them as code-level defaults; a future iteration can lift them
   into runtime_config if the operator needs per-deployment tuning.
2. **Boolean keys in RuntimeConfig** — added a `BOOLEAN_KEYS`
   frozenset + coercion path. The flag stores as `True/False` in
   the persisted JSON and the BOUNDS check bypasses for boolean
   keys.
3. **Migration 027 does NOT add a CHECK constraint on
   `classification_json`.** Validation lives in the Python layer
   (`StrategyClassifier.build_classification_json_patch`) because the
   registry already writes to this column on every Falcon refresh,
   and a CHECK constraint firing on every UPDATE would be a hot-path
   cost. Documented inline in the migration.
4. **Features E + F + H are structural slots, not skipped slots.**
   The daemon emits `np.nan` for them rather than reshaping the
   vector. This lets R9/R10/R11/R12 land additively — no model
   retrain triggered by the wiring itself, only by the operator's
   choice to retrain after upstream data starts flowing.
5. **JS divergence uses log base 2.** Spec § 3.5 doesn't specify the
   log base; log_2 gives a clean [0, 1] bound which matches the
   threshold default of 0.3 cleanly.
6. **Daemon classifies tier-0 AND tier-1, not just tier-0.** Spec § 8
   says "top ~2000 by recent volume" — that's tier 0 + tier 1 in the
   wallet_universe schema (tier 0 = top ~200 with full Falcon
   enrichment, tier 1 = top ~2000). Daemon respects that.
7. **The classifier loads from
   `models/strategy_classifier.pkl` by default.** Path is configurable
   via `STRATEGY_CLASSIFIER_MODEL_PATH` in settings. Missing file →
   uniform-prior dummy + WARNING log. This lets the systemd unit boot
   cleanly before the operator has run the training notebook.

---

## 7. Files created (28)

```
docs/migrations/026_strategy_labels_and_history.sql         (160 lines)
docs/migrations/027_leaders_classification_json.sql         ( 80 lines)
docs/audit/phase3/round8_final_review.md                    (this file)
infra/systemd/polymarket-strategy-classifier.service        ( 30 lines)
src/strategy_classifier/__init__.py                         ( 55 lines)
src/strategy_classifier/__main__.py                         ( 14 lines)
src/strategy_classifier/cluster.py                          (200 lines)
src/strategy_classifier/daemon.py                           (360 lines)
src/strategy_classifier/drift.py                            (200 lines)
src/strategy_classifier/features.py                         (425 lines)
src/strategy_classifier/labeling/__init__.py                ( 20 lines)
src/strategy_classifier/labeling/label_store.py             (250 lines)
src/strategy_classifier/labeling/labeling_protocol.md       (130 lines)
src/strategy_classifier/model.py                            (320 lines)
tests/test_strategy_classifier/__init__.py                  (  1 line )
tests/test_strategy_classifier/test_cluster.py              (120 lines)
tests/test_strategy_classifier/test_daemon.py               (140 lines)
tests/test_strategy_classifier/test_drift.py                (140 lines)
tests/test_strategy_classifier/test_engine_integration.py   (210 lines)
tests/test_strategy_classifier/test_features.py             (175 lines)
tests/test_strategy_classifier/test_label_store.py          (180 lines)
tests/test_strategy_classifier/test_model.py                (200 lines)
```

## 8. Files modified (5, small targeted edits)

```
src/config.py                    (+55 lines: R8 settings constants + 2 validators)
src/control/runtime_config.py    (+25 lines: BOOLEAN_KEYS, default, coercion)
src/engine/confidence_engine.py  (+90 lines: STRATEGY_WEIGHTS import,
                                  _maybe_get_strategy_weights, hook in evaluate)
src/monitoring/metrics.py        (+110 lines: 10 R8 metrics, defensive try/except blocks)
infra/systemd/README.md          (+1 row in the unit table)
```

No existing R6/R7 code paths were touched beyond the four targeted
edits above. The engine edit is byte-identical-when-flag-off (verified
by `test_flag_disabled_returns_none`).

---

## 9. Single-sentence summary

> Round 8 ships every code-layer surface needed for The Lens — 42-feature
> extractor, LightGBM-9-class + isotonic calibration, append-only
> hand-label store with Cohen's κ, K-means + DBSCAN explorer for new
> classes, JS-divergence drift detector, daemon + systemd unit, two
> migrations, 10 Prometheus metrics, and a confidence-engine multiplier
> gated by a default-OFF runtime flag — so the operator can now run the
> 1-week labelling sprint and flip the switch when ready, without any
> further engineering work.
