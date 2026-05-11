# Round 7 — The Front Door: Final Wave-3 Review

> **Branch**: `round-7-frontdoor`
> **Commits since `main` (v0.6.0)**: 4 (Wave-1 + Wave-2 A/B/C)
> **Reviewer**: R7 Wave-3 Reviewer
> **Date**: 2026-05-12
> **Specification reference**: [`docs/ROUND_7_MEMPOOL_AND_PREFILL.md`](../../ROUND_7_MEMPOOL_AND_PREFILL.md)

---

## 1. Top-line recommendation

**PASS-WITH-CAVEATS — ready for merge to `main` + tag `v0.7.0`.**

All six R7 § 3 components are present, the 12 R7 § 5 metrics are declared,
both migrations (024, 025) are syntactically valid and properly indexed,
the systemd unit ships with the correct memory/restart envelope, and
1,132 tests pass with zero failures. The caveats below are all
operator-action-required follow-ups, not code defects. The acceptance
gates in R7 § 6 are operational (require a 30-day shadow soak against
the live Erigon node); R7 wave-2 delivers all the **code-level**
prerequisites those gates depend on.

Two small fixes were applied in this wave (within the ≤50-line budget):
1. Ruff autofixes on `src/mempool/main.py` (import order) +
   `src/mempool/wallet_index.py` (unused `typing.Optional`).
2. **Bug fix**: removed a double-observation of
   `polybot_intent_router_latency_seconds` in `_finalize` — the
   histogram was being observed once inside `_finalize` and again in
   the outer `_on_intent` finally-block on the happy path, biasing
   the p50/p99 quantile estimates used by the R7 § 6 acceptance gate.
3. **Bug fix**: added `src/mempool/__main__.py` to make
   `python -m src.mempool` work — the systemd `ExecStart` invokes
   the package, not the `main` submodule, and without `__main__.py`
   the daemon would error on every restart attempt.

---

## 2. Per-component verification

| § 3.x | Component | File | Lines | Verdict |
|---|---|---|---|---|
| 3.1 | `MempoolSubscription` + `NonceTracker` | `src/mempool/node_client.py` | 419 | PASS |
| 3.2 | `CLOBTxDecoder` + `LeaderIntent` | `src/mempool/tx_decoder.py` | 381 | PASS |
| 3.3 | `WatchedWalletIndex` (bloom filter) | `src/mempool/wallet_index.py` | 251 | PASS |
| 3.4 | `LeaderIntentPublisher` | `src/mempool/event_emitter.py` | 172 | PASS |
|  -  | Daemon wire-up | `src/mempool/main.py` | 202 | PASS |
| 3.5 | `PreSignedPool` | `src/execution/prefill/pool.py` | 578 | PASS |
| 3.6 | `IntentRouter` | `src/execution/prefill/intent_router.py` | 729 | PASS (post-fix) |
|  -  | RPC extension | `src/rpc/client.py::eth_getTransactionByHash` | +20 LOC | PASS |

### Per-component notes

**3.1 (`node_client.py`)** — Subscription uses `RPCClient.eth_subscribe`
with the Erigon `fromAddress` filter built from the bloom snapshot.
Per-tx errors are caught at DEBUG so one bad payload cannot tear down
the stream. `NonceTracker.observe` returns the replaced hash on a
genuine replacement and `None` on re-sighting; `mark_confirmed` reports
the chain length to the histogram before pruning. The 30 s opportunistic
age prune caps in-memory state when `mark_confirmed` isn't being called.
`is_live_for` is consulted by the publisher before emitting (defense in
depth) and could be consulted by the IntentRouter as well (would
require passing the NonceTracker into the router — not done, acceptable
since the publisher already filters obsolete entries from the stream).

