# Profiler Module — Behavioral Profiles + Error Modeling

**Purpose**: Build hierarchical per-leader behavioral profiles, then feed them to a 3-phase error model
that predicts P(leader loses) in a given market context. Detect behavioral drift via CUSUM.

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **behavior_profiler.py**: Per-leader profile from observed trades. Extract:
  - preferred_categories: Dirichlet distribution over market categories (politics, crypto, sports, etc.)
  - entry_patterns: contrarian vs momentum rate, time-of-day distribution (KDE)
  - sizing: mean, EWMA-smoothed sizing (Exponential Weighted Moving Average)
  - accuracy: per-category win/loss counts (Beta posteriors), overall win rate

- **error_model.py**: Hierarchical 3-phase model. Phase determined by count of resolved positions:
  - Phase 1 (0-99 resolved): Beta-Binomial per market category
  - Phase 2 (100-499 resolved): Bayesian Logistic Regression on 90-day behavior features
  - Phase 3 (500+ resolved): LightGBM + Platt calibration on all resolved trades
  Includes CUSUM drift detection; downgrade phase if accuracy drops.

- **models.py**: Profile, ErrorPrediction, ModelPhase dataclasses.

---

## Key Algorithms

### Behavioral Profile (Real-time, O(1) update)

**Dirichlet for category preference** (size-weighted):
```
α_category[i] = pseudo-count for category_i
On new resolved position in category j:
  weight = _size_weight(size_usdc, profile.sizing.ewma_size)
  α_category[j] += weight
P(next trade in category_j) = α_category[j] / Σ α
```

`_size_weight` returns a value in `[0.5, 3.0]` via `sqrt(size / ewma_size)`,
clamped. Larger trades carry more weight in the leader's preferences (a
$50k trade indicates more conviction than a $50 one), but the
sub-linear scaling prevents a single whale trade from dominating the
prior. A trade with `size_usdc <= 0` or no baseline yet falls back to
weight `1.0`. Uninformed prior: all α = 1 (uniform).

**EWMA for position sizing** (updated FIRST, before the size-weighted
posteriors that depend on it):
```
μ_size_ewma = λ · μ_size_ewma_prev + (1 - λ) · size_new
λ = 0.94 (half-life ≈ 10-15 days)
```
Update on every trade (not just closed positions). O(1) scalar update.

**KDE for timing distribution**:
```
Fit KDE on (hour_of_day) from 60+ resolved trades
Returns: peak_time, spread (σ), probability of trading in [0:00, 6:00) UTC, etc.
Update weekly (batch job).
```

### Error Model — Phase 1: Beta-Binomial (0-99 resolved)
Simplest phase. Per (leader, market_category):
```
P(loss | category) = β / (α + β)  [Beta posterior]

On resolved position:
  If loss: β += 1
  If win: α += 1

Prediction: P(leader loses) = β / (α + β)
Uninformed prior: α = β = 1
```
Update O(1) per resolution.

### Error Model — Phase 2: Bayesian Logistic Regression (100-499 resolved)
More features. Fit every 24h on 90-day sliding window:
```
Features (per trade):
  - hours_since_category_last_trade
  - market_volatility_24h
  - leader_sizing_deviation (how much size deviates from EWMA)
  - hours_since_last_loss (CUSUM indicator)
  - category_accuracy_so_far
  - day_of_week (cyclical)

Target: Y = 1 if loss, 0 if win

Model: Bayesian LogReg (numpyro or sklearn.BayesianRidge)
Output: P(loss | features, posterior)
```
Re-fit every 24h. Takes ~30s for 90 days of data.

### Error Model — Phase 3: LightGBM + Platt Calibration (500+ resolved)
Full power. Fit weekly on ALL resolved data:
```
Features: same as Phase 2, plus:
  - Market liquidity score (from Falcon agent 575)
  - Time since market opened (days)
  - Leader's follower count (from graph module)

Model: LightGBM (gradient boosting)
Output: raw score ∈ [0, 1]
Post-process: Platt calibration to convert score → P(loss)
  [fit isotonic or sigmoid on held-out validation set]

Training: stratified 80/20 split, early stopping on val loss.
```
Re-fit every 7 days (weekend batch). Full training ~5 min.

### CUSUM Drift Detection (Cold path, batch)
Detect if leader's error rate is rising (behavioral change):
```
S = max(0, S_prev + error - baseline - slack)
error = 1 if actual_loss else 0
baseline = model's predicted P(loss)
slack = 0.05 (allow 5% tolerance)

If S > threshold (e.g., 3): leader's behavior has drifted
Action: downgrade error_model_phase by 1, reset S = 0, accumulate fresh data

Why: a leader's style might change over time (e.g., switch from momentum to contrarian).
Avoid stale profiles.
```

---

## Critical Pitfalls

1. **Profiler FEEDS error model**: Profile is input, not output. Profile must be rich and accurate.
   Don't skip behavioral feature extraction.

2. **deviation_score is KEY**: How much does this trade deviate from the leader's EWMA sizing?
   `deviation_score = |size_actual - μ_size_ewma| / μ_size_ewma`. High deviation → leader is
   unsure → higher error risk. Must include in Phase 2+.

3. **Don't re-fit Phase 2+ too often**: Re-fit every 24h max. More frequent refits → overfitting.
   Phase 3 (LightGBM) re-fit every 7 days only.

4. **Category matters**: A leader might be 80% accurate in crypto, 40% in politics.
   Don't use single global error rate. Always stratify by (leader, category).

5. **Drift detection DOWNGRADES, not resets**: If drift detected, downgrade phase (e.g., 3→2),
   keep historical data, but start fresh learning. Don't delete old profiles.

6. **Update `_update_sizing` BEFORE the size-weighted posteriors**: `_update_dirichlet` and
   `_update_accuracy` both call `_size_weight(size_usdc, ewma_size)`. If you swap that order,
   the first trade for a new leader will use a stale (zero) baseline and weight wrong.
   The `on_position_closed` path enforces this ordering explicitly.

---

## Testing Approach

- **Unit tests**:
  - Mock 10 resolved trades (6 wins, 4 losses) in "crypto" category. Verify Dirichlet α_crypto = 7, β = 5.
  - Verify Beta posterior P(loss) = 5/12 ≈ 0.42.
  - Mock position sizes [100, 110, 95, 120, 100, ...]. Verify EWMA tracking trend smoothly.
  - Test CUSUM: inject 5 consecutive losses when model predicts 20% loss rate. Verify S rises, drift triggered.

- **Integration tests**:
  - Real DB: insert 120 resolved positions (30 per category) for a leader.
  - Verify Phase 1 profiles (Dirichlet) calculated correctly.
  - Trigger Phase 2: fit Bayesian LogReg, verify model converges.
  - Verify error predictions: P(loss) ∈ [0, 1], sensible given features.
  - Inject drift: add 10 trades with unusual features. Verify CUSUM triggers downgrade.

---

## References
- Falcon agent 575: Market Insights (liquidity for Phase 3 features)
- Database: `leader_profiles`, `positions_reconstructed` tables (master CLAUDE.md § 6)
- Constants: `EWMA_LAMBDA` (0.94), `MIN_TRADES_FOR_PROFILE` (20), phase triggers from config.py
- Libraries: scipy (KDE), numpyro (Phase 2), lightgbm + sklearn (Phase 3)
- Batch schedule: `BATCH_HOUR_UTC` (3 AM) from config.py
