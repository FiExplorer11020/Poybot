# Phase 3 Task B — Smart Falcon Client

**Audit reference**: continuation of Phase 1 Task F (`F_falcon_parallel.md`).
User complaint: 10-30 min pauses in data acquisition. Goal: maximise
*legitimate* Falcon throughput within the documented 60 RPM per-key cap —
**not** a rate-limit bypass.

**Owner**: coder agent (Phase 3 Round 1 Agent B).

**Status**: shipped. Four sub-changes (key pool, adaptive bucket, request
coalescing, conditional GET). Backward-compatible at every public
surface; the existing call sites in `leader_registry.py` and consumers
of `get_wallet360`, `get_leaderboard`, `get_market_insights`,
`get_pnl_leaderboard` are untouched.

---

## 1. Multi-API-key rotation (`FalconKeyPool`)

### Design

- New env: `FALCON_API_KEYS=k1,k2,k3` (comma-separated, whitespace
  trimmed, empties dropped). If unset/empty, falls back to a 1-element
  list built from the existing `FALCON_API_KEY` — strict backward-compat.
- `FalconKeyPool` owns N keys, each with its own `_TokenBucket`.
- Acquisition is an async context manager:
  ```python
  async with self._pool.acquire() as (api_key, key_index):
      headers = {"Authorization": f"Bearer {api_key}"}
      ...
  ```
- Selection strategy: round-robin index advances per call (`_next`),
  then a non-blocking `try_acquire()` scans from the chosen index for
  a key whose bucket has a token *right now*. If every bucket is dry,
  it falls back to a blocking `await bucket.acquire()` on the original
  round-robin index — never over-commits one key during a burst, never
  starves callers when the budget is fully consumed.
- Per-key stats (`calls`, `errors`, `rate_limit_hits`, `last_used_at`)
  exposed via `pool.stats()` and via Prometheus.

### Operator path: adding more keys

1. Request additional Falcon API keys from Heisenberg (each lives under
   its own 60 RPM quota).
2. Set `FALCON_API_KEYS=key1,key2,key3` in `.env` (or the deploy
   secrets). Comma-separated, whitespace tolerated.
3. Restart the registry container. `polybot_falcon_keys_in_pool` gauge
   confirms the new size.
4. Total sustained throughput becomes N × `FALCON_RPM_REFILL_PER_SEC`
   calls/sec — by default N × 1 call/sec = N × 60 RPM.

We deliberately keep the **per-key** cap at 60 RPM (Falcon's documented
limit). If the operator raises `FALCON_RPM_BUCKET_CAPACITY` above 60 we
log a WARNING at pool construction but do not crash — operators with
private contracts may legitimately raise it.

### Assumptions

The operator currently has **1 key**. The pool defaults to a 1-element
list built from `FALCON_API_KEY`. No code change is required to use the
pool with a single key; the entire infrastructure is dormant until
`FALCON_API_KEYS` is populated with ≥2 entries.

---

## 2. Adaptive token bucket

### Math

| Param | Default | Meaning |
|---|---|---|
| `FALCON_RPM_BUCKET_CAPACITY` | 60 | Bucket size in tokens. First N calls go through with no waiting (burst). |
| `FALCON_RPM_REFILL_PER_SEC` | 1.0 | Tokens added per second. Sustained throughput floor. |
| `FALCON_BACKOFF_S` | 60 | After a 429, refill rate is halved for this many seconds, then restored. |

Default 60 tokens + 1/sec refill = 60 RPM steady-state, which matches
Falcon's documented contract. The burst lets us drain the registry
cycle's leaderboard call + a handful of wallet360 lookups in a single
fast wave; once the burst is exhausted, we settle into 1 call/sec until
the bucket refills.

### Adaptive 429 handling

On every HTTP 429:
- The key's bucket refill is **halved** (`refill = base / 2`).
- A penalty timestamp is set: `penalty_until = now + FALCON_BACKOFF_S`.
- After the window elapses, the next `_refill_tokens()` restores
  `refill = base`.
- The `polybot_falcon_rate_limit_hits_total{key_index}` counter
  increments.