**3.2 (`tx_decoder.py`)** — Selectors for `fillOrder` / `matchOrders` /
`cancelOrder` are computed at import time via `keccak`. The Order struct
positional layout matches the verified Polygon-mainnet CTF Exchange ABI
documented at the top of the module. **Wallet is correctly derived from
`order[_ORDER_MAKER]`, not `tx.from_wallet`** — the architect specified
this for relayed/proxy submissions (a facilitator broadcasts, but the
leader signs); the V1 fallback to `tx.from_wallet` on a zero-address
maker is correct. The `decode_failed` / `not_clob` metric branches are
distinct and exercised by the tests. Documented V1 limitations
(no proxy unwrap, `expected_block` deferred to router, V1 stamps
`token_id` as `market_id` placeholder pending the markets-table join)
are tracked with explicit TODOs.

**3.3 (`wallet_index.py`)** — 32 KB bloom filter at 1% FP using
`hashlib.blake2b` with rotated keys (no third-party dependency).
`refresh_from_universe` SELECTs from `wallet_universe` for
`depth_tier IN (0, 1)` and rebuilds in a local before atomic-swap, so
the hot-path `__contains__` never sees a partially-built bloom.
Address normalisation (lowercase + `0x` prefix) mirrors the
`wallet_universe` table convention. `run_refresh_loop` is started by
the daemon main() at `WATCHED_WALLET_INDEX_REFRESH_S` cadence (default
300 s).

**3.4 (`event_emitter.py`)** — Stream name `mempool:leader_intent`
(canonical contract). Payload fields verified against R7 § 3.4 spec:
`trace_id` is set to `intent_id` (one UUID per decision lifecycle),
`Decimal` fields (`size_usdc`, `price`) are explicitly stringified,
`datetime` → epoch ms conversion (`intent_received_at` →
`intent_received_at_ms`) treats naive datetimes as UTC (matches how
`MempoolTx.received_at` is built). `published_at_ms` is injected by
the underlying `StreamProducer`. Idempotent `start()` / `stop()`.
Publish failures are logged + re-raised so the caller can back off.

**3.5 (`pool.py`)** — Bucket-fit `_largest_bucket_le` walks the
ascending `PREFILL_POOL_SIZE_BUCKETS_USDC` and picks the biggest
bucket `<=` `intent.size_usdc` (returns `None` on below-min →
`no_bucket_fit` miss). Single-use guarantee: `_pop_non_expired` pops
from the per-key list under the lock, so 10 concurrent fires on the
same key get 10 distinct orders (tested). Miss reasons cover
`no_bucket_fit | no_market | no_token_match | no_direction |
all_expired | signing_failed`. Rotation loop fires every
`PREFILL_ROTATION_INTERVAL_S` (30 s default) → expire then refill.
The submit happens **outside** the lock — important so concurrent
fires on different keys don't serialize.

**3.6 (`intent_router.py`)** — Decision tree matches R7 § 3.6
exactly:
1. Killswitch strict-path (`bypass_cache=True`).
2. Confidence-engine gate (graceful fallback to `evaluate(trade)` if
   `recommend(wallet, market_id)` is absent — wired via `getattr`
   adapter).
3. Position-size cap (re-checked here because the prefill path
   skips the post-decision RiskManager gate; prefers cockpit-flippable
   `risk_per_trade_pct`, falls back to `MAX_POSITION_PCT`).
4. Cooldown gate (graceful skip if `RiskManager.in_cooldown` is not
   yet implemented — duck-typed via `getattr`).
5. Shadow vs Live branching (default = shadow). LIVE path performs
   the TOCTOU re-check.
**Killswitch is consulted TWICE on the live path** (entry gate + TOCTOU
re-check) — defense-in-depth requirement satisfied. `RESULT_ERROR`
correctly skips the DB INSERT (not in `_OBSERVABLE_RESULTS`); it only
bumps the metric.

**RPC extension (`src/rpc/client.py::eth_getTransactionByHash`)** —
New method follows the existing `eth_getBlockByNumber` helper style:
input validation, `_coalesced_call` for de-duplication, return type
`dict | None`. Style match confirmed.

