# ARCHITECTURE — Target End-State

> The shape of the system after Round 6–13 lands.
> Read [VISION.md](../VISION.md) for the why and [ROADMAP.md](../ROADMAP.md)
> for the sequencing. This file describes WHAT we are building, module
> by module, at the end.
>
> For each module: **role in the target system**, **current state**,
> **target state**, **dependencies**, **data contracts**.

---

## System diagram (target)

```
                 EXTERNAL DATA SOURCES (10 of them, point-in-time-correct)
   ┌─────────────────────────────────────────────────────────────────┐
   │ Falcon ×10 │ data-api │ CLOB WS │ Polygon mempool │ NewsAPI │ X │
   │            │          │  L3     │ (node + RPC)     │         │ TG │ Discord │
   │            │          │         │                  │         │ Kalshi │
   │            │          │         │                  │         │ Manifold │
   └─────┬──────┴────┬─────┴─────┬───┴────┬─────────────┴─────┬───┴───┬──┘
         │           │           │        │                   │       │
         ▼           ▼           ▼        ▼                   ▼       ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                       INGESTION LAYER                              │
   │ src/registry/ │ src/observer/{trade,book,clob}_observer            │
   │ src/mempool/ (NEW R7) │ src/social/ (NEW R12) │ src/cross_market/ │
   └─────┬─────────┬─────────┬─────────┬─────────┬───────────────────┘
         │ trades  │ books   │ intents │ social  │ cross-venue
         ▼         ▼         ▼         ▼         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │   REDIS STREAMS (durable, at-least-once, dead-letter)              │
   │   trades:stream │ books:stream │ mempool:leader_intent │ ...       │
   └─────┬─────────┬─────────┬─────────┬─────────┬───────────────────┘
         │         │         │         │         │
         ▼         ▼         ▼         ▼         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                  STORAGE + FEATURE STORE                           │
   │ PostgreSQL (partitioned trades, history tables, feature_store.py)  │
   │ Migrations 011–022+ : retention, partition, as-of correctness      │
   └────────────────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                     MODELING LAYER                                 │
   │ src/profiler/         (behavior, error_model, feature_store)       │
   │ src/strategy_classifier/  (NEW R8 — supervised per-wallet)         │
   │ src/graph/hawkes_*  (R5 BIC bivariate, R9 multivariate)            │
   │ src/follower_volume/  (NEW R9 — Kalman state-space)                │
   │ src/causal/  (NEW R10 — IV, do-calculus, counterfactual)           │
   │ src/calibration/  (NEW R13 — drift, auto-disable)                  │
   └────────────────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                     DECISION LAYER                                 │
   │ src/engine/confidence_engine.py  (Thompson + strategy-conditional) │
   │ src/engine/decision_router.py     (paper / live / dual)            │
   │ src/engine/risk_manager.py        (runtime-mutable knobs)          │
   │ src/control/killswitch.py         (DB + strict-path consultation)  │
   └────────────────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                     EXECUTION LAYER                                │
   │ src/engine/paper_trader.py        (virtual portfolio)              │
   │ src/engine/live_trader.py         (CLOB via py-clob-client)        │
   │ src/execution/prefill/  (NEW R7 — pre-signed pool + intent router) │
   └────────────────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │       OBSERVABILITY + DASHBOARD + ALERTS                           │
   │ /metrics (Prometheus, 60+ series at end-state)                     │
   │ docs/monitoring/alerts.yml (Round 7+ adds mempool alerts)          │
   │ src/api/  (FastAPI dashboard + WS bridge)                          │
   │ src/monitoring/ingest_health.py  (Round 3 — gap detector)          │
   │ src/telegram_bot/  (operator alerts + /commands)                   │
   │ research/  (NEW R13 — Jupyter notebooks)                           │
   └────────────────────────────────────────────────────────────────────┘
```

---

## Layer-by-layer breakdown

### Ingestion layer

#### `src/registry/`

**Role**: Identify and continuously enrich the leader watchlist.

**Current state (Phase 3 R1)**:
- `falcon_client.py`: FalconKeyPool (1→N keys), adaptive token bucket
  with 429-backoff, in-flight call coalescing, conditional GET on 4
  agents
- `leader_registry.py`: event-driven `refresh_wallet(wallet, reason)`
  callable from anywhere; 30-min wall-clock timer kept only as a floor;
  daily Falcon budget guardrail in Redis
- `event_bridge.py`: WS-trade-event → `refresh_wallet` bridge

