# Phase 3 Round 2 Agent Y — Point-in-time feature store

**Audit reference**: `docs/audit/05_ml_pipeline.md` MG-3 §3.1 (training
leakage) and the Phase 0 Task C groundwork in
`docs/audit/phase0/C_liquidity.md`.

**Owner**: Round 2 Agent Y.

**Status**: Shipped. Migration 016, `feature_store.py`, dual-write in
`sync_markets`, asof read in `error_model._fetch_training_data`,
operator-gated backfill, retention policy, 3 Prometheus metrics, 9
new tests.

---

## 1. The leakage we just closed

Phase 0 Task C left this comment block in `error_model.py`:

> `error_model._fetch_training_data` reads `markets.liquidity_score`
> AS-OF-NOW for historical positions. A market that became liquid two
> weeks AFTER `pr.open_time` will look liquid in training but was
> illiquid at decision time.

Task C added `markets.liquidity_score_updated_at` and
`liquidity_score_source` as groundwork. Round 2 Agent Y closes the
loop: the training pipeline now reads a *point-in-time-correct*
`liquidity_score` from a new append-only table, instead of the
AS-OF-NOW value of `markets.liquidity_score`.

---

## 2. Schema (`docs/migrations/016_market_features_history.sql`)

```sql
CREATE TABLE market_features_history (
    id              BIGSERIAL PRIMARY KEY,
    market_id       VARCHAR(100) NOT NULL,
    captured_at     TIMESTAMPTZ  NOT NULL,
    liquidity_score NUMERIC(10,4),
    volume_24h      NUMERIC(20,2),
    category        VARCHAR(50),
    fee_rate_pct    NUMERIC(5,4),
    source          VARCHAR(32),    -- mirrors markets.liquidity_score_source
    extra_json      JSONB           -- future-feature slot
);
CREATE INDEX idx_mfh_market_time ON market_features_history (market_id, captured_at DESC);
```

**Why APPEND-ONLY**: every `sync_markets` refresh INSERTs a fresh row.
No UPSERT, no UPDATE — the table is the time series. Volume estimate:
~50 markets/day × 1–2 refreshes = ~100 rows/day = 36k rows/year.
Trivial.

**Why `extra_json`**: Agent Z (Round 2 OB imbalance) can add columns
via a later migration without breaking the existing read path because
the SELECT consumer is dict-based. Z is told NOT to touch
`error_model.py` directly — they extend this schema or add their own
`feature_store` entries.

---

## 3. Read API (`src/profiler/feature_store.py`)

Two helpers; both take an explicit `asof_ts` so the training pipeline
never reads "now":

* `get_market_features_asof(conn, market_id, asof_ts) → dict | None`
  — single-row lookup. Used by hot-path readers that want one
  feature at one point in time.

* `get_market_features_asof_batch(conn, queries) → dict[(market_id, asof_ts), dict | None]`
  — batched LATERAL JOIN over the input list. One round-trip
  regardless of N. Used by `error_model._fetch_training_data` for
  thousands of historical positions.

The batched SQL shape:

```sql
WITH inputs(idx, market_id, asof) AS (
    SELECT * FROM UNNEST($1::int[], $2::text[], $3::timestamptz[])
)
SELECT i.idx, i.market_id AS in_market_id, i.asof AS in_asof, h.*
FROM inputs i
LEFT JOIN LATERAL (
    SELECT * FROM market_features_history
    WHERE market_id = i.market_id AND captured_at <= i.asof
    ORDER BY captured_at DESC LIMIT 1
) h ON TRUE
```

`idx_mfh_market_time` (DESC) gives the planner an index-only LIMIT 1
descent inside the LATERAL.

---

## 4. Write path: `sync_markets` dual-writes

`src/registry/leader_registry.py:sync_markets` now performs TWO
execute() calls per market: the existing `markets` UPSERT and a new
APPEND-ONLY `market_features_history` INSERT carrying the same
values + the source tag from Phase 0 Task C. The history INSERT is
wrapped in its own try/except — a failure logs-and-continues so the
main UPSERT is never aborted by a write to the history table.

---

## 5. Read path: `error_model._fetch_training_data` migration

The `# LEAKAGE:` marker Phase 0 placed inside the SELECT is updated
to describe the new behavior. The Python loop is now:

1. Build `asof_queries = [(market_id, pr.open_time) for r in rows]`.
2. Single call to `get_market_features_asof_batch(conn, asof_queries)`.
3. Per position: if the LATERAL returned a row with `liquidity_score
   IS NOT NULL`, use it. Otherwise fall back to the live
   `markets.liquidity_score` (already on `row["liquidity_score"]`)
   and increment `fallback_live_count`.
4. After the loop, `record_fallback_live(fallback_live_count)` bumps
   `polybot_feature_store_lookups_total{result="fallback_live"}`.

This preserves every training sample (no NULL-ing the feature) while
giving us a metric to watch the fallback rate trend down as the
history accumulates.

---

## 6. Fallback rate baseline

