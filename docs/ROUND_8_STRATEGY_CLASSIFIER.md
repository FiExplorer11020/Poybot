# Round 8 — Strategy Classifier (The "Why" Layer)

> **Formal title**: Per-Leader Strategy Fingerprinting
> **Colloquial name**: The Lens
> **Prerequisite**: Round 6 ([THE SPINE](ROUND_6_THE_SPINE.md)) — needs
> `wallet_universe`, on-chain trade history, and the cold Parquet tier
> for training-set construction.

---

## 1. The thesis — a bot that knows the WHY of every trade

Today every leader looks the same to the bot: a wallet with a Falcon
score and a Beta-Binomial accuracy posterior. We FOLLOW or FADE based
purely on win-rate. That's blunt.

A swing trader behaves nothing like a market-maker. A momentum trader
behaves nothing like an arbitrage bot. The features that make their
trades profitable (or predictable) are different. The signals to act
on are different. The exit timing is different.

> **The Lens classifies every leader into one of 9 strategy classes
> with calibrated probability.** Downstream code conditions on the
> output: FOLLOW confidence high on directional swing; FADE confidence
> high on info-leak gone cold; SKIP on structural bots (already
> excluded). Position sizing scales differently per class.

This is the layer that converts "leader X is 60 % accurate" into
"leader X is a directional swing trader who's 60 % accurate in the
political category and 40 % accurate in crypto, with α/μ = 1.4 against
the political-follower pool, average holding period 4 days, never
trades fees > 1 %." That second sentence is actionable. The first one
isn't.

---

## 2. The 9 strategy classes

| Class | Behavioral signature | How to trade them |
|---|---|---|
| **directional** | Long holding period (days-weeks), trades on conviction, low cancel-to-fill | FOLLOW with leader-confidence weight, ride the move |
| **momentum** | Enters AFTER price moves, short holding period (hours), high volume | Conditional FOLLOW based on regime; FADE on exhaustion patterns |
| **contrarian** | Enters AGAINST price moves, longer holding | FOLLOW with patience — exits are the alpha, not entries |
| **arb_2way** | Symmetric YES+NO positions, exits on mid-price convergence | SKIP — we can't replicate the arb edge |
| **arb_3way** | Cross-market or cross-token arbitrage | SKIP — same reason |
| **market_maker** | Tight spreads on both sides, frequent quote updates, low fill rate | SKIP — pure spread capture, not directional |
| **structural_bot** | < 100 ms decision latency, deterministic patterns | EXCLUDE entirely (already handled by Phase 0 logic) |
| **info_leak** | Entries cluster minutes after news events; rare but high-edge | FADE when their classification "decays" (drift detected) |
| **social_driven** | Entries cluster with X/Telegram posting velocity | FOLLOW if social signal corroborates; SKIP if social is silent |

The taxonomy is **opinionated, not exhaustive**. We may discover new
classes via unsupervised clustering (§ 3.5). When we do, we add a class,
re-label, retrain.

---

## 3. Component breakdown

### 3.1 `src/strategy_classifier/features.py` — Feature engineering

Each leader gets a fixed-shape feature vector computed from the cold
tier (DuckDB query against 90 days of trades + book events). Features
are **strategy-discriminating**, not just descriptive.

