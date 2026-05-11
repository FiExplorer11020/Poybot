# Round 13 — Continuous Calibration Loop + Research Notebook

> **Formal title**: Self-Validation & Research Substrate
> **Colloquial name**: The Mirror
> **Prerequisite**: Rounds 6–12 all shipped. R13 closes the loop on
> everything; it has no value as a standalone round.

---

## 1. The thesis — a bot that knows when it's wrong

After R6–12 the bot has substantial machinery: data sovereignty (R6),
mempool front-door (R7), strategy classifier (R8), multivariate Hawkes
+ Kalman (R9), causal inference (R10), microstructure features (R11),
social + cross-market (R12). Each component makes predictions.

**Predictions decay.** Strategy classes drift (R8 § 3.5 detects this).
Hawkes coupling matrices grow stale as new leaders enter the universe.
Causal estimates become invalid when the underlying instrument validity
changes. Without continuous re-validation, a bot built on a perfect
foundation runs blind into a stale model and bleeds money silently.

> Round 13 builds the **continuous calibration loop**: every decision
> the bot makes gets logged with a counterfactual prediction; every
> day a batch job replays yesterday's decisions to compute per-model
> calibration loss; when any model's loss exceeds a threshold, the
> bot **automatically suppresses that model's contribution** to the
> decision flow until manual review.

Plus, R13 ships the **research substrate** — a directory of Jupyter
notebooks that use the cold tier + feature store + everything else
to answer ad-hoc questions in minutes, not hours. This is what
sustains the research velocity over the years the bot will run.

---

## 2. The architecture — one new daemon, one notebook tree

```
systemd units (post-R13):
  polymarket-engine.service          # (existing)
  polymarket-observer.service        # (existing)
  polymarket-onchain.service         # (R6)
  polymarket-crawler.service         # (R6)
  polymarket-falcon-refresher.service # (R6)
  polymarket-mempool.service         # (R7)
  polymarket-book-l3.service         # (R11)
  polymarket-social.service          # (R12)
  polymarket-crossmarket.service     # (R12)
  polymarket-calibration.service     # (R13 — NEW)
  polymarket-api.service             # (existing)
```

New directory at the **repository root** (not under `src/`):
```
research/
├── README.md
├── notebooks/
│   ├── 00_data_loader.ipynb
│   ├── 01_strategy_classifier_validation.ipynb
│   ├── 02_causal_analysis.ipynb
│   ├── 03_counterfactual_replay.ipynb
│   ├── 04_what_if_explorer.ipynb
│   └── 05_calibration_review.ipynb
└── duckdb/
    └── research.duckdb           # local DuckDB file, gitignored
```