* Immediately after deploy: 100% fallback (history table is empty).
* After ~24h: every active market has at least one history row
  stamped by `sync_markets`. Any position with `open_time` between
  deploy and that first refresh continues to fall back.
* After 90 days: phase-2 training window (`PHASE2_LOOKBACK_DAYS=90`)
  is fully covered. Fallback rate for phase-2 retrains should fall
  to near-zero on new positions.
* Phase-3 training (5000 samples, no time cutoff) keeps a long tail
  of legacy positions falling back. The operator-gated backfill
  (§7) gives a coarse seed for those.

Watch:
`rate(polybot_feature_store_lookups_total{result="fallback_live"}[1h])`
vs the corresponding `asof_hit` rate.

---

## 7. Operator-gated backfill

`scripts/backfill_market_features_history.py`. Walks every existing
`markets` row, inserts a single seed `market_features_history` row
dated `liquidity_score_updated_at OR updated_at`. Coarse but better
than 100% fallback for legacy phase-3 retraining.

Usage:

```bash
python scripts/backfill_market_features_history.py --dry-run
python scripts/backfill_market_features_history.py --yes      # apply
```

Refuses to run without `--yes`. Idempotency: the history table has no
UNIQUE constraint by design (it's time series), but the script warns
if the table is non-empty before applying.

---

## 8. Retention

Added to `scripts/batch_runner.py` `RETENTION_POLICIES`:

```python
RetentionPolicy("market_features_history", "captured_at", 540),
```

Default 540d = 18 months. Overridable via
`RETENTION_MARKET_FEATURES_HISTORY_DAYS`. Gated by the existing
`RETENTION_ENABLED` switch.

---

## 9. Prometheus instrumentation

Added to `src/monitoring/metrics.py`:

| Metric | Type | Labels | What it tells you |
|---|---|---|---|
| `polybot_feature_store_lookups_total` | Counter | `table`, `result` (`asof_hit` / `fallback_live` / `miss`) | Lookup outcome — fallback rate is the headline number |
| `polybot_feature_store_batch_size` | Histogram | — | (market_id, asof) tuples per batched lookup — proves N+1 avoidance at a glance |
| `polybot_feature_store_lookup_latency_seconds` | Histogram | `batch_size_bucket` | Latency, bucketed so single-row hot-path reads and 5k-row training reads don't share a histogram |

---

## 10. Tests

New: `tests/test_profiler/test_feature_store.py` (8 tests) — single +
batch read semantics, empty input fast-path, dual-write contract,
history-write-failure-does-not-abort, round-trip ordering.

New: `tests/test_profiler/test_error_model_asof_features.py` (3
tests) — asof value wins over live, fallback path bumps metric,
empty history table = 100% fallback.

Updated: `tests/test_registry/test_leader_registry.py` — the
TestSyncMarkets tests now look at the MARKETS-specific execute() call
rather than the LAST execute() call (which is now the history INSERT),
via the new `_markets_upsert_args` helper.

Result: 62 passes in the targeted test slice. The 5 pre-existing
failures in `test_behavior_profiler.py` and
`TestEnrichLeaders::test_skips_wallet_on_none_response` are unrelated
to this round (verified via `git stash` + re-run).

---

## 11. Follow-ups

* **Add `volume_24h`, `category`, `fee_rate_pct` to `_build_features`
  as asof features**. Right now only `liquidity_score` is consumed
  from the asof read; the other columns are written but unread. The
  audit's §4.1 feature-store proposal lists more columns to migrate.
* **Drop the 24h staleness gate in `sync_markets` to 1h for active
  markets** (audit MG-3 fix-c). Out of scope for this round; needs
  the per-agent budgeting from Phase 3 Task B first.
* **Per-trade-arrival enrichment in `trade_observer.py:1297`** still
  writes via the legacy path. That's a hot-path concern and would
  need its own rate-limit handling. `sync_markets` remains the
  authoritative history-write site.
* **Backtest harness parity** (audit §4.4). The asof read makes
  offline reconstruction point-in-time correct for liquidity, but
  the live `_build_trade_context` still reads
  `markets.liquidity_score` AS-OF-NOW. Wire the same `feature_store`
  helper into `confidence_engine._build_trade_context` so live and
  offline match.

---

## 12. Files touched

```
docs/migrations/016_market_features_history.sql              (new)
docs/audit/phase3/round2_Y_feature_store.md                  (this report)
src/profiler/feature_store.py                                 (new)
src/profiler/error_model.py                                   (LEAKAGE comment closed + asof read)
src/registry/leader_registry.py                               (dual-write in sync_markets)
src/monitoring/metrics.py                                     (+3 metrics)
scripts/batch_runner.py                                       (retention policy)
scripts/backfill_market_features_history.py                   (new, operator-gated)
tests/test_profiler/test_feature_store.py                     (new, 8 tests)
tests/test_profiler/test_error_model_asof_features.py         (new, 3 tests)
tests/test_registry/test_leader_registry.py                   (TestSyncMarkets updated)
```
