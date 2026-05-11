# Round 6 — The Spine

> **Formal title**: Data Sovereignty Layer
> **Colloquial name**: The Spine
> **Why this round comes first**: every model, every prediction, every
> trade in Rounds 7–13 depends on this layer being solid. The earlier
> ROADMAP put mempool ingestion at Round 6; that was the right
> *component* but the wrong *frame*. Mempool watching is one consumer
> of the broader architecture this round builds.

---

## 1. The thesis — flip the data-acquisition model

Every existing bot (ours included, today) treats Polymarket like a
black-box exchange and consumes via rate-limited public APIs:

```
  Bot ─── REST poll (5s) ────► data-api.polymarket.com  (rate-limited)
  Bot ─── WS subscribe ──────► clob.polymarket.com      (no wallets)
  Bot ─── 60 RPM per key ────► Falcon ×10 agents         (rate-limited)
```

That model has a ceiling. Every API is opinionated, rate-limited, and
gives us a *projection* of the underlying truth. Holes happen at the
projection layer, not the data layer.

**The breakthrough**: Polymarket's CLOB is a smart contract. Every
trade emits a `LOG` event on the Polygon blockchain. **The chain IS
the canonical record** — Falcon and data-api are value-added layers on
top of the same data. So:

```
                Polygon blockchain (canonical source of truth)
                       │
                       ▼
            ┌──────────────────────┐
            │ Our self-hosted node │ ◄── infinite rate, sub-2s latency
            └──────────┬───────────┘
                       │
                       ▼
       Bot's ingestion daemon (process-per-source supervisor)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
   chain events   API enrichment  social signal
   (100% covg)    (Falcon, etc.)  (X, TG, etc.)
```

**100% coverage by construction.** No rate limit on our own node. APIs
become enrichment, not the primary feed. Holes become a memory.

This is the round that makes everything else possible.

---

## 2. The Hetzner-specific architecture

### Current state
- **box-1** = `polymarket-prod` (Helsinki, CX23 = 2 vCPU / 4 GB / 80 GB SSD)
  - Runs: engine, observer, API, Postgres, Redis — all on one host
  - Memory headroom: tight (~500 MB free under normal load)
  - Network: 20 TB/mo included, gigabit internal

### Target state — two boxes, private-network linked
- **box-1** stays as-is (the bot)
- **box-2** = NEW, `polymarket-node` (Helsinki, CX31 = 4 vCPU / 8 GB / 80 GB SSD + 200 GB Volume)
  - Runs: Polygon Erigon node (pruned, not archive — see § 3.1)
  - Cost: €13.10/mo (CX31) + €8/mo (200 GB volume) = **€21/mo**
  - Connected to box-1 via **Hetzner private network** (free, gigabit, no NAT)

**Why two boxes, not one upgraded box**: blast-radius isolation. If the
node crashes or rebuilds (it WILL during initial sync), the bot keeps
running on the fallback paid-RPC providers. If the bot OOM-kills
itself, the node keeps syncing. Failure modes don't cross.

**Hetzner-specific advantages we exploit**:
- Free private-network traffic between boxes → unlimited bandwidth for
  RPC calls
- Hetzner volumes can be detached and reattached → node disk survives
  instance rebuilds
- Hetzner storage box (€3.40/mo for 1 TB) for cold Parquet archival
- Hetzner snapshot backups → restore node from snapshot, not from
  genesis-resync (saves 2 weeks)

### Why Erigon (and not Geth)?
- Pruned-mode disk footprint: ~150 GB vs Geth's ~300 GB
- 3-4× faster sync on commodity hardware
- Better WebSocket subscription performance (we'll have ~10 concurrent
  subscriptions)
- Native support for `eth_subscribe('logs', ...)` with high-volume
  filters

### Why pruned, not archive
- Archive node disk: 2 TB+ for Polygon. Cost: €70/mo just for the volume.
- For real-time trade ingestion we only need recent state (last ~256
  blocks = ~10 minutes of history)
- Historical backfill (the one-time wallet-universe crawl) uses paid
  RPC providers — one-time cost, then we drop them
- We can always add an archive node later if research demands it

---

## 3. Component breakdown

### 3.1 `infra/polygon-node/` — Erigon deployment

Not a Python module, but a deployment artifact tracked in the repo
because it's part of the bot's runtime topology.