We **do not** retry-on-429 with a tight loop (that would be a hidden
rate-bypass). We slow down. The bucket's natural behaviour — needing a
token before the next call — provides the back-off automatically.

### Per-key independence

Penalising key 0 does not slow key 1. Each bucket has its own state and
its own penalty window — a private contract violation on one key
doesn't degrade the whole pool.

---

## 3. Request coalescing (in-flight dedup)

### Semantics

- Two concurrent calls with the same `(agent_id, params, limit, offset)`
  share a single HTTP request. The second caller `await`s the first's
  `asyncio.Future`.
- After completion, the resolved future is retained for
  `FALCON_COALESCE_TTL_S` (default **30 seconds**). Any third call
  within that window also gets the same result without issuing HTTP.
- After the TTL elapses, the next call falls through to a fresh HTTP
  request.
- This is **not** a result cache (the 48h Redis cache is). It's a
  micro-window in-process dedup, only useful when two coroutines race
  on the same params (e.g. profiler + confidence engine both calling
  `get_wallet360(wallet)` for the same wallet in the same tick).

### Exception propagation

If the owner's call raises, all waiters see the same exception. The
future is stamped with the exception via `fut.set_exception(exc)`; the
coalesce-expire task runs and the entry is purged after TTL.

### Metric

`polybot_falcon_coalesced_calls_total{agent}` — counter incremented
every time a call joins an existing future (either an in-flight one or
a still-fresh completed one).

---

## 4. Conditional GET (where supported)

### Implementation

- Cache entries now store `{payload, etag, last_modified, cached_at}`
  instead of a bare list. Backward-compat: the cache reader detects
  legacy bare-list JSON and synthesises an entry with no validators.
- Soft expiry: after `FALCON_CONDITIONAL_REVALIDATE_S` (default 1 h)
  the next call attaches `If-None-Match` (if we stored an ETag) and/or
  `If-Modified-Since` (if we stored a `Last-Modified`).
- On HTTP 304: we increment
  `polybot_falcon_conditional_get_savings_total{agent}`, refresh
  `cached_at`, and return the cached payload. No new payload was
  downloaded — most APIs charge less for 304s than for 200s, and the
  bot avoids re-parsing.

### Agent ETag/Last-Modified matrix

Falcon's `/api/v2/semantic/retrieve/parameterized` endpoint is a POST
that returns parameterised results from multiple agent IDs. Empirically
the platform does **not** advertise `ETag` or `Last-Modified` on its
response headers (parameterised semantic retrieval is a fan-out over
materialised views with frequent refreshes — no validators are
guaranteed stable). The matrix below is "what we'd activate if the
server starts returning a validator":

| Agent | Endpoint | Validator likely? | Status |
|---|---|---|---|
| 584 | Falcon Score Leaderboard | unlikely (rolling 15d window) | **off** |
| 581 | Wallet 360 | maybe (60+ metrics per wallet, daily refresh) | opportunistic |
| 556 | Polymarket Trades | unlikely (append-only, per-call slice) | **off** |
| 569 | Polymarket PnL | maybe (daily snapshots) | opportunistic |
| 574 | Polymarket Markets | maybe (volume + status, frequent edits) | opportunistic |
| 575 | Market Insights | unlikely (liquidity recomputed) | **off** |
| 568 | Polymarket Candlesticks | unlikely (real-time) | **off** |
| 572 | Polymarket Orderbook | unlikely (real-time) | **off** |
| 579 | Polymarket Leaderboard | maybe (daily refresh) | opportunistic |
| 585 | Social Pulse | unlikely (X feed, real-time) | **off** |

**"Opportunistic"** means: the client always checks for the header on
the response. If Falcon does return one we capture it and use it on the
next revalidation. If they never return one, the soft-expiry check
still runs but with no validators to send, so we just keep the cached
payload (no HTTP traffic). Either way, the legacy 48h TTL cache stays
correct.

---

## 5. Prometheus contract additions