```python
class LeaderFeatureExtractor:
    """Per-wallet feature vector (~40 dimensions) computed for an
    asof_ts using point-in-time-correct reads.

    Categories:

    A. VELOCITY (5 features)
       - trades_per_day (median across 30d)
       - trades_per_day_std
       - inter_trade_interval_median_s
       - inter_trade_interval_p99_s
       - active_day_fraction  (days with ≥1 trade / 30)

    B. HOLDING PERIOD (5 features)
       - holding_period_median_s  (from positions_reconstructed)
       - holding_period_p25_s, p75_s
       - close_method_distribution  (sell|merge|resolution)
       - fraction_closed_within_1h

    C. SIZING (4 features)
       - size_median_usdc
       - size_p25, p75
       - size_cv  (std/mean — high CV = whale-like volatility)

    D. CATEGORY MIX (5 features)
       - category_entropy  (Dirichlet entropy on preferred_categories)
       - top_category_share
       - distinct_categories_30d
       - fees_paid_pct  (have they avoided fee-heavy categories?)
       - resolution_market_share  (do they hold to resolution?)

    E. ENTRY MICROSTRUCTURE (8 features)
       - microprice_deviation_at_entry_median  (from R2 OB)
       - spread_bps_at_entry_median
       - depth_imbalance_at_entry_median
       - price_momentum_5m_at_entry  (signed)
       - price_momentum_60m_at_entry
       - book_age_ms_at_entry_median  (stale-book trades?)
       - cancel_to_fill_ratio_30d
       - takes_vs_makes_ratio  (R11 microstructure will refine)

    F. EXIT MICROSTRUCTURE (4 features)
       - exit_vs_resolution_pnl_ratio  (did they get a better price by exiting early?)
       - exit_after_news_event_pct  (R10 cross-ref)
       - sequential_exit_chunks_median  (one big sell vs many small)
       - merge_exit_pct  (sophisticated exit pattern)

    G. NETWORK (4 features)
       - confirmed_follower_count  (R9 multivariate Hawkes output)
       - alpha_mu_ratio_to_follower_pool
       - is_followed_back_pct  (mutual following)
       - cluster_density  (graph clustering coefficient)

    H. SOCIAL (4 features, R12 wires)
       - social_signal_density  (R12)
       - tweets_per_active_day
       - tweet_to_trade_lag_median_s  (R12)
       - social_signal_strategy_concordance  (NLP-classified intent)

    I. TEMPORAL (3 features)
       - trading_hour_kde_peak  (from profiler KDE)
       - weekday_bias  (weekday vs weekend trades)
       - time_of_day_entropy

    Total: ~42 features. Each feature is itself an as-of read via the
    feature_store API.
    """
```

**Critical**: every feature is computed via `feature_store.get_*_asof`
with the wallet's `last_active` timestamp as `asof_ts`. **No future
leakage** in the training data.

### 3.2 `src/strategy_classifier/labeling/` — Hand-label store

```
src/strategy_classifier/labeling/
├── label_store.py           # SQL-backed store with audit trail
├── batch_labeler.ipynb      # Jupyter notebook for the human label pass
└── labeling_protocol.md     # Operational guide for hand-labellers
```

The label store is **append-only** with provenance:

```sql
-- Migration 026 (part 1)
CREATE TABLE strategy_labels (
    label_id BIGSERIAL PRIMARY KEY,
    wallet_address VARCHAR(100) NOT NULL,
    label_window_start DATE NOT NULL,
    label_window_end DATE NOT NULL,
    primary_strategy VARCHAR(20) NOT NULL,  -- one of the 9 classes
    secondary_strategy VARCHAR(20),          -- mixed-strategy wallets
    confidence FLOAT NOT NULL,               -- labeller's 0-1 confidence
    labeller VARCHAR(50) NOT NULL,           -- operator name
    labelled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rationale TEXT,                          -- why this label
    CONSTRAINT chk_strategy CHECK (
        primary_strategy IN (
            'directional', 'momentum', 'contrarian',
            'arb_2way', 'arb_3way', 'market_maker',
            'structural_bot', 'info_leak', 'social_driven'
        )
    )
);
CREATE INDEX idx_strategy_labels_wallet ON strategy_labels (wallet_address, labelled_at DESC);
```

**Per-(wallet, 30-day-window) labels, not per-wallet**. Same trader may
switch strategy across periods. The window granularity matches the
feature extractor's 30-day rolling lookback.

**Inter-labeller agreement**: for a 20-wallet validation set, two
operators label independently; we measure Cohen's κ. Target κ > 0.7;
disagreements get adjudicated. This catches taxonomy fuzziness early.

