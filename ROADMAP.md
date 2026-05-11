# ROADMAP — Polymarket Leader Intelligence Engine

> The path from the current foundation (Phase 3 Round 5, 888 tests, 0
> failures) to the product described in [VISION.md](VISION.md).
>
> Each round below has: **deliverables**, **dependencies**,
> **effort estimate**, **risk score (1–5)**, **acceptance criteria**.
> Rounds are sequential where dependencies require; parallel where they
> don't.

---

## Round 6 — The Spine (Data Sovereignty Layer)

**The foundation round.** Every later round assumes the data is there
— no holes, no rate limits, infinite history. This round makes that
assumption true.

The breakthrough insight: Polymarket trades are on-chain Polygon events.
The Falcon API and data-api are value-added layers on top of the same
chain data. We stop being a consumer of opinionated APIs and become a
**node operator** with our own Polygon node + multi-RPC redundancy +
process-split ingestion + tiered storage + universal wallet coverage.

### Headline deliverables
- **`infra/polygon-node/`** — self-hosted Erigon pruned node on a 2nd
  Hetzner box (CX31 + 200 GB volume, €21/mo). Private-network link to
  the bot box.
- **`src/rpc/`** — multi-provider RPC abstraction (local Erigon as
  primary, Alchemy + QuickNode as fallback). Circuit breaker, in-flight
  call coalescing, adaptive token buckets per provider.
- **`src/onchain/clob_listener.py`** — direct subscription to
  Polymarket CLOB contract events. Every trade arrives in ~2s with
  wallet attribution NATIVELY from the chain. 100% coverage by
  construction.
- **`src/crawler/`** — Universal Wallet Crawler covering ALL ~1.5M
  Polymarket wallets ever, with adaptive depth tiers (full / periodic /
  light tracking).
- **`src/ingestion_daemon/`** — process-per-source split via systemd.
  Engine GIL stalls can no longer cascade into ingestion. This is the
  structural fix for "10-30 min pauses."
- **`src/cold_storage/`** — nightly Parquet export of all hot tables;
  DuckDB virtual views for research notebooks. Years of history
  queryable in seconds.
- **`src/monitoring/coverage_reconciler.py`** — cross-source comparison
  every 5 min. Alerts fire if any source sees < 95% of the chain truth.

### Full spec: see [`docs/ROUND_6_THE_SPINE.md`](docs/ROUND_6_THE_SPINE.md)

### Dependencies
- A second Hetzner box. €21/mo. Free private-network link.
- Paid RPC providers (Alchemy / QuickNode free tiers initially) — used
  during initial Erigon sync, then demoted to standby.
- 2 TB of public Polygon snapshot ingestion (one-time, bootstraps
  Erigon faster than network sync).

### Effort
~9 weeks single-dev (Erigon sync runs in parallel with code, so wall
time ≈ engineering time).

### Risk: 3/5 (composite)
- Most components are well-understood; the integration is the work.
- The biggest unknown is whether the wallet-universe backfill (1.5M
  rows via paid-RPC eth_getLogs) blows the free-tier budget — pre-
  flight against the providers' cost calculators before committing.

### Acceptance criteria
- `polybot_coverage_ratio{source="onchain"} = 1.0` for 7 consecutive days
- `polybot_coverage_ratio{source="rest_poll"} > 0.95` (REST stays a healthy redundancy)
- `polybot_chain_blocks_behind < 3` in steady state
- `polybot_wallet_universe_size > 1_000_000` after backfill
- A DuckDB notebook query against 90 days of cold trades returns in < 5 s
- All ingester daemons survive `kill -9` without trade loss (Redis Stream consumer group recovers)

---

## Round 7 — Mempool watcher + pre-signed order pool

**The headline.** The only legitimate path to "BEFORE the leader." Watch
Polygon mempool for known leader-wallet transactions, decode them, fire
pre-signed orders to the CLOB.

> **Note**: this was the original Round 6. Round 6 was renamed to The
> Spine because mempool watching is one consumer of the broader
> ingestion architecture — it builds on `src/rpc/` and the local Erigon
> node from Round 6. The substrate makes Round 7 simpler than the
> original R6 plan.

