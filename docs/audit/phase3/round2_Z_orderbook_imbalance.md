# Phase 3 Round 2 Agent Z — Order-book imbalance feature pipeline

Closes the "highest-ROI new data source" recommendation in
`docs/audit/05_ml_pipeline.md` (summary). The raw feed already exists
(`book_quality_snapshots`, migration 005); this work adds the rollup,
the AS-OF feature read, and the metrics + engine wiring.

---

## 1. Pipeline shape

```
       Polymarket CLOB WebSocket (book channel)
                       │
                       ▼
     ┌────────────────────────────────────────────────┐
     │  trade_observer._record_book_metrics           │
     │  → _persist_book_quality_snapshot (existing)   │
     │  one INSERT per WS book update, many/sec/token │
     └────────────────────────────────────────────────┘
                       │
                       ▼
       ┌──────────────────────────────────────┐
       │  book_quality_snapshots  (raw table) │
       │  -- migration 005, no UNIQUE         │
       └──────────────────────────────────────┘
                       │
                       │   every 60 s,
                       │   lookback 70 s
                       ▼
     ┌────────────────────────────────────────────────┐
     │  OrderBookObserver (Agent Z, this round)       │
     │  src/observer/orderbook_observer.py            │
     │  - fetch raw snapshots in window               │
     │  - aggregate per (market_id, token_id, minute) │
     │  - ON CONFLICT DO UPDATE                       │
     └────────────────────────────────────────────────┘
                       │
                       ▼
       ┌──────────────────────────────────────────┐
       │  orderbook_features_minute  (migration   │
       │  018, PK (market_id, token_id, bucket_ts)│
       └──────────────────────────────────────────┘
                       │
                       │   point-in-time read
                       │   WHERE bucket_ts <= asof
                       │     AND bucket_ts >= asof - 300s
                       ▼
     ┌────────────────────────────────────────────────┐
     │  feature_store.get_orderbook_features_asof()   │
     │  src/profiler/feature_store.py                 │
     └────────────────────────────────────────────────┘
                       │
                       ▼
            error_model._build_features  (← Agent Y wires)
            confidence_engine FADE confidence
```

The raw writer is owned by `trade_observer` exclusively (see
`src/observer/trade_observer.py:1315` `_persist_book_quality_snapshot`).
Agent Z does NOT subscribe to the WS book channel; doing so would
double the DB write load. The orderbook observer is read-only on the
source table.

---

## 2. Feature definitions (math)

For one raw snapshot:

```
depth_imbalance = (bid_size_at_best - ask_size_at_best)
                  / (bid_size_at_best + ask_size_at_best)      ∈ [-1, +1]

spread_bps      = (best_ask - best_bid) / midprice * 10_000     bps

microprice      = (best_bid * ask_size + best_ask * bid_size)
                  / (bid_size + ask_size)                       price units

microprice_deviation = | microprice - midprice |               price units
```

**Microprice intuition** — the microprice is a depth-weighted midprice,
weighted toward the THIN side of the book. A book with `bid_size=100`
and `ask_size=10` has microprice ≈ `best_ask` because lifting the
offer is "cheap" (small queue to clear); the microprice predicts where
the next trade is most likely to print. The deviation
`|microprice - midprice|` is the order-flow pressure signal.

**Per-minute rollup** (one row per (market_id, token_id, minute)):

```
depth_imbalance_mean      = arithmetic mean of per-snapshot imbalances
depth_imbalance_max       = signed value of the snapshot whose |imbalance|
                            was largest in the minute
                            (preserves sign so the error model can tell
                             "bid-heavy" from "ask-heavy" extremes)
spread_bps_mean           = arithmetic mean
spread_bps_max            = max over the minute
microprice_mean           = arithmetic mean
microprice_deviation_mean = arithmetic mean of |microprice - midprice|
n_snapshots               = raw rows rolled up (incl. unusable ones)
```

Edge cases (per-snapshot features set to `None`, snapshot still counted
in `n_snapshots`):

* Crossed book (`best_bid >= best_ask`) — typically a stale tick.
* One-sided book (`bids` or `asks` empty).
* Zero depth at best (`bid_size + ask_size == 0`).

Means / maxes are computed over the non-None subset, so a minute with
59 healthy snapshots + 1 crossed tick still reports the 59-snapshot
mean correctly.

---

## 3. Rollup semantics

* **Cadence**: `ORDERBOOK_ROLLUP_INTERVAL_S = 60` (env-overridable).
* **Lookback**: `ORDERBOOK_ROLLUP_LOOKBACK_S = 70` — 10 s overlap so a
  snapshot at the minute boundary always lands in one of the runs.
  The PK + `ON CONFLICT DO UPDATE` makes the overlap safe.
