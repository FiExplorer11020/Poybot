# Round 13 — The Mirror: Wave-3 Independent Review

> **Reviewer**: Wave-3 independent (post-merge, post-`v0.13.0`)
> **Date**: 2026-05-12
> **Branch**: `main` (R13 already merged at commit `26ec6c2`)
> **Spec**: [`docs/ROUND_13_CALIBRATION_AND_RESEARCH.md`](../../ROUND_13_CALIBRATION_AND_RESEARCH.md)
> **Predecessor**: [`round13_final_review.md`](./round13_final_review.md)
>   — written by the orchestrator after the Wave-1 architect hit a rate
>   limit mid-stream and the orchestrator completed the missing pieces
>   inline (loss aggregator, drift detector, daemon, metrics block,
>   config constants, tests, research substrate). This wave-3 review
>   audits that finish-up with fresh eyes.

---

## 1. Top-line verdict

**PASS with two critical fixes applied in-scope.**

The R13 code-side deliverable holds together: the math helpers are
numerically correct (cross-validated against sklearn), the drift
detector + auto-disabler enforce the spec § 3.4 `follow_confidence`
protection guard, the daemon orchestration is testable + cancellable,
and the 6-notebook research substrate is valid JSON with graceful
empty-data degradation. The orchestrator's inline finish-up (notably
the `model_drift_streak` extension to migration 040) is consistent
with the architect's `auto_disable.py` contract.

Two latent defects required in-scope fixes before the migrations are
applied to production:

1. **Migrations 039 and 040 — nullable column in PRIMARY KEY rejects
   the aggregate row.** The aggregator writes `strategy_class = NULL`
   for the "aggregate across classes" record. PostgreSQL implicitly
   applies `NOT NULL` to PRIMARY KEY columns, so the bare `PRIMARY KEY
   (model, strategy_class, measured_at)` would have crashed every
   nightly batch with `null value … violates not-null constraint`.
   Fixed by replacing the PK with a `UNIQUE INDEX … NULLS NOT DISTINCT`
   (PG 15+) — verified end-to-end against the local Postgres 15.17
   container.

2. **`compute_causal_residual` — Python chained-comparison trap.** The
   length-check `if len(a) != len(b) != len(c): return None` parses as
   `(a != b) and (b != c)`, which silently ACCEPTS the (len(a) ==
   len(c), len(a) != len(b)) case. Fixed by explicit pairwise compare
   + a regression test pinning the failure modes.

Both fixes are isolated and within the wave-3 scope. The deferred
engine + Telegram wiring (§ 4.A and § 4.B of the orchestrator's
review) remains operator-scope and is **not** touched by this wave —
the audit doc explicitly flags this so the deferral can't be silently
forgotten.

---

## 2. Per-component verification matrix

