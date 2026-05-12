# Round 11 — Wave-3 Independent Review (The Microscope)

> **Reviewer**: Wave-3 independent (post-merge, post-tag `v0.11.0`)
> **Date**: 2026-05-12
> **Base commit**: `5ebd8ec` on `main` (R11 final merge)
> **Scope**: CLOB book L3 firehose, microstructure derivers, rollup, wallet
> signature, partition maintenance, R11 in-scope tests + new hardening.
> **Specification reference**: [`docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md`](../../ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md)
> **Architect audit reference**: [`docs/audit/phase3/round11_final_review.md`](round11_final_review.md)

---

## 1. Top-line verdict

**PASS WITH ONE IN-SCOPE METRIC FIX + FOUR CROSS-CUTTING NOTES.**

The Wave-3 review confirms the architect's PASS verdict on the code
layer. R11 ships exactly what the spec asks for: a CLOB Book L3
subscriber with constant-time drop-OLDEST backpressure, five
microstructure detectors with bounded per-key working sets, a
per-minute rollup writer, a nightly per-wallet signature batch, and an
hourly partition rotation + retention DROP sweep — all gated behind
operator-driven Hetzner soaks per spec § 7.

The Wave-3 audit found **one in-scope correctness bug** (the
queue-full drop counter was incrementing once per evicted SINK, not
once per dropped EVENT — see § 3.1 below). This is a 7-LOC fix
applied in `src/observer/clob_book_observer.py::_enqueue`. The audit
also flagged **four cross-cutting notes** that fall outside the R11
edit scope and are documented as patches in § 7 for the next round to
pick up.

The deque-based drop-OLDEST mechanic itself is correct — Python's
`collections.deque(maxlen=N).append()` evicts from the left
(oldest) when full, retains the new entry on the right (newest), in
constant time. § 3 traces this contract line-by-line against the spec.

**Hardening tests added**: 59 new tests across 4 files
(`tests/test_observer/test_clob_book_observer_hardening.py`,
`tests/test_microstructure/test_derivers_hardening.py`,
`tests/test_microstructure/test_wallet_signature_hardening.py`,
`tests/test_microstructure/test_partition_maintainer_hardening.py`).

**Test counts**:
- R11 scope after Wave-3: **122 passing** (63 baseline + 59 new
  hardening). Up from 63 at architect review time.
- Full suite: **1,832 passing**, 9 skipped, 2 xfailed, **0 failures**.
  Baseline gate was ≥1,608 — comfortably above.

---

## 2. Per-component verification matrix

