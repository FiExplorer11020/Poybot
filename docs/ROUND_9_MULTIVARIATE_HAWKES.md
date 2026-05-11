# Round 9 — Multivariate Hawkes + Follower-Pool Dynamics

> **Formal title**: Population-Level Causal Coupling
> **Colloquial name**: The Web
> **Prerequisite**: Round 5 (bivariate Hawkes + BIC), Round 6 (universal
> wallet coverage), Round 8 (strategy classifier — provides the pool
> clustering).

---

## 1. The thesis — from pairs to populations

Round 5's bivariate Hawkes (with BIC regularization) gave us a clean
yes/no for any single (leader, follower) pair: does this wallet truly
follow this leader, controlling for chance coincidences? That's the
right tool to confirm individual edges.

**But trading edge doesn't come from confirming individual followers.
It comes from predicting POPULATION FLOW.** When a leader trades, we
care about:
- How much **collective volume** the follower pool will deploy
- How **fast** they'll deploy it (latency distribution)
- How the volume **decays** over the following hour
- How the prediction is **modulated** by other leaders trading
  simultaneously, by market state, by news

That's a population-level dynamical system. Bivariate Hawkes can't
model it. Multivariate Hawkes can.

> **Round 9 builds a population-level causal-coupling model**:
> N-dimensional Hawkes where the dimensions are not individual wallets
> but **STRATEGY-CLUSTERED FOLLOWER POOLS** (from Round 8). Plus a
> Kalman state-space model on per-leader follower-pool size, updated
> on every observed follow-trade.

This is what enables the "volume anticipation" entry policy described
in VISION § 1 — trade ahead of predicted flow, not ahead of leader
signal.

---

## 2. The mathematical setup

### 2.1 Multivariate Hawkes intensity

For a population with N processes (1 leader process + K follower-pool
processes clustered by strategy):

```
λ_i(t) = μ_i + Σ_{j=1..N} α_{ij} · Σ_{k: t_k^j < t} exp(-β · (t - t_k^j))
```

- `μ_i`: baseline rate for process i
- `α_{ij}`: excitation magnitude from process j → process i
- `β`: kernel decay (shared across processes for identifiability)

For a single leader + K follower-pool setup:
- Process 1 = leader, processes 2..K+1 = follower pools
- `α_{i,1}` for i > 1 = leader → pool i (the alpha we want)
- `α_{1,j}` for j > 1 = pool → leader (usually 0 — pools don't excite
  leaders; if non-zero, that's actually a signal that the "leader" is
  echoing follower activity, which is a strategy red flag)
- `α_{ii}` = self-excitation within pool (clustered trades within
  pool i)

The N×N matrix `α` is what we fit.

### 2.2 Block-sparse priors for identifiability

Without constraints, N² parameters are unidentifiable with the data
we have. We enforce:

**Block structure**:
```
       leader   pool_1  pool_2  ...  pool_K
       ┌──────┬──────┬──────┬──────┬───────┐
leader │ self │  ~0  │  ~0  │  ... │   ~0  │  (rare reverse coupling)
pool_1 │ free │ free │  ~0  │  ... │   ~0  │  (no cross-pool excitation)
pool_2 │ free │  ~0  │ free │  ... │   ~0  │
...    │      │      │      │      │       │
pool_K │ free │  ~0  │  ~0  │  ... │ free  │
       └──────┴──────┴──────┴──────┴───────┘
```

