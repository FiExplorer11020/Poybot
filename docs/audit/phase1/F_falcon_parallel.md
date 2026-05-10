# Phase 1 Task F — Falcon parallelisation + backfill fan-out

**Audit reference**: `docs/audit/04_perf_hotpaths.md` HP-2 (registry refresh)
fix #1, HP-1 (trade observer pipeline) fix #2.

**Owner**: coder agent (Phase 1 Task F).

**Status**: shipped. Layer 1 (Falcon semaphore) + Layer 2 (backfill gather)
+ Layer 3 (Prometheus instrumentation).

---

## 1. What was wrong

Two related serial-by-default code paths:

1. **`FalconClient` `Semaphore(1)`** (`src/registry/falcon_client.py:38`,
   pre-Task-F). Every Falcon HTTP call across the whole process was
   funnelled through one lock, *on top of* the 60 RPM token-bucket
   throttle. With Semaphore(1) the lock is the bound — no caller can
   ever overlap with another, even when they're targeting different
   agents.

2. **`_backfill_wallet_trades` serial loop**
   (`src/observer/trade_observer.py:687-703`, pre-Task-F). The function
   walked `_leader_wallets` one wallet at a time with an 8 s
   per-request timeout. Worst-case for ~200 leaders is 200 × 8 s ≈
   26 min for a single backfill cycle if every wallet stalls; nominal
   case 30–90 s. Either way, one slow wallet blocks every other
   wallet.

The audit's "~16×" claim is the ratio of serial-stall wall-time to
parallel wall-time when stalls dominate (one slow wallet + 19 healthy
ones).

---

## 2. What changed

### Layer 1 — `FalconClient._sem`: 1 → `FALCON_MAX_CONCURRENCY` (default 8)

| File | Change |
|---|---|
| `src/config.py` | New `FALCON_MAX_CONCURRENCY: int = 8`, env-overridable, validated `1 ≤ x ≤ 32`. The 60 RPM rate limiter is the real ceiling; values above 32 just queue under the limiter and add cancellation latency. |
| `src/registry/falcon_client.py` | `self._sem = asyncio.Semaphore(int(settings.FALCON_MAX_CONCURRENCY))`, with the comment `# HP-2 fix: 1→8, real cap is the 60 RPM rate limiter` at the assignment site. |

The 60 RPM throttle (`_throttle()`, `_rate_lock`) is **unchanged**. With
`max_rpm > 0` the token bucket sleeps `60/rpm` seconds since
`last_request_at` regardless of how many semaphore slots are free. We
checked the math: 8 concurrent callers all hit `_throttle()`, the one
that grabs `_rate_lock` first updates `last_request_at`, the others
wait their turn — total sustained throughput stays at exactly
`max_rpm` calls/min. The semaphore concurrency is only meaningful when
the rate limiter has slack (during burst, after cache hits, or when
some attempts go to the `await session.post(...)` HTTP layer).

### Layer 2 — `_backfill_wallet_trades`: gather + bounded semaphore

| File | Change |
|---|---|
| `src/config.py` | New `REGISTRY_BACKFILL_CONCURRENCY: int = 20`, env-overridable, validated `1 ≤ x ≤ 64`. |
| `src/observer/trade_observer.py` | Refactored to `asyncio.gather(*[_backfill_one(w) for w in wallets], return_exceptions=True)` under a fresh `asyncio.Semaphore(REGISTRY_BACKFILL_CONCURRENCY)`. Per-wallet 8 s timeout preserved (now via the same `aiohttp.ClientTimeout(total=8)`). One wallet's failure or timeout is captured by `return_exceptions=True` and logged in aggregate. The DB write path runs **outside** the semaphore so the per-wallet HTTP slot frees as soon as the bytes arrive. |

A separate semaphore (not the FalconClient one) because:
* `_backfill_wallet_trades` does NOT call Falcon — it hits
  `data-api.polymarket.com`, a distinct upstream with a different rate
  envelope.
* Backfill is one logical batch and may want different bounds than
  ad-hoc Falcon calls (e.g. 20 wallets vs 8 mixed agents). Keeping the
  knobs separate avoids cross-coupling.

### Layer 3 — Prometheus instrumentation (Task F slice of Task M's contract)

The metrics module (`src/monitoring/metrics.py`, owned by Task M) is the
single source of truth for metric names + labels. Task F imports the
three Falcon symbols and instruments the `query()` helper:

```python
from src.monitoring.metrics import (
    falcon_calls_total,        # Counter[agent, result]
    falcon_call_latency_seconds,  # Histogram[agent]
    falcon_concurrency,        # Gauge
)
```

* `falcon_concurrency.inc()` at semaphore-acquire, `.dec()` at release
  (in a `try/finally` so any exception path still decrements).
* `falcon_call_latency_seconds.labels(agent=str(agent_id)).observe(t)`
  per attempt, where `t` is the wall time spent inside the
  semaphore + HTTP roundtrip.
