# Round 10 — Causal Inference Layer

> **Formal title**: Causal Attribution & Counterfactual Replay
> **Colloquial name**: The Truth Test
> **Prerequisite**: Round 8 (strategy classifier), Round 9 (multivariate
> Hawkes + follower-pool), Round 6 (cold tier for IV identification
> queries). Round 12 (social/news data, partial) feeds the instruments.

---

## 1. The thesis — correlation isn't causation, and this matters

Every model in Rounds 5–9 produces statistical association: bivariate
Hawkes says "leader's trades and follower's trades are temporally
linked." Multivariate Hawkes refines it to populations. BIC says the
link is statistically significant.

None of those models can answer the question that actually decides
profit:

> **Did the leader cause the followers, or did they both react to the
> same external event?**

When a news event hits ("X polling collapse", "Crypto ETF approved"),
both the leader and the followers move on the news. Our models will
attribute the followers' moves to "the leader caused them," confidently
firing volume_anticipation entries. We're trading on a chimera.

This isn't theoretical. It's the audit's MASTER_REPORT § 6 finding:
*"the codebase has no end-to-end ownership of any decision's lifecycle…
fire-and-forget message-passing in a domain that demands exactly-once
accounting."* Round 6 fixed the data-coverage half; Round 10 fixes the
**inference** half.

> Round 10 layers causal inference (instrumental variables, do-calculus,
> counterfactual replay) on top of Hawkes statistical association.
> When the IV-corrected ATE significantly differs from the Hawkes α,
> we know we were trading correlation, not causation — and the
> volume_anticipation policy gates correctly.

---

## 2. The causal DAG we commit to

```
        ┌──────────────────┐
        │  News event /    │       (exogenous)
        │  Oracle update / │
        │  Related-market  │
        │  resolution      │
        └────┬────────┬────┘
             │        │
        ┌────▼────┐ ┌─▼──────────┐
        │ Leader  │ │ Follower    │
        │ trades  │ │ trades      │
        └────┬────┘ └─▲──────────┘
             │        │
             └────────┘     (the causal arrow we want to estimate)

Confounders we must adjust for:
   - Market state at leader-trade time (book imbalance, spread)
   - Time of day
   - Recent leader/follower history
   - Strategy class (R8 output)
```

The arrow `Leader trades → Follower trades` is what Hawkes estimates.
The presence of an exogenous parent (news, oracle) means Hawkes
**overestimates** that arrow whenever both children fire on the
exogenous parent.

The fix: **instrumental variables**. Find a variable that affects
followers ONLY via leader trades (an instrument), then identify the
causal effect cleanly.

### 2.1 Valid instruments we'll use

| Instrument | Why it's valid |
|---|---|
| Leader's **mempool-detection time delta** (Round 7) | Random across leaders; affects when follower SEES the trade (via REST poll) but doesn't affect what they'd otherwise do |
| Leader's **gas price quirk** | If leader pays higher gas → faster confirmation → followers see the trade sooner. Gas decisions are about block inclusion, not about the trade's correctness. |
| **Polymarket API outage windows** | When data-api is down (we see this from R6 coverage_reconciler), followers can't follow — natural experiment |
| **Leader's account-funding events** | When leader gets fresh capital, their trade SIZE distribution shifts — followers respond to size — but the funding source is exogenous to whether the trade is right |