### Deliverables
- `src/mempool/` new module:
  - `node_client.py` — connects to Polygon RPC + WS endpoint, subscribes
    to `newPendingTransactions`
  - `tx_decoder.py` — decodes pending tx bytecode against the Polymarket
    CLOB contract ABI; recovers (wallet, market, token, side, size, price)
  - `wallet_index.py` — bloom filter of watched leader wallets for O(1)
    filtering of the firehose
  - `event_emitter.py` — publishes detected leader-intent events to the
    new `mempool:leader_intent` Redis Stream (consumed by execution layer)
- `src/execution/prefill/` new submodule:
  - `presigned_pool.py` — maintains a pool of pre-signed CLOB orders per
    market/token/direction with rolling expiry, signed by the bot's
    trading key
  - `intent_router.py` — on a `leader_intent` event, validates against
    risk limits, picks the matching pre-signed order, fires it
- Migration **020** — `mempool_observations` table (intent_id, wallet,
  market_id, observed_at, confirmed_at, latency_ms, confirmed_in_block)
- Metrics:
  - `polybot_mempool_observations_total{wallet_class, result}`
  - `polybot_mempool_detection_latency_seconds` (event timestamp vs our
    observation)
  - `polybot_mempool_to_fire_latency_seconds` (observation to order submit)
  - `polybot_presigned_pool_size{market, direction}`
  - `polybot_presigned_pool_misses_total{reason}` (no matching pre-sign)

### Dependencies
- A running Polygon archive node OR a paid RPC provider (Alchemy /
  Infura / QuickNode) supporting `eth_subscribe newPendingTransactions`.
- Bot trading key already provisioned (it is — `live-trading-setup.md`).

### Effort
~3 weeks single-dev. Polygon node setup is the long pole.

### Risk: 4/5
- Mempool tx might be replaced (gas-price war) — our pre-sign fires but
  the leader's tx never lands. Loss mitigation: bound exposure per
  detected-intent.
- Polygon RPC providers rate-limit aggressively; need to mix two
  providers in case one drops.
- Bot trading key signing latency could exceed the mempool advantage
  on a slow VM. Benchmark before committing.

### Acceptance criteria
- p50 detection-to-fire latency < 200 ms.
- 95% of mempool-detected leader trades result in either a fired order
  OR a logged "no_match" reason.
- Live shadow run for 30 days: bot positions filled within ±2 blocks of
  leader's confirmation.

---

## Round 8 — Strategy classifier (the "why" layer)

**Why this matters**: today the bot treats every leader the same. A
swing trader behaves nothing like a market-maker, but our FOLLOW
confidence ignores the difference. The strategy classifier gives every
leader a per-strategy posterior.

### Deliverables
- `src/strategy_classifier/` new module:
  - `features.py` — per-wallet feature engineering:
    trade velocity, position-cycle duration distribution, size
    distribution shape, market category preferences (already in profiler),
    entry-vs-resolution timing, fee sensitivity (do they trade in
    zero-fee categories more?), slippage pattern (do they cross the
    spread vs sit on the bid?), order-placement-to-cancel ratio
  - `model.py` — supervised classifier:
    LightGBM multi-class for {directional, momentum, contrarian,
    arb_2way, arb_3way, market_maker, structural_bot, info_leak,
    social_driven}. Trained on hand-labelled set of 100 wallets.
    Outputs softmax probabilities, stored as `classification_json` on
    `leaders` table.
  - `labeling/` — Jupyter notebook + label-store schema for the manual
    100-wallet hand-labelling pass (one-off; ~1 dev-week of focused work)
  - `cluster.py` — unsupervised K-means / DBSCAN on the same features
    for **discovery** of new strategy classes not in our taxonomy
- Migration **021** — extends `leaders.classification_json` schema:
  ```json
  {
    "strategy_probs": {"directional": 0.7, "momentum": 0.2, ...},
    "primary_strategy": "directional",
    "confidence": 0.7,
    "model_version": "sc.v1.0",
    "fitted_at": "..."
  }
  ```
- `confidence_engine.py` extension: FOLLOW confidence becomes
  `f(thompson_sample, primary_strategy)` — e.g., FOLLOW is high on
  directional swing leaders, low on structural bots (already-excluded
  anyway), nuanced on momentum (depends on regime).
- Metrics: per-strategy confusion-matrix from the hand-label validation
  set, drift detection on classifier predictions.