**Target state**:
- Add ingestion of agent 568 (candlesticks), 572 (orderbook history), 585
  (social pulse) — currently unused per the audit inventory
- Per-wallet refresh policies: structural bots refresh quarterly, social-
  driven leaders refresh hourly (tied to X firehose intensity)
- Multi-source attribution: every wallet's Falcon score cross-validated
  against the wallet's actual on-chain P&L (computed from
  `trades_observed` against oracle outcomes)

**Data contracts**:
- Writes: `leaders` table (UPSERT), `markets` table (UPSERT), and
  `market_features_history` (append-only, point-in-time)
- Reads: `falcon:budget:YYYYMMDD` (rate-limit guardrail), Redis
  `event_bridge` channel

---

#### `src/observer/`

**Role**: Capture every trade and book update from Polymarket, with
no time-window holes.

**Current state (Phase 3 R1)**:
- `trade_observer.py`: 5-second REST poll + ETag conditional GET +
  continuous cursor (Redis) + bounded queue + dedicated `_db_writer_loop`
  with micro-batched commits + backpressure
- `websocket_client.py`: WS client with freshness watchdog, force-reconnect
  on >60s silence, bounded backfill (≤24h) on reconnect
- `position_tracker.py`: OPEN→CLOSE position reconstruction with
  persistent state (warm-start across restarts)
- `orderbook_observer.py` (Phase 3 R2): per-minute rollup of
  `book_quality_snapshots` into `orderbook_features_minute`

**Target state**:
- NEW `clob_book_observer.py` (Round 11) — captures every book event
  (placement, modification, cancellation) at full WS granularity, into
  the partitioned `clob_book_events` table
- NEW `microstructure.py` (Round 11) — derives iceberg detection, spoof
  patterns, order-flow-imbalance
- All observers emit to Redis Streams (trades:stream from Round 3 R1 +
  new books:stream and microstructure:stream)

**Data contracts**:
- Reads: Polymarket WS, data-api, Falcon agent 556 (backfill)
- Writes: `trades_observed` (partitioned by time since R2),
  `book_quality_snapshots`, `orderbook_features_minute`, NEW `clob_book_events`,
  Redis cursors (`observer:cursor:trades:*`)

---

#### `src/mempool/` — NEW (Round 7)

**Role**: Detect leader trade intent **before** chain confirmation.

**Target state** (no current code):
- `node_client.py` — Polygon RPC/WS subscriber for `newPendingTransactions`
- `tx_decoder.py` — decodes pending tx against Polymarket CLOB
  contract ABI
- `wallet_index.py` — bloom filter of watched leader wallets for O(1)
  filtering
- `event_emitter.py` — publishes to `mempool:leader_intent` Redis Stream

**Data contracts**:
- Reads: Polygon mempool firehose (~thousands of tx/sec)
- Writes: `mempool:leader_intent` stream, `mempool_observations` table
  (latency tracking)

**Dependencies**: Polygon RPC provider (paid) OR self-hosted archive node.

---

#### `src/social/` — NEW (Round 12)

**Role**: Capture pre-trade signal from X / Telegram / Discord.

**Target state**:
- `x_firehose.py` — X API filtered stream on watched-leader handles +
  market URLs
- `nlp_classifier.py` — LLM-distilled tweet classifier
  {entry_signal, exit_signal, noise}
- `telegram_listener.py`, `discord_listener.py` — public-channel readers

**Data contracts**:
- Writes: `social_signals` table, Redis Stream `social:stream`

---

#### `src/cross_market/` — NEW (Round 12)

**Role**: Detect leaders who trade simultaneously on Kalshi, Manifold,
PredictIt — and use their other-venue positions as features.

**Target state**:
- One client module per venue
- `wallet_resolver.py` — semi-manual cross-venue wallet matching
- `position_aggregator.py` — same-wallet positions across venues

**Data contracts**:
- Writes: `cross_market_positions` table

---

### Storage layer

#### PostgreSQL (with as-of correctness everywhere)

**Current schema (after migration 019)**:

| Table | Role | Partition | Retention |
|---|---|---|---|
| `leaders` | Per-wallet metrics + Falcon score + classification | — | none (low-volume) |
| `trades_observed` | Every observed trade | by month (013) | 90 d, DROP PARTITION |
| `positions_reconstructed` | OPEN→CLOSE position cycles | — | 180 d |
| `position_tracker_state` | Persistent in-memory shadow (R2 task C) | — | live state |
| `markets` | Market metadata | — | none |
| `market_features_history` | Append-only feature history (R2 task Y) | — | 18 mo |
| `paper_trades` | Virtual portfolio | — | 180 d |
| `decision_log` | Every routing decision | — | 90 d |
| `live_orders` | py-clob-client live orders | — | 180 d |
| `follower_edges` | Bivariate Hawkes per leader-follower pair | — | none |
| `book_quality_snapshots` | Raw book updates | — | 30 d |
| `orderbook_features_minute` | Per-minute rollup (R2 task Z) | — | 90 d |

**Target additions (Round 6–13)** — synchronized with the per-round specs:

| Table | Migration | Round | Purpose |
|---|---|---|---|
| `wallet_universe` | 020 | R6 | All ~1.5 M Polymarket wallets ever, with adaptive depth tier |
| `trades_observed` (ext.) | 021 | R6 | + `block_number`, `tx_hash`, `log_index` + UNIQUE for chain-source dedup |
| `chain_sync_state` | 022 | R6 | Last processed Polygon block (resume on restart) |
| `rpc_health_history` | 023 | R6 | Per-provider availability + latency |
| `mempool_observations` | 024 | R7 | Tx detected in mempool — observation→confirmation latency |
| `live_orders` (ext.) | 025 | R7 | + `intent_id` FK to `mempool_observations` |
| `strategy_labels` + `leader_strategy_history` | 026 | R8 | Hand-labels + classifier outputs (append-only) |
| `leaders.classification_json` (formalised schema) | 027 | R8 | Strategy probs / primary / confidence / model_version |
| `multivariate_hawkes_fits` | 028 | R9 | N-dim Hawkes results per leader |
| `follower_pool_state` | 029 | R9 | Kalman state per (leader, pool_class) |
| `causal_estimates` | 030 | R10 | IV-adjusted ATE vs Hawkes statistical |
| `instrumental_events` | 031 | R10 | News / oracle / API-outage events used as instruments |
| `clob_book_events` | 032 | R11 | Every order-life event, partitioned by hour |
| `microstructure_features` | 033 | R11 | Per-minute rollups (iceberg / spoof / OFI) |
| `wallet_microstructure_signature` | 034 | R11 | Per-wallet 30-day signatures |
| `social_signals` | 035 | R12 | Tweet / TG / Discord captured signals |
| `cross_market_operators` | 036 | R12 | Cross-venue identity resolutions |
| `cross_market_positions` | 037 | R12 | Same-wallet positions across venues |
| `decision_predictions` | 038 | R13 | Per-decision model predictions, captured atomically |
| `calibration_loss_history` | 039 | R13 | Daily per-model loss snapshots |
| `model_disable_state` | 040 | R13 | Auto-disable flags per model |

**Discipline**: every new table that backs a model FEATURE is append-only
(history table). Every "current value" table that the model READS is
matched by a `_history` shadow.

#### `src/profiler/feature_store.py` (Round 2 R2)

**Role**: Point-in-time-correct reads against history tables.

**Current API**:
```python
get_market_features_asof(conn, market_id, asof_ts) -> dict | None
get_market_features_asof_batch(conn, queries) -> dict[(market_id, asof), dict]
get_orderbook_features_asof(conn, token_id, asof_ts, lookback_s) -> dict
```

**Target extensions** (Round 7+):
```python
get_strategy_probs_asof(conn, wallet, asof_ts) -> dict
get_follower_pool_state_asof(conn, leader, pool_class, asof_ts) -> dict
get_causal_estimate_asof(conn, leader, follower, asof_ts) -> dict
get_social_signals_asof(conn, wallet, asof_ts, window_s) -> list[dict]
```

All batched, all single-roundtrip via CTE + LATERAL JOIN.

---

### Modeling layer

#### `src/profiler/behavior_profiler.py`

**Current**: size-weighted Dirichlet categories, EWMA sizing, Beta
accuracy posteriors, CUSUM drift detection, decision_process features.

**Target**: feeds the strategy classifier (R7) as the per-leader feature
vector. No structural change; the profiler is correct as-is.

#### `src/profiler/error_model.py`

**Current**: 3-phase (Beta-Binomial → BayesianRidge → LightGBM+Platt)
with as-of liquidity reads from `market_features_history` (Round 2 R2).

**Target additions**:
- Phase-2/3 input features extend to include: orderbook microstructure
  (Round 10), follower-pool size (Round 8), causal-coupling strength
  (Round 9), social-signal density (Round 11)