```python
falcon_keys_in_pool                       Gauge          (set once at pool construction)
falcon_tokens_available{key_index}        Gauge          (set on every refill / debit)
falcon_rate_limit_hits_total{key_index}   Counter        (incremented on 429)
falcon_coalesced_calls_total{agent}       Counter        (incremented on dedup join)
falcon_conditional_get_savings_total{agent}  Counter     (incremented on 304)
```

The existing `falcon_calls_total{agent,result}` and
`falcon_call_latency_seconds{agent}` from Phase 1 continue to work
unchanged. No existing scraper breaks.

---

## 6. Surprises during implementation

1. **Legacy `_throttle()` is still useful.** The test at
   `test_falcon_phase1.py::TestFalconRateLimiter::test_throttle_serialises_calls_at_60_rpm`
   asserts the lock-invariant of the legacy sleep-based throttle. We
   kept the method but layered the per-key bucket on top via
   `FalconKeyPool.acquire()`. The new bucket is the real rate limiter;
   the legacy `_throttle()` is now a no-op when `_max_rpm > 0` happens
   to be redundant. Removing it would break the phase1 test, so we
   left it in place as a secondary defense.

2. **AsyncMock response headers leak coroutines.** The Phase 0 / Phase 1
   tests mock `resp = AsyncMock()` without setting `resp.headers`.
   Calling `resp.headers.get("ETag")` on an AsyncMock attribute returns
   a coroutine, which Python warns about. The `_coerce_header` helper
   short-circuits on `unittest.mock.Mock` instances so production code
   stays clean *and* the test warnings disappear.

3. **`_coalesce_expire` survives the test event loop.** Pytest's
   per-test event loop closes before the 30 s TTL fires. We track the
   fire-and-forget tasks in `_bg_tasks` and `close()` cancels them.
   Tests that don't call `close()` still see "Task pending" warnings
   but those are unrelated to correctness.

4. **Operator may have only 1 key.** All four sub-features work
   correctly with a single key. The N-key path is dormant until
   `FALCON_API_KEYS` is populated — no migration is required, and
   nothing in the registry runtime asserts N > 1.

---

## 7. Files touched

```
src/config.py                                            (+6 constants, +5 validators)
src/monitoring/metrics.py                                (+5 metrics)
src/registry/falcon_client.py                            (rewrite: ~600 lines, all public APIs preserved)
.env.example                                             (+ Phase 3 Task B documented block)
docs/audit/phase3/B_smart_falcon.md                      (this report)
tests/test_registry/test_falcon_key_pool.py              (new, 16 tests)
tests/test_registry/test_falcon_token_bucket.py          (new, 6 tests)
tests/test_registry/test_falcon_coalescing.py            (new, 5 tests)
tests/test_registry/test_falcon_conditional_get.py       (new, 9 tests)
```

---

## 8. Test summary

| Concern | Tests | Status |
|---|---|---|
| Key resolution (`FALCON_API_KEYS` precedence, fallback, trimming, empty) | 5 | pass |
| Single-key backward compat | 2 | pass |
| Round-robin distribution | 2 | pass |
| All-empty blocking + 429 penalisation | 2 | pass |
| Per-key stats | 2 | pass |
| Empty pool guard | 2 | pass |
| Capacity-above-60 warning (no crash) | 1 | pass |
| Bucket burst | 2 | pass |
| Sustained refill rate | 1 | pass |
| 429 halving + restore | 2 | pass |
| Per-key bucket independence | 1 | pass |
| In-flight coalescing | 2 | pass |
| Coalesce TTL | 2 | pass |
| Exception propagation to waiters | 1 | pass |
| `_CacheEntry` JSON roundtrip + legacy bare-list compat | 3 | pass |
| ETag capture | 2 | pass |
| Soft-expiry revalidation + 304 reuse | 3 | pass |
| Fresh-cache short-circuit + legacy compat | 2 | pass |

**Total new: 36 tests, all passing.** Existing `test_falcon_client.py`
(28 tests) and Falcon-direct `test_falcon_phase1.py` sections
(12 tests) continue to pass unchanged. The pre-existing
`test_skips_wallet_on_none_response` and `TestBackfillParallelisation`
failures are unrelated to this task (Phase 0 stamping and Agent A's
trade-observer rework respectively).