Contents:
```
infra/polygon-node/
├── README.md                 # node operator runbook
├── erigon.service            # systemd unit
├── docker-compose.yml        # alternative: containerized Erigon
├── config.toml               # Erigon config (pruned, polygon-mainnet)
├── snapshot-restore.sh       # restore from Hetzner snapshot
└── healthcheck.py            # exports `polygon_node_health` to Prometheus
```

**Sync strategy**:
1. Bootstrap from a public Polygon snapshot (saves ~10 days of sync)
2. Catch up the remainder via the network (~2-3 days)
3. Steady state: 2-second block production, instant ingestion

**Operational invariants**:
- Node must be at chain-head within 60 s, otherwise alert
- Disk usage < 80 % of volume, otherwise alert
- Memory usage < 6 GB (out of 8), otherwise alert

### 3.2 `src/rpc/` — Multi-RPC abstraction layer

The Bot doesn't call any RPC provider directly. It calls a smart
router that handles fallback, circuit breaking, and request coalescing.

```python
# src/rpc/client.py
class RPCClient:
    """Multi-provider Polygon RPC client.

    Tries providers in priority order:
      1. Local Erigon (priority 0, infinite rate, ~5ms latency)
      2. Alchemy (priority 1, paid tier, fallback)
      3. QuickNode (priority 2, free tier, last resort)

    Per-provider:
      - Adaptive token bucket (extends the Phase 1 FalconClient pattern)
      - Circuit breaker: 5 consecutive failures → 60s cooldown
      - HTTP/2 multiplexing via httpx
      - In-flight call coalescing (identical concurrent requests share
        one HTTP call, 30s TTL — same pattern as FalconClient)

    Methods mirror eth-rpc semantics but with our defensive layer:
      - eth_subscribe(filter) → AsyncIterator[log]
      - eth_call(contract, method, args) → result
      - eth_getLogs(filter, from_block, to_block) → list[log]
      - eth_getBlockByNumber(num) → block
    """
```

```python
# src/rpc/providers.py
class ProviderPool:
    """Holds N RPCProvider instances, exposes acquire() returning the
    best available provider given current circuit-breaker + budget state."""
```

**Metrics**:
- `polybot_rpc_calls_total{provider, method, result}`
- `polybot_rpc_latency_seconds{provider, method}`
- `polybot_rpc_circuit_breaker_open{provider}` (gauge)
- `polybot_rpc_fallback_total{from_provider, to_provider}` (counter)
- `polybot_rpc_coalesced_calls_total{provider, method}`

### 3.3 `src/onchain/` — Polymarket CLOB on-chain ingestion

This is the heart of the round. Direct subscription to CLOB contract
events. **Every trade arrives natively with wallet attribution.**

```python
# src/onchain/clob_listener.py
class CLOBChainListener:
    """Subscribes to Polymarket CLOB contract events on Polygon.

    Events we decode (Polymarket CTF Exchange ABI):
      - OrderFilled(maker, taker, makerAssetId, takerAssetId, ...)
      - OrderCancelled(orderHash)
      - OrdersMatched(takerOrderHash, takerOrderMaker, ...)
      - FeeRateUpdated, TradingStatusUpdated, ...

    For each event:
      1. Decode against the ABI
      2. Resolve wallet from event topic (it's right there!)
      3. Look up the market_id / token_id from event data
      4. Publish to Redis Stream `chain:trades:stream`
      5. UPSERT into `trades_observed` (the existing table) with
         source='onchain' for cross-source dedup
    """
```

**Why this is a game-changer**:

| Today (REST poll) | After Round 6 (on-chain) |
|---|---|
| 5 s latency floor (poll cadence) | ~2 s latency (block time) |
| Rate-limited (data-api throttles) | Infinite (our own node) |
| Coverage depends on poll-window correctness | 100% by construction |
| Wallet attribution via separate REST call | Native — wallet is in the event topic |
| Holes when data-api is slow/down | Holes only when chain itself stalls |

**Reconciliation with existing observer**:
- `trade_observer.py` (Phase 3 R1 work) keeps running — REST polling
  becomes a SECONDARY source for cross-validation
- Both sources write to `trades_observed` with the existing UNIQUE INDEX
  → automatic deduplication
- New metric `polybot_trade_source_disagreement_total{primary, secondary}`
  fires when only one source sees a trade — that's a data-quality alert,
  not a normal occurrence