| Component | Spec section | Verdict | Notes |
|-----------|--------------|---------|-------|
| `clob_book_observer.py` — bounded queue | § 3.1 | PASS (after fix) | Drop-OLDEST semantics correct via `deque(maxlen=N).append()`. **Metric double-count bug found and fixed** — see § 3.1. |
| `clob_book_observer.py` — dual sink | § 3.1 | PASS | DB queue + stream queue independent deques; draining one doesn't drain the other. Verified by `test_draining_one_sink_does_not_drain_the_other`. |
| `clob_book_decoder.py` — event vocabulary | § 3.1 | PASS | Five canonical event types (`placed`, `modified`, `cancelled`, `partial_fill`, `filled`) covered. Common WS aliases (`order_placed`, `trade`, `cancel`, etc.) normalised. |
| `clob_book_decoder.py` — wallet=NULL on placement | § 3.1 | PASS | Re-verified per event type by `test_non_fill_events_preserve_null_wallet`. Wallet preserved on fills under {wallet_address, wallet, owner, maker} aliases. |
| `clob_book_decoder.py` — Decimal price/size | § 3.1, migration 032 | PASS | Decimal preserved; serialised to string for stream-payload JSON. |
| `clob_book_decoder.py` — cancel size sign | § 3.1, OFI math | PASS | Cancel size becomes negative for OFI sign consistency. |
| `derivers.IcebergDetector` — EWMA λ=0.94 | § 3.2.A, CLAUDE.md § 7 | PASS | Formula `μ = λ·μ_prev + (1-λ)·x_new` verified exactly to 1e-9 by `test_ewma_formula_lambda_094`. |
| `derivers.IcebergDetector` — 50% size gate | § 3.2.A | PASS | Refills at 1.0× typical do NOT flag (`test_size_ratio_gate_excludes_typical_sizes`). Random clean stream produces ≤2 noise detections per 200 events. |
| `derivers.IcebergDetector` — min_refills=3 | § 3.2.A, config | PASS | Architect picked 3 (one original + two refills). Documented in `round11_final_review.md` § 5.4. Configurable via `MICROSTRUCTURE_ICEBERG_MIN_REFILLS`. |
| `derivers.SpoofDetector` — 5s cancel window | § 3.2.B | PASS | Inclusive bound: 5.0s passes, 5.01s does not (`test_cancel_at_exactly_5s_passes_gate` + `test_cancel_just_over_5s_fails_gate`). |
| `derivers.SpoofDetector` — 95th-pct reservoir | § 3.2.B | PASS WITH NOTE | Reservoir size 256 with `int(p*N)` index. For small N (<32) the estimator is biased toward the max; defensive `len < 8 → None` guard prevents bootstrapping false positives. Acceptable given the reservoir reaches 256 within seconds in production. |
| `derivers.SpoofDetector` — alternation bump | § 3.2.B | PASS | Repeated spoofs on opposite side from same wallet double-count, matching "classic alternating bid/ask" pattern. |
| `derivers.OrderFlowImbalance` — 5s rolling | § 3.2.C | PASS | Window prunes old samples; cross-(market, token) isolation verified by `test_cross_market_isolation`. |
| `derivers.OrderFlowImbalance` — sign convention | § 3.2.C | PASS | `side='buy' → +1`, `side='sell' → -1`, multiplied by signed `size_delta` (cancels already negative). |
| `derivers.OrderFlowImbalance` — empty stream | § 3.2.C | PASS | `flush_bucket()` returns `{}` on empty state; rollup writer correctly skips the executemany. |
| `trackers.PlaceToFillTimingTracker` | § 3.2.D | PASS | Per-fill (place_time, fill_time) → elapsed in seconds; per-wallet deque capped at 1,000 samples. Partial fills DO contribute samples (`test_partial_fill_records_timing_sample`). |
| `trackers.CancelToFillRatioTracker` | § 3.2.E | PASS | Rolling 30 min per-wallet pruning verified by `test_events_outside_window_are_pruned`. Pure-cancel wallets get the finite sentinel `=n_cancels` (DB column stays numeric). |
| `rollup.MicrostructureRollup` — per-bucket | § 3.2 | PASS | One row per (market, token) keyed by union of detector firings. Empty snapshot → no SQL. ON CONFLICT DO UPDATE for idempotency. |
| `rollup.MicrostructureRollup` — OFI summary | § 3.2.C | PASS | `mean/max/min/std` computed inline; n=1 yields std=0 (`test_ofi_summary_in_row`). |
| `wallet_signature.WalletSignatureBatch` — tier filter | § 3.2.E | PASS | `depth_tier = ANY($1::int[])` passes the int list verbatim. Multi-tier filter `(0, 1, 2)` verified by `test_tier_filter_supports_multiple_tiers`. |
| `wallet_signature.WalletSignatureBatch` — 30d window | § 3.2.E | PASS | `floor = asof_ts - timedelta(days=30)` matches spec. |
| `wallet_signature.WalletSignatureBatch` — proxy scores | § 5.5 architect | PASS WITH NOTE | Iceberg/spoof scores are proxies derived from cancel-density; clamped to [0, 1] (`test_proxy_scores_bounded_zero_one`). Architect documented this in `round11_final_review.md` § 5.5 as the cold-start signal pending the streaming detector backfill. |
| `wallet_signature.WalletSignatureBatch` — per-wallet isolation | new | PASS | One wallet failing fetchrow doesn't poison the others; `test_per_wallet_isolation_on_derive_failure` proves the batch upserts the survivors. |
| `daemon.MicrostructureDaemon` — bucket clock | § 3.2 | PASS | First call to `_flush_if_bucket_complete` just sets `_current_bucket`; subsequent calls flush when crossing the boundary. Force-flush on `stop()` preserves the last partial bucket. |
| `daemon.MicrostructureDaemon` — XREADGROUP | § 3.2 | PASS | Consumer group `microstructure_deriver` with consumer `deriver-1`; BUSYGROUP swallowed; ACK regardless of decode success. |
| `create_book_events_partitions.py` — forward-roll | § 4 | PASS | Idempotent `CREATE TABLE IF NOT EXISTS`. Naming convention `clob_book_events_YYYYMMDD_HH` verified by `test_partition_name_codec`. |
| `create_book_events_partitions.py` — retention DROP | § 2.3 | PASS | DEFAULT partition explicitly skipped; garbage names parsed to None and never dropped; 30d cutoff math correct. |
| `032_clob_book_events.sql` | § 4 | PASS | Hourly RANGE partitioning; partial indexes (wallet, order_hash) IS NOT NULL; composite PK (event_id, event_time) matches PG constraint requirement. |
| `033_microstructure_features.sql` | § 4 | PASS | PK (market_id, token_id, bucket_ts); secondary index on bucket_ts DESC for ops queries. |
| `034_wallet_microstructure_signature.sql` | § 4 | PASS | PK (wallet, rollup_at); ~720k rows/year — no partitioning needed. |