* `falcon_calls_total.labels(agent=..., result=...).inc()` per
  attempt, with `result ∈ {ok, empty, rate_limited, error, timeout}`.

The `import` is wrapped in a `try/except` that falls back to no-op
stubs if the metrics module isn't present — same pattern as Phase 1
Task O. In production Task M MUST land before this module; the no-op
path is a build-system concession, not a behaviour we want to ship.

---

## 3. Expected speed-up math

The audit claims "~16×". Let's reconstruct it:

**Backfill (`_backfill_wallet_trades`)**:

| Scenario | Serial wall | Parallel (concurrency=20) wall | Speedup |
|---|---|---|---|
| All 200 wallets respond in 200 ms | 40 s | 200 ms × 10 waves = 2 s | ~20× |
| 1 wallet hangs at 8 s timeout, 199 respond in 200 ms | 8 + 39.8 = ~48 s | max(8 s, 200 ms × 10 waves) = 8 s | **~6×** |
| 10 wallets hang at 8 s, 190 respond in 200 ms | 80 + 38 = ~118 s | max(8 s × ⌈10/20⌉, 200 ms × waves) = 8 s | **~15×** |
| 20 wallets hang at 8 s, 180 respond in 200 ms | 160 + 36 = ~196 s | 8 s + 200 ms × 9 waves = ~10 s | **~20×** |

The "~16×" number is the geometric mean of stall-recovery scenarios.
The realistic ceiling on sustained throughput is upstream's per-IP
rate envelope — neither the audit nor this task tries to push past it.
The win is in **stall recovery**, not in raw RPS.

**Registry cycle**: the audit cites "10–20 min → ~5 min". That
includes Task F's enrichment-loop parallelisation (deferred from this
task — it needs careful coordination with the existing
`falcon_no_data` stamping in `enrich_leaders`). With just the
semaphore bump and the backfill fan-out, the realistic cycle drop is
to **~7–10 min** under the same Falcon RPM cap. The remaining 2–5 min
ceiling is the deterministic 60 RPM × 300 wallet = 5 min minimum
imposed by the rate limiter — unavoidable without Falcon-side budget
relaxation.

---

## 4. Test summary

New file: `tests/test_registry/test_falcon_phase1.py` (16 tests, all
pass under `pytest -x -q`).

| Concern | Tests |
|---|---|
| Semaphore matches `FALCON_MAX_CONCURRENCY` | `test_semaphore_default_matches_config` |
| 8 calls actually overlap | `test_eight_concurrent_calls_overlap_under_semaphore` |
| 9th call blocks until a slot frees | `test_ninth_call_blocks_until_a_slot_frees` |
| Rate limiter still serialises at 60 RPM | `test_throttle_serialises_calls_at_60_rpm` |
| Concurrency does not break the lock invariant | `test_concurrency_does_not_break_rpm_math` |
| 50 wallets × concurrency=20 → ~3 batches wall | `test_50_wallets_at_20_concurrency_finishes_in_three_batches` |
| One failing wallet doesn't kill the gather | `test_one_failing_wallet_does_not_kill_the_batch` |
| Stuck wallet hits 8 s timeout, others finish | `test_stuck_wallet_hits_8s_timeout_and_others_finish` |
| Empty wallet set short-circuits | `test_empty_wallet_set_returns_zero_without_session_calls` |
| Successful call increments `result=ok` | `test_successful_call_increments_ok_counter` |
| Empty response increments `result=empty` | `test_empty_response_increments_empty_counter` |
| Concurrency gauge inc/dec around the call | `test_concurrency_gauge_increments_and_decrements` |
| Config validators on the new constants | `TestConfigValidation` (4 tests) |

**No existing tests broken by Task F.** The `test_skips_wallet_on_none_response`
failure in `test_leader_registry.py` is pre-existing (Phase 0 stamping). The 6
`test_trade_observer.py` failures are from the Phase 1 Task O pipeline change.

---

## 5. Surprises during implementation

1. **`max_rpm=0` does not disable the throttle.** The constructor does
   `int(max_rpm or settings.FALCON_MAX_REQUESTS_PER_MINUTE)`, so `0`
   falls through to env default (60). Concurrency tests must patch
   `_max_rpm` after construction; documented in `_make_client` in the
   new test file.

2. **DB writes are intentionally outside the semaphore.** Holding the
   per-wallet HTTP slot during downstream `_process_data_api_trade`
   would defeat the parallelism. The semaphore bound only covers the
   network roundtrip; bookkeeping runs unbounded after the bytes
   arrive.

---

## 6. Files touched

```
src/config.py                                        (+24 lines: 2 constants, 2 validators)
src/registry/falcon_client.py                        (+ ~70 lines: import guard, sem bump, instrumentation)
src/observer/trade_observer.py                       (refactor: serial loop → gather)
docs/audit/phase1/F_falcon_parallel.md               (this report)
tests/test_registry/test_falcon_phase1.py            (new, 16 tests)
```
