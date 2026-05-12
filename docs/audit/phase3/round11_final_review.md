# Round 11 — The Microscope: Final Code-Layer Review

> **Branch**: `round-11-microscope`
> **Reviewer**: R11 single-architect+implementer (one-pass)
> **Date**: 2026-05-12
> **Specification reference**: [`docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md`](../../ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md)
> **Risk rating**: 2/5 (data-layer round; no live-trading surface; the
> only live-impact path is the R8 classifier retrain, gated by the
> operator-driven 3pp accuracy gate)

---

## 1. Top-line recommendation

**PASS — code layer complete, awaiting operator-driven gates.**

R11 ships the full code-layer of The Microscope: a CLOB Book L3
firehose subscriber (`src/observer/clob_book_observer.py`) with the
producer/consumer pattern from R3 trade_observer (bounded queue,
oldest-drop backpressure, dual sinks → DB + Redis Stream), a
microstructure feature deriver (`src/microstructure/derivers.py`) with
five detectors (Iceberg / Spoof / OFI / PlaceToFill / CancelToFill), a
per-minute rollup writer (`src/microstructure/rollup.py`), a nightly
per-wallet signature batch (`src/microstructure/wallet_signature.py`),
a deriver daemon (`src/microstructure/daemon.py`), three migrations
(032 partitioned-hourly clob_book_events, 033 microstructure_features,
034 wallet_microstructure_signature), 12 Prometheus metrics, two new
systemd units, an hourly partition rotation script with retention DROP
sweep, all R11 constants registered in `src/config.py` with validators,
and the **R8 strategy-classifier wiring** that consumes the new
features through `feature_store.get_microstructure_features_asof` +
`get_wallet_microstructure_signature_asof`.

**Tests**: 60 new R11 tests + 2 new R8-extension tests; full suite
**1,432 passed**, 9 skipped, 2 xfailed (zero failures). Baseline was
1,370 tests; R11 added 62 tests (matches the diff).

**Headline acceptance criterion**: the R8 classifier slot-wiring code
that consumes microstructure features is **present, tested, and
shape-preserving** (spec § 6 → ≥ 3pp accuracy improvement gate). The
slot SHAPE doesn't change — only the values stop being np.nan when
data exists. The 3pp retrain gate itself is operator-driven (30 days
of feature accumulation + R8 retrain trigger).

**Operator-only gates remain** (spec § 6 / § 7) — explicitly out of
scope for the code pass:

1. **Hetzner volume provisioning** — 500 GB additional volume on
   `polymarket-prod` to fit the ~390 GB of L3 event data + retention
   margin. €18/mo cost, runbook entry in `docs/INFRA.md` (operator
   updates after volume mount).
2. **7-day clean-capture soak** (spec § 7 Phase 11.A) — Polymarket-prod
   runs the `polymarket-book-l3.service` for 7 consecutive days with
   no queue drops, no decode failures, and partition rotation
   confirmed working. The metric to watch is
   `polybot_book_events_dropped_total{reason="queue_full"} == 0` over
   any 24h window.
3. **3-day microstructure soak** (Phase 11.B) — `microstructure_features`
   table populated for 3 days, manual sanity-check of a handful of
   flagged iceberg/spoof patterns against the raw clob_book_events
   log.
4. **Tier-0/1 wallet signature backfill** (Phase 11.C) — the
   `WalletSignatureBatch.run()` is wired but only operates on
   already-captured data; the operator triggers a backfill once the
   30-day clob_book_events window is full.
5. **R8 retrain trigger** (Phase 11.D) — after 30 days of feature
   accumulation, the operator triggers a fresh R8 model fit with the
   E/F microstructure slots populated. The validation gate is ≥ 3 pp
   improvement on overall accuracy or on the minority classes
   (`market_maker`, `info_leak`) — spec § 6.
6. **Partition maintenance cron** — wire the hourly cron entry shown
   in migration 032's docstring + the script docstring:

       30 * * * * cd /opt/polymarket-bot && python -m \
         scripts.maintenance.create_book_events_partitions

7. **Cold-tier export plumbing** — the R6 nightly Parquet exporter
   needs the new `clob_book_events` table added to its config-driven
   table list. R11 doesn't touch that file; the operator wires the
   one-line addition.

---

## 2. What landed

### 2.1 Migrations

| Migration | Lines | Purpose |
|-----------|-------|---------|
| `032_clob_book_events.sql` | 218 | Hourly-partitioned L3 event store with 3 indexes (market+time DESC; wallet+time DESC partial; order_hash partial). 24 hours of initial partitions inline. |
| `033_microstructure_features.sql` | 60 | Per-(market, token, minute) rollup with iceberg / spoof / OFI columns. Single secondary index on bucket_ts for ops queries. |
| `034_wallet_microstructure_signature.sql` | 75 | Per-wallet 30d signature for tier-0/1 wallets. PK (wallet, rollup_at) + secondary index on rollup_at DESC. |