### 3.4 `src/crawler/` — Universal Wallet Crawler

> **The "all the freaking wallets" component.** The audit estimates
> 1.5M wallets have traded on Polymarket. Today we track 200. After
> this round: we track all of them, with adaptive depth.

```python
# src/crawler/universe.py
class WalletUniverse:
    """Maintains the wallet_universe table — every wallet that has ever
    traded on Polymarket, with light-touch metadata.

    Population strategy (one-time backfill):
      - Scan every block since CLOB contract deployment
      - Extract `maker` and `taker` from every OrderFilled event
      - INSERT INTO wallet_universe ON CONFLICT DO NOTHING

    Ongoing maintenance:
      - Each on-chain event from CLOBChainListener checks the wallet
        against wallet_universe and inserts if new

    Volume estimate:
      - 1.5M wallets × avg 20 trades = 30M edges
      - Trivial for partitioned Postgres (already at-scale-ready post-R2)
    """

# src/crawler/depth_tiers.py
class AdaptiveDepth:
    """Decides how deeply each wallet gets enriched.

    Tier 0 — full enrichment (currently top 200):
      - All Falcon agents on a daily refresh
      - Strategy classifier (Round 7)
      - Hawkes pairwise fit (Round 8)
      - Daily decision flow

    Tier 1 — periodic refresh (top 2000 by recent 30d volume):
      - Falcon 581 (Wallet360) + 569 (PnL) weekly
      - Strategy classifier monthly
      - Coarse Hawkes against the leader pool

    Tier 2 — light tracking (everyone else, ~1.5M):
      - Just timestamps + sizes + markets from on-chain
      - No Falcon calls — Falcon would be the bottleneck if we tried
      - Promoted to Tier 1 if their 7-day volume crosses threshold

    Promotion/demotion runs nightly. The bot's compute spend per wallet
    is automatically inversely proportional to wallet count per tier.
    """
```

**Migration 020** — `wallet_universe`:
```sql
CREATE TABLE wallet_universe (
    wallet_address VARCHAR(100) PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL,
    last_active TIMESTAMPTZ NOT NULL,
    total_trades_ever BIGINT NOT NULL DEFAULT 0,
    total_volume_usdc_ever NUMERIC(20, 2) NOT NULL DEFAULT 0,
    depth_tier SMALLINT NOT NULL DEFAULT 2,  -- 0/1/2 per AdaptiveDepth
    last_tier_review TIMESTAMPTZ
);
CREATE INDEX idx_wu_tier ON wallet_universe (depth_tier);
CREATE INDEX idx_wu_last_active ON wallet_universe (last_active DESC);
```

### 3.5 `src/ingestion_daemon/` — Process supervisor

Today, ingestion runs in the same Python process as the engine. If the
engine GIL stalls on a model fit, ingestion stalls too. **That's the
core cause of the "10-30 minute pauses" the operator reported.**

Round 6 splits ingestion into **separate processes**, each owning one
source, supervised by systemd.

```
systemd units (on box-1):
  polymarket-engine.service       # the bot's decision logic
  polymarket-observer.service     # REST + WS observers (existing)
  polymarket-onchain.service      # NEW — CLOB chain listener
  polymarket-crawler.service      # NEW — universe maintenance
  polymarket-falcon-refresher.service  # NEW — event-driven Falcon refreshes
  polymarket-api.service          # FastAPI dashboard
```

Each process:
- Owns exactly one ingestion source
- Writes only to Redis Streams (the contract from Phase 3 R1)
- Has its own systemd `Restart=always` policy
- Logs to journald (`journalctl -u polymarket-onchain`)
- Exports its own Prometheus metrics on a dedicated port

**Memory budget per process** (CX23 with 4 GB total, target ≤3 GB
combined to leave headroom for Postgres/Redis):
- engine: 800 MB
- observer: 400 MB
- onchain: 400 MB
- crawler: 200 MB
- falcon-refresher: 200 MB
- api: 300 MB
- Postgres + Redis: ~700 MB

That fits CX23. If it doesn't in production, upgrade to CX33 (8 GB,
€11/mo). The process split is the structural fix; the memory upgrade
is a 5-minute reaction.

### 3.6 `src/cold_storage/` — Tiered storage with Parquet archival