* **Idempotency**: re-running the rollup over the same wall-clock
  window produces the same row count and the same column values
  (verified by `test_idempotent_rerun_same_window`).
* **Best effort**: if the rollup misses a minute (DB hiccup, crash
  during the 60 s sleep), the minute is missed. Backfill is an explicit
  operator action via `scripts/orderbook_backfill.py` (NOT in scope for
  this round). The audit's MG-4 ("real-time gap, no backfill") is
  honoured: the live path never tries to be smart about lost data.
* **Watchdog supervision**: registered with the engine watchdog as
  `orderbook_observer`. Heartbeat key: `heartbeat:orderbook_observer`.
  Standard linear-backoff restart, max 3 retries before fatal.

---

## 4. Integration notes for the error model

Agent Y owns `error_model._build_features` for this round; Agent Z
provides the read function only. The integration call site looks like:

```python
# Inside error_model._build_features, after the market lookup:
ob = await get_orderbook_features_asof(
    conn,
    token_id=position.token_id,
    asof_ts=position.open_time,   # AS-OF, NOT now
    lookback_s=300,
)

if ob is not None:
    features.extend([
        float(ob["depth_imbalance_mean"]   or 0.0),
        float(ob["spread_bps_mean"]        or 0.0),
        float(ob["microprice_deviation_mean"] or 0.0),
    ])
    features_present = True
else:
    features.extend([0.0, 0.0, 0.0])
    features_present = False   # one-hot flag for "no signal"
```

**Critical**: the call MUST pass `position.open_time`, not
`datetime.utcnow()`. Reading AS-OF-NOW for historical positions
re-introduces the exact train/serve skew the audit's MG-3 finding
flagged for `liquidity_score`. The function returns `None` when no
rollup row exists within the lookback — the error model treats that
as "no orderbook signal" and lets the existing feature defaults stand.

**Feature vector growth**: today's vector is 18 floats
(`error_model.py:211`); this adds 3 numeric features + 1 presence flag
= 22 floats. Phase 2 BayesianRidge handles the extra dimensions
without retraining the architecture; Phase 3 LightGBM trees split on
the new features automatically once enough resolved positions
accumulate that include them.

**FADE confidence consumer**: the confidence engine's FADE path
(`confidence_engine.py:142`-ish) currently gates on
`error_model.confidence >= FADE_MIN_CONFIDENCE`. The new microprice
deviation signal lets us additionally require
`microprice_deviation_mean < threshold_bps` for the FADE leg — high
order-flow asymmetry means the market is already pricing the leader's
exit. Agent Y is the one who'll thread that through; not in scope here.

---

## 5. Surface area summary

| Path                                                             | New / Changed | Owner                |
|------------------------------------------------------------------|---------------|----------------------|
| `docs/migrations/018_orderbook_features_minute.sql`              | new           | Z                    |
| `src/observer/orderbook_observer.py`                             | new           | Z                    |
| `src/profiler/feature_store.py` (+ `get_orderbook_features_asof`) | extended      | Z (Y owns the file)  |
| `src/monitoring/metrics.py` (+ 4 metrics)                        | extended      | Z                    |
| `src/engine/main.py` (+ observer wiring)                         | extended      | Z                    |
| `tests/test_observer/test_orderbook_observer.py`                 | new           | Z                    |
| `tests/test_profiler/test_feature_store_orderbook.py`            | new           | Z                    |

---

## 6. Expected accuracy lift

The audit calls this the "cheapest single change with the highest
expected accuracy lift" — the rationale:

1. **The data is already arriving.** `book_quality_snapshots` is
   ingested by every WS book update; the only marginal cost is the
   per-minute aggregator. No new external API, no new rate-limit
   exposure.
2. **The features encode private-information arrival.** A leader's
   directional trade is followed by depth imbalance on the same side
   within seconds — that lift in `depth_imbalance_mean` is the
   short-horizon residual signal the error model currently can't see
   (it only knows category / liquidity / time-of-day).
3. **It plugs into both the FOLLOW and FADE confidence paths.** Most
   audit recommendations help one of the two; this one is symmetric.

Expected lift on Brier score (uncalibrated estimate, based on the
analogous OB-imbalance lift in equities microstructure literature):
~0.01-0.02 on Phase 3 LightGBM, growing as the rollup history
accumulates. Calibration health is monitored via the
`polybot_orderbook_features_lookup_total{result}` series
(hit / stale / miss); a hit-rate < 50 % is a signal that the rollup
cadence isn't keeping up with the demand pattern.