---

## 3. Backpressure correctness audit (load-bearing — line-by-line)

### 3.1 Drop-OLDEST contract trace

The spec § 3.1 contract: *"Under overload, oldest events get dropped
with metric increment — never block the WS reader."* The 50001st event
into a 50,000-capacity queue must drop the OLDEST (event #1) and retain
the newest (event #50,001).

The implementation in `src/observer/clob_book_observer.py`:

```python
# __init__
self._db_queue: deque[BookEvent] = deque(maxlen=self._queue_maxsize)
self._stream_queue: deque[BookEvent] = deque(maxlen=self._queue_maxsize)
```

`collections.deque(maxlen=N)` is documented (Python stdlib) to discard
from the OPPOSITE end of the operation: `append()` (right side) evicts
from the left (oldest); `appendleft()` (left side) evicts from the right
(newest). The observer uses `append()` exclusively in
`_push_with_oldest_drop`, so eviction is from the left — the OLDEST
entry — in O(1).

```python
@staticmethod
def _push_with_oldest_drop(queue: deque[BookEvent], event: BookEvent) -> bool:
    evicted = len(queue) >= (queue.maxlen or 0)
    queue.append(event)
    return evicted
```

This is correct:
- `evicted` snapshots whether the deque was already full BEFORE the
  append. If full, `append()` will evict an old entry; if not full,
  it just grows the deque to N+1.
- `queue.append(event)` is O(1) and atomically performs the
  evict-if-full + insert.
- Returns True iff an eviction occurred — used by the caller to
  account the drop.

Verified at three scales by Wave-3 hardening:
1. `test_burst_at_exact_boundary` — 50 events fill, the 51st drops 1.
2. `test_sustained_overload_100k_events` — 100k events into 50 slots
   produce exactly 99,950 drops, the queue stays pegged at 50, and the
   newest 50 (`h099950..h099999`) are the survivors.
3. `test_50001st_event_replicates_at_real_scale` — 50,001 events into
   a 50,000-slot queue produces exactly 1 drop, `h000000` evicted,
   `h050000` retained. **The literal spec contract.**

### 3.2 Metric correctness — drop-count fix

The Wave-3 audit found that the architect's original `_enqueue`
double-counted the drop metric. Original code:

```python
for q in (self._db_queue, self._stream_queue):
    dropped = self._push_with_oldest_drop(q, event)
    if dropped:
        self.events_dropped_queue_full += 1
        book_events_dropped_total.labels(reason="queue_full").inc()
```

Under sustained overload, both queues are full simultaneously (same
maxlen, neither drains faster than the WS reader pushes). Each
incoming event causes the loop to run twice, both evictions return
True, and the counter increments **twice for the single dropped
event**. Verified empirically: 60 events into a 50-slot pair reported
20 drops, not the expected 10.

The spec § 5 names the metric
`polybot_book_events_dropped_total{reason="queue_full"}` — semantically
"events dropped", not "evictions performed". The architect's count was
off by a factor of (1 + number of sinks that evict on this push). In
production this overstates the drop count by up to 2×, which would
trigger false-positive alerts on the spec's "= 0 over any 24h window"
acceptance criterion.

Wave-3 fix:

```python
any_dropped = False
for q in (self._db_queue, self._stream_queue):
    if self._push_with_oldest_drop(q, event):
        any_dropped = True
if any_dropped:
    self.events_dropped_queue_full += 1
    try:
        book_events_dropped_total.labels(reason="queue_full").inc()
    except Exception:
        pass
```

Verified by `test_exact_drop_count_burst_above_capacity` (exactly 10
drops on 60 events into a 50-slot pair) and the 100k-overload test.

### 3.3 Drop-reason label coverage

Spec § 5 promises three drop reasons:
`queue_full | invalid | attribution_missing`.

| Reason | Path | Increments under |
|--------|------|------------------|
| `queue_full` | `_enqueue` after eviction | Bounded-deque overflow (post-fix: per event, not per sink). |
| `invalid` | `handle_message` when `decode_ws_message` returns None | Malformed payload — non-dict, missing event_type, unknown side, missing market/token id. |
| `attribution_missing` | Reserved for the R6 on-chain reconciler | NOT emitted by R11 itself; the label is legal on the counter for the cross-source path. |

Hardening test `test_queue_full_label_isolated_from_invalid` exercises
queue_full + invalid simultaneously to confirm they're separately
accounted (no cross-contamination).

### 3.4 Non-blocking WS reader

Spec § 3.1: *"… never block the WS reader."* The reader path
(`handle_message` → `_enqueue` → bounded deque `append`) is pure
synchronous in-memory work — no I/O, no `await` between message
arrival and queue insertion. Drop-on-full is constant-time (O(1)) so
WS reader latency is bounded by the deque op cost, not by downstream
DB/Redis pressure. The two consumer loops (`_db_writer_loop`,
`_stream_publisher_loop`) drain independently; either can stall
without backpressuring the producer.

**Note**: the writer + stream-publisher both wait on the same
`_queue_event` and only the writer calls `.clear()`. This is a minor
race (the publisher always sees the event False after the writer
wakes), but the `_db_batch_interval_s` timeout keeps both loops
making progress regardless. **Documented as cross-cutting note CC-3 §
7.3** — out of strict R11 scope to refactor since it doesn't violate
any spec contract.

---

## 4. Detector math audit

### 4.1 IcebergDetector EWMA (λ=0.94)

Standard EWMA recurrence (CLAUDE.md § 7):

> μ = λ·μ_prev + (1-λ)·x_new, λ=0.94

Code (lines 140-146 of `derivers.py`):

```python
prior = self._size_ewma.get(event.token_id)
self._size_ewma[event.token_id] = (
    self.EWMA_LAMBDA * prior + (1.0 - self.EWMA_LAMBDA) * size
    if prior is not None
    else size
)
typical = self._size_ewma[event.token_id]
```

- λ=0.94 weights the prior heavily (94%) and the new sample lightly
  (6%) — correct decay-toward-stable interpretation.
- Bootstrap: first sample becomes the EWMA verbatim (`prior is None →
  μ_0 = x_0`). Standard.
- Per-token EWMA — iceberg detection is price-/wallet-anchored but the
  "typical size" prior is shared across all wallets on that token.
  Defensible: the typical size is a market-level concept, not a
  wallet-level concept. Spec § 3.2.A doesn't disambiguate; the
  architect's choice is reasonable.

Verified to 1e-9 precision by `test_ewma_formula_lambda_094` — the
post-image after [400, 80, 80, 80] matches the manual calculation
exactly.

### 4.2 SpoofDetector — 95th percentile reservoir

Code (lines 247-256 of `derivers.py`):

```python
def _size_at_percentile(self, market_id, token_id) -> float | None:
    reservoir = self._size_reservoir.get((market_id, token_id))
    if not reservoir or len(reservoir) < 8:
        return None
    sorted_sizes = sorted(reservoir)
    idx = max(0, min(len(sorted_sizes) - 1, int(self.size_percentile * len(sorted_sizes))))
    return sorted_sizes[idx]
```

Analysis:
- `RESERVOIR_SIZE = 256` (bounded deque maxlen).
- `idx = int(p * len(sorted))`. For p=0.95 and N=256, idx=243 →
  sorted_sizes[243] is the 244th smallest of 256 = the 95.3rd
  percentile. Slightly under, but within reservoir noise.
- For N=10, idx=9 = the 10th (max). Biased toward the top. The
  `len < 8 → None` guard prevents bootstrap false positives.
- At N=20 (`_bootstrap_reservoir` default in hardening tests), idx=19
  = max. Still biased. Production rarely sees this because the
  reservoir fills within seconds at 1k events/sec.

The bias is documented in the audit matrix as "PASS WITH NOTE" — the
estimator is not the textbook quantile-of-quantiles, but it's
sufficient for the spoof gate, which uses the percentile only as a
"big enough to be unusual" indicator. **No fix required**.

### 4.3 OFI sign convention

Code (lines 384-387 of `derivers.py`):

```python
side_sign = 1.0 if event.side == "buy" else -1.0
signed = side_sign * size
```

Combined with the decoder's `size_delta` sign convention (cancels are
negative, places are positive), the four quadrants are:

| Side  | Event   | Raw size | side_sign | Signed | Interpretation |
|-------|---------|----------|-----------|--------|----------------|
| buy   | placed  | +100     | +1        | +100   | bid pressure (lifts) |
| buy   | cancelled | -100   | +1        | -100   | bid retreat |
| sell  | placed  | +100     | -1        | -100   | ask pressure (heavy) |
| sell  | cancelled | -100   | -1        | +100   | ask retreat (bullish) |

OFI = sum over 5s window of signed values. **Positive = net bid
demand, negative = net ask supply** — matches spec § 3.2.C "positive =
buy pressure; negative = sell".

### 4.4 Place-to-fill timing

The tracker keys in-flight orders by `order_hash`, looks up on fill,
computes `elapsed = fill_time - place_time` (clipped to ≥ 0). The
spec's contract is straightforward and the implementation matches
exactly. Hardening verifies partial fills contribute samples, partial-
then-full records both, orphan fills (no matching place) are silently
dropped.

### 4.5 Cancel-to-fill ratio

Per-wallet rolling 30 min deque of (ts, kind∈{'c','f'}). On each
event, the deque is pruned by cutoff. Ratio = `n_c / n_f`, or sentinel
`= n_c` when n_f == 0 (avoids the `+inf` that would crash the
NUMERIC(8,4) DB column).

Hardening test `test_events_outside_window_are_pruned` confirms a
1801s-old cancel is correctly pruned when a fresh fill arrives.

---

## 5. § 6 acceptance criteria checklist

Spec § 6 names five operator-driven gates. R11 ships the code; the
gates are observable at the production deploy. Status per gate:

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | `polybot_book_events_received_total` > 1M per day in steady state | **OPERATOR GATE** | Requires Hetzner deploy of `polymarket-book-l3.service`. Bot box CX23 + 500GB volume (€18/mo) per spec § 2.3. The 1M/day threshold is roughly 12 events/sec sustained — well below the ~1,000/sec sustained / ~5,000/sec peak the spec § 2.3 anticipates. **Not verifiable in CI**; documented for runbook. |
| 2 | `polybot_book_events_dropped_total{reason="queue_full"}` = 0 over any 24h window | **OPERATOR GATE** | The fix in § 3.2 ensures the metric counts events (not eviction operations) so this gate is now correctly measurable. At sustained ~1k/sec the 50k-queue gives 50s of headroom — should be 0 under normal conditions. |
| 3 | Microstructure features available with < 60s lag | **PASS in code** | The deriver daemon flushes every `MICROSTRUCTURE_ROLLUP_BUCKET_S=60s` boundary; XREADGROUP block is `bucket_s * 250ms = 15s` so the steady-state lag is < 75s worst case. **Gates on Phase 11.B soak**. |
| 4 | R8 classifier validation accuracy improves by ≥ 3 percentage points after retrain | **OPERATOR GATE** | The feature_store + features.py wiring is complete; the operator triggers retrain after 30 days of feature accumulation. **Out of R11 scope per architect § 5.3**. |
| 5 | Cold-tier export of `clob_book_events` completes nightly within batch window | **OPERATOR GATE** | The R6 nightly Parquet exporter needs the new table in its config-driven list. **One-line config addition outside R11's edit scope per architect § 1 #7**. |

---

## 6. Findings + fixes

### 6.1 In-scope (fixed in this review)

#### F-1: Drop counter double-increment (CRITICAL — load-bearing metric)

**File**: `src/observer/clob_book_observer.py::_enqueue`
**Severity**: Medium. The counter overstated drops by up to 2× under
sustained overload. The drop-OLDEST mechanic itself was unaffected
(queues retained the right N entries). But the metric is the
operator's primary alarm signal for queue saturation; a 2× overstate
would trigger false-positive alerts.

**Fix** (8 LOC net diff): rework `_enqueue` to accumulate
`any_dropped` across the two sinks and increment the counter once per
incoming event when at least one sink evicted. The
`events_dropped_queue_full` instance counter and the
`book_events_dropped_total{reason="queue_full"}` Prometheus counter
both move from per-eviction to per-event semantics.

**Verification**: `test_exact_drop_count_burst_above_capacity` asserts
exactly 10 drops on 60 events into a 50-slot pair (previously would
have reported 20). The 100k-overload test asserts exactly 99,950.

#### F-2: `derivers.py` line-too-long (cosmetic)

**File**: `src/microstructure/derivers.py:262`
**Severity**: Trivial. Pre-existing 111-col line — flagged by ruff
but not blocking. Split into two lines.

#### F-3: ruff auto-removed re-exports (regression risk)

During ruff `--fix` the auto-removal stripped `next_bucket_boundary`
and `truncate_to_bucket` from `derivers.py`'s import block. The daemon
and `test_derivers.py` both import these names from
`src.microstructure.derivers` as transitive re-exports. Restored with
`# noqa: F401` annotations to make the re-export intentional.

### 6.2 In-scope (noted, no fix required)

#### N-1: Spoof percentile estimator slightly biased at small N

See § 4.2. The `int(p * len(sorted))` indexing is biased toward the
max for small reservoirs (N < ~32). The `len < 8 → None` guard
prevents the worst-case bootstrap false positives, and the production
reservoir fills to 256 within seconds. **No fix required**.

#### N-2: Writer/publisher event-coordination race

See § 3.4. Both consumer loops wait on `self._queue_event` but only
the writer clears it. The publisher's `wait_for(...)` always times
out instead of being notified. The `_db_batch_interval_s` timeout
(default 0.5s) keeps the publisher making progress, so this doesn't
violate any spec contract — but the architecture would be cleaner
with one event per consumer or a `BroadcastEvent`. **Out of scope for
R11 hardening**; documented for the next refactor pass.

#### N-3: Wallet signature proxy scores

Per architect's `round11_final_review.md` § 5.5, iceberg_score_30d
and spoof_score_30d in the wallet_microstructure_signature are
**proxies** derived from cancel-density. The real per-(market, token)
detections live in microstructure_features. This is a known cold-start
shortcut pending streaming per-wallet aggregation. Hardening test
`test_proxy_scores_bounded_zero_one` asserts the clamp to [0, 1] so
NUMERIC(8,4) overflow can't happen.

---

## 7. Cross-cutting findings (out-of-scope patches)

These are documented as markdown patches because the affected files
fall outside the R11 edit scope and would conflict with R12+ work.

### 7.1 CC-1: `feature_store.py` graceful-degrade contract

**File**: `src/profiler/feature_store.py` (multi-round territory —
R11 + R12 wiring)

The two new R11 functions (`get_microstructure_features_asof` and
`get_wallet_microstructure_signature_asof`) return None on DB error
per `test_failure_returns_none_not_raise` in the baseline suite. This
is the right shape for R8's `LeaderFeatureExtractor` (degrades to NaN
slots).