- Strategy-conditional retraining: the phase-3 LightGBM is fit
  per strategy class (Round 7), not globally — better calibration on
  rare classes like info_leak

#### `src/strategy_classifier/` — NEW (Round 7)

**Role**: Classify each leader into one of 9 strategy classes with
calibrated probabilities. Per-leader, recomputed daily.

**API**:
```python
class StrategyClassifier:
    def featurize(self, wallet: str, asof_ts: datetime) -> np.ndarray
    def predict_proba(self, features: np.ndarray) -> dict[str, float]
    async def refresh_leader(self, wallet: str) -> dict
    async def run_batch(self) -> int
```

#### `src/graph/hawkes_*` — bivariate (R5) → multivariate (R8)

**Current** (`hawkes_fitter.py`, Round 5):
- Bivariate (leader→follower) MLE with BIC regularization
- α/μ > 1 ⟹ confirmed leader→follower coupling
- Fits per pair, populated nightly via batch

**Target** (`hawkes_multivariate.py`, Round 8):
- N-dim intensity matrix with i, j ∈ {leader_pool, follower_pool_1,
  follower_pool_2, ...} where pools are clustered by strategy class
- Block-sparse priors enforce identifiability
- BIC penalty extends naturally: `k_penalty = (non-zero α_ij count)`

#### `src/follower_volume/` — NEW (Round 8)

**Role**: Predict E[follower volume in next N min | leader trade], with
uncertainty.

**API**:
```python
class FollowerVolumePredictor:
    async def predict(
        self, leader: str, market: str, trade_size: float, asof_ts: datetime
    ) -> dict  # {expected_volume, ci_low, ci_high, pool_breakdown}
```

Internally: Kalman state-space on per-leader follower-pool sizes +
Hawkes excitation kernel + strategy-class conditional priors.

#### `src/causal/` — NEW (Round 9)

**Role**: Distinguish causation from association. Provides ATE
estimates that complement Hawkes statistical estimates.

#### `src/calibration/` — NEW (Round 12)

**Role**: Continuous validation that models are still calibrated. Drift
detection per strategy class. Auto-disable on regression.

---

### Decision layer

#### `src/engine/confidence_engine.py`

**Current**: Thompson Sampling FOLLOW vs FADE vs SKIP per leader.

**Target additions**:
- FOLLOW confidence becomes `f(thompson_sample, strategy_class,
  follower_volume_prediction, causal_ate)`. Strategy-conditional
  weights: directional leaders get high weight on Thompson, info-leak
  leaders get high weight on causal_ate, social-driven leaders get high
  weight on social-signal recency.
- New entry policy: `volume_anticipation` (Round 8) — sized by
  `follower_volume_predictor`, not by leader confidence.

#### `src/engine/decision_router.py`

**Current**: paper / live / dual routing.

**Target**: same, plus a new branch `prefill_intent` (Round 6) that
routes a mempool-detected intent to the pre-signed order pool ahead of
the leader's chain confirmation.

#### `src/engine/risk_manager.py` + `src/control/runtime_config.py`

**Current**: runtime-mutable risk knobs (Redis-backed, validated)
consulted by RiskManager. Killswitch consultation via strict path
(Phase 0 R2 task B fix).

**Target**: same — these are correct as-is. Add the new runtime knobs
for Round 8 / Round 9 entry policies (volume_anticipation threshold,
causal_ate threshold).

---

### Execution layer

#### `src/engine/paper_trader.py`

**Current** (Phase 0 wrapped in tx, Phase 3 R1 dual-writes to streams):
correct. No structural change.

#### `src/engine/live_trader.py`

**Current** (Phase 0 R2 task B wired killswitch strict-path
consultation): correct. The mempool-driven prefill (Round 6) calls into
`live_trader.open_trade` after the pre-signed order has been chosen.

#### `src/execution/prefill/` — NEW (Round 6)

**Role**: Maintain a pool of pre-signed CLOB orders ready to fire on
mempool-detected leader intent. The 10ms-from-detect-to-submit path
that makes "BEFORE" possible.

**API**:
```python
class PreSignedPool:
    async def warm(self, markets: list[str]) -> int  # generate orders
    async def fire(self, intent: LeaderIntent) -> LiveOrder | None
    async def expire_stale(self) -> int  # rotate before signature expiry
```

---

### Observability + Dashboard

#### `src/monitoring/metrics.py` + `/metrics` (Phase 1)