| § 3.x | Component | File | Verdict | Notes |
|---|---|---|---|---|
| 3.1 | `DecisionPrediction.from_decision_context` | `src/calibration/decision_replay.py` | PASS | Duck-types Decision dataclass; no engine import. Survives missing trade_context + non-dict context (new hardening tests). |
| 3.1 | `record_decision_predictions` (ON CONFLICT DO NOTHING) | `src/calibration/decision_replay.py` | PASS | Caller-owned txn; idempotent. |
| 3.1 | `fill_actual_outcomes` (COALESCE) | `src/calibration/decision_replay.py` | PASS | Preserves prior values on partial updates. |
| 3.1 | `fill_actual_outcomes_for_position` convenience hook | `src/calibration/decision_replay.py` | PASS | Resolves decision_id from (wallet, market, open_time). |
| 3.2 | `compute_brier` | `src/calibration/loss_aggregator.py` | PASS | Cross-validated against `sklearn.metrics.brier_score_loss` to relative tolerance 1e-9. |
| 3.2 | `compute_mape` | `src/calibration/loss_aggregator.py` | PASS | Cross-validated against `sklearn.metrics.mean_absolute_percentage_error`. ε floor protects /0. |
| 3.2 | `compute_ci_coverage` | `src/calibration/loss_aggregator.py` | PASS | Inclusive [lo, hi]; returns hit fraction. |
| 3.2 | `compute_log_loss` | `src/calibration/loss_aggregator.py` | PASS | Cross-validated against `sklearn.metrics.log_loss` (binary + 3-class). Natural log, clipped to [ε, 1-ε]. |
| 3.2 | `compute_causal_residual` | `src/calibration/loss_aggregator.py` | **FIX APPLIED** | Chained-comparison guard repaired; regression test pins the failure mode. |
| 3.2 | `ModelLossAggregator.run_for_day` + `_persist` (ON CONFLICT DO UPDATE) | `src/calibration/loss_aggregator.py` | PASS | Per-strategy split only for `follow_confidence` — architect's choice, documented in § 5 below. |
| 3.3 | `ModelDriftMonitor` z-score (small-n fallback + zero-std floor) | `src/calibration/drift_detector.py` | PASS | n < 3 → raw signed diff; std ≤ 1e-9 → clamped. |
| 3.3 | Rolling 30-day baseline | `src/calibration/drift_detector.py` | PASS | `IS NOT DISTINCT FROM` join on strategy_class. |
| 3.3 | Rate-limited Telegram (1/h per key) | `src/calibration/drift_detector.py` | PASS | `time.monotonic` based; 3 existing test cases. |
| 3.3 | Consecutive-day streak (FOR UPDATE row lock) | `src/calibration/drift_detector.py` | PASS | Same-day re-runs idempotent; persists in `model_drift_streak`. |
| 3.4 | `PROTECTED_FROM_AUTO_DISABLE = {"follow_confidence"}` | `src/calibration/auto_disable.py` | PASS | Frozen set; existence + content asserted by `test_protected_set_contains_follow_confidence_only`. |
| 3.4 | `disable_model(..., auto_or_manual="auto")` refuses protected | `src/calibration/auto_disable.py` | PASS | Returns False; emergency Telegram alert fires. |
| 3.4 | `disable_model(..., auto_or_manual="manual")` always succeeds | `src/calibration/auto_disable.py` | PASS | Operator override; protected set bypassed. |
| 3.4 | 30 s read cache TTL + bust on write | `src/calibration/auto_disable.py` | PASS | `_cache_fetched_at` reset to 0 on every write. |
| §   | `CalibrationDaemon.run_once` orchestration | `src/calibration/daemon.py` | PASS | aggregator → drift_monitor → auto_disable handoff. |
| §   | `run_forever` (hourly poll, UTC day rollover, graceful cancel) | `src/calibration/daemon.py` | PASS | `_stop` event + `asyncio.CancelledError` re-raised. |
| §   | `_initial_backfill_if_needed` (cold-start) | `src/calibration/daemon.py` | PASS | Empty history → 90-day backfill; populated → skip; DB outage → silent log (new hardening tests). |
| 3.5 | 6 research notebooks | `research/notebooks/*.ipynb` | PASS | All 6 valid JSON; graceful empty-data degradation documented. |
| 3.6 | Telegram `/calibration`, `/disable`, `/enable`, `/disabled` | n/a | **DEFERRED** | Operator scope per `round13_final_review.md` § 4.B. Public API ready. |
| § 4 | Migration 038 (decision_predictions) | `docs/migrations/038_decision_predictions.sql` | PASS | FK to decision_log with ON DELETE CASCADE; 3 supporting indexes. |
| § 4 | Migration 039 (calibration_loss_history) | `docs/migrations/039_calibration_loss_history.sql` | **FIX APPLIED** | NULLS NOT DISTINCT unique index replaces broken PK. |
| § 4 | Migration 040 (model_disable_state + model_drift_streak) | `docs/migrations/040_model_disable_state.sql` | **FIX APPLIED** | Same NULL-in-PK fix on the orchestrator's `model_drift_streak` companion. |
| § 5 | 10 Prometheus metrics | `src/monitoring/metrics.py` | PASS | All 10 declared in defensive try/except blocks. |