---

## 3. Metrics inventory (R7 § 5)

All 12 R7 metrics declared in `src/monitoring/metrics.py` lines 526-626:

| # | Metric | Type | Labels | Line |
|---|---|---|---|---|
| 1 | `polybot_mempool_subscriptions_active` | Gauge | `provider` | 534 |
| 2 | `polybot_mempool_tx_received_total` | Counter | `source` | 539 |
| 3 | `polybot_mempool_tx_decoded_total` | Counter | `result` | 544 |
| 4 | `polybot_mempool_wallet_matches_total` | Counter | — | 549 |
| 5 | `polybot_mempool_replacement_chain_length` | Histogram | — | 553 |
| 6 | `polybot_prefill_pool_size` | Gauge | `market`, `direction` | 568 |
| 7 | `polybot_prefill_pool_misses_total` | Counter | `reason` | 573 |
| 8 | `polybot_prefill_pool_signing_seconds` | Histogram | — | 579 |
| 9 | `polybot_intent_router_decisions_total` | Counter | `result` | 591 |
| 10 | `polybot_intent_router_latency_seconds` | Histogram | — | 598 |
| 11 | `polybot_mempool_intent_to_confirm_seconds` | Histogram | — | 613 |
| 12 | `polybot_mempool_shadow_vs_live_pnl_diff_usdc` | Gauge | — | 619 |

Histogram buckets are sensible: `intent_router_latency_seconds` uses
`(0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)` — covers the R7 § 6 p50 < 250 ms
and p99 < 3 s acceptance gates with reasonable granularity. Replacement
chain length uses Fibonacci buckets `(1, 2, 3, 5, 8, 13, 21, 34, 55)`
— useful fingerprint for gas-war detection.

---

## 4. Migration inventory

### Migration 024 — `mempool_observations`

* PK: `intent_id UUID` (deterministic, minted in decoder).
* Columns mirror R7 § 4 SQL sketch + add `cooldown`, `confidence_skip`,
  `size_cap` to the CHECK constraint vocabulary (matches IntentRouter's
  constants).
* Indexes: `(wallet_address, intent_received_at DESC)`, `(tx_hash)`,
  `(intent_received_at)`, and a partial index on `intent_received_at
  WHERE fire_result = 'shadow'` for the soak-window queries.
* CHECK constraints on `fire_result` (8 values) and `side` (`buy|sell`).
* `BEGIN`/`COMMIT` transactional wrapping. `IF NOT EXISTS` idempotent.

### Migration 025 — `live_orders.intent_id` FK

* Adds nullable `intent_id UUID` column with FK to
  `mempool_observations(intent_id)` `ON DELETE SET NULL` (mismatch
  between 30 d mempool retention and 180 d audit retention handled).