- Diagonal: self-excitation (within-pool clustering)
- First column: leader → pool (the alphas that matter)
- First row: pool → leader (constrained to ~0 with light prior)
- Off-diagonal pool-to-pool: constrained to 0 (no cross-pool excitation;
  if a pool excites another pool, it's via the leader)

**This collapses N² ≈ K² parameters down to ~3K + N**. With K=4 pools,
that's ~16 parameters — comfortably fit on 30 days of data.

### 2.3 BIC extension to multivariate

The Round 5 BIC criterion generalizes naturally:

```
   2 · (NLL_at_α_ij=0 − NLL_at_MLE) > log(N_events) · k_penalty
```

where `k_penalty = number of α_ij not constrained to zero in the
block-sparse mask`. For our setup with K=4: `k_penalty = K+K = 8`
(K leader→pool + K self-excitation). The threshold becomes
`8·log(N)` ≈ 60 for N ≈ 1500 events — much stricter than the
bivariate case, which is the right discipline at higher param count.

---

## 3. Component breakdown

### 3.1 `src/graph/hawkes_multivariate.py` — The N-dim fitter

```python
class MultivariateHawkesFitter:
    """N-dim Hawkes MLE with block-sparse priors and BIC model
    selection.

    Inputs:
      leader_times: 1D array
      pool_times: dict[pool_class, 1D array]
      window: T

    Reuses the Round 5 BIC machinery. The block-sparse mask is a
    static parameter (set at construction time per the audit Box
    diagram in § 2.2).

    Solver: L-BFGS-B with bounded constraints on each free α_ij.
    Initialization: H0-mask seed (all α_ij = 0) PLUS one seed per
    free entry with α_ij = 0.1·μ_diagonal.

    Output:
      {
        'alpha_matrix': dict[(i, j), float],  # only free entries
        'mu_vector': dict[i, float],
        'beta': float,
        'log_likelihood': float,
        'bic_threshold': float,
        'bic_statistic': float,
        'accepted_couplings': dict[(i, j), bool],
        'convergence': 'converged' | 'fallback' | 'bic_rejected',
      }
    """
```

### 3.2 `src/follower_volume/kalman.py` — State-space pool dynamics

The Hawkes model tells us **whether** a leader excites a pool. It
doesn't tell us **how much volume** that pool will deploy. For that,
Kalman.

```python
class FollowerPoolKalman:
    """Per-(leader, pool_class) state-space model on pool-deployed
    volume.

    State vector (3D):
      x = [pool_size_usdc,      # current pool capital available
           recent_response_pct, # what fraction of pool reacted to last
                                # leader trade
           decay_rate]          # how fast the response decays

    Observation: each new leader trade triggers a follow-volume burst.
    We observe the burst total over the next 30 min.

    Dynamics:
      x_{t+1} = F · x_t + w_t       (state evolution)
      y_t    = H · x_t + v_t       (observation)

    F is mostly identity with slow decay; H projects to the observed
    volume. w_t, v_t are Gaussian noise.

    Update on each leader trade:
      1. Predict E[follow_volume] = H · x_predicted
      2. After 30 min: observe actual follow_volume = y_observed
      3. Kalman update: x_corrected = x_predicted + K · (y_observed - y_predicted)
      4. Persist x_corrected to follower_pool_state table

    Prediction interface for confidence_engine:
      forecast(leader, trade_size, asof_ts) -> {
        'expected_volume_usdc': float,
        'ci_low': float,
        'ci_high': float,
        'time_to_peak_s': float,
        'half_life_s': float,
      }
    """
```

### 3.3 `src/follower_volume/volume_predictor.py` — The headline API

```python
class FollowerVolumePredictor:
    """The headline metric the rest of the bot consumes.

    Given a leader trade event, returns the expected follower-pool
    volume that will be deployed in the next 30 min, broken down by
    strategy class.

    Combines:
      - Multivariate Hawkes intensity at time t
      - Kalman state at time t per pool class
      - Strategy classifier prior on which pools will react

    Output:
      {
        'total_volume_usdc': 12450.00,
        'ci_low': 7200.00,
        'ci_high': 19800.00,
        'by_pool': {
          'directional_follower_pool': 8200.00,
          'momentum_follower_pool': 3100.00,
          'social_follower_pool': 1150.00,
        },
        'time_distribution': {  # CDF of volume arrival
          '0-5min':   0.40,
          '5-15min':  0.35,
          '15-30min': 0.20,
          '30-60min': 0.05,
        },
        'confidence': 0.78,    # model's confidence in this forecast
      }
    """
```

### 3.4 `decision_router` — New entry policy `volume_anticipation`

A new branch in `decision_router.py`:

```python
def route_decision(self, signal, ...):
    # ... existing FOLLOW / FADE / SKIP logic ...

    # NEW: Round 9 volume_anticipation policy
    volume_forecast = self.volume_predictor.forecast(
        leader=signal.leader, trade_size=signal.size, asof_ts=signal.time
    )
    if volume_forecast['total_volume_usdc'] > VOLUME_ANTICIPATION_THRESHOLD:
        kelly_fraction_from_volume = self._kelly_from_volume(
            expected_volume=volume_forecast['total_volume_usdc'],
            market_depth=signal.market_depth,
            confidence=volume_forecast['confidence'],
        )
        position_size = min(
            kelly_fraction_from_volume * current_capital,
            MAX_POSITION_PCT * current_capital,
        )
        return Decision(
            action='volume_anticipation',
            size_usdc=position_size,
            reason=f"E[volume]={volume_forecast['total_volume_usdc']:.0f}",
        )
```

The new policy is **complementary** to FOLLOW — they can both fire on
the same leader trade. The bot now has two kinds of edge: leader-
correctness and follower-flow.

### 3.5 Drift handling

Both the Hawkes matrix and the Kalman state drift over time. We retrain:
- Multivariate Hawkes: nightly batch, 30-day rolling window
- Kalman: continuous update on every observed pool response

If a leader's Hawkes coupling significantly drops (the BIC test starts
rejecting), the follower-pool prediction loses validity → emit a drift
alert + the confidence engine gates volume_anticipation entries for
that leader.