---

## 3. Loss-math cross-validation against sklearn

Numerical equality at relative tolerance 1e-9, executed in
`tests/test_calibration/test_loss_aggregator_hardening.py`:

| Helper | Test input | Ours | sklearn | Δ |
|---|---|---|---|---|
| `compute_brier` | p=[0.1, 0.4, 0.9, 0.7, 0.3], y=[0,0,1,1,0] | 0.0720000000 | 0.0720000000 | 0 |
| `compute_brier` | p=[0.5]*10, y=mixed | 0.25 (closed-form) | 0.25 | 0 |
| `compute_log_loss` | 2-class probs + indices | 0.2869793086 | 0.2869793086 | 0 |
| `compute_log_loss` | 3-class probs + indices | match | match (with `labels=[0,1,2]`) | 0 |
| `compute_mape` | f=[110,90,50,200], a=[100,100,50,220] | 0.0727272727 | 0.0727272727 | 0 |

The clip-at-ε strategy for `log_loss` matches sklearn's internal
`clip(p, eps, 1-eps)` for finite values; the natural-log base
(`math.log`) is consistent with `sklearn.metrics.log_loss`'s implicit
natural-log default.

`compute_causal_residual` is NOT a sklearn helper — it's a project-
specific construct (R10 residual normalised by CI width). Its
correctness is pinned by hand-checkable test cases
(`test_causal_residual_zero_when_estimates_agree`,
`test_causal_residual_ci_width_normalisation`) + the new
chained-comparison regression tests.

`compute_ci_coverage` is a uniform-distribution coverage statistic;
no sklearn analogue, but the math is `mean(1{lo <= a <= hi})` and
covered by 4 existing test cases.

---

## 4. Auto-disable protection guard audit (spec § 3.4 invariant)

The spec § 3.4 hard requirement: `follow_confidence` MUST NEVER be
auto-disabled (the manual operator override is allowed). Verified at
three independent layers:

| Layer | Mechanism | Test |
|---|---|---|
| Constant | `PROTECTED_FROM_AUTO_DISABLE = frozenset({"follow_confidence"})` (module-level immutable frozenset, exported in `__all__`) | `test_protected_set_contains_follow_confidence_only` |
| Auto-disabler | `disable_model(..., auto_or_manual="auto")` short-circuits on `model in PROTECTED_FROM_AUTO_DISABLE`, returns False, fires emergency notify | `test_auto_disable_refuses_protected_follow_confidence` |
| Drift detector | `_trigger_auto_disable` short-circuits BEFORE consulting the disabler when `alert.model in PROTECTED_FROM_AUTO_DISABLE` (defence-in-depth) | `test_trigger_auto_disable_skips_protected_model`, new `test_five_day_streak_on_follow_confidence_does_not_auto_disable` |
| Operator path | `disable_model(..., auto_or_manual="manual")` BYPASSES the guard. The protected set only fires when `auto_or_manual == "auto"`. | `test_manual_disable_of_protected_model_succeeds` |

The wave-3 hardening test
`test_five_day_streak_on_follow_confidence_does_not_auto_disable`
exercises the full path with a real `ModelAutoDisabler` instance
(fake DB) and verifies: (a) no row written to `model_disable_state`,
(b) the emergency `CRITICAL` alert reaches the notify_fn. This is the
load-bearing invariant for the entire R13 calibration loop — a bug
here would let the auto-disabler silently kill the bot's core signal.

---

## 5. Drift-detector streak persistence audit

The orchestrator finish-up extended migration 040 with a
`model_drift_streak` companion table to avoid recomputing streaks
from `calibration_loss_history` every night. Schema (post-fix):