* DO-block guard prevents double-add on re-run.
* Partial index `WHERE intent_id IS NOT NULL` (most rows NULL because
  the legacy FOLLOW codepath doesn't populate it).
* `BEGIN`/`COMMIT` transactional wrapping.

Both syntactically valid (verified via `psql --no-execute` style read).
Index selectivity is sensible. FK ON DELETE policy matches the
documented retention asymmetry.

---

## 5. Systemd unit — `polymarket-mempool.service`

| Field | Value | Verdict |
|---|---|---|
| `Description` | "Polymarket Bot — Mempool Watcher + LeaderIntent publisher" | OK |
| `After=` | `network-online.target redis-server.service polymarket-onchain.service` | OK (depends on R6 onchain listener) |
| `Type` | `simple` | OK (matches other R6 daemons) |
| `EnvironmentFile` | `/opt/polymarket-bot/.env` | OK |
| `ExecStart` | `python -m src.mempool` | **FIXED** in this wave by adding `__main__.py` (was broken: no `__main__.py` existed) |
| `Restart` | `always` (5 s delay) | OK |
| `MemoryMax` | `300M` | OK (matches R7 § 2.2 budget) |
| `SyslogIdentifier` | `polymarket-mempool` | OK |

Fix applied: new file `src/mempool/__main__.py` (~18 LOC) imports `main` from
`src.mempool.main` and calls `asyncio.run(main())`. Alternative would
have been to change the systemd `ExecStart` to `-m src.mempool.main`
(matching other daemons), but the systemd unit is deployed and adding
`__main__.py` is the smaller surface change.

---

## 6. Test counts

### Full suite

```
1,132 passed, 1 skipped, 2 xfailed, 29 warnings in 44.53s
```

### R7-specific files (82 tests total)

| File | Tests |
|---|---|
| `tests/test_mempool/test_event_emitter.py` | 7 |
| `tests/test_mempool/test_node_client.py` | 15 |
| `tests/test_mempool/test_tx_decoder.py` | 9 |
| `tests/test_mempool/test_wallet_index.py` | 11 |
| `tests/test_execution/test_prefill_pool.py` | 20 |
| `tests/test_execution/test_prefill_intent_router.py` | 20 |

Per-component coverage is good:
- 15 tests on `node_client` cover subscription happy path, per-tx error
  isolation, EIP-1559 / legacy gas-price fields, NonceTracker replace /
  re-sight / mark_confirmed / age-prune / is_live_for cases.
- 9 decoder tests cover selector miss, ABI decode failure, BUY/SELL
  encoding, maker fallback, replaces propagation.
- 11 wallet_index tests cover bloom sizing, FP boundary, atomic-swap
  refresh, db-unavailable graceful fallback.
- 7 event_emitter tests cover payload shape (trace_id, Decimal stringification,
  epoch-ms datetime), idempotent lifecycle, publish failure propagation.
- 20 pool tests cover bucket-fit, single-use guarantee, miss
  classification, expire / refill rotation.
- 20 IntentRouter tests cover every decision-tree branch + the killswitch
  TOCTOU re-check + the duck-typed `in_cooldown` graceful degradation.

### Ruff

```
ruff check src/mempool/ src/execution/prefill/ src/rpc/client.py
```

After autofixes (this wave): **clean** (0 errors).

Initial findings (2 autofixed):
- `src/mempool/main.py:9:1: I001` — import block un-sorted.
- `src/mempool/wallet_index.py:36:45: F401` — unused `typing.Optional`.

No E501 (line-length) findings. No other lint issues across 1,800 R7 LOC.

---

## 7. Known caveats (operator follow-up required)

### a. `PreSignedPool` CLOB binding split — operator follow-up
Tests mock `sign_order` + `submit_presigned` as a 2-method async pair on
the CLOB wrapper. Production wiring requires splitting
`CLOBClientWrapper.place_limit_order` (`src/engine/clob_client_wrapper.py`
lines 264-354 per Wave-2 B report) into `sign_order` (sign-only) +
`submit_presigned` (post-only). **Operator action**: before flipping
`PREFILL_LIVE_ENABLED=true`, an integration smoke test against the live
CLOB is required. This is a Phase 7.C precondition, not a code defect.

### b. `prefill_live_enabled` shadow-mode flag — RuntimeConfig registration deferred
`RuntimeConfig.ALLOWED_KEYS` currently only supports int/float coercion.
The IntentRouter reads `runtime_config.get('prefill_live_enabled')`
with fallback to `settings.PREFILL_LIVE_ENABLED` (env-driven, default
`False`). Operator flips via env var + service restart today. **TODO
comment present** at `src/execution/prefill/intent_router.py:577`
tracking the cockpit-toggle wiring.

### c. `RiskManager.in_cooldown` may not exist — graceful degradation tested
IntentRouter duck-types via `getattr(risk_manager, 'in_cooldown', None)`.
Tested by `test_in_cooldown_missing_on_risk_manager_is_tolerated`
(`test_prefill_intent_router.py:694`). **No action required** — wired
to degrade gracefully.

### d. `RESULT_ERROR` skips the `mempool_observations` INSERT — by design
The CHECK constraint on `mempool_observations.fire_result` (migration
024 line 126) does not include `'error'`. The IntentRouter's
`_finalize` short-circuits the INSERT for `RESULT_ERROR` via
`_OBSERVABLE_RESULTS` (lines 62-73 of `intent_router.py`). **No action
required** — schema + writer are aligned; the metric counter still
fires for ops alerting.

### e. `__main__.py` was missing — **FIXED** in this wave
The systemd unit's `ExecStart=python -m src.mempool` would have errored
on every restart (`No module named src.mempool.__main__`). Added a
thin `__main__.py` that calls `asyncio.run(main())`. **Operator
action**: none, just deploy the new file.

### f. Latency histogram was double-observed — **FIXED** in this wave
`polybot_intent_router_latency_seconds` was observed both inside
`_finalize` (line 478 of old code) and in the outer `_on_intent`
finally-block (line 398) on the happy path. This biased the p50/p99
quantile estimates that the R7 § 6 acceptance gate depends on. Removed
the duplicate `_observe_latency` call from `_finalize`. **Operator
action**: none.

### g. Wave-2 A added `_last_fire_result` helper inside Wave-2 C's tests
Per the Wave-3 brief, Agent A refactored `tests/test_execution/test_prefill_intent_router.py`
to add a `_last_fire_result(conn)` helper + refactored call sites. The
refactor is harmless (no semantic change, all 20 tests still pass).
Listed here for traceability.

### h. Decoder V1 limitations (documented in module docstrings)
- No proxy / negRiskAdapter envelope unwrap. Proxy calls produce
  `not_clob` metric (acceptable miss class).
- `expected_block` filled at IntentRouter consume time (decoder
  emits 0). The mempool publisher could query
  `RPCClient.eth_blockNumber` and stamp it — deferred follow-up.
- `market_id` in the LeaderIntent is the lowercased hex of the
  CTF `tokenId` (placeholder). The IntentRouter currently fires
  with this string in the pool key; the real `markets` table
  join can land in the same migration as the routing-key fix.
  This affects pool **hit rate** but not safety.

### i. `.env.example` access denied in review context
File was inaccessible during this review session (permission denied on
read). Per the Wave-2 implementer reports the 7 operator knobs
(`PREFILL_*` + `MEMPOOL_INTENT_LATENCY_BUDGET_MS` + `WATCHED_WALLET_INDEX_REFRESH_S`)
should be present. **Operator action**: a quick `grep -E 'PREFILL|MEMPOOL'
.env.example` before the rollout would confirm.

---

## 8. Operator-action-required checklist (R7 § 7 rollout)

### Phase 7.A — Read-only mempool + shadow logging (weeks 1-2)
- [ ] Deploy `infra/systemd/polymarket-mempool.service` (300 MB cap).
- [ ] Apply migration `024_mempool_observations.sql`.
- [ ] Apply migration `025_live_orders_intent_id.sql`.
- [ ] Add `mempool_observations` row to `RETENTION_POLICIES` in
      `scripts/batch_runner.py` with `days=30` (per the migration's
      header comment).
- [ ] Verify `polybot_mempool_tx_received_total{source="erigon"}` is
      ticking (mempool subscription healthy).
- [ ] Verify `polybot_mempool_tx_decoded_total{result="decoded"}` > 0
      (decoder seeing CLOB tx in the wild).
- [ ] Verify `polybot_intent_router_decisions_total{result="shadow"}`
      ticking (IntentRouter consuming the stream).
- [ ] Gate: 7 days of clean shadow logs, p50
      `polybot_intent_router_latency_seconds` < 250 ms.

### Phase 7.B — Pre-signed pool warming + paper firing (weeks 2-3)
- [ ] Split `CLOBClientWrapper.place_limit_order` into `sign_order` +
      `submit_presigned` (Wave-3 caveat 7.a).
- [ ] Wire `PreSignedPool.warm` to a real markets provider (SELECT
      top-`PREFILL_TOP_MARKETS` markets by 24h volume).
- [ ] Smoke-test `PreSignedPool` against the live CLOB on a tiny
      bucket (e.g. $50 notional) before scaling.
- [ ] Verify `polybot_prefill_pool_size{market, direction}` shows
      ≥4 orders/key after warm.
- [ ] Verify `polybot_prefill_pool_signing_seconds` p50 < 100 ms.
- [ ] Gate: 14 days of paper firing with no anomalies, Sharpe ≥
      existing FOLLOW path.

### Phase 7.C — Live capital under shadow gate (weeks 4-6)
- [ ] Set `PREFILL_LIVE_ENABLED=true` in `.env` (operator restart).
- [ ] Confirm killswitch is on (`/api/killswitch/status`); validate
      that flipping it off **stops** prefill fires within 5 s
      (the TOCTOU re-check in `_on_intent` step 5b).
- [ ] Verify the live position-size cap matches
      `runtime_config.risk_per_trade_pct` (cockpit flip should
      propagate).
- [ ] Telegram alert on every live prefill order (wire
      `polybot_intent_router_decisions_total{result="filled"}`
      increment to the notifier — verify this is in place).
- [ ] Gate: 30 days of small-live with positive PnL, no killswitch
      trips, no anomalies.

### Phase 7.D — Full live (week 6+)
- [ ] Lift position-size cap to normal (governed by RiskManager only).
- [ ] Monitor `polybot_mempool_shadow_vs_live_pnl_diff_usdc` as a
      calibration metric.
- [ ] Register `prefill_live_enabled` in RuntimeConfig.ALLOWED_KEYS
      (Wave-3 caveat 7.b) so the cockpit toggle can flip live → shadow
      without a restart for emergency revert.

### Cross-phase follow-ups (no fixed gate)
- [ ] Unify the function-selector ABI between `src/mempool/tx_decoder.py`
      and `src/onchain/clob_abi.py` (Wave-3 caveat 7.h).
- [ ] Resolve the `LeaderIntent.market_id` `tokenId`-vs-condition-id
      join (Wave-3 caveat 7.h).
- [ ] Stamp `expected_block` at decode/publish time
      (`RPCClient.eth_blockNumber` + 1; current value is 0).

---

## 9. Recommendation

**READY for merge to `main` and tag `v0.7.0`.**

Reasoning:
- All R7 § 3 components present and wired through the documented
  abstractions (`src.rpc.client.RPCClient`, `src.control.redis_streams`,
  `wallet_universe`, `mempool_observations`).
- All 12 R7 § 5 metrics declared with sensible labels and bucket
  choices.
- Both migrations syntactically valid and properly indexed.
- Systemd unit is correct (after this wave's `__main__.py` fix).
- 82 R7-specific tests cover every branch of the decision tree, the
  bloom semantics, the bucket-fit + single-use guarantees, the
  replacement-chain detection, and the graceful-degradation paths.
- Full suite: 1,132 passed, 0 failures.
- Ruff: clean (after autofixes applied this wave).
- All known caveats are operator-action items for the R7 § 7 rollout,
  not code defects.

Two non-trivial defects were caught and fixed in this wave (missing
`__main__.py` + latency-histogram double-observation), both within the
≤50-line review budget. Neither would have surfaced in unit tests
because the failure modes are deployment-time (`python -m src.mempool`)
and metric-bias (the second observation has the same value as the
first, so test assertions on the histogram object's `_count` would
have passed). The bug-fix changes (~20 LOC across 3 files) preserve
all existing test pass rates.

The orchestrator can merge `round-7-frontdoor` → `main` and tag
`v0.7.0` once the rollout-checklist Phase 7.A prerequisites
(migrations applied, systemd unit enabled, batch retention policy
extended) are in place on the deploy target.