```python
# src/cold_storage/exporter.py
class ColdExporter:
    """Nightly export of yesterday's hot+warm data to Parquet.

    Tables exported:
      - trades_observed (the previous day's partition)
      - book_quality_snapshots
      - orderbook_features_minute
      - decision_log
      - positions_reconstructed

    Output structure (local disk on box-1, then optionally synced to
    Hetzner Storage Box):
      /data/cold/
        ├── trades_observed/
        │     └── year=2026/month=05/day=11/part-00000.parquet
        ├── orderbook_features_minute/
        │     └── year=2026/month=05/day=11/part-00000.parquet
        ...
    """

# src/cold_storage/duckdb_view.py
class DuckDBResearchView:
    """Exposes the entire cold history as DuckDB virtual tables.

    Usage from research/ notebooks:
      import duckdb
      con = duckdb.connect('research.duckdb')
      con.execute('CREATE VIEW trades AS SELECT * FROM '
                  '"/data/cold/trades_observed/**/*.parquet"')
      df = con.execute('SELECT wallet_address, COUNT(*) FROM trades '
                       'WHERE year=2026 GROUP BY 1 ORDER BY 2 DESC '
                       'LIMIT 100').df()

    No need to load into pandas. DuckDB scans the Parquet files
    directly with predicate pushdown. Queries that would melt Postgres
    run in seconds against years of data.
    """
```

**Why this matters**: research velocity. The current setup makes
"give me every trade by wallet X over the last 6 months" a 30s
Postgres query. The same query on DuckDB+Parquet is <1s. Round 12's
counterfactual replay becomes practical.

### 3.7 Cross-source coverage observability

```python
# src/monitoring/coverage_reconciler.py
class CoverageReconciler:
    """Periodic cross-source comparison.

    Every 5 minutes, for the previous 5 minutes' window:
      - Count trades by source: onchain, rest_poll, ws_observer, falcon_556
      - Compute pairwise disagreement: trades seen by source A but not B
      - Emit metrics:
          polybot_coverage_disagreement_total{primary, missed_by}
          polybot_coverage_ratio{source}  (= trades_seen / chain_truth)

    If onchain (the source of truth) shows N trades and rest_poll shows
    less than 95% of N, that's the alert that fires before the operator
    notices any hole. This is the actual closure of the
    'data-acquisition holes' problem.
    """
```

**New alert in `docs/monitoring/alerts.yml`**:
```yaml
- alert: TradeIngestionCoverageLow
  expr: polybot_coverage_ratio < 0.95
  for: 10m
  labels: { severity: critical }
  annotations:
    summary: "Source {{ $labels.source }} seeing <95% of on-chain trades"
```

---

## 4. Migration sequence

| Migration | Round | Purpose |
|---|---|---|
| 020 | 6.1 | `wallet_universe` table + initial indexes |
| 021 | 6.2 | `trades_observed`: add `block_number`, `tx_hash`, `log_index` columns + UNIQUE on (tx_hash, log_index) for chain-source dedup |
| 022 | 6.3 | `chain_sync_state` (last processed block, for resume on restart) |
| 023 | 6.4 | `rpc_health_history` (per-provider availability + latency) |

---

## 5. New Prometheus metrics (round 6 contributes ~15)