```sql
CREATE TABLE IF NOT EXISTS model_drift_streak (
    model              VARCHAR(40) NOT NULL,
    strategy_class     VARCHAR(20),         -- nullable; NULL = aggregate
    consecutive_days   INTEGER NOT NULL DEFAULT 0,
    last_breach_at     DATE
);
CREATE UNIQUE INDEX uq_mds_streak_model_strat
    ON model_drift_streak (model, strategy_class) NULLS NOT DISTINCT;
CREATE INDEX idx_mds_streak_recent
    ON model_drift_streak (last_breach_at DESC) WHERE consecutive_days > 0;
```

Audit of the write path (`drift_detector._increment_streak`):

* Read-modify-write inside one transaction (`FOR UPDATE` row lock).
* Same-day idempotency: `if prev_breach == measured_at: return prev_days` (caller's same-day re-run of `evaluate_day` won't double-increment).
* Best-effort fallback: DB failure logs at DEBUG and returns 1 (the alert still fires this run).
* Reset path (`_reset_streak`): upserts `(consecutive_days=0, last_breach_at=NULL)` on the first clean day.

The "FOR UPDATE" lock is the right defensive choice — concurrent
calibration runs (operator triggers a manual run while the daemon is
also running) won't double-increment. The `NULLS NOT DISTINCT` index
ensures the aggregate row (strategy_class IS NULL) is properly
uniquified.

End-to-end verified against the local Postgres 15.17 container:
inserting both `('volume_forecast', NULL, 1, '2026-05-11')` and the
ON CONFLICT update to `(2, '2026-05-12')` produces a single row with
the latest values — see § 8 below.

---

## 6. Research substrate audit

| Notebook | Cells | Empty-data behavior | Spec § |
|---|---|---|---|
| `00_data_loader.ipynb` | 7 | DuckDB views over Parquet; explicit "no cold-tier data, populate via X" path | 3.5 |
| `01_strategy_classifier_validation.ipynb` | (per JSON validation) | Joins R8 labels; degrades on empty `strategy_labels` | 3.5 |
| `02_causal_analysis.ipynb` | (valid JSON) | R10 IV vs Hawkes disagreement plot; empty → no data message | 3.5 |
| `03_counterfactual_replay.ipynb` | 5 | Uses R10's CounterfactualReplayer; the < 5 min wall-time gate is documented in the audit doc § 4.D-6 | 3.5 |
| `04_what_if_explorer.ipynb` | (valid JSON) | Per-hypothesis explorer template | 3.5 |
| `05_calibration_review.ipynb` | 7 | Reads `calibration_loss_history` + `list_disabled`; empty history → "NO DATA — wait for first daemon run" | 3.5 |

JSON validity confirmed for all 6 notebooks via:

```bash
for nb in research/notebooks/*.ipynb; do
  python -c "import json; json.load(open('$nb'))" && echo "OK: $nb"
done
# → 6/6 OK
```

`research/requirements.txt` pins jupyter + jupyterlab + duckdb +
pyarrow + pandas + numpy + scipy + matplotlib + seaborn + asyncpg +
nest-asyncio — covers the notebook runtime. `research/README.md` (85
lines) walks the operator through setup; `.gitignore` excludes the
working `research/duckdb/` directory.

---

## 7. Spec § 6 acceptance criteria checklist

| Criterion | Status | Notes |
|---|---|---|
| Nightly calibration batch completes in < 10 min | INFRA READY | Daemon shape supports it; per-day cardinality matches spec §6's projection. Operator soak-gate. |
| Per-model loss history populates for all major models within 7 days | INFRA READY | Aggregator dispatches all 4 models; soak-gate. |
| Drift detector fires in 90-day historical replay | INFRA READY | Drift evaluator runs against any populated history; soak-gate per spec §7.C. |
| Research notebook answers a what-if in < 5 min | INFRA READY | Notebook 03 uses R10's < 5 min replayer (operator-verified on prod VM per § 4.D-6). |
| Operator disables a model via Telegram in < 30 s | DEFERRED | Telegram commands are spec § 3.6, deferred per `round13_final_review.md` § 4.B. ModelAutoDisabler public API is ready. |
| 30 days of live operation → ≥ 1 auto-disable event (or proven perfect calibration) | OPERATOR SOAK GATE | Out of code scope. |