Notebooks are git-tracked (history of analyses); the duckdb file is
not (it's a working DB).

The calibration daemon writes to a new schema (migration 046); the
notebooks read from the cold tier (R6) and the new schema, plus the
existing feature store.

---

## 3. Component breakdown

### 3.1 `src/calibration/decision_replay.py` — Decision counterfactual logger

Every decision the bot makes already gets logged to `decision_log`
(since Phase 0). R13 adds a SISTER table that records what each model
PREDICTED at the time:

```sql
-- Migration 046
CREATE TABLE decision_predictions (
    decision_id BIGINT NOT NULL REFERENCES decision_log(id),
    predicted_at TIMESTAMPTZ NOT NULL,
    -- Per-model predictions captured at decision time:
    follow_confidence NUMERIC(8, 6),
    fade_confidence NUMERIC(8, 6),
    strategy_class VARCHAR(20),
    strategy_confidence NUMERIC(8, 6),
    hawkes_alpha_mu NUMERIC(10, 6),
    volume_forecast_usdc NUMERIC(20, 2),
    volume_forecast_ci_low NUMERIC(20, 2),
    volume_forecast_ci_high NUMERIC(20, 2),
    causal_ate NUMERIC(10, 6),
    causal_ate_ci_low NUMERIC(10, 6),
    causal_ate_ci_high NUMERIC(10, 6),
    -- Outcomes (filled later when the position closes):
    actual_pnl_usdc NUMERIC(20, 2),
    actual_followup_volume_usdc NUMERIC(20, 2),
    closed_at TIMESTAMPTZ,
    PRIMARY KEY (decision_id)
);
```

Decision-time predictions are captured atomically with the decision
itself (same transaction in `confidence_engine.decide()`). When the
position closes (paper or live), the actual outcomes get filled in
via a `position_tracker` hook.

### 3.2 `src/calibration/loss_aggregator.py` — Per-model loss computation

Nightly batch:

```python
class ModelLossAggregator:
    """For each model, compute calibration loss over yesterday's
    decisions.

    Model: 'follow_confidence'
      Loss: Brier score = mean((predicted_win_prob - 1{realised_win})²)

    Model: 'volume_forecast'
      Loss: MAPE = mean(|forecast - actual| / actual)
      Plus: CI-coverage rate = fraction of times actual ∈ [ci_low, ci_high]
      Target: coverage ≈ 0.95 (well-calibrated)

    Model: 'causal_ate'
      Loss: comparison vs realized counterfactual estimate (via
            instrumental-variable re-fit on yesterday's data)

    Model: 'strategy_class'
      Loss: log-loss against ground truth strategy (derived from
            actual trades — high cancel-to-fill ratio = market_maker etc.)
      This is weaker than the labelled validation set, but it's
      continuous + automated.

    Per-model loss history → `calibration_loss_history` (mig. 047)
    """
```

### 3.3 `src/calibration/drift_detector.py` — Threshold-based alerting

```python
class ModelDriftMonitor:
    """For each (model, strategy_class) pair, maintain a rolling
    30-day baseline of calibration loss. Alert if today's loss
    deviates > 2σ from baseline.

    Alert pathways:
      1. Prometheus metric polybot_model_drift_score{model, strategy}
      2. Telegram alert via the notifier (rate-limited 1 per model
         per hour)
      3. Auto-disable hook (see § 3.4) if 3 consecutive days of drift
    """
```

### 3.4 `src/calibration/auto_disable.py` — Self-suppression

```python
class ModelAutoDisabler:
    """When a model has drifted for 3+ consecutive days, automatically
    suppress its contribution to the decision flow.

    Suppression mechanism (per model):
      - Set runtime_config flag: <model>_enabled = false
      - confidence_engine reads the flag, skips that model's
        contribution
      - Operator notified via Telegram; manual review required to
        re-enable

    Examples:
      - If 'volume_forecast' drifts → R9's volume_anticipation policy
        is gated off; FOLLOW / FADE continue
      - If 'causal_ate' drifts → R10's causal gate is removed; we
        revert to pure-Hawkes confidence (and accept the higher
        false-positive rate)
      - If 'strategy_class' drifts → R8 conditional weights revert to
        uniform; FOLLOW / FADE decisions remain but without strategy
        flavor

    The bot DEGRADES GRACEFULLY rather than failing catastrophically.
    """
```

### 3.5 `research/notebooks/` — The research substrate

| Notebook | Purpose |
|---|---|
| `00_data_loader.ipynb` | Sets up DuckDB views over the cold Parquet tier. Reusable cell-1 setup for all other notebooks. |
| `01_strategy_classifier_validation.ipynb` | Re-validates R8 against held-out hand-labels; surfaces wallets where the classifier disagrees with manual labels. |
| `02_causal_analysis.ipynb` | Plots IV vs Hawkes disagreement; surfaces (leader, pool) pairs where statistical and causal estimates diverge most. |
| `03_counterfactual_replay.ipynb` | Interactive what-if: change a runtime parameter, replay last 30 days' decisions, compute hypothetical Sharpe diff. |
| `04_what_if_explorer.ipynb` | Per-hypothesis explorer: "what if R8 had a 10th class for X?", "what if we'd shipped R11 a quarter earlier?" |
| `05_calibration_review.ipynb` | Reads from calibration_loss_history; shows per-model drift trajectories; the analyst's auto-disable triage tool. |

The notebooks themselves are the deliverable. They're git-tracked so
their evolution is auditable; they pin to specific versions of
feature_store, DuckDB, the cold-tier schema.

### 3.6 Operator-facing Telegram commands

```python
# Extends src/telegram_bot/commands.py

/calibration                 → returns per-model loss summary today
/calibration <model>         → returns detailed loss + drift score
/disable <model>             → manually disable a model (operator override)
/enable <model>              → re-enable
/disabled                    → list currently-disabled models
```

The auto-disabler can be operator-overridden in both directions.

---

## 4. Migration sequence

```sql
-- Migration 046
-- See § 3.1 for `decision_predictions`

-- Migration 047
CREATE TABLE calibration_loss_history (
    model VARCHAR(40) NOT NULL,
    strategy_class VARCHAR(20),  -- NULL = aggregate
    measured_at DATE NOT NULL,
    n_decisions INTEGER NOT NULL,
    brier_score NUMERIC(8, 6),
    log_loss NUMERIC(8, 6),
    mape NUMERIC(8, 6),
    ci_coverage NUMERIC(5, 4),
    PRIMARY KEY (model, strategy_class, measured_at)
);
CREATE INDEX idx_clh_measured ON calibration_loss_history (measured_at DESC);

-- Migration 048
CREATE TABLE model_disable_state (
    model VARCHAR(40) PRIMARY KEY,
    is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    disabled_at TIMESTAMPTZ,
    disabled_reason VARCHAR(200),
    auto_or_manual VARCHAR(10) NOT NULL DEFAULT 'auto'  -- auto|manual
);
```

---

## 5. New Prometheus metrics (Round 13 contributes ~10)

```
polybot_calibration_runs_total                      # nightly batch runs
polybot_calibration_loss{model, strategy_class}     # gauge, daily
polybot_calibration_baseline_loss{model, strategy_class}  # rolling 30d baseline
polybot_model_drift_score{model, strategy_class}    # z-score vs baseline

polybot_model_disabled{model}                       # 0/1 gauge per model
polybot_model_auto_disable_total{model}             # counter
polybot_model_manual_disable_total{model}           # counter
polybot_model_enable_total{model}                   # counter

polybot_counterfactual_replay_duration_seconds{kind}
polybot_research_notebook_executions_total          # operator-driven
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| decision_predictions schema + hook into decision flow | 0.5 |
| Loss aggregator nightly batch | 0.75 |
| Drift detector + Telegram alerts | 0.5 |
| Auto-disable runtime-config integration | 0.5 |
| Research notebooks (5 of them) | 1.0 |
| Migrations + tests + audit doc | 0.25 |
| Operator-facing Telegram commands | 0.25 |
| **Total** | **~3.75 weeks** |

### Dependencies

- ALL of R6–R12. R13 has nothing to calibrate without them.
- The cold tier (R6 § 3.6) is the substrate for the research notebooks
- decision_log (already exists since Phase 0); decision_predictions
  joins it

### Risk: 2/5

| Risk | Severity | Mitigation |
|---|---|---|
| Auto-disable too sensitive → silent under-trading | Medium | Conservative thresholds (3 days of >2σ drift); operator gets Telegram alert before auto-disable; manual override always available |
| Calibration loss computation is itself buggy | Low | Unit tests per model loss function; weekly manual spot-check |
| Notebook environment rot (Python packages drift) | Low | Pin all notebook deps in `research/requirements.txt`; CI executes notebooks weekly to catch breakage |
| Over-reliance on the auto-disabler | Low | Operator-facing dashboard surfaces which models are running vs disabled; obvious whenever auto-disable has degraded the system |

### Acceptance criteria

- Nightly calibration batch completes in < 10 min
- Per-model loss history populates for ALL major models within 7 days
- Drift detector fires (in shadow mode) for at least one model in a
  90-day historical replay (proves it's responsive)
- A research notebook can answer "what if R8 strategy weights for
  directional were 2.0 instead of 1.5" in < 5 min wall time
- Operator can disable any model via Telegram in < 30 s
- After 30 days of live operation, at least one auto-disable event
  fires (or the bot is provably perfectly calibrated — either way,
  signal)

---

## 7. Rollout plan

### Phase 13.A — Prediction logging (week 1)
1. Migration 046 (decision_predictions)
2. Hook into confidence_engine.decide() — atomic write
3. Hook into position_tracker close events to fill outcomes
4. **Gate**: 7 days of clean prediction logging, ≥ 95 % of decisions
   have populated outcomes after 30 min

### Phase 13.B — Loss aggregation (week 2)
1. Migration 047 (calibration_loss_history)
2. Nightly batch job in batch_runner.py
3. Backfill 90 days of historical losses from existing decision_log
   + position outcomes
4. **Gate**: per-model loss history populated for 90 days

### Phase 13.C — Drift detector + Telegram (week 2.5)
1. Rolling 30-day baseline + drift z-score per model
2. Telegram alert wired
3. **Gate**: 7 days of drift monitoring, at least 1 alert fires in
   historical replay

### Phase 13.D — Auto-disable + override (week 3)
1. Migration 048 (model_disable_state)
2. confidence_engine reads disable flags before model contributions
3. Telegram /disable /enable /disabled commands
4. **Gate**: operator-tested commands work end-to-end

### Phase 13.E — Research notebooks (week 3.5)
1. 5 notebooks with tutorial-quality comments
2. DuckDB cold-tier views set up
3. `research/README.md` walks a researcher through setup
4. **Gate**: a new analyst can run notebook 03 and answer one what-if
   without operator help

---

## 8. What this round explicitly does NOT do

- **Does NOT retrain models automatically**. R13 detects drift and
  disables the affected model; retraining stays a manual operator
  decision (with the notebook substrate to support the analysis).
  Auto-retraining is a Round-14+ research topic.
- **Does NOT change the decision logic itself**. R13 OBSERVES the
  decision logic and gates contributions, never substitutes for the
  models.
- **Does NOT build a separate ML platform** (MLflow, Weights &
  Biases). Notebooks + cold tier + Postgres are sufficient at our
  scale. Adding MLflow would be premature complexity.
- **Does NOT add public-facing analytics**. The notebooks are for
  the operator + any internal analysts. No web UI; that's the
  dashboard's job and the dashboard doesn't need to expose research.

---

## 9. The non-obvious gains

1. **The calibration_loss_history table becomes the bot's own
   experience replay buffer**. Every future model improvement can be
   evaluated retrospectively: "if we'd shipped this improvement on
   day X, the drift score on day Y would have been Z." Compound
   learning over years of operation.

2. **Auto-disable converts unknown-unknowns into known-unknowns**.
   Today, if R10 causal inference silently degrades, we lose money for
   weeks before noticing. After R13, we lose money for ≤ 3 days then
   the model gets auto-suppressed AND we get a Telegram alert.
   Reduction in tail risk is enormous.

3. **The research substrate is the recruiting tool**. A new analyst
   can be productive in week 1 by running existing notebooks and
   forking. The notebooks ARE the onboarding curriculum.

4. **Per-model loss histories enable model-vs-model trade-off
   analysis**. "When the strategy classifier and volume forecast
   disagree, which is more likely to be right?" → query the history,
   weight future decisions accordingly. Meta-model behavior emerges
   from the data, not from hand-coding rules.

5. **Operator confidence grows in proportion to the auto-disable
   safety net**. The operator becomes willing to allow higher
   automation (less hand-holding) because they trust the bot to
   self-suppress on drift. This unblocks Round 14+'s "more autonomy"
   work — but only because R13 makes it safe.

---

## 10. The single sentence

> Round 13 makes the bot **see itself** — every prediction logged
> with its outcome, daily calibration loss per model, drift-aware
> auto-suppression of stale models, plus a research-notebook
> substrate that turns the cold tier and feature store into a
> what-if exploration toolkit the operator and any future analyst
> can use in five minutes flat.