---

## 4. Migration sequence

```sql
-- Migration 036
CREATE TABLE multivariate_hawkes_fits (
    leader_wallet VARCHAR(100) NOT NULL,
    fit_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pool_classes VARCHAR(200) NOT NULL,  -- comma-sep list
    alpha_matrix_json JSONB NOT NULL,    -- {(i,j): value} for free entries
    mu_vector_json JSONB NOT NULL,
    beta NUMERIC(10, 6) NOT NULL,
    log_likelihood NUMERIC(15, 4),
    bic_statistic NUMERIC(15, 4),
    accepted_couplings_json JSONB,
    convergence VARCHAR(20),
    PRIMARY KEY (leader_wallet, fit_at)
);

-- Migration 037
CREATE TABLE follower_pool_state (
    leader_wallet VARCHAR(100) NOT NULL,
    pool_class VARCHAR(20) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pool_size_usdc NUMERIC(20, 2),
    recent_response_pct NUMERIC(5, 4),
    decay_rate NUMERIC(8, 6),
    state_cov_json JSONB,  -- Kalman covariance matrix
    n_observations INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (leader_wallet, pool_class)
);

CREATE INDEX idx_fps_updated ON follower_pool_state (updated_at DESC);
```

Append-only history pattern (per the cross-cutting architecture
principle): every Kalman update writes a snapshot to
`follower_pool_state_history` for as-of training reads.

---

## 5. New Prometheus metrics (Round 9 contributes ~12)