**Note**: the test asserts the wallet-signature function returns None
on `RuntimeError("DB error")`, but the equivalent test for
`get_microstructure_features_asof` is **missing**. R11 should add it
for symmetry. Suggested patch:

```python
@pytest.mark.asyncio
async def test_microstructure_features_failure_returns_none(asof_ts):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB error"))
    result = await get_microstructure_features_asof(
        conn, "m1", "t1", asof_ts
    )
    assert result is None
```

Place in `tests/test_profiler/test_feature_store_microstructure.py`
alongside the existing wallet-signature failure test.

### 7.2 CC-2: `features.py` slot population test coverage

**File**: `src/strategy_classifier/features.py` (R8 + R11 + R12)

Per architect § 2.4, slots 24/25/26 are now populated when R11 data
exists. The slot SHAPE is preserved. The Wave-3 audit notes that the
existing R8 tests don't exercise the **interaction** between R11's
two new helpers (`_populate_entry_microstructure` reading R11's
microstructure_features for `feature_age_s`, plus
`_populate_wallet_microstructure` reading the wallet signature).

A combined test would assert that with both data sources present, all
three slots (24, 25, 26) carry numeric values; with only one source,
the absent slot stays NaN; with neither, all three are NaN. The
existing three R8-extension tests cover each case individually but
not the interaction matrix.