---

## 8. Findings + fixes

### 8.A Critical: Migration 039 + 040 NULL-in-PRIMARY-KEY rejection

**Symptom**: PostgreSQL implicitly applies NOT NULL to every PRIMARY
KEY column. Migrations 039 and 040 declare `PRIMARY KEY (model,
strategy_class, ...)` with nullable `strategy_class`. The aggregator
and drift detector both write `strategy_class = NULL` for the
aggregate row. In production the INSERT would fail with `null value
in column "strategy_class" of relation "calibration_loss_history"
violates not-null constraint`.

**Reproduction** (against the live `polymarket_db` PG 15.17 container,
before fix):

```sql
CREATE TABLE test_pk_null (
    model VARCHAR(40) NOT NULL,
    strategy_class VARCHAR(20),
    measured_at DATE NOT NULL,
    PRIMARY KEY (model, strategy_class, measured_at)
);
INSERT INTO test_pk_null VALUES ('m1', NULL, '2026-01-01');
-- ERROR: null value in column "strategy_class" of relation "test_pk_null"
--        violates not-null constraint
```

**Fix**: Replace the PRIMARY KEY with a UNIQUE INDEX with
`NULLS NOT DISTINCT` (PG 15 feature). The application code's
`ON CONFLICT (model, strategy_class, measured_at) DO UPDATE` still
matches; reads via `IS NOT DISTINCT FROM` are unchanged.

```sql
-- 039 (calibration_loss_history) — applied:
CREATE TABLE calibration_loss_history (
    model VARCHAR(40) NOT NULL,
    strategy_class VARCHAR(20),
    measured_at DATE NOT NULL,
    n_decisions INTEGER NOT NULL DEFAULT 0,
    brier_score NUMERIC(8, 6),
    log_loss NUMERIC(8, 6),
    mape NUMERIC(8, 6),
    ci_coverage NUMERIC(5, 4)
);
CREATE UNIQUE INDEX uq_clh_model_strat_day
    ON calibration_loss_history (model, strategy_class, measured_at)
    NULLS NOT DISTINCT;

-- 040 (model_drift_streak) — applied: same shape.
```

**Verification** (post-fix, end-to-end against PG 15.17):

```sql
INSERT INTO calibration_loss_history (model, strategy_class, measured_at, ...)
VALUES ('follow_confidence', NULL, '2026-05-11', ...);
INSERT INTO calibration_loss_history (model, strategy_class, measured_at, ...)
VALUES ('follow_confidence', NULL, '2026-05-11', ...)
ON CONFLICT (model, strategy_class, measured_at) DO UPDATE
SET n_decisions = EXCLUDED.n_decisions;
-- → single row, latest values. Idempotent ✓
```

**Severity**: load-bearing — every nightly batch would have crashed
on the aggregate row write.

### 8.B Major: `compute_causal_residual` chained-comparison guard

**Symptom**: Python's chained comparison `a != b != c` parses as
`(a != b) and (b != c)`. The intended semantic ("any pair differs"
→ reject) is the OR of those two, not the AND. As written, the guard
silently accepts the case where len(a) == len(c) but differs from
len(b).

**Reproduction**:

```python
def f(a, b, c):
    if len(a) != len(b) != len(c):
        return None
    return "OK"

f([1,2], [3,4,5], [6,7])
# → "OK" (BUG: lengths are 2,3,2 — should reject)
```

**Fix** (in `src/calibration/loss_aggregator.py`):

```python
if (
    len(hawkes_alpha_mus) != len(causal_ates)
    or len(causal_ates) != len(ci_widths)
):
    return None
```

**Verification**: New regression tests
`test_causal_residual_rejects_a_eq_c_but_b_diff` and
`test_causal_residual_rejects_b_eq_c_but_a_diff` pin both failure
modes.

**Severity**: low in practice (callers always supply matched-length
arrays, derived from the same DB row set), but the guard exists
precisely as a defence against bad input — a guard that silently
fails is worse than no guard.

