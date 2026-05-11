# VISION — Polymarket Leader Intelligence Engine

> **The product that has not been built yet.**
> What we are building, why nobody has done it, and why we will.

---

## 1. The thesis in one sentence

> Every existing copy-trading bot **trades the leader's signal**.
> **We trade the volatility the leader creates among their followers.**

That's the never-done part. The distinction is everything.

A copy bot says: *"This wallet is profitable. Mirror it. If the leader is
right, we win."* You're betting on the leader's edge — and inheriting
their losses when they're wrong.

This product says: *"This wallet has 1,247 followers who will pile into
the same market with a measurable lag distribution. Their flow will move
the price by ΔP with a known variance. We position ahead of that flow,
not ahead of the leader's prediction."* You're betting on a flow
prediction — and the leader's prediction accuracy becomes a feature, not
a bet.

When the leader is right, you ride the same wave the followers create.
When the leader is wrong, you FADE the followers' panic exits at a
discount. Both branches profit, because both branches are predictable
flow events.

---

## 2. The five pillars that don't exist anywhere else

| Pillar | What existing tools have | What we will have |
|---|---|---|
| **Data acquisition** | Single-source REST polling, wallet-attributed via Polymarket data-api on a 30-second timer. | Multi-source attribution (Falcon ×10 agents + data-api + on-chain Polygon mempool watcher + CLOB WS book-level-3 + cross-market index against Kalshi/Manifold + Twitter/X social firehose for known leader handles). Every signal point-in-time correct, no holes, sub-second freshness on hot paths. |
| **Leader modeling** | Falcon Score ranking + win-rate. Static. | Per-leader **strategy fingerprint** (directional / momentum / contrarian / arb-2way / arb-3way / market-maker / structural-bot / info-leak / social-driven), **error model** conditioned on strategy + market state, **follower-pool size estimator** updated as new follow-trades arrive. Hierarchical Bayesian — new wallets borrow strength from their strategy cluster. |
| **Causal inference** | Beta-Binomial co-occurrence counts. Cannot distinguish "leader caused follower" from "both reacted to news". | Multivariate Hawkes for joint leader-pool → follower-pool intensity, **plus** do-calculus / instrumental-variable analysis on news-event natural experiments. We **know** whether the leader truly causes the follower or both are reacting to the same exogenous signal. |
| **Execution layer** | REST order submission with 2-3s wallet-attribution lag. | **Polygon mempool watcher** detects leader tx 200ms-2s **before** chain confirmation + **pre-signed order pool** ready to fire on detect + **smart slippage routing** that uses the predicted follower-volume to size the entry without front-running our own future flow. |
| **Continuous validation** | Backtests against historical data; production drift goes unnoticed. | **Always-on calibration**: every decision logs a counterfactual (what the model predicted vs what happened), the calibration loss feeds back into model selection weights, and the bot **knows when it should stop trading a leader** because their strategy fingerprint has shifted. |

No single competitor has even three of these pillars wired together. None
have all five.

---

## 3. The thing that makes this work — point-in-time correctness everywhere

Every model in section 2 is worthless if the training-time data doesn't
match the serving-time data. The audit's `# LEAKAGE:` discovery (Phase 0
Task C, closed in Phase 3 Round 2) is the canary. **Every feature, every
table, every cache key needs to be as-of-able.**

The architecture commits to this. `market_features_history` (migration
016) is the first instance. `follower_pool_size_history` will be next.
`leader_strategy_history` after that. There is no "current value" in
the model layer — only "value as of timestamp T."

This is the discipline that lets the backtest match production, and lets
production debug match the backtest. It's expensive — every feature
double-writes — but it's the only way a research loop converges.

---

## 4. Why now

Three things have to be true for this product to be possible. They are
all true today, and were not true 18 months ago:

1. **Polymarket reached scale**: the CLOB processes enough volume that
   leader-follower flow is statistically detectable. Pre-2024, the
   signal-to-noise ratio made this impossible.
2. **L2 mempool tooling matured**: running a Polygon archive node + tx
   parser is a weekend project now, not a quarter of infrastructure
   work.
3. **Falcon API exists**: the per-wallet metric layer (10 agents, the
   audit catalogued each) replaces what used to be a 6-month feature
   engineering project. We can spend our engineering time on the model
   layer instead of the data layer.

The window is open. It closes when (a) Polymarket gets large enough that
exchange-grade HFT firms enter, (b) Falcon-tier metric APIs commoditize,
or (c) the SEC/CFTC writes prediction-market HFT rules. We have ~18
months.

---

## 5. What success looks like

- **Year 1**: Sharpe > 1.5 net of fees on paper, on a 6-month
  out-of-sample window. The bivariate Hawkes false-positive bug (closed
  Round 5) showed this is fundamentally a precision problem; we need
  the strategy classifier + multivariate Hawkes + mempool execution to
  close it.
- **Year 1 deeper**: 200+ leader wallets fully fingerprinted (strategy
  + follower-pool + error model in steady state), 50+ confirmed via
  hand-label cross-validation.
- **Year 2**: live capital deployed in a thin layer behind the paper
  layer. Live PnL matches paper within 2σ — that's the success metric
  for execution-layer fidelity.
- **Year 2 deeper**: a research notebook that can ask *"what if
  leader X used strategy Y instead"* and run a counterfactual replay
  in under a minute. The feature store + causal model make this
  possible.

---

## 6. Anti-goals — what we will NOT do

- **HFT co-location**. The latency advantage from a colocated server is
  µs. The mempool watcher gives us 200ms-2s. That gap is enormous in
  prediction markets where holding periods are days. Co-location is the
  wrong optimization.
- **Custom L2 infra** (rollup, sidechain). We use Polygon as-is. Custom
  L2 is a year of work for a marginal gain.
- **HFT-style adversarial execution** (spoof orders, layering). Polymarket
  is small enough that this is detectable, our edge doesn't require it,
  and the legal grey area widens fast.
- **Catch-all feature engineering**. The temptation to add 500 features
  and let LightGBM sort them out is real. We resist it — each new
  feature must trace to a causal hypothesis from section 2.
- **Pre-mature live capital**. Paper trading proves the math, live
  trading proves the execution. We do them in that order, with the
  killswitch (Phase 0) as the final gate.
- **Building our own Falcon-equivalent**. The Falcon API is good enough.
  We pay for it, we don't replicate it.

---

## 7. The current state — where we start

After 8 audit-driven commits (Phase 0 through Phase 3 Round 5) the bot
has:

- 50 Prometheus metrics, `/metrics` endpoint, 7 alert rules wired to the
  operator's reported pain (>30 min ingest silence fires).
- 5 s REST poll + continuous cursor + event-driven Falcon refresh +
  WS freshness watchdog → median trade-to-react latency 2-3 s (was 16 s).
- Redis Streams CDC with consumer groups + dead-letter routing
  replacing lossy pubsub for 6 downstream consumers.
- `market_features_history` + `feature_store.py` with as-of reads
  closing the train/serve skew on `liquidity_score`.
- Bivariate Hawkes with BIC regularisation — false-positive
  "every clustered retail trader gets confirmed" bug is closed.
- 888 unit tests passing, 0 failures, 2 documented intentional xfails.

This is the **foundation**. None of it is the product. The product is
the five pillars in section 2, built on this foundation. The roadmap
(`ROADMAP.md`) sequences the work.