### 2.2 Source modules

| Module | Lines | Purpose |
|--------|-------|---------|
| `src/observer/clob_book_observer.py` | 475 | L3 firehose subscriber + decoder + bounded-queue backpressure + Redis Stream publisher. |
| `src/observer/clob_book_main.py` | 122 | Daemon entry. systemd's `ExecStart=python -m src.observer.clob_book_main`. |
| `src/observer/clob_book_entry.py` | 16 | Thin shim mirror of `src.mempool.__main__`. |
| `src/microstructure/__init__.py` | 47 | Package exports. |
| `src/microstructure/derivers.py` | 526 | Five detectors + composer + bucket-boundary math. |
| `src/microstructure/rollup.py` | 130 | Per-bucket rollup writer with ON CONFLICT DO UPDATE. |
| `src/microstructure/wallet_signature.py` | 261 | Nightly per-wallet 30d signature batch. |
| `src/microstructure/daemon.py` | 191 | Composes deriver pipeline. Reads `book:events:stream`, runs bucket clock, flushes rollups. |
| `src/microstructure/__main__.py` | 18 | `python -m src.microstructure` shim. |
| `scripts/maintenance/create_book_events_partitions.py` | 243 | Hourly forward-roll + retention DROP. Idempotent. |

All files under 500 lines except the deriver (526) — kept together
because the five detectors share helper functions (`_to_float`,
`_event_ts`, `_MAX_TRACKED_KEYS_PER_DETECTOR`). Splitting would have
introduced cross-module coupling worse than the size overage.

### 2.3 Feature store extension

`src/profiler/feature_store.py` gained two functions per spec § 3.3:

* `get_microstructure_features_asof(conn, market_id, token_id, asof_ts, lookback_s=300)`
* `get_wallet_microstructure_signature_asof(conn, wallet, asof_ts, lookback_days=30)`

Both return None when no row qualifies. Both use parameterized async
SQL. Neither touches the existing functions — additive only.

### 2.4 R8 classifier wiring

`src/strategy_classifier/features.py`:

* `_populate_entry_microstructure` now reads the R11 microstructure
  rollup and populates **slot 24** (`e_book_age_ms_at_entry_median`)
  from the microstructure feature-age when present. Slots 19-21
  (microprice/spread/depth) continue to come from R2's
  `orderbook_features_minute`.
* New helper `_populate_wallet_microstructure` reads the wallet
  signature and populates **slot 25** (`e_cancel_to_fill_ratio_30d`)
  + **slot 26** (`e_takes_vs_makes_ratio` derived from
  `n_fills_30d / n_orders_30d`).
* Slots 22 (mom_5m) and 23 (mom_60m) remain np.nan — they need
  candlestick data, not R11 scope.

**Feature-vector SHAPE is preserved exactly.** 42 slots, same order,
same names. PENDING_FEATURE_NAMES set unchanged. The model trained
against the pre-R11 vector still loads; the only behavioural diff is
that the values previously always-nan now carry numbers when the R11
data exists.

### 2.5 Configuration + metrics

`src/config.py` gained 14 R11 constants in a labelled block, with
validators on the load-bearing ones (queue maxsize bounds, retention
days bounds, bucket size bounds, spoof percentile bounds).

`src/monitoring/metrics.py` declared the 12 metrics from spec § 5
inside the existing defensive try-except pattern.

### 2.6 Systemd units

* `infra/systemd/polymarket-book-l3.service` — 500 MB envelope.
* `infra/systemd/polymarket-microstructure.service` — 400 MB envelope.
* `infra/systemd/README.md` — table updated; install commands extended.

---

## 3. Per-component verification

### 3.1 CLOB Book L3 firehose (`clob_book_observer.py`)

* **Decoder**: `decode_ws_message` handles every canonical event type
  + the common aliases. Tested across 10 raw type strings, all five
  canonical outputs. NULL wallet preserved on placement events
  (spec § 3.1).
* **Backpressure**: bounded `deque(maxlen=N)` gives constant-time
  oldest-drop on overflow — the spec's "drop OLDEST not newest"
  contract. Verified in `test_50001st_event_drops_oldest`: pushing
  the (N+1)th event evicts the 1st, queue stays at N, the
  `events_dropped_queue_full` counter increments. Metric label is
  `reason="queue_full"`.
* **Dual sink**: each event lands in BOTH the DB queue and the
  stream queue. Each is bounded independently — a slow DB doesn't
  block Redis Stream publish and vice versa.
* **Wallet attribution caveat**: when `wallet_address` is provided
  on a fill event we honour it; when missing on a placement event
  we preserve the NULL (the spec contract). Downstream readers join
  with trades_observed on (tx_hash, log_index) for attribution.