### 3.3 `src/strategy_classifier/model.py` — The classifier

```python
class StrategyClassifier:
    """LightGBM multi-class classifier mapping LeaderFeatureExtractor
    output → 9-class softmax + isotonic calibration.

    Training data: 100 hand-labelled (wallet, window) pairs from
    strategy_labels. Stratified by primary_strategy. 80/20 train/val.

    Model: LightGBM with 9-way multiclass objective, monotone
    constraints where the feature has natural directionality (e.g.,
    higher cancel_to_fill_ratio → more market_maker-like).

    Calibration: isotonic regression on the val set's per-class scores
    so the output probabilities are well-calibrated (a 0.7 directional
    means 70 % of such-predicted wallets are truly directional).

    Output:
      {
        'strategy_probs': {'directional': 0.7, 'momentum': 0.2, ...},
        'primary_strategy': 'directional',
        'confidence': 0.7,
        'model_version': 'sc.v1.0',
        'fitted_at': '...'
      }

    Stored in leaders.classification_json (Phase 3 R2 schema extension)
    and append-only to leader_strategy_history (migration 026 part 2).
    """
```

### 3.4 `src/strategy_classifier/cluster.py` — Unsupervised discovery

```python
class UnsupervisedStrategyExplorer:
    """K-means + DBSCAN on the same feature vector. Output: cluster
    assignments per wallet.

    Purpose: discover strategies we forgot to include in the taxonomy.
    The notebook walks the operator through clusters that are SIZABLE
    (>20 wallets) yet POORLY-MATCHED by the supervised classifier
    (avg confidence < 0.5 across the cluster). Those are candidate new
    classes.

    NOT used in production decision flow. This is a research tool only.
    """
```

This is where new strategy classes get born. Example: if K-means finds
a cluster of 50 wallets with high `tweet_to_trade_lag_median_s = -30s`
(tweets AFTER trade) and high `social_signal_density`, that's a new
"shill" or "tout" class we missed.

### 3.5 `src/strategy_classifier/drift.py` — Drift detection

Strategies aren't static. A directional swing trader can pivot to
market-making after they accumulate capital. The classifier output for
that wallet must reflect the change.

```python
class StrategyDriftDetector:
    """For each watched wallet, run the classifier daily. Compare
    today's strategy_probs against the 30-day rolling distribution.
    If the JS divergence exceeds threshold (default 0.3), flag drift.

    On drift:
      - Mark the leader's confirmed-edge entries (R9) as STALE
      - Suppress FOLLOW/FADE recommendations until 30 days of new data
        accumulate
      - Emit polybot_strategy_drift_detected_total{wallet, from, to}
    """
```

### 3.6 Integration with `confidence_engine`

The classifier output becomes a multiplier on the existing Thompson
Sampling output:

```python
# Conceptual sketch — actual code is in confidence_engine.py

def decide(self, leader: str, market: str, signal: dict) -> Decision:
    # ... existing Thompson sampling on Beta(α_follow, β_follow) ...
    follow_score = thompson_sample(profile.accuracy)
    fade_score = thompson_sample(profile.fade_accuracy)

    # NEW: condition on strategy class
    strategy = self.classifier.get(leader)  # from leader_strategy_history
    weights = STRATEGY_WEIGHTS[strategy.primary_strategy]
    #   directional:  FOLLOW=1.5, FADE=0.5, SKIP=1.0
    #   momentum:     FOLLOW=1.0, FADE=1.0, SKIP=1.2
    #   contrarian:   FOLLOW=1.2, FADE=0.8, SKIP=1.0
    #   ...
    #   structural_bot: FOLLOW=0.0, FADE=0.0, SKIP=∞  (already excluded)
    #   info_leak:    FOLLOW=0.5, FADE=2.0, SKIP=1.0  (we FADE these)

    follow_score *= weights['follow']
    fade_score *= weights['fade']
    skip_score = weights['skip'] * baseline_skip

    return Decision(argmax({follow, fade, skip}))
```