**Current**: 50 series across ingestion, Falcon, Redis Streams, ingest
health, position tracker, killswitch, Hawkes.

**Target end-state**: ~80 series. Each new module adds ~5:
- Round 6 (mempool): observations, detection latency, fire latency,
  pool size, pool misses, intent→fill timing
- Round 7 (classifier): per-strategy classification rate,
  classifier calibration loss, drift detection
- Round 8 (multivariate Hawkes / follower-volume): forecast MAPE,
  Kalman innovation, pool-size distribution
- Round 9 (causal): IV instrument validity score, ATE confidence width
- Round 11 (social): tweet ingestion rate per leader, NLP classifier
  precision

#### `docs/monitoring/alerts.yml` (Phase 3 R1)

**Current**: 7 alert rules including `IngestSourceDown if silent >30min`.

**Target**: 1-2 new alert rules per round:
- Round 6: `MempoolDetectionRateLow` if observations/min drops below
  baseline
- Round 7: `StrategyClassifierDriftHigh` if classifier loss > threshold
- Round 12: per-strategy `CalibrationLossExceedsBudget`

#### `src/api/`

**Current**: FastAPI dashboard with WS bridge, terminal snapshot, 22+
endpoints including wallet drilldown, ML diagnostics, data-quality
endpoints.

**Target**: add 4–5 endpoints per round to surface the new model
outputs:
- Round 7: `GET /api/leader/{wallet}/strategy` — strategy probabilities
- Round 8: `GET /api/leader/{wallet}/follower_volume_forecast`
- Round 9: `GET /api/edge/{leader}/{follower}/causal_estimate`
- Round 12: `GET /api/calibration/per_strategy` — loss + drift dashboard

#### `src/telegram_bot/` (Phase 0/1/2 untouched)

**Current**: outbound alerts + inbound `/commands` (status, pnl,
positions, mode, killswitch, pause, resume).

**Target**: add `/refresh <wallet>` (Round 7), `/strategy <wallet>`
(Round 7 surface), `/forecast <wallet>` (Round 8 surface). Keep the
bot scope small — it's an operational tool, not a research tool.

#### `research/` — NEW (Round 12)

**Role**: Jupyter notebook directory for ad-hoc analysis using the
feature store. Top-level (not under `src/`) because notebooks aren't
production code.

**Initial files**:
- `00_data_loader.ipynb` — point-in-time-correct feature loading
- `01_strategy_classifier_validation.ipynb`
- `02_causal_analysis.ipynb`
- `03_counterfactual_replay.ipynb`

---

## Cross-cutting principles

### 1. Point-in-time correctness, no exceptions
Every feature read by a model goes through the feature store. The
feature store reads from append-only history tables. No `SELECT
current_value FROM live_table` in training code.

### 2. Redis Streams for durability, pub/sub for speed
After the Phase 3 R1 dual-write soak, every cross-module event flows
through Redis Streams with consumer groups. Pub/sub stays for
ephemeral UI updates (dashboard).

### 3. Everything observable
Every new code path emits at least one metric. The threshold for "is
this code production-ready" includes "do I have a Prometheus alert
that would fire if this breaks?"

### 4. Append-only logs over mutable state
`market_features_history`, `clob_book_events`, `decision_log`,
`mempool_observations`, `social_signals` — all append-only. Mutable
state (`leaders.classification_json`, `position_tracker_state`) exists
for hot-path reads but is shadowed by a history table.

### 5. Tests against the contract, not the implementation
Tests assert that the feature store returns the right value at a given
asof, not that the SQL has a particular shape. This is the discipline
that lets us refactor (e.g., move to TimescaleDB later) without
rewriting tests.

### 6. The killswitch is sacred
Live-trade execution always goes through `KillswitchService.is_real_execution_enabled(bypass_cache=True)`.
Every new execution path (Round 6 prefill router) adds this check.

---

## What this architecture is NOT

- **Not a microservice mesh**. Single Python process, single Postgres,
  single Redis. The complexity is in the models, not the infra.
- **Not a real-time-everything system**. Mempool-detect-to-fire is the
  only sub-second path. Everything else is comfortably second-scale.
- **Not a no-code platform**. Operators interact via Telegram +
  `/api/risk/update` + the dashboard. Researchers use Jupyter
  notebooks. There's no end-user UI beyond that.
- **Not a multi-asset system**. Polymarket only, for now. The
  architecture generalises but the assumption stays implicit.