### 3.2 Microstructure derivers (`derivers.py`)

* **IcebergDetector**: rolling deque per (wallet, market, token,
  price). EWMA-based "typical size" gate at 50%. Min refills
  configurable. Skips events with no wallet attribution (so
  placement-only events without fills don't false-positive).
* **SpoofDetector**: per-order in-flight tracker keyed by
  order_hash; on cancel within 5 s with zero fills AND size
  ≥ 95th percentile, flagged. Reservoir-based percentile (256
  samples per market-token).
* **OrderFlowImbalanceCalculator**: per (market, token) rolling 5 s
  signed-size deque; on each new event computes the rolling sum
  and adds it to the bucket. Per-minute summary returns mean / max
  / min / std (spec § 3.2.C).
* **PlaceToFillTimingTracker**: per-order in-flight keyed by
  order_hash; on fill, records elapsed (sec) to a per-wallet deque
  capped at 1000 samples. Percentile lookup is O(N log N) per
  query; called once per nightly batch.
* **CancelToFillRatioTracker**: per-wallet 30 min rolling deque of
  (ts, kind). Pure-cancel wallets get a finite sentinel
  (= n_cancels) rather than +inf so the DB column stays numeric.

All detectors bound their per-key working set with
`_MAX_TRACKED_KEYS_PER_DETECTOR = 50_000` so a long-running daemon
never accumulates unbounded state.

### 3.3 Rollup writer (`rollup.py`)

* Single `executemany` per flush. ON CONFLICT DO UPDATE → idempotent.
* Empty snapshot → no SQL fired (avoids zero-row executemany).
* OFI summary computed inline (mean / max / min / std). Single-sample
  buckets correctly yield std = 0.

### 3.4 Wallet signature batch (`wallet_signature.py`)

* Tier filter passed to wallet_universe query as `int[]`. Default
  `(0, 1)` per spec § 3.2.E.
* Per-wallet single-roundtrip SQL combining COUNT FILTER aggregates
  + percentile_cont on the place-to-fill join — one fetchrow per
  wallet.
* min_orders gate: wallets below the floor are silently skipped
  (no upsert). Default 50 events over 30 days.
* Idempotent ON CONFLICT DO UPDATE on (wallet, rollup_at).

### 3.5 Deriver daemon (`daemon.py`)

* Reads `book:events:stream` via XREADGROUP with consumer-group
  `microstructure_deriver`. BUSYGROUP on group-create is swallowed.
* Bucket clock: `run_once` flushes the rollup when crossing a
  bucket boundary. On `stop()` flushes any pending partial bucket
  so the last window isn't lost.
* Each stream entry is ACKed after decode, regardless of whether
  the decode succeeded — failed decodes don't backlog the consumer.

### 3.6 R8 wiring (`features.py`)

* New helper `_populate_wallet_microstructure` runs alongside the
  existing `_populate_entry_microstructure`.
* Slot 24 (`e_book_age_ms_at_entry_median`) populated from
  orderbook-feature age (NOT just R11 — we kept it backwards-
  compatible so a fresh deploy with the R11 daemons OFF still
  populates this slot if R2 orderbook_features_minute has data).
* Slot 25 (`e_cancel_to_fill_ratio_30d`) populated from the wallet
  signature when present.
* Slot 26 (`e_takes_vs_makes_ratio`) computed as
  `n_fills_30d / n_orders_30d` from the wallet signature.
* Test coverage:
  * `test_microstructure_features_missing_when_no_orderbook` —
    all E slots remain np.nan when upstream sources are None.
  * `test_microstructure_wallet_signature_populates_slots` —
    when wallet signature returns a populated row, slots 25/26
    carry real numbers; slot SHAPE preserved.
  * `test_microstructure_per_token_rollup_populates_book_age` —
    when orderbook returns a row with `feature_age_s`, slot 24
    carries the ms value.

---

## 4. Prometheus metrics inventory (12 — spec § 5)

| Metric | Type | Labels | Owner |
|--------|------|--------|-------|
| `polybot_book_events_received_total` | Counter | event_type | clob_book_observer |
| `polybot_book_events_dropped_total` | Counter | reason | clob_book_observer |
| `polybot_book_ws_latency_seconds` | Histogram | — | clob_book_observer |
| `polybot_book_queue_depth` | Gauge | — | clob_book_observer |
| `polybot_book_partitions_open` | Gauge | — | partition maintainer |
| `polybot_book_partition_rows_total` | Gauge | partition | partition maintainer |
| `polybot_microstructure_features_emitted_total` | Counter | — | rollup |
| `polybot_iceberg_detections_total` | Counter | — | derivers.IcebergDetector |
| `polybot_spoof_detections_total` | Counter | — | derivers.SpoofDetector |
| `polybot_ofi_calculations_per_minute` | Gauge | — | derivers.OrderFlowImbalanceCalculator |
| `polybot_wallet_signatures_updated_total` | Counter | — | wallet_signature |
| `polybot_wallet_signatures_cardinality` | Gauge | — | wallet_signature |

All 12 declared inside the existing defensive `try/except Exception:
pass` pattern so pytest hot-reload doesn't trip the prometheus
default registry.

---

## 5. Decisions reviewers need to know

### 5.1 Partition maintenance handled by a NEW script, not an extension

R6 ships `scripts/maintenance/create_trades_partitions.py` which is
hard-coded to one table (`trades_observed`) and one granularity
(monthly). Extending it to handle hourly partitions + retention
DROP would have widened its blast radius without buying anything.

I shipped `scripts/maintenance/create_book_events_partitions.py` as a
companion. Both scripts share the design (asyncpg + idempotent DDL +
`IF NOT EXISTS`); the R6 monthly cadence and the R11 hourly cadence
are different enough that a unified script would have been more
complex, not less. The migration 032 header references the new
script explicitly.

### 5.2 Wallet=NULL semantics on placement events

Per spec § 3.1, Polymarket's WS does NOT include wallet_address on
placement / modification / cancellation — only on fills. The R11
codebase honours this: the decoder reads `wallet_address` /
`wallet` / `owner` / `maker` fields when present (the `maker` field
is what the fill event carries), and falls back to NULL otherwise.

The DB column allows NULL. The partial index
`idx_cbe_wallet_time WHERE wallet_address IS NOT NULL` skips the 4
of 5 NULL event types so the index size stays bounded.

Downstream features that need wallet attribution per event (e.g.
the iceberg detector, which buckets by wallet) silently skip
events without wallet attribution — they're useless for the
detector. The wallet signature batch fixes this by joining
clob_book_events to trades_observed at batch time via the R6
cross-source reconciliation path.

### 5.3 R8 slot wiring — shape stability

The R8 feature vector is 42 slots. R11 changes none of them.
Slots 25 (cancel_to_fill) and 26 (takes_vs_makes) get values
when the wallet signature exists; otherwise they stay nan, just
like pre-R11. Slot 24 (book_age) was nan; now it carries a real
value when orderbook_features_minute has a hit (which is R2 data,
not R11 — we widened the population logic but didn't make it
R11-dependent).

The LightGBM model trained against the pre-R11 vector therefore
loads without retraining. The 3pp acceptance gate (spec § 6)
requires the operator to TRIGGER a retrain after 30 days of
feature accumulation — that's outside R11's scope and tracked as
an operator-only gate above.

### 5.4 IcebergDetector min_refills=3 default

Spec § 3.2.A says "N orders" without specifying N. I picked 3 (one
original + two refills) as the architect's default in `config.py`
under `MICROSTRUCTURE_ICEBERG_MIN_REFILLS`. Below 3 the
false-positive rate from natural latency-induced retries
dominates; above 3 the detector misses the short-lived iceberg
patterns common in low-liquidity Polymarket markets.

### 5.5 Spoof score and iceberg score proxies in the wallet signature

The wallet_microstructure_signature stores `iceberg_score_30d` and
`spoof_score_30d`. R11's streaming derivers emit per-(market,
token) aggregates into `microstructure_features`. The per-wallet
batch can't easily aggregate those back to wallet level because
the streaming detectors do their work on `wallet+price`
(iceberg) or `order_hash` (spoof) — not on (market, token).

For R11 I use a **proxy**: the wallet-cancel-density. A wallet
that cancels 80% of its orders is doing SOMETHING (might be
iceberg refilling, might be spoofing); the streaming detector
distinguishes them at per-(market, token) level for the R8
classifier, while the per-wallet signature carries the coarse
behavioural signal.

If the R8 retrain shows the proxy is insufficient (e.g. accuracy
gains are < 3 pp), a follow-up could extend the streaming
detectors to also push per-wallet counters into a third Redis
key the batch reads. That's a 2-day extension when the data
proves the need.

---

## 6. Migrations validated

```
docs/migrations/032_clob_book_events.sql       — clob_book_events (partitioned, 24h inline)
docs/migrations/033_microstructure_features.sql — microstructure_features (per-minute rollup)
docs/migrations/034_wallet_microstructure_signature.sql — per-wallet 30d signature
```

All three open with `BEGIN` and close with `COMMIT`. All three use
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` so
re-running is a no-op. The partition rotation script applies the
same `IF NOT EXISTS` pattern to forward-rolled partitions.

---

## 7. Confirmation

Working tree is dirty (R11 changes uncommitted). Ready for the
orchestrator to commit `round-11-microscope` and tag `v0.11.0`
once they accept this report.