The strategy weights themselves are **hyperparameters**, not learned.
They're operator-tunable via runtime config. The classifier provides
the strategy; the operator decides the policy per strategy.

---

## 4. Migration sequence

| Migration | Purpose |
|---|---|
| 026 | `strategy_labels` (hand-labels) + `leader_strategy_history` (model outputs, append-only) |
| 027 | `leaders.classification_json` schema extension (already partially used; formalize the schema) |

---

## 5. New Prometheus metrics (Round 8 contributes ~10)

```
polybot_classifier_predictions_total{strategy, source}    # source: scheduled|on_demand
polybot_classifier_confidence{strategy}                    # histogram
polybot_classifier_loss{set}                               # set: train|val|live
polybot_classifier_calibration_loss{strategy}              # per-class Brier
polybot_classifier_drift_score{wallet}                     # gauge JS divergence
polybot_strategy_drift_detected_total{from, to}            # transitions
polybot_strategy_label_set_size{strategy}                  # hand-label set growth
polybot_unsupervised_clusters_unmatched                    # how many K-means clusters our supervised model doesn't fit
polybot_classifier_inference_seconds                       # wall time per inference
polybot_classifier_feature_extraction_seconds              # the 42-feature load via DuckDB
```

---

## 6. Effort, dependencies, risk

### Effort (single dev + 1 week labeller, mostly serial)

| Component | Weeks |
|---|---|
| Hand-label 100 wallets (the bottleneck — see § 7.A) | **1.0** (dedicated, single-task) |
| 3.1 — feature engineering + DuckDB queries | 1.0 |
| 3.2 — label_store + protocol doc + labelling notebook | 0.5 |
| 3.3 — classifier model + isotonic calibration + serialization | 0.75 |
| 3.4 — unsupervised cluster explorer | 0.5 |
| 3.5 — drift detector | 0.5 |
| 3.6 — confidence_engine integration | 0.5 |
| Migrations + tests + audit doc | 0.5 |
| **Total** | **~5 weeks** (1w label + 4w engineering) |

### Dependencies

- Round 6: `wallet_universe`, cold Parquet tier (the training-set query
  scans 90 days of trades — only practical against DuckDB)
- Round 5 BIC Hawkes: feature G uses α/μ ratio from `follower_edges`
- Round 11 (CLOB book L3): would refine features E + F if available;
  not blocking — we ship V1 of features without microstructure depth
  and refine in R11

### Risk: 3/5