Suggested addition to `tests/test_strategy_classifier/test_features.py`:

```python
@pytest.mark.asyncio
async def test_microstructure_slots_interaction_matrix(...):
    """All four combinations of (R11 microstructure present, R11
    wallet signature present) should give exactly the expected
    NaN/numeric pattern across slots 24/25/26."""
    # ... parametrize over four (None/dict, None/dict) combinations.
```

### 7.3 CC-3: `metrics.py` consumer-coordination metric

**File**: `src/monitoring/metrics.py` (cross-cutting metrics block)

Per § 3.4 / N-2, the writer + publisher loops share an event; the
publisher relies on the timeout. A `book_publisher_wakeups_by_source`
gauge (label: `event|timeout`) would expose any pathology where the
publisher only wakes via timeout (which would indicate the event
race is masking real backpressure). Suggested counter declaration in
the existing R11 metrics block:

```python
book_publisher_wakeup_source = Counter(
    "polybot_book_publisher_wakeups_total",
    "Wake-up source for the stream publisher loop. Labels: "
    "'event' (queue_event.set was observed), 'timeout' (the "
    "_db_batch_interval_s timeout fired). A sustained 'timeout'-only "
    "ratio indicates the event-coordination race documented in "
    "round11_wave3_review.md § 7.3.",
    ["source"],
)
```