```
polybot_rpc_calls_total{provider, method, result}
polybot_rpc_latency_seconds{provider, method}
polybot_rpc_circuit_breaker_open{provider}
polybot_rpc_fallback_total{from_provider, to_provider}
polybot_rpc_coalesced_calls_total{provider, method}

polybot_chain_blocks_processed_total
polybot_chain_blocks_behind  (gauge: chain head - our processed head)
polybot_chain_events_decoded_total{event_type}
polybot_chain_events_failed_decode_total{event_type, reason}
polybot_chain_ingestion_latency_seconds  (block_ts → our publish time)

polybot_wallet_universe_size  (gauge: total wallets known)
polybot_wallet_universe_tier_count{tier}  (gauge: per-tier wallet count)
polybot_wallet_universe_promotions_total{from_tier, to_tier}

polybot_cold_export_rows_total{table}
polybot_cold_export_bytes_total
polybot_cold_export_duration_seconds{table}

polybot_coverage_ratio{source}  (gauge: trades_seen / chain_truth, 5min)
polybot_coverage_disagreement_total{primary, missed_by}

polybot_ingestion_daemon_up{service}  (gauge: 0/1 per systemd unit)
polybot_ingestion_daemon_restarts_total{service}
polybot_ingestion_daemon_memory_bytes{service}
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks | Notes |
|---|---|---|
| 3.1 — Erigon node deploy | 1.0 | + 2 weeks wall-time for chain sync (parallel) |
| 3.2 — `src/rpc/` | 1.0 | Build on the existing FalconClient patterns |
| 3.3 — `src/onchain/` CLOB listener | 2.0 | ABI decoding, schema work, dedup |
| 3.4 — `src/crawler/` Universe + tiers | 1.5 | One-time backfill is the biggest single step |
| 3.5 — `src/ingestion_daemon/` split | 1.5 | systemd units, IPC contracts, memory budgets |
| 3.6 — `src/cold_storage/` | 1.0 | DuckDB integration is the fun part |
| 3.7 — Coverage reconciler | 0.5 | Small but critical |
| Documentation + audit doc | 0.5 | round6_the_spine.md, migrations doc |

**Total: ~9 weeks single-dev** (the chain sync runs in parallel, so
calendar time ≈ wall time)

### Dependencies

This round depends on nothing — it's the foundation. **Every later
round depends on this one**:
- Round 7 (strategy classifier): needs the wallet universe + on-chain
  event stream as feature inputs
- Round 8 (multivariate Hawkes): needs comprehensive follower coverage
  that only on-chain ingestion guarantees
- Round 9 (causal inference): needs cross-source independence
  (on-chain vs API) for IV identification
- Round 10 (CLOB book L3): trivially gets dedicated ingester daemon
  via 3.5
- Round 11 (social + cross-market): trivially adds as new daemon
- Round 12 (calibration loop): DuckDB cold tier is the substrate

### Risk (1–5)

| Risk | Score | Mitigation |
|---|---|---|
| Erigon sync takes longer than 2 weeks | 2/5 | Start with public snapshot; parallel-track regular dev work |
| CLOB contract ABI changes | 2/5 | Pin to current ABI version; alert on decode-fail spike |
| 1.5M wallet backfill blows up Postgres | 3/5 | Use batched INSERT with retries; partition `wallet_universe` if needed (~50 MB at full size, probably fine) |
| Process split causes IPC bugs | 3/5 | Redis Streams contract is well-tested from Phase 3 R1; consumer groups handle the heavy lifting |
| €21/mo for the node feels expensive | 1/5 | It's the single highest-leverage spend in the project — eliminates infinite future rate-limit pain |
| Cross-source disagreement reveals a deep bug | 4/5 | This is the goal, not a risk — we WANT to discover any current source bug |

### Acceptance criteria

- **Coverage**: `polybot_coverage_ratio{source="onchain"} = 1.0` (by
  construction) and `polybot_coverage_ratio{source="rest_poll"} > 0.95`
  in steady state. If REST drops below 95%, alert fires — and we
  *learn something true* about the data-api.
- **Latency**: p95 `chain_ingestion_latency_seconds < 4.0` (block
  produced → trade published to stream).
- **Universe**: `polybot_wallet_universe_size > 1_000_000` after
  initial backfill.
- **Reliability**: process-split survives a kill-9 on any one ingester
  without losing trades (Redis Streams consumer group recovers).
- **DuckDB**: a notebook query against 90 days of cold trades returns
  in < 5 s wall time.

---

## 7. Rollout plan

Strictly sequential, since each component depends on the previous:

### Phase 6.A — Node + RPC (weeks 1-2, parallel chain sync week 1-3)
1. Provision `polymarket-node` Hetzner box + private network link
2. Deploy Erigon (`infra/polygon-node/`)
3. Start chain sync (runs in background)
4. Build `src/rpc/` against paid providers (Alchemy + QuickNode) —
   this lets `src/onchain/` be developed before the node finishes
   syncing
5. Tests pass against mocked RPC responses
6. **Gate**: `src/rpc/` ships, paid providers wired, tests green

### Phase 6.B — On-chain ingestion (weeks 3-5)
1. Build `src/onchain/clob_listener.py` against the paid RPC pool
2. Migration 021 (`trades_observed` block/tx columns + UNIQUE)
3. Migration 022 (`chain_sync_state`)
4. Run in shadow mode for 1 week: writes new rows but downstream
   consumers still use REST data. Reconciler verifies coverage.
5. Switch primary source to on-chain; REST becomes redundancy
6. **Gate**: `polybot_coverage_ratio{source="onchain"} = 1.0` for 7
   consecutive days

### Phase 6.C — Switch RPC primary to local node (week 4-5, after sync)
1. Once Erigon is at chain-head, add it to the RPCClient pool at
   priority 0
2. Watch the `polybot_rpc_fallback_total` counter for sanity
3. After 48h of stable local-node usage, demote paid providers to
   backup-only (still configured, only used if local fails)
4. **Gate**: Local node serves >99% of RPC traffic for 7 days

### Phase 6.D — Wallet Universe + crawler (weeks 5-7)
1. One-time historical backfill: scan from CLOB contract genesis,
   populate `wallet_universe`. Uses the paid-RPC pool (this is the
   only time we hit them heavily again).
2. Build adaptive-depth manager
3. Migration 020 (`wallet_universe` schema)
4. **Gate**: `wallet_universe_size > 1_000_000`, tier promotions/
   demotions running nightly

### Phase 6.E — Process supervisor split (weeks 7-8)
1. Write systemd units for each daemon
2. Migrate daemons one at a time; each daemon's PID lock prevents
   conflicts during cutover
3. Validate via `polybot_ingestion_daemon_up` gauges
4. **Gate**: 7 days of running with no manual restarts, all daemons
   green

### Phase 6.F — Cold storage + observability (weeks 8-9)
1. Build `src/cold_storage/exporter.py` + nightly cron
2. Build `src/cold_storage/duckdb_view.py` + research notebook example
3. Build `src/monitoring/coverage_reconciler.py`
4. New alert rules in `docs/monitoring/alerts.yml`
5. **Gate**: a research notebook can answer "trades by wallet X over
   the last 90 days" in < 5 s

---

## 8. What this round explicitly does NOT do

- **Does NOT run a full archive Polygon node**. Pruned only. Archive
  is a research-grade upgrade for a later round if and when the
  research demands it.
- **Does NOT implement the mempool watcher**. That was the old R6
  scope; it becomes Round 7 (and gets simpler because it now uses our
  RPC client + on-chain infrastructure as substrate).
- **Does NOT implement social or cross-market ingestion**. Round 11's
  scope. The daemon framework built here makes them trivial to add.
- **Does NOT replace Falcon**. Falcon agents 581 / 584 / 569 / 585
  remain enrichment layers — we read the chain for trades, Falcon for
  the value-added per-wallet metrics.
- **Does NOT introduce a message broker** (RabbitMQ, Kafka). Redis
  Streams is more than enough for our volume; introducing Kafka would
  be premature complexity.
- **Does NOT touch the API/dashboard**. The dashboard reads from
  Postgres, which is now populated by more sources. No API change
  required.

---

## 9. The non-obvious gains

Listed in expected-impact order:

1. **The bot becomes resilient to Polymarket itself**. If the data-api
   goes down for an hour, the bot keeps trading — it has on-chain
   ingestion. This has never been possible before.

2. **Backtests get honest**. Today our backtest replays trades from
   `trades_observed`, which contains whatever the REST polling caught.
   Holes in production data = silent holes in the backtest. After
   on-chain ingestion, every backtest replays against the **canonical
   record**. The Sharpe numbers will move; they will move toward truth.

3. **The "Universal Wallet Crawl" eliminates seed-bias**. Today we
   only know about the top 200 wallets by Falcon score. Falcon's
   ranking has its own biases. The wallet universe lets us discover
   leaders Falcon hasn't ranked yet — useful in itself, but more
   importantly it makes the strategy classifier (Round 7) honest by
   removing label leakage from the training set.

4. **Cross-source disagreement becomes a feature, not a bug**. When
   the reconciler shows REST polling missed a trade, that's a
   debug-able event. We learn something about the data-api's
   pathologies. Over time, the disagreement metric trends toward zero
   — and when it doesn't, we get pinged before any human notices.

5. **The cold tier breaks the research/production divide**. Today,
   "I want to look at every trade for leader X" requires a custom
   script. After Phase 6.F, it's one DuckDB SQL in a notebook. The
   compounding effect on iteration speed is hard to overstate.

---

## 10. The single sentence

> Round 6 makes the bot **own its data sovereignty** — direct chain
> reads + multi-RPC redundancy + process-split ingestion + cold-tier
> archive + universal wallet coverage — so every later round can
> assume *the data is there, no holes, no rate limits, indefinitely*.