### 8.C Minor: ruff import-order on 5 calibration files

`ruff check src/calibration/ --fix` reorganized imports in
`__init__.py`, `auto_disable.py`, `daemon.py`, `drift_detector.py`,
`loss_aggregator.py`. Pure formatting; no semantic change.

---

## 9. Cross-cutting findings (deliberately NOT touched — operator scope)

These are markdown-patch placeholders for the operator's follow-up.
They mirror `round13_final_review.md` § 4 and are reproduced here as
the closing audit of the R6–R13 roadmap so they cannot be silently
forgotten.

### 9.A Engine + position_tracker hook wiring (spec § 3.1)

```python
# src/engine/confidence_engine.py — inside decide(), within the SAME
# transaction that writes to decision_log:
from src.calibration import record_decision_predictions, DecisionPrediction
predictions = DecisionPrediction.from_decision_context(decision)
await record_decision_predictions(conn, decision_id, predictions)

# src/engine/main.py or scheduler — optional in-engine cron for the
# nightly batch (alternative to polymarket-calibration.service):
scheduler.add_job(
    func=CalibrationDaemon().run_once,
    trigger="cron",
    hour=settings.CALIBRATION_BATCH_HOUR_UTC,
    minute=settings.CALIBRATION_BATCH_MINUTE,
)
```

```python
# src/observer/position_tracker.py — inside the close path:
from src.calibration import fill_actual_outcomes_for_position
await fill_actual_outcomes_for_position(
    wallet_address=position.wallet_address,
    market_id=position.market_id,
    open_time=position.open_time,
    pnl_usdc=position.pnl_usdc,
    followup_volume_usdc=followup_volume,
    closed_at=position.close_time,
)
```

### 9.B Telegram commands (spec § 3.6)

`/calibration`, `/calibration <model>`, `/disable <model>`,
`/enable <model>`, `/disabled` — all consume the
`ModelAutoDisabler` public API (`disable_model`, `enable_model`,
`is_disabled`, `list_disabled`) which is already test-covered. Wiring
is ~50 LOC under `src/telegram_bot/commands.py` + an operator-
chat-id authorization check.

### 9.C Metrics block (spec § 5)

All 10 R13 metrics already declared in `src/monitoring/metrics.py`
(lines 1262–1357). No-op for the wave-3 reviewer.

### 9.D Config constants (spec § 6/§7)

All declared in `src/config.py` (lines 1157–1207) with field
validators:
* `CALIBRATION_BATCH_HOUR_UTC` (0–23)
* `CALIBRATION_BATCH_MINUTE` (0–59)
* `CALIBRATION_DRIFT_Z_THRESHOLD` (0.5–10.0)
* `CALIBRATION_DRIFT_CONSECUTIVE_DAYS_FOR_DISABLE` (1–14)
* `CALIBRATION_BASELINE_WINDOW_DAYS` (7–365)
* `CALIBRATION_REPLAY_ENABLED` (bool)
* `CALIBRATION_TELEGRAM_RATE_LIMIT_S` (float)
* `CALIBRATION_INITIAL_BACKFILL_DAYS` (int, default 90)

No-op for the wave-3 reviewer.

---

## 10. Hardening tests added (21 new, 78 total in `tests/test_calibration/`)

| File | Tests | Coverage |
|---|---|---|
| `test_loss_aggregator_hardening.py` | 9 | sklearn cross-validation (Brier × 2, log_loss × 2, MAPE × 1); chained-comparison regression × 3; NaN filtering + ε floor pin |
| `test_drift_detector_hardening.py` | 4 | Cold-start baseline (n=0 and n=1) + 5-day-streak protected-model guard + 3-day-streak unprotected-model handoff |
| `test_daemon_hardening.py` | 3 | Initial backfill triggers / skips / silently degrades on DB outage |
| `test_decision_replay_hardening.py` | 4 | All-fields-present + all-fields-missing + no-trade_context-attr + malformed-context-type |