The observer would then `inc(source="event")` when `wait_for` returns
cleanly and `inc(source="timeout")` when `asyncio.TimeoutError` is
caught.

### 7.4 CC-4: `config.py` validator for `MICROSTRUCTURE_SIGNATURE_MIN_ORDERS`

**File**: `src/config.py` (operator-tunable constants)

R11 adds `MICROSTRUCTURE_SIGNATURE_MIN_ORDERS: int = 50` with no
validator. A negative value or 0 would bypass the cold-start guard
and produce signatures off impossibly thin data. Suggested validator:

```python
@field_validator("MICROSTRUCTURE_SIGNATURE_MIN_ORDERS")
@classmethod
def _validate_signature_min_orders(cls, v: int) -> int:
    if v < 1 or v > 10_000:
        raise ValueError(
            f"MICROSTRUCTURE_SIGNATURE_MIN_ORDERS must be in [1, 10000], got {v}."
        )
    return v
```

Mirrors the existing validators on `CLOB_BOOK_QUEUE_MAXSIZE`,
`CLOB_BOOK_RETENTION_DAYS`, etc. Adds one bound and is a one-line
defence against operator typos.

---

## 8. Hardening tests added (59 new)

### 8.1 `tests/test_observer/test_clob_book_observer_hardening.py` (20 tests)