### Dependencies
- 100 hand-labelled wallets. **This is the hard part** — needs ~1 week of
  manual work classifying trades by wallet. Use the wallet drilldown
  endpoints we already have (Phase 0 dashboard WIP).

### Effort
~4 weeks (1 week labeling + 3 weeks model + integration).

### Risk: 3/5
- Hand-labelling subjectivity — same wallet may show two strategies in
  different periods. Mitigation: label per (wallet, 30-day window).
- Class imbalance — most leaders are directional; arb_3way is rare.
  Use stratified sampling + class weights.

### Acceptance criteria
- Held-out validation accuracy > 75% on hand-labelled set.
- `confidence_engine` FOLLOW recommendations on confirmed-strategy
  wallets show >20% lift in Sharpe vs the strategy-agnostic baseline
  in a 30-day backtest.

---

## Round 9 — Multivariate Hawkes + follower-pool dynamics

**Round 5's BIC fix closed the false-positive bug**. Round 9 generalises
the model from pairwise causality to **population-level** dynamics. This
is what enables the volume-prediction edge in VISION § 1.

### Deliverables
- `src/graph/hawkes_multivariate.py` — N-dimensional Hawkes:
  intensity matrix `λ_i(t) = μ_i + Σ_j α_ij·Σ_k exp(-β·(t-t_k^j))`
  with i, j ∈ {leader_pool, follower_pool_1, follower_pool_2, ...}.
  Follower pools are clustered by Round 7 strategy class — so we model
  "directional-follower-pool" separately from "social-driven-follower-pool".
- `src/follower_volume/` new module:
  - `kalman.py` — state-space model on follower-pool size per leader,
    updated each observed follow-trade. Predicts E[follower flow in
    next 30 min | leader trade] with uncertainty band.
  - `volume_predictor.py` — combines Kalman + Hawkes + Round 7 strategy
    class to output the headline metric: `E[follower_volume_usdc | trade]`
    with a 95% CI.
- Migration **022** — `follower_pool_state` table:
  ```sql
  leader_wallet, pool_class, mean_pool_size, var_pool_size,
  last_updated, n_observations
  ```
  Tracks the Kalman state per (leader, pool_class).
- `decision_router` extension: a new entry-policy branch
  `volume_anticipation` that takes positions sized by **predicted
  follower volume**, not by the leader's confidence. The Kelly fraction
  becomes a function of `E[volume]` / current market depth.

### Dependencies
- Round 7 strategy classifier (for follower-pool clustering)
- The bivariate Hawkes work from Round 2 + Round 5 (BIC) generalises
  cleanly to multivariate; the BIC formula extends to k-parameter penalty.

### Effort
~5 weeks. The Kalman state-space model is the longest pole — needs
careful initialization and observation-model design.

### Risk: 4/5
- N-dimensional Hawkes has O(N²) parameters; identifiability degrades
  fast. Mitigation: enforce block-sparsity (most α_ij ≈ 0 a priori),
  fit per pool-cluster instead of per individual.
- Kalman observation model must distinguish "follower entered" from
  "random non-follower entered" — uses Round 7 strategy classifier as
  the membership oracle.

### Acceptance criteria
- 24-hour-out follower-volume predictions: mean absolute percentage
  error < 30% on a 30-day backtest.
- Backtest Sharpe of the `volume_anticipation` entry policy > Sharpe of
  the current FOLLOW/FADE policy on the same window.

---

## Round 10 — Causal inference layer

**Why**: Hawkes catches statistical association. Statistical association
isn't causation. When a news event hits, both the leader and the
followers move — but our model says "the leader caused the followers".
This is a false positive that no amount of Hawkes can fix.

### Deliverables
- `src/causal/` new module:
  - `instruments.py` — identifies natural experiments:
    news events (NewsAPI), oracle updates, related-market resolutions.
    These are exogenous shocks that affect followers but not the leader's
    intent.
  - `iv_estimator.py` — instrumental-variable estimator for the causal
    effect of leader trades on follower trades, controlling for shared
    shocks.
  - `do_calculus.py` — Pearl-style do-calculus implementation against a
    DAG of known causal pathways (leader → followers, news → both,
    market_state → followers).
  - `counterfactual_replay.py` — answers "what if leader X had used
    strategy Y": replays the historical decision stream with a modified
    classifier output, recomputes downstream confidence and PnL.
