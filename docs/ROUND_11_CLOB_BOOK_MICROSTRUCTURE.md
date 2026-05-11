# Round 11 — CLOB Book L3 + Microstructure Features

> **Formal title**: Sub-Trade Order-Flow Intelligence
> **Colloquial name**: The Microscope
> **Prerequisite**: Round 6 (ingestion daemon framework + cold tier).
> Parallel to Round 9 (Microscope doesn't block multivariate Hawkes;
> Hawkes doesn't block microstructure capture).

---

## 1. The thesis — the data BELOW the trade

Today's pipeline sees Polymarket as a stream of trades:
- Trade fires → captured by `trade_observer` (R3) or `clob_listener` (R6)
- Per-minute book rollup → `orderbook_features_minute` (R2 Z)

But **a trade is the END of an order's life**. Before each trade, the
order was placed, sometimes modified, sometimes cancelled, sometimes
partially filled. The behavior visible at the **order placement** level
is qualitatively different from the behavior visible at the trade
level:

- A market-maker places + cancels 100 orders per second, fills 5
- A spoofer places large orders, cancels before fill, captures spread
- An iceberg trader places one visible order, refills as it gets eaten
- A retail trader places one order, walks away

All four would show identical trade patterns under R2's per-minute
rollup. At order-event granularity, they look completely different.

> Round 11 captures **every order event** (placement, modification,
> cancellation, partial fill) at full WebSocket granularity, derives
> microstructure features (iceberg detection, spoof scoring, order
> flow imbalance), and feeds them into the Round 8 strategy classifier
> as new dimensions.

This is the data layer the strategy classifier (R8) needs to graduate
from "blunt instrument" to "precise tool" — the highest-leverage
features in § 3.1 of R8 (`cancel_to_fill_ratio`, `iceberg_usage_pct`,
`spoof_score`) all require sub-trade granularity.

---

## 2. The Hetzner-specific architecture

Round 11 adds **one new daemon** on box-1 (the bot box), reusing the
Round 6 `ingestion_daemon` framework:

```
systemd units (post-R11):
  polymarket-engine.service          # (R0 — original)
  polymarket-observer.service        # (R3 — trade-level REST+WS)
  polymarket-onchain.service         # (R6 — on-chain CLOB events)
  polymarket-crawler.service         # (R6 — universe maintenance)
  polymarket-falcon-refresher.service # (R6 — event-driven refresh)
  polymarket-mempool.service         # (R7 — mempool subscriber)
  polymarket-book-l3.service         # (R11 — NEW: order-event firehose)
  polymarket-api.service             # (R0 — FastAPI dashboard)
```

### 2.1 Why a separate daemon

L3 book event volume is high — peak ~5,000 events/sec across the
top-100 markets during news cycles. If this lived in `polymarket-observer`,
the Python GIL would block trade ingestion during bursts. Per Round 6's
daemon split principle: **one daemon per source**, blast-radius isolation.

### 2.2 Memory budget

```
polymarket-book-l3.service: 500 MB target
  - WebSocket connection state: 50 MB
  - Per-market book replicas (top-100): 100 MB
  - Iceberg/spoof detection rolling buffers: 100 MB
  - Output stream batching buffers: 50 MB
  - Headroom: 200 MB
```

Fits comfortably in the CX23's 4 GB envelope alongside the other
daemons.

### 2.3 Storage growth

This is the highest-volume table in the entire system:
- ~5,000 events/sec peak, ~1,000 events/sec sustained
- 1,000/s × 86,400 s = ~86M rows/day
- Avg row size: ~150 bytes → ~13 GB/day raw

**Mitigation**: partitioned by HOUR (not day), 30-day retention via
`DROP PARTITION` (Round 2 pattern). Total disk: 13 × 30 = ~390 GB.
Add a 500 GB Hetzner volume to box-1 — €18/mo.

The cold-tier Parquet export (R6 § 3.6) compresses this ~10× for
research queries.

---

## 3. Component breakdown

### 3.1 `src/observer/clob_book_observer.py` — L3 firehose subscriber

```python
class CLOBBookObserver:
    """Subscribes to Polymarket WS at maximum book granularity.

    Stream events captured:
      - order_placed(market, token, side, price, size, wallet?)
      - order_modified(order_hash, new_price, new_size)
      - order_cancelled(order_hash)
      - order_partial_fill(order_hash, fill_size, remaining_size)
      - order_filled(order_hash, final_fill_size)

    Wallet attribution: Polymarket WS does NOT include wallet on order
    placement events (only on fills). For full attribution, join with
    on-chain CLOB events from Round 6 on (tx_hash, log_index).

    Output: writes events to `clob_book_events` (migration 040,
    partitioned by hour) AND publishes to Redis Stream
    `book:events:stream` for downstream microstructure derivations.

    Backpressure: bounded asyncio.Queue (size 50_000) + dedicated
    _db_writer_loop (same pattern as Phase 1 trade_observer). Under
    overload, oldest events get dropped with metric increment — never
    block the WS reader.
    """
```

### 3.2 `src/observer/microstructure.py` — Derived features

```python
class MicrostructureFeatureDeriver:
    """Real-time derivation of microstructure features from the
    `book:events:stream` Redis Stream. Writes per-minute rollups to
    `microstructure_features` table.

    Feature families:

    A. ICEBERG DETECTION
       Pattern: same wallet places a small visible order at price P,
                immediately after the previous one at P is filled.
       Implementation: rolling window per (wallet, price level) over
                       60 s; if N orders at same price by same wallet
                       within window AND each is ≤ 50 % of typical
                       order size → iceberg_score += 1.
       Per-minute output: iceberg_orders_count, iceberg_total_size

    B. SPOOF DETECTION
       Pattern: large order placed, never partially fills, cancelled
                within 5 s. Repeated by same wallet on opposite side.
       Implementation: track (wallet, order_hash, place_time,
                       cancel_time, max_visible_size) ; flag if size
                       > 95th pct AND cancel_time - place_time < 5s
                       AND fill_pct = 0.
       Per-minute output: spoof_orders_count, spoof_total_size,
                          spoof_score_by_wallet

    C. ORDER FLOW IMBALANCE (OFI)
       Definition: (bid_size_delta − ask_size_delta) over rolling
                   window. Positive = buy pressure; negative = sell.
       Implementation: track signed size deltas at top of book; sum
                       over 5s rolling.
       Per-minute output: ofi_mean, ofi_max, ofi_min, ofi_std

    D. PLACE-TO-FILL TIMING DISTRIBUTION
       For each fill, record (place_time, fill_time, place_to_fill_s).
       Wallets with low place_to_fill_s = aggressive takers; high =
       patient makers. Used as input feature to R8 strategy classifier.
       Per-minute aggregation: histogram by wallet.

    E. CANCEL-TO-FILL RATIO PER WALLET
       cancel_to_fill_ratio = n_cancellations / n_fills over rolling
                              30 min. High ratio → market-maker or
                              spoofer.
       Per-minute output: per-wallet ratio for tier-0/1 wallets only
                          (tier-2 is too high-cardinality to store)
    """
```

### 3.3 Feature store integration

The microstructure features become reads via the existing feature store
(R2):

```python
# Extend src/profiler/feature_store.py

async def get_microstructure_features_asof(
    conn,
    market_id: str,
    token_id: str,
    asof_ts: datetime,
    lookback_s: int = 300,
) -> dict | None:
    """Per-token microstructure features as-of asof_ts."""

async def get_wallet_microstructure_signature_asof(
    conn,
    wallet: str,
    asof_ts: datetime,
    lookback_days: int = 30,
) -> dict | None:
    """Per-wallet rolling microstructure: cancel_to_fill_ratio,
    iceberg_score, place_to_fill_s_p50, etc. The new R8 features."""
```

### 3.4 Strategy classifier (R8) feature additions

When R11 ships, R8's `LeaderFeatureExtractor` automatically incorporates
the new microstructure features (since they're added to the feature
store). R8's retraining cadence picks them up; the classifier accuracy
improvement is the measurable acceptance criterion (§ 6).

---

## 4. Migration sequence

```sql
-- Migration 040
-- Partitioned by hour (not day) because of 13 GB/day volume.
CREATE TABLE clob_book_events (
    event_id BIGSERIAL,
    event_time TIMESTAMPTZ NOT NULL,
    market_id VARCHAR(100) NOT NULL,
    token_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(20) NOT NULL,  -- placed|modified|cancelled|partial_fill|filled
    side VARCHAR(4) NOT NULL,
    price NUMERIC(10, 6),
    size_delta NUMERIC(20, 2),
    order_hash VARCHAR(100),
    wallet_address VARCHAR(100),  -- NULL except on fills (per § 3.1)
    source VARCHAR(20) NOT NULL,  -- ws|onchain_reconciled
    raw_payload JSONB,
    PRIMARY KEY (event_id, event_time)
) PARTITION BY RANGE (event_time);

CREATE INDEX idx_cbe_market_time ON clob_book_events
    (market_id, token_id, event_time DESC);
CREATE INDEX idx_cbe_wallet_time ON clob_book_events
    (wallet_address, event_time DESC) WHERE wallet_address IS NOT NULL;
CREATE INDEX idx_cbe_order_hash ON clob_book_events
    (order_hash) WHERE order_hash IS NOT NULL;

-- (Then create initial partitions for the next 24h.)

-- Migration 041
CREATE TABLE microstructure_features (
    market_id VARCHAR(100) NOT NULL,
    token_id VARCHAR(100) NOT NULL,
    bucket_ts TIMESTAMPTZ NOT NULL,
    iceberg_orders_count INTEGER,
    iceberg_total_size NUMERIC(20, 2),
    spoof_orders_count INTEGER,
    spoof_total_size NUMERIC(20, 2),
    ofi_mean NUMERIC(10, 4),
    ofi_max NUMERIC(10, 4),
    ofi_min NUMERIC(10, 4),
    ofi_std NUMERIC(10, 4),
    PRIMARY KEY (market_id, token_id, bucket_ts)
);
CREATE INDEX idx_mf_bucket ON microstructure_features (bucket_ts DESC);

-- Migration 042
CREATE TABLE wallet_microstructure_signature (
    wallet_address VARCHAR(100) NOT NULL,
    rollup_at TIMESTAMPTZ NOT NULL,
    cancel_to_fill_ratio_30d NUMERIC(8, 4),
    iceberg_score_30d NUMERIC(8, 4),
    spoof_score_30d NUMERIC(8, 4),
    place_to_fill_seconds_p50 NUMERIC(10, 4),
    place_to_fill_seconds_p99 NUMERIC(10, 4),
    n_orders_30d INTEGER,
    n_fills_30d INTEGER,
    PRIMARY KEY (wallet_address, rollup_at)
);
```

---

## 5. New Prometheus metrics (Round 11 contributes ~12)

```
polybot_book_events_received_total{event_type}
polybot_book_events_dropped_total{reason}     # queue_full|invalid|attribution_missing
polybot_book_ws_latency_seconds               # WS msg → our publish
polybot_book_queue_depth                      # gauge
polybot_book_partitions_open                  # gauge of active partitions
polybot_book_partition_rows_total{partition}

polybot_microstructure_features_emitted_total
polybot_iceberg_detections_total
polybot_spoof_detections_total
polybot_ofi_calculations_per_minute           # gauge

polybot_wallet_signatures_updated_total
polybot_wallet_signatures_cardinality         # how many distinct wallets have signatures
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| WS L3 subscription + decode | 0.75 |
| `clob_book_events` schema + partition automation (extends R6 partition maintenance script) | 0.5 |
| Microstructure feature derivers | 1.5 |
| Wallet-signature rollup (nightly) | 0.5 |
| Feature store integration | 0.25 |
| Tests + audit doc | 0.5 |
| **Total** | **~4 weeks** (can run parallel with R9/R10) |

### Dependencies

- Round 6: daemon framework + partition maintenance script
- Round 6: cold tier (microstructure events are obvious cold-tier
  candidates given the volume)
- Round 8: R8 will retrain with the new features once available;
  feeds back as an acceptance criterion (§ 6.A)

### Risk: 2/5

| Risk | Severity | Mitigation |
|---|---|---|
| WS subscription cost / Polymarket rate-limits us | Low | Polymarket WS is unmetered per their docs; if they tighten, we already have multi-key support patterns from R7 |
| 13 GB/day storage explodes the box | Low | Partition + 30d retention bounds it to ~400 GB; volume costs €18/mo |
| Iceberg/spoof detection false positive rate | Medium | Per-wallet calibration; only the AGGREGATE scores feed R8, individual classifications stay internal |
| Wallet attribution gap (no wallet on placement) | Medium | Document the gap; downstream features that need wallet use the on-chain reconciliation path (R6 join) |

### Acceptance criteria

- `polybot_book_events_received_total` > 1M per day in steady state
- `polybot_book_events_dropped_total{reason="queue_full"}` = 0 over
  any 24-hour window
- Microstructure features available with < 60 s lag (place → minute
  rollup)
- R8 classifier validation accuracy improves by ≥ 3 percentage points
  after retraining with microstructure features (the headline
  measurable downstream benefit)
- Cold-tier export of `clob_book_events` completes nightly within the
  batch window

---

## 7. Rollout plan

### Phase 11.A — Subscription + raw capture (weeks 1-2)
1. Deploy `polymarket-book-l3.service`
2. WS subscription, raw event decode, write to clob_book_events
3. Validate volume against expected ~13 GB/day
4. **Gate**: 7 days of clean capture, no queue drops, partitioning works

### Phase 11.B — Microstructure derivation (week 3)
1. Implement iceberg / spoof / OFI / place-to-fill derivers
2. Per-minute rollups to `microstructure_features`
3. Manual sanity-check a few flagged iceberg/spoof patterns against
   the raw event log
4. **Gate**: 3 days of microstructure rollups, manual audit looks
   reasonable

### Phase 11.C — Wallet signature rollup (week 3.5)
1. Nightly batch: derive per-wallet 30-day signatures
2. Backfill 30 days from cold tier
3. **Gate**: signatures table populated for all tier-0/1 wallets

### Phase 11.D — R8 classifier retrain (week 4)
1. Trigger R8 retrain with microstructure features included
2. Validation: out-of-sample accuracy comparison vs pre-R11 model
3. **Gate**: ≥ 3 pp improvement on overall accuracy or on minority
   classes (especially `market_maker` and `info_leak`)

---

## 8. What this round explicitly does NOT do

- **Does NOT use the order-event data for live trading decisions
  directly**. Microstructure features feed R8 (strategy classification)
  and R7 (intent decoding); they don't directly trigger trades.
- **Does NOT do colocation or HFT**. The latency we care about is
  feature-freshness (per-minute), not trade-routing-latency.
- **Does NOT capture trades from venues other than Polymarket CLOB**.
  Cross-market data is Round 12.
- **Does NOT do order-book replay simulation**. The data enables it
  (a research notebook could replay the book), but the simulation
  toolchain isn't part of R11's scope.

---

## 9. The non-obvious gains

1. **Spoof detection becomes a feature for fade signals**. If a wallet
   has a high spoof score, their visible "intent" is misleading — and
   THEIR followers may be misled. The bot can fade the followers (R8
   strategy class `info_leak` may overlap with high-spoof wallets).

2. **The raw event log is operationally invaluable**. Whenever
   something weird happens in the market, the operator can grep the
   raw event log to reconstruct exactly what happened. Forensics
   capability we don't have today.

3. **Iceberg detection identifies institutional flow**. Sophisticated
   traders use icebergs because they don't want their full size
   showing. Knowing where iceberg flow is sitting tells us where
   true demand/supply is — useful for sizing our own entries.

4. **Order flow imbalance is a leading indicator**. OFI predicts
   short-term price moves (decades of equities-research literature
   confirms this). For the mempool-driven R7 pre-fill path, OFI
   at the moment of detection adjusts our pre-signed pool selection.

---

## 10. The single sentence

> Round 11 captures **every order-life event** — placement,
> modification, cancellation, partial fill — derives microstructure
> features (iceberg / spoof / OFI), and feeds them as new dimensions
> into the R8 strategy classifier so it can finally distinguish a
> market-maker from a directional swing trader from a spoofer.