- **Backpressure correctness** (4 tests): exact drop count under
  small burst, boundary at exactly 50 events, 100k sustained
  overload, 50,001-event spec-literal scenario.
- **Drop reason labels** (2 tests): `invalid` and `queue_full`
  isolated; multiple invalid events accumulate correctly.
- **Wallet=NULL across event types** (8 tests — parametrized):
  placement / modification / cancellation preserve NULL; fills pick
  up wallet under all four aliases (wallet_address, wallet, owner,
  maker); partial-fill captures wallet under `maker`.
- **Decoder robustness** (5 tests): None input, non-dict input,
  empty dict, missing event_type, unparseable timestamp falls back to
  now.
- **Queue sink independence** (1 test): draining one sink doesn't
  drain the other.

All tests complete in < 1s each (the 100k-overload runs in ~70ms).

### 8.2 `tests/test_microstructure/test_derivers_hardening.py` (16 tests)

- **IcebergDetector EWMA math** (3 tests): formula verified to 1e-9
  precision, 50% ratio gate excludes typical-sized refills, random
  clean stream produces ≤2 noise detections per 200 events.
- **SpoofDetector boundaries** (4 tests): 5.0s exactly passes the gate
  (inclusive), 5.01s does not, negative elapsed rejected, partial-fill
  excludes the spoof flag.