- New table — `causal_estimates`:
  ```sql
  leader_wallet, follower_wallet, period,
  hawkes_alpha, hawkes_alpha_mu_ratio,           -- statistical
  causal_ate, causal_ate_ci_low, causal_ate_ci_high,  -- IV-corrected
  shared_shocks_pct,
  fitted_at
  ```
- `confidence_engine` extension: confirmed-follower gate uses
  `causal_ate > threshold` AND `hawkes_alpha > threshold`, not OR.

### Dependencies
- Round 8 (the follower-pool model is the dependent variable in the IV
  regression).
- News-event ingestion infrastructure (Round 11) — but partial
  implementation can use just oracle-update events for an MVP.

### Effort
~6 weeks. Causal inference on observational data is genuinely hard;
this is the most research-heavy round.

### Risk: 5/5
- IV identification depends on instrument validity. Wrong instrument →
  wrong causal estimate, often confidently wrong.
- The math is unforgiving; needs an experienced reviewer (or external
  research consultant) to sign off on the methodology.

### Acceptance criteria
- For each pair (leader, follower) in the validated set, the IV-adjusted
  ATE has a 95% CI that excludes 0 for ≥60% of pairs the Hawkes flags
  as confirmed.
- A clear list of pairs where statistical and causal estimates disagree
  — these become the "fade the news" trading opportunities.

---

## Round 11 — CLOB book-level-3 ingestion + microstructure features

**Why**: today we aggregate book data per minute (Round 2 orderbook
pipeline). Real microstructure — iceberg orders, order-placement
velocity, cancel-to-fill ratios — lives at the per-event level. This is
the data layer that strategy classifier (Round 7) needs for its highest-
leverage features.

### Deliverables
- `src/observer/clob_book_observer.py` — subscribes to Polymarket WS
  book channel at maximum granularity, captures every order
  placement/modification/cancellation.
- Migration **023** — `clob_book_events` (partitioned by hour):
  ```sql
  event_id, market_id, token_id, event_time, event_type,
  side, price, size_delta, wallet_address NULL, source
  ```
  Note: `wallet_address` is mostly NULL (WS doesn't carry it) but
  joinable with `trades_observed` on confirmed fills.
- `src/observer/microstructure.py` — derived features:
  iceberg detection (large hidden orders surfacing in chunks), spoof
  detection (place-then-cancel patterns), order-flow-imbalance
  (OFI in finance literature) — bid_size_delta − ask_size_delta over
  rolling windows.
- New per-leader features fed into Round 7 classifier: `cancel_to_fill_ratio`,
  `iceberg_usage_pct`, `spoof_score`.

### Dependencies
- None hard; can run in parallel with Round 8 if you have the dev
  bandwidth.

### Effort
~3 weeks.

### Risk: 2/5
- Storage cost — order-book events are high-volume. Partitioning + 90d
  retention bounds it.

### Acceptance criteria
- `polybot_clob_book_events_ingested_total` > 1M/day at full subscription.
- Microstructure features measurably improve Round 7 classifier accuracy
  (≥3 pp on the validation set).

---

## Round 12 — Social signal + cross-market index

**Why**: many leaders telegraph entries on X/Twitter/Discord MINUTES
before the trade. Pre-trade signal. Plus cross-market arb wallets
trade on Kalshi/Manifold simultaneously — knowing their other-venue
positions is alpha.

### Deliverables
- `src/social/` new module:
  - `x_firehose.py` — X API filtered stream on watched-leader handles
    + market keywords + Polymarket URLs
  - `nlp_classifier.py` — small LLM-distilled classifier:
    is this tweet (a) signaling entry, (b) signaling exit, (c) noise?
    Output a confidence score per tweet.
  - `telegram_listener.py` — public Telegram channels where leaders post
  - `discord_listener.py` — same for public Discord servers
- `src/cross_market/` new module:
  - `kalshi_client.py`, `manifold_client.py`, `predictit_client.py` —
    per-venue clients
  - `wallet_resolver.py` — cross-venue wallet matching (semi-manual,
    seeded with public profiles)
  - `position_aggregator.py` — same-wallet positions across venues,
    flagged as informational features for the classifier
- New tables: `social_signals` (wallet, source, text, ts, classification),
  `cross_market_positions` (wallet, venue, market_id, side, size, ts)

### Dependencies
- X API access ($100/mo paid tier minimum)
- Round 7 classifier (consumer of the new features)

### Effort
~4 weeks.

### Risk: 3/5
- X NLP signal-to-noise is brutal. Most tweets are noise. Heavy
  per-leader allow-listing required.
- Cross-venue wallet matching is partial at best — many leaders use
  different addresses per venue. Worth doing for the ones we can match.

### Acceptance criteria
- For top-20 watched leaders, capture ≥80% of their public entry/exit
  tweets within 60s of posting.
- Cross-market features detect ≥10 confirmed arb-style leaders.

---

## Round 13 — Continuous calibration loop + research notebook

**Why**: a bot that doesn't know when it's wrong silently dies. The
audit's Phase 0 work added the prerequisites (decision logging, outcome
tracking) — Round 12 closes the loop into automated model selection.