| Risk | Severity | Mitigation |
|---|---|---|
| Hand-label subjectivity → low inter-labeller agreement | Medium | Cohen's κ measurement on 20-wallet validation pair-label; adjudicate disagreements; refine protocol |
| Class imbalance (most are directional) | High | Stratified sampling + class weights in LightGBM + targeted oversampling for arb_3way and info_leak |
| Concept drift over time | Medium | § 3.5 detector + monthly retrain |
| Falcon score correlates with classifier output → label leakage | Medium | Exclude Falcon score from features (it's a meta-feature about classifier-able-ness, not a strategy primitive); train holding it out |
| 100 labels too few → overfit | High | Bootstrap confidence intervals on val set; if instability is high, label 100 more. Don't deploy if val AUC < 0.7 |

### Acceptance criteria

- Cohen's κ between two independent labellers on the 20-wallet
  validation set ≥ 0.7
- Held-out validation accuracy ≥ 75 % overall, ≥ 60 % on minority
  classes (info_leak, arb_3way)
- Calibration: per-class Brier score ≤ 0.15
- Confidence-engine A/B test: 30-day paper backtest with classifier-
  conditional weights shows Sharpe ≥ 1.2× baseline (strategy-agnostic)
- Drift detector fires for at least 5 wallets in a 90-day backwards
  validation — proves it's not silent

---

## 7. Rollout plan

### Phase 8.A — Hand-labelling sprint (week 1, single-dev focused)
1. Operator + 1 second labeller (could be from outside the project)
   independently label 20 validation wallets
2. Measure inter-labeller κ; if < 0.7, refine protocol, re-label
3. Operator labels remaining 80 wallets solo (validated by 5-wallet
   spot-check from 2nd labeller)
4. **Gate**: 100 labels stored in `strategy_labels`, κ ≥ 0.7 on
   validation subset

### Phase 8.B — Feature pipeline (week 2)
1. Implement `LeaderFeatureExtractor` against DuckDB (cold tier)
2. Validate feature distributions per labelled class — sanity-check
   that "directional" labels really do have long holding periods, etc.
3. **Gate**: feature extraction completes in < 1 s per wallet

### Phase 8.C — Train + calibrate (week 3)
1. Train LightGBM with hyperparameter sweep (Bayesian opt, ~50 runs)
2. Isotonic calibration on val set
3. **Gate**: val accuracy ≥ 75 %, Brier ≤ 0.15

### Phase 8.D — Shadow integration (weeks 4-5)
1. `StrategyClassifier` runs daily, writes to
   `leader_strategy_history`, but `confidence_engine` does NOT yet
   condition on its output
2. Operator inspects predictions; manual audit on top-100 wallets
3. Drift detector enabled, alerts on transitions
4. **Gate**: top-100 audit shows ≥ 80 % "looks right" from operator

### Phase 8.E — Confidence integration (week 5)
1. Flip `STRATEGY_CONDITIONAL_CONFIDENCE_ENABLED=true` in runtime
   config (defaults to false until this round ships)
2. A/B in paper: half the decisions go through the classifier-weighted
   path, half don't
3. **Gate**: A/B Sharpe difference is positive at 95 % statistical
   significance after 30 days

### Phase 8.F — Unsupervised discovery (parallel to all of A–E)
1. K-means/DBSCAN runs weekly on the live wallet universe
2. Operator reviews any cluster with > 20 wallets and avg classifier
   confidence < 0.5
3. If a cluster looks like a real new strategy: add to taxonomy,
   label its members, retrain. Manual loop.

---

## 8. What this round explicitly does NOT do

- **Does NOT replace the existing FOLLOW/FADE logic**. The classifier
  adds a multiplier; the underlying Beta-Binomial / Thompson Sampling
  stays intact.
- **Does NOT auto-label new wallets without operator review**. The
  unsupervised explorer surfaces candidates; the operator decides
  whether to add them to the labelled set.
- **Does NOT use deep learning**. LightGBM is the right tool for
  tabular data at this scale; transformer / NN approaches would be
  overkill and harder to calibrate.
- **Does NOT classify retail wallets**. The classifier only runs on
  the wallet universe's tier-0 and tier-1 (top ~2000 by recent
  volume). Tier-2 stays in the universe for tracking but isn't
  classified — saves compute, and the model wasn't trained on tier-2.

---

## 9. The non-obvious gains

1. **The labelled set becomes the project's most valuable artifact**.
   Manual labels of 100 wallets are 100 hours of expert human
   judgment encoded as data. Every future model can be evaluated
   against this set. It's the project's ground truth.

2. **Drift detection becomes a leader-health metric**. A leader
   shifting strategies isn't just a classifier event — it's
   actionable intel ("this wallet is changing behavior, watch for
   anomalies"). Surface in Telegram for the operator.

3. **The strategy weights are operator-facing levers**. Instead of
   one global `MAX_POSITION_PCT`, the operator can dial up FOLLOW for
   directional + dial down for info_leak independently. Risk-management
   precision goes from one knob to nine.

4. **Unsupervised exploration becomes the team's research seedbed**.
   Every cluster that looks weird is a story to investigate. Over time
   the taxonomy grows organically.

---

## 10. The single sentence

> Round 8 gives every leader a **strategy fingerprint** — 9 classes,
> calibrated probabilities, drift-aware, operator-tunable — so every
> FOLLOW / FADE / SKIP decision is conditioned on what kind of trader
> we're dealing with, not just on win-rate.