Existing R13 tests (57) all still pass post-fixes. Full suite excluding
the 2 pre-existing R10 IV-estimator numerical edge failures
(`tests/test_causal/test_iv_estimator_hardening.py::*`,
unrelated to R13 scope): **1,847 passed / 9 skipped / 2 xfailed / 0
failed**.

---

## 11. Orchestrator finish-up audit

The orchestrator inherited a partial tree after the Wave-1 architect
hit a rate limit. What the orchestrator produced inline:

* `src/calibration/loss_aggregator.py` (430 lines) — 4 pure math
  helpers + aggregator. **Audit verdict**: math correctness verified
  against sklearn; one chained-comparison defect in
  `compute_causal_residual` fixed in-scope.
* `src/calibration/drift_detector.py` (348 lines) — z-score, baseline,
  streak persistence, rate-limited alerts. **Audit verdict**: correct;
  the streak persistence in the `model_drift_streak` companion table
  is the right choice (avoids O(window × n_models) recomputation each
  night).
* `src/calibration/daemon.py` (210 lines) — orchestration shell.
  **Audit verdict**: correct; `run_forever` properly handles the UTC
  day rollover + `asyncio.CancelledError`; `_initial_backfill_if_needed`
  degrades gracefully on DB outage (verified by new hardening test).
* `src/monitoring/metrics.py` — all 10 R13 metrics declared in
  defensive try/except blocks at lines 1262–1357. **Audit verdict**:
  matches spec § 5 exactly.
* `src/config.py` — 8 R13 settings + 3 field validators at lines
  1157–1207. **Audit verdict**: bounds are sensible; `field_validator`
  catches misconfigurations early.
* `docs/migrations/040_model_disable_state.sql` — extended with
  `model_drift_streak`. **Audit verdict**: structurally correct; the
  NULL-in-PK pitfall it inherited from the architect's 039 has been
  fixed in-scope by the wave-3 reviewer.
* Tests (57 unit tests across 5 files) — **Audit verdict**: well-
  factored, AAA structure, real-DB-shaped fakes for asyncpg surface.
  21 hardening tests added by the wave-3 reviewer to cover sklearn
  cross-validation, chained-comparison regression, cold-start
  baseline, protected-model streak escalation, daemon backfill
  triggering, and Decision-context full-field extraction.
* `research/` substrate — 6 notebooks, README (85 lines),
  requirements.txt (pinned), `.gitignore` entry for the working
  DuckDB file. **Audit verdict**: JSON validity confirmed; empty-data
  degradation behavior documented in the README.

**Consistency check**: the orchestrator's `auto_disable.py` contract
matches every consumer. `disable_model(model, reason, auto_or_manual)`
is used by the drift detector with `auto_or_manual="auto"`, by the
hardening tests with `auto_or_manual="manual"`, and is documented in
the public API surface of `__init__.py`. The `PROTECTED_FROM_AUTO_DISABLE`
frozenset is the single source of truth and is exported via
`__all__`.

---

## 12. Reporting

* **Verdict**: PASS with 2 critical migration fixes + 1 chained-comparison fix applied in-scope.
* **Files changed**: 7 (loss_aggregator.py, 039_calibration_loss_history.sql, 040_model_disable_state.sql, plus 4 new hardening-test files; plus 5 ruff import-order autofixes; plus this audit doc).
* **Hardening test count**: 21 new tests (across 4 files).
* **Cross-cutting findings count**: 4 (engine hooks, Telegram commands, metrics, config — three of which are already declared, awaiting wiring; one is operator scope).
* **Test counts**: `tests/test_calibration/` = 78 passed; full suite = 1,847 passed / 9 skipped / 2 xfailed / 2 pre-existing R10 numerical failures unrelated to R13.
* **sklearn cross-validation status**: Brier, log_loss (binary + 3-class), MAPE all match `sklearn.metrics` to relative tolerance 1e-9.
* **Notebook JSON validity**: 6/6 OK.
* **Dirty-tree confirmation**: working tree dirty; no commit made (per wave-3 charter constraint).