### Deliverables
- `src/calibration/` new module:
  - `decision_replay.py` — for each closed position, recompute what
    the model predicted at entry vs what happened
  - `loss_aggregator.py` — per-strategy Brier score, log-loss,
    calibration plots, drift detection
  - `auto_disable.py` — when a strategy class's loss drifts beyond a
    threshold, the corresponding leader-classifier output is suppressed
    in the confidence engine until the next retrain
- `research/` — top-level Jupyter notebook directory:
  - `00_data_loader.ipynb` — point-in-time-correct feature load
  - `01_strategy_classifier_validation.ipynb` — hand-label cross-check
  - `02_causal_analysis.ipynb` — IV vs Hawkes disagreement explorer
  - `03_counterfactual_replay.ipynb` — "what if leader X used strategy Y"
- Automated nightly job: `scripts/calibration_nightly.py` — replays
  yesterday's decisions, updates per-strategy loss metrics, emits
  Prometheus alerts on drift.

### Dependencies
- Round 7, 8, 9 (the things we're calibrating)
- The feature store + as-of read path (already shipped Round 2)

### Effort
~3 weeks.

### Risk: 2/5

### Acceptance criteria
- Per-strategy loss metrics emit daily, plotted in Grafana.
- A manual operator command can suppress a specific strategy in <1
  minute.
- Research notebook can answer a what-if in <5 minutes wall time.

---

## Cross-round infrastructure work

Threaded through all rounds, not blocking any:

### Observability
- Grafana dashboards corresponding to every Round (Round 6: mempool
  detection panel, Round 7: classifier-output panel, …).
- Distributed tracing (`trace_id` from Round 1 propagated end-to-end);
  visualize in Tempo/Jaeger.

### Reliability
- Backups (`docs/backups.md`) move from idle to enabled (Cloudflare R2
  populated).
- Per-Round chaos test: pull the plug on a downstream service mid-flow,
  verify the upstream backs off cleanly.

### Documentation
- After every Round: update `docs/audit/CHANGELOG.md` with the new
  series + metrics + env vars + tables.
- Every new module gets a `src/<module>/CLAUDE.md` following the
  existing convention.
- Major architecture shifts get an ADR in `docs/adr/` (new directory) —
  short docs (≤2 pages each) explaining a single decision.

---

## Round numbering — explicit choices

We **do not** combine multiple rounds in a single sprint. Each round
ends with: tests green, metrics live, audit doc written, commit landed.
This is the discipline that compounds.

If a round can't ship in its estimated effort window, the next round
**does not start**. The roadmap is sequential under dependency, parallel
where independent (Round 11 ‖ Round 9, Round 12 ‖ everything after
Round 8). Bottleneck: hand-labelling 100 wallets for Round 8 is
single-dev-blocking.

Total: ~39 weeks single-dev to land Round 6 → Round 13.

Round 6 alone is 9 weeks but is the prerequisite for every other
round. It's worth it.

---

## What "done" looks like at the end

A bot that:
- Sees every leader trade within 200ms-2s **before** chain confirmation
- Knows each leader's strategy with calibrated probability
- Predicts the follower-pool volume their trade will trigger with
  ±30% MAPE
- Distinguishes "leader caused this" from "news caused both"
- Fires pre-signed orders sized by predicted follow-flow, not by leader
  confidence
- Continuously calibrates its own predictions and disables stale
  strategies before they cost money
- Can answer any what-if in 5 minutes

That product does not exist. We have the path.