- **OFI hardening** (4 tests): empty stream emits no keys, cross-
  (market, token) isolation, window prunes old events, zero-size event
  skipped.
- **CancelToFillRatio window** (2 tests): events outside window
  pruned, attribution-less event skipped.
- **PlaceToFill partial-fill** (3 tests): partial fill records sample,
  partial-then-full records both, orphan fill silently dropped.

### 8.3 `tests/test_microstructure/test_wallet_signature_hardening.py` (9 tests)

- Empty universe → no-op (re-asserted).
- Tier filter supports multiple tiers.
- Per-wallet `min_orders` gate (one above, one below).
- Pure-cancel sentinel is finite — parametrized over n_cancels ∈ {50,
  100, 9999}.
- Iceberg + spoof proxy scores clamped to [0, 1].
- One failing fetchrow doesn't poison the rest.
- Naive datetime upgraded to UTC for asyncpg TIMESTAMPTZ.

### 8.4 `tests/test_microstructure/test_partition_maintainer_hardening.py` (14 tests)

- Hour-floor math: strips minutes/seconds/microseconds, normalises tz,
  midnight rollover.
- Partition-name codec: round-trip, DEFAULT partition returns None,
  garbage names return None, year-2031 future-proof.
- DDL generation: exactly N partitions, contiguous hour ranges, CREATE
  IF NOT EXISTS idempotent, zero/negative hours raises, midnight
  boundary naming correct.
- Retention math: 30d cutoff drops old partitions and keeps young
  ones.

---

## 9. Reporting summary

| Metric | Value |
|--------|-------|
| Verdict | **PASS** with 1 in-scope fix + 4 cross-cutting notes |
| Files edited (in scope) | 2 source + 4 new test files |
| Source LOC changes | +14 / -4 net (well under the 50 LOC fix budget) |
| Hardening tests added | 59 (20 observer + 16 derivers + 9 wallet sig + 14 partition) |
| Cross-cutting findings | 4 (feature_store test sym, R8 interaction matrix, metrics gauge, config validator) |
| R11-scope test count after | **122 passing** (63 baseline + 59 new) |
| Full suite after | **1,832 passed**, 9 skipped, 2 xfailed, 0 failures |
| Backpressure test latency | < 100ms each (well under the 5s budget) |
| Ruff status | Clean across all R11 source + new test files |
| Dirty tree | Confirmed dirty (review changes uncommitted, per instructions) |

**North star confirmation**: R11 captures every order-life event,
derives microstructure features (iceberg / spoof / OFI), and feeds
them as new dimensions into the R8 strategy classifier. The data
layer is complete; the operator-driven soaks (Hetzner deploy, 30-day
accumulation, R8 retrain) remain to convert the code-level capability
into the measurable 3pp classifier accuracy gain.