We do **not** use as instruments:
- News events themselves (they're the confounder, not the instrument)
- Market state at trade time (it's the confounder)
- Other followers' trades (endogenous — they share the same parent)

---

## 3. Component breakdown

### 3.1 `src/causal/instruments.py` — Instrument identification

```python
class InstrumentRegistry:
    """Identifies natural experiments / exogenous shocks usable as
    instrumental variables for the (leader → follower) causal estimate.

    Detection pipelines (each runs on a separate cadence):

    NewsEventDetector:
      Source: NewsAPI + Polymarket market descriptions (NER for entities)
      Output: time-stamped events with affected market_ids
      Cadence: every 5 min

    OracleUpdateDetector:
      Source: Polygon UMA / Polymarket adapter contract events
      Cadence: real-time via Round 6 eth_subscribe('logs', ...)

    RelatedMarketResolver:
      Source: trades_observed (last 30 days) + market clustering
      Output: when market X resolves, which other markets historically
              experience volume bursts? Those are exposed/related.
      Cadence: hourly batch

    LeaderGasQuirkDetector:
      Source: mempool_observations (Round 7)
      Output: leader-specific gas-price-vs-trade-correctness pairs
              that show random variation
      Cadence: weekly batch

    APIOutageWindowDetector:
      Source: polybot_coverage_ratio metric (Round 6)
      Output: timestamped windows when data-api was unavailable
      Cadence: on alert
    """
```

### 3.2 `src/causal/iv_estimator.py` — 2SLS estimation

```python
class TwoStageLeastSquaresEstimator:
    """Two-Stage Least Squares for the causal effect of leader-trade
    intensity on follower-trade intensity.

    Stage 1: regress leader trade intensity on instruments
        L_t = f(instruments_t) + e1
        → get L_hat (predicted leader intensity from instruments alone)

    Stage 2: regress follower intensity on L_hat
        F_t = g(L_hat) + e2
        → coefficient on L_hat is the causal effect (ATE)

    Standard errors via bootstrap (1000 resamples per pair).
    Identification check: Wu-Hausman test for instrument validity.
    Weak-instrument check: first-stage F-statistic > 10.

    Output per (leader, follower_pool):
      {
        'ate': float,         # causal effect estimate
        'ci_low': float,
        'ci_high': float,
        'wu_hausman_p': float, # null: OLS == 2SLS
        'first_stage_f': float, # > 10 = strong instruments
        'instruments_used': [...]
      }
    """
```

### 3.3 `src/causal/do_calculus.py` — Pearl-style do-operator

```python
class DoCalculusEngine:
    """Pearl-style do-calculus over the committed causal DAG (§ 2).

    Supports queries:
      do(leader_trade=X) → P(follower_trade)
        # "If leader is forced to trade X, what do followers do?"

      do(news_event=X) → P(follower_trade)
        # "If news event X happens, what do followers do?"

      counterfactual("follower would have traded if leader hadn't")
        # P(F | not L, evidence)

    Implementation:
      The DAG structure is fixed (§ 2). Conditional probability tables
      (CPTs) are estimated from data using the IV-adjusted estimates
      from § 3.2 for the leader → follower arrow, and conditional
      observational estimates for confounders → child arrows.
    """
```

### 3.4 `src/causal/counterfactual_replay.py` — What-if analysis

The research notebook payoff:

```python
class CounterfactualReplayer:
    """Replays historical decision streams under hypothetical
    counterfactuals.

    Example queries the operator can run:

      "What if leader X had been classified as 'momentum' instead of
       'directional' over April 2026?"
        → run R8 with the override, propagate through R9 forecasts,
          re-run R7 intent router, compute hypothetical PnL diff.

      "What would our paper PnL have been if R9 volume_anticipation
       had been disabled in March 2026?"
        → run the decision_router with the policy gated off, replay.

      "If we had detected the [event X] news 2 minutes earlier, how
       many additional intents would have fired?"
        → replay mempool data with shifted timestamps on the event.
    """
```

The replays use the cold-tier Parquet (Round 6 § 3.6) for speed. A
30-day replay completes in < 5 min wall time.

### 3.5 Integration: confidence_engine becomes causal-aware

```python
def decide(self, leader, market, signal):
    # ... Round 5/8/9 statistical decisions ...

    # NEW (R10): consult causal_estimates
    causal = self.feature_store.get_causal_estimate_asof(
        leader, signal.follower_pool, signal.time
    )
    if causal is None or causal['ci_low'] <= 0:
        # No causal evidence; downgrade the follow confidence
        follow_confidence *= 0.5
        # Likewise gate volume_anticipation entries
        if signal.action == 'volume_anticipation':
            return Decision('skip', reason='no_causal_evidence')

    # Causal effect is real → full confidence
    return Decision(...)
```

The IV estimates **don't replace** the Hawkes statistical estimates;
they **gate** them. When statistical α is high but IV-corrected ATE
is near zero, that's the news-confounding case — we don't trade it.

---

## 4. Migration sequence

```sql
-- Migration 038
CREATE TABLE causal_estimates (
    leader_wallet VARCHAR(100) NOT NULL,
    pool_class VARCHAR(20) NOT NULL,
    estimated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    -- Statistical (from R5/R9)
    hawkes_alpha_mu_ratio NUMERIC(10, 6),
    hawkes_log_likelihood NUMERIC(15, 4),
    -- Causal (from IV / 2SLS)
    causal_ate NUMERIC(10, 6),
    causal_ate_ci_low NUMERIC(10, 6),
    causal_ate_ci_high NUMERIC(10, 6),
    wu_hausman_p NUMERIC(8, 6),
    first_stage_f NUMERIC(10, 2),
    instruments_used VARCHAR(200),
    PRIMARY KEY (leader_wallet, pool_class, estimated_at)
);

-- Migration 039
CREATE TABLE instrumental_events (
    event_id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(40) NOT NULL,  -- news|oracle_update|api_outage|funding|gas_quirk
    event_time TIMESTAMPTZ NOT NULL,
    affected_market_ids VARCHAR(2000),   -- comma-sep or jsonb array
    payload_json JSONB,
    source VARCHAR(40) NOT NULL,
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 1.0
);
CREATE INDEX idx_ie_time ON instrumental_events (event_time DESC);
CREATE INDEX idx_ie_type_time ON instrumental_events (event_type, event_time DESC);
```

---

## 5. New Prometheus metrics (Round 10 contributes ~10)

```
polybot_iv_estimates_total{result}             # converged|weak_instruments|failed
polybot_iv_first_stage_f                       # histogram
polybot_iv_wu_hausman_p                        # histogram

polybot_causal_ate_vs_hawkes_disagreement      # |ate - alpha| / alpha
polybot_causal_ate_excludes_zero_count{leader} # how many pool estimates are clean

polybot_instruments_detected_total{event_type}
polybot_instrumental_event_lag_seconds         # detection latency per type

polybot_counterfactual_replays_total{kind}
polybot_counterfactual_replay_duration_seconds

polybot_confidence_engine_causal_gates_total{result}  # downgraded|allowed
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| Instrument detection pipelines (news + oracle + outage + gas) | 1.5 |
| IV / 2SLS estimator + bootstrap CI + diagnostic tests | 1.5 |
| Do-calculus engine | 1.0 |
| Counterfactual replayer | 1.0 |
| confidence_engine causal gate integration | 0.5 |
| Migrations + research notebook + audit doc | 1.0 |
| **Total** | **~6.5 weeks** |

### Dependencies

- Round 8 (pool classes — IV is estimated per pool class)
- Round 9 (Hawkes statistical estimates — we COMPARE causal to statistical)
- Round 6 (cold tier — counterfactual replay scans years of data)
- Round 12 (partial): news event ingestion via X firehose. For an MVP
  we can use NewsAPI alone; full social signal arrives in R12

### Risk: 5/5 (highest of any round)

| Risk | Severity | Mitigation |
|---|---|---|
| Instrument invalidity (the instrument affects followers directly, not only via leader) | Very High | Wu-Hausman test + first-stage F-stat as automated gates. Manual sanity check on each instrument's causal pathway before trusting the estimate. |
| Multiple-testing inflation (many leaders × pools = many tests) | High | Bonferroni or BH correction on the per-leader-pool tests; report q-values, not p-values |
| Causal inference math is harder than we think | High | The math is sound; the **application** is hard. Plan in a 1-week external-reviewer pass on the methodology before deploying. |
| Counterfactual replays disagree with reality systematically | Medium | Validation: replay last week's actual decisions; computed PnL should match within Kalman noise. If it doesn't, model is wrong somewhere. |
| Operator over-trusts "causal" estimates | Medium | Dashboard emphasizes confidence intervals + p-values; the bot UI doesn't show a "causal: yes/no" boolean, it shows the full distribution |

### Acceptance criteria

- For ≥ 60 % of (leader, pool) pairs that R9 statistical model flags
  with α/μ > 1, the IV-adjusted ATE has 95 % CI excluding 0
- Wu-Hausman p < 0.05 for ≥ 70 % of pairs (instruments are pulling
  weight, not just noise)
- First-stage F-statistic > 10 (strong instruments) for ≥ 80 % of pairs
- Counterfactual replay of last 30 days matches realized paper PnL
  within Kalman 95 % CI on ≥ 90 % of decisions
- A/B: 60-day paper backtest with causal gate enabled shows **higher
  Sharpe** AND **lower max drawdown** than without (lower drawdown
  because we skip the news-confounding cases that previously cost us)

---

## 7. Rollout plan

### Phase 10.A — Instrument pipelines (weeks 1-2)
1. NewsAPI ingestion + NER entity extraction
2. Oracle event subscription via Round 6 eth_subscribe
3. Backfill instrumental_events from cold tier (90 days)
4. **Gate**: ≥ 100 events per instrument type detected in 30-day backfill

### Phase 10.B — IV estimator + nightly run (weeks 2-3)
1. Implement 2SLS + diagnostics
2. Nightly run: estimate causal effect per (leader, pool) for all
   top-200 leaders
3. Compare estimates against the Hawkes α matrix; surface disagreements
4. **Gate**: 80 % of pairs converge with first-stage F > 10

### Phase 10.C — Methodology review (week 4)
1. External methodology review (causal-inference expert; ~1 week budget)
2. Address feedback; potentially revise instrument choices
3. **Gate**: external sign-off OR a documented disagreement we accept

### Phase 10.D — Causal gating in confidence_engine (weeks 5-6)
1. Enable causal gate in paper mode
2. A/B: 50/50 split for 60 days
3. **Gate**: Sharpe ≥ baseline AND max drawdown ≤ baseline

### Phase 10.E — Counterfactual replay + dashboards (week 6.5)
1. Notebook + dashboard panel for what-if queries
2. Operator-facing documentation
3. **Gate**: a researcher can answer one what-if in < 5 min

---

## 8. What this round explicitly does NOT do

- **Does NOT do randomized controlled trials**. We're in observational
  causal inference. RCTs would require us to literally control leader
  behavior — impossible.
- **Does NOT publish causal claims about specific individuals**. The
  output is per (leader, pool) ATE estimates. We don't claim that
  any single wallet was specifically caused; we estimate population
  effects.
- **Does NOT replace Hawkes**. Hawkes stays the primary statistical
  tool. Causal estimates are an **adjustment** to Hawkes, not a
  replacement.
- **Does NOT use deep causal inference (Neural Causal Inference,
  GAN-based counterfactuals)**. Pearl + 2SLS is sufficient for our
  data scale and the math is unambiguously sound. NN approaches
  trade interpretability for marginal-at-best accuracy gains.

---

## 9. The non-obvious gains

1. **A/B testing infrastructure for free**. The counterfactual
   replayer + cold tier means every future model can be A/B-tested
   against historical decisions without waiting weeks for live data.
   "What if we'd shipped R11 last quarter?" → 5-minute query.

2. **The Wu-Hausman test becomes an alert**. When an instrument starts
   FAILING the validity test, it means the underlying causal structure
   changed (e.g., the data-api outage windows started overlapping with
   specific market events — now they're not exogenous anymore). The
   alert tells us to find a new instrument.

3. **The IV estimates are publishable**. The mathematics is rigorous
   enough that a research paper showing "follower trading is X% causal,
   Y% confounded by news on Polymarket" is a publishable artifact.
   Optional, but valuable as both reputation and disciplinary check.

4. **Causal gating reduces drawdown more than it reduces win rate**.
   The news-confounding case is precisely where we'd take an oversized
   loss when the news reverses. Filtering it filters our worst trades.
   This is why the acceptance criterion includes max-drawdown, not
   just Sharpe.

---

## 10. The single sentence

> Round 10 makes the bot **trade on causation, not correlation** —
> instrumental variables + do-calculus + counterfactual replay tell us
> when a confirmed Hawkes edge is real and when it's news leaking
> through both leader and followers, so the volume_anticipation policy
> stops firing on chimeras.