```
polybot_mvhawkes_fits_total{result}             # converged|bic_rejected|failed
polybot_mvhawkes_fit_duration_seconds
polybot_mvhawkes_alpha_value{pool_class}        # histogram of leader→pool α
polybot_mvhawkes_couplings_accepted{leader_wallet}
polybot_mvhawkes_bic_statistic                  # distribution

polybot_kalman_updates_total{pool_class}
polybot_kalman_innovation_magnitude{pool_class} # large = model wrong
polybot_pool_size_estimate{pool_class}          # gauge across all pools

polybot_volume_forecasts_total
polybot_volume_forecast_mape                    # mean abs % error on closed forecasts
polybot_volume_forecast_ci_coverage             # % of times actual fell in 95% CI
polybot_volume_anticipation_entries_total       # decisions that fired
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| Block-sparse multivariate fitter | 1.5 |
| BIC extension + identifiability tests | 0.5 |
| Kalman state-space model | 1.5 |
| Volume predictor API + integration | 0.75 |
| `decision_router` `volume_anticipation` policy | 0.5 |
| Drift detection + alerts | 0.25 |
| Migrations + 30+ unit tests + audit doc | 1.0 |
| **Total** | **~6 weeks** |

### Dependencies

- Round 6: comprehensive trade coverage (on-chain ingestion ensures
  the follower-pool observations are not artifacts of REST polling
  holes — Round 5 had this problem before R6 closed it)
- Round 8: strategy classifier provides the pool clustering. Without
  R8, the K follower pools collapse to K=1 (all followers in one
  pool), which is just bivariate Hawkes with extra steps.

### Risk: 4/5

| Risk | Severity | Mitigation |
|---|---|---|
| Identifiability of N²-parameter Hawkes | High | Block-sparse mask (§ 2.2), shared β, BIC threshold scales with k. Pre-flight Monte Carlo: simulate from known params, verify recovery. |
| Kalman model misspec → biased forecasts | Medium | `polybot_kalman_innovation_magnitude` metric surfaces if the residuals are systematically biased. Manual model adjustment when alerted. |
| Computational cost: nightly fit per leader × 200 leaders × 4 pools = ~800 fits | Medium | Fits parallelize trivially across leaders. Budget: each fit ~30s, total ~7h nightly. Fits inside the existing batch window (3 AM UTC). |
| Pool-class boundaries shift as R8 classifier updates | Medium | Re-derive pool membership weekly from R8 outputs; Kalman state migrates via projection when membership changes (lose some info, accepted). |
| Forecast accuracy too low for live trading | High | Hard gate: don't enable volume_anticipation policy until MAPE < 30% on 30-day backtest. Otherwise the edge is theoretical. |

### Acceptance criteria

- Multivariate fit converges (no `bic_rejected` for leaders with confirmed
  bivariate edges) on ≥ 80 % of top-200 leaders
- Kalman state-space achieves CI-coverage = 0.95 ± 0.03 on a 60-day
  backwards-validation set
- Volume forecast MAPE < 30 % over a 30-day out-of-sample period
- `decision_router` A/B: 60-day paper backtest with volume_anticipation
  enabled shows Sharpe ≥ 1.3× vs FOLLOW-only baseline
- No regression in Round 5 bivariate tests: `test_independence_yields_low_alpha_mu`
  still passes (multivariate fitter is additive, not a replacement)

---

## 7. Rollout plan

### Phase 9.A — Multivariate fitter shipping (weeks 1-2)
1. Implement `MultivariateHawkesFitter` with block-sparse mask
2. Monte Carlo identifiability test: simulate with known α matrix,
   verify recovery within tolerance
3. Run nightly fits in shadow (writes to `multivariate_hawkes_fits`
   but no downstream consumer)
4. **Gate**: 7 nights of clean fits, identifiability test passes

### Phase 9.B — Kalman + volume predictor (weeks 3-4)
1. Implement Kalman state-space; update on every observed follow trade
2. Predictor API exposed; `confidence_engine` reads it but does NOT
   yet use it for decisions
3. Daily CI-coverage monitoring metric
4. **Gate**: CI coverage holds 0.95 over 14-day shadow

### Phase 9.C — volume_anticipation policy (weeks 5-6)
1. Enable the new `decision_router` branch in **paper** mode
2. Compare paper PnL against the FOLLOW-only baseline
3. Drift detection wired to suppress volume_anticipation on stale leaders
4. **Gate**: paper Sharpe ≥ 1.3× baseline over 30 days, MAPE < 30 %

### Phase 9.D — Live (gated, weeks 7-8)
1. Lift to live with the same gradual size-ramp as Round 7 (start
   at 0.1 % of bankroll per trade)
2. Telegram alerts on every live volume_anticipation order
3. Operator can disable via runtime config

---

## 8. What this round explicitly does NOT do

- **Does NOT replace bivariate Hawkes**. The R5 bivariate fitter still
  runs nightly for individual edge confirmation. The multivariate
  model is for population dynamics; bivariate is for per-pair
  validation.
- **Does NOT model cross-leader excitation**. The current scope is
  ONE leader × K follower pools. Leader-to-leader coupling (R10's
  causal layer addresses this differently via IV).
- **Does NOT use neural networks**. Closed-form Kalman + L-BFGS-B
  MLE are the right tools at this scale. NN-based Hawkes (Mei-Eisner
  Neural Hawkes) would be 10x more code and not measurably more
  accurate.
- **Does NOT predict follower IDENTITY**. We predict pool-level
  volume; we don't claim "leader X will pull 18 specific wallets in."

---

## 9. The non-obvious gains

1. **The Hawkes α matrix becomes a fingerprint of leader influence
   STRUCTURE**. A leader with α[directional_pool] = 0.8 and
   α[momentum_pool] = 0.1 attracts a different crowd than one with
   the inverse. This is a feature for R10 causal modeling (instrument
   selection: pools that selectively react are better instruments).

2. **Kalman innovations are a leading indicator of regime change**.
   When the model's prediction error spikes for a leader, something
   has changed — maybe the leader's strategy shifted (R8 drift
   detector), maybe a competing leader entered the market, maybe
   the followers got smart. Each interpretation has a different
   action.

3. **The volume forecast is a feature for the OPERATOR, not just
   the bot**. Dashboard panel showing "tomorrow's expected
   follower flow per leader" is operationally invaluable — it
   tells the operator when to prepare for high-volume sessions
   (raise capital limits) or low-volume drag (reduce exposure).

4. **The block-sparse mask itself is testable**. The "no
   cross-pool excitation" assumption is empirical. Testing it
   (fitting WITHOUT the mask occasionally) tells us when our model
   structure is wrong. Cheap research instrument.

---

## 10. The single sentence

> Round 9 makes the bot **see the followers as a population, not as
> individuals** — multivariate Hawkes for excitation structure +
> Kalman for volume dynamics — so every entry decision is sized by
> predicted flow, not by the leader's prior accuracy.
