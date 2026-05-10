# Audit 04 — Performance hot paths (data acquisition + refresh)

**Scope.** Latency and throughput of the data-acquisition pipeline. From "external
event happens" → "system has reacted." Schema/client correctness is out of scope
(see audits 01–03).

**Methodology.** Static reading of the runtime modules listed in each section,
cross-referenced with `src/config.py` defaults and the production deployment
notes in `docs/INFRA.md`. No live profiling — all latency numbers are
order-of-magnitude estimates based on call structure (RTTs, await counts,
DB roundtrips per event).

**Targets being optimized for.**

* **Trade-to-react latency** — time between a leader trade hitting the
  `data-api` and the corresponding row being committed in `trades_observed`
  + `trades:observed` published. Today: dominated by the 30 s polling
  cadence (median ≈ 15 s, p99 ≈ 30 s + DB write). Goal: < 2 s p50, < 5 s p99.
* **Headroom for 10× volume.** Today the WebSocket consumer is single-loop
  and the DB write is one `INSERT … RETURNING id` plus 1–3 lookup queries
  *per trade*. At ~1 trade/s the system breathes; at 10/s it queues; at
  100/s it falls behind.
* **Refresh cadence.** The `LeaderRegistry.run()` loop is sequential and
  blocks on Falcon for the full ~600 wallet enrichments. Adding a leader
  shouldn't add minutes of latency to the next tick.

---

## HP-1 — Trade observer pipeline

### Current behavior

`src/observer/trade_observer.py:333-348` starts two coroutines in
`asyncio.gather`: a single `PolymarketWSClient` (WS market channel — book
+ price\_change events only, **no wallet attribution**) and a
`_backfill_loop` that polls `data-api.polymarket.com` every
`TRADE_OBSERVER_POLL_INTERVAL_S = 30 s`.

The WS path
(`src/observer/websocket_client.py:96-119` → `trade_observer.py:355-408`)
is throughput-light: messages are dispatched to `_record_book_metrics`
(persists a row to `book_quality_snapshots` per `book` event) or
republished to Redis pub/sub. Every WS message also takes 2 Redis
roundtrips (`SET ws:market:last_message_ts`, `INCRBY` minute bucket,
`EXPIRE`). Per `book` event we additionally persist a row
(`_persist_book_quality_snapshot` `trade_observer.py:446-510`) — that's
~15 Redis ops + 1 DB INSERT per WS book message.

The actual ingestion happens in `_backfill_loop`
(`trade_observer.py:606-619`) → `_backfill_from_data_api`
(`trade_observer.py:621-635`):

1. Iterates `_leader_wallets` **serially** (`_backfill_wallet_trades`
   `:687-703`), one HTTP call per wallet, 8 s timeout each. With 200
   leaders that's a worst-case 200 × 8 s = 26 min, best-case ~10–60 s
   sequential, before *any* trade is processed.
2. Then a single global market sweep
   (`_backfill_market_activity` `:705-730`) for `DATA_API_GLOBAL_TRADES_LIMIT
   = 1500` trades, filtered in Python.
3. For each trade, `_process_trade` (`:885-1027`) does:
   - 1× Redis `SET NX` for dedup (`_is_duplicate` `:838-840`)
   - 1× DB `INSERT INTO markets … ON CONFLICT DO NOTHING` (stub)
   - 1× DB `INSERT INTO trades_observed … RETURNING id`
   - 1× DB `SELECT … FROM markets` (re-reads the row we may have just
     written)
   - Optionally 1× UPDATE `trades_observed` to refresh category
   - Optionally 1× DB `SELECT … FROM leaders`
   - On enrichment-needed path: 1× Gamma HTTP request + 1× DB upsert
   - 1× Redis `PUBLISH trades:observed`

So **3–7 DB roundtrips and 2–4 Redis ops per trade**, all serialised
through *the same coroutine*.

`_repair_market_from_trade_hint` (`:1123-1230`) is also invoked per
trade and has its own conditional UPDATE.

### Estimated bottleneck

**DB roundtrip + Python serial loop.** The wire-time per trade is
dominated by 3–7 sequential `await conn.execute` calls (each ~1–2 ms LAN
RTT in Hetzner Helsinki, plus parse+plan). At 5 ms × 5 hops = 25 ms per
trade in the happy path; closer to 80 ms when Gamma enrichment fires.

### Latency now (estimate)

| Stage | p50 | p99 |
|---|---|---|
| External trade → data-api visible | 500 ms | 2 s |
| data-api visible → backfill polls it | 15 s | 30 s |
| Backfill picks it up → DB INSERT | 30 ms | 200 ms (Gamma path) |
| DB INSERT → Redis pub/sub published | <5 ms | <10 ms |
| **External event → reactor sees it** | **~16 s** | **~32 s** |

**The 30 s poll is the entire game.** Everything downstream measures in
tens of milliseconds.

### Bottleneck rank

**1 (worst).** This is *the* perf wall for the whole bot. WS gives book
data but no wallet → all leader-attributed signal is gated by REST polling.

### Fix proposals (ordered by ROI)

1. **Cut poll interval from 30 s to 3–5 s for the global market sweep,
   and use ETag/If-Modified-Since on `data-api`** if it supports them
   (a HEAD probe on first call confirms). Gain: **~6–10× freshness**
   (p50 16 s → 2–3 s). Effort: **S**. The wallet-by-wallet loop should
   stay at 30 s — it's expensive — but the global sweep is one HTTP call
   and is what catches >90% of leader trades.

2. **Parallelise `_backfill_wallet_trades`** with `asyncio.gather` +
   `asyncio.Semaphore(20)` instead of the current `for wallet in
   list(self._leader_wallets)`. With 200 leaders @ 8 s timeout the
   serial worst case is 26 min; capped at 20 in flight it's 80 s. Gain:
   **~16× when the API stalls**. Effort: **S**.

3. **Micro-batch DB writes.** Replace the per-trade `INSERT … RETURNING
   id` with a buffer that flushes every 100 ms or 200 rows (whichever
   first) via `executemany` or `COPY … FROM STDIN`. With ~5 ms RTT that
   alone is **~5–10× ingestion throughput** at burst. Effort: **M** —
   you keep the dedup gate before the buffer; the publish-on-Redis step
   moves to post-flush. Plus a small bounded queue between backfill and
   the writer so the WS+REST loops never block on Postgres.

4. **Collapse the read-after-write on `markets`.** `_process_trade`
   reads `SELECT question, category, … FROM markets` *immediately
   after* upserting it (`:981-988`). Use `INSERT … RETURNING …` instead
   so it's one roundtrip. Same idea for the post-update on category
   (`:1006-1014`). Gain: ~25% off `_process_trade` wall time. Effort:
   **S**.

5. **Drop `_trade_exists` DB probe in the `SOURCE_API_MARKET` dedup
   re-check** (`:906-915`). Today, every market-sweep trade that hits a
   Redis dedup hit pays an extra `SELECT 1 FROM trades_observed` to
   "verify". This is wasted: the unique index on `trades_observed`
   already protects you, and you have a `_clear_dedup_key + retry` path
   if the redis cache went cold. Effort: **S**.

6. **Make `_record_book_metrics` async-fire-and-forget.** Today every
   `book` WS event blocks the WS loop on a Postgres INSERT
   (`book_quality_snapshots`). With 1900 active markets and 100
   subscribed, that's ~50–500 inserts/min on the WS coroutine alone.
   Push it through a `asyncio.Queue(maxsize=1000)` consumed by a
   dedicated writer task; on full queue, drop oldest (this is metrics,
   not trade data). Effort: **S**.

7. **Shard the WS connection.** Today **one** `PolymarketWSClient`
   processes all leaders' subscribed markets, and the
   `async for raw in ws` loop runs in a single task — *one slow
   `_handle_ws_message` blocks every other event*. Split into 2–4 WS
   clients (sharded by `hash(token_id) % N`), each with its own consumer
   task, behind a fan-in `asyncio.Queue`. Gain: **2–4× headroom under
   burst**, eliminates head-of-line blocking. Effort: **M**.

---

## HP-2 — Leader registry refresh

### Current behavior

`src/registry/leader_registry.py:491-513` — `run()` is a sequential
loop:

```python
await self.refresh_leaderboard(conn)   # 1 Falcon call
await self.enrich_leaders(conn)         # 1 + up to 300 Falcon calls
await self.sync_markets(conn)           # 1 + up to 300 Falcon calls (+ Gamma fallback)
await self.recategorize_unknowns(conn)  # all CPU, no I/O — fast
# sleep FALCON_REFRESH_INTERVAL_S = 1800 s (30 min)
```

`enrich_leaders` (`:117-192`) iterates `LIMIT 300` stale rows
**serially** — `await self.falcon.get_wallet360(wallet)` per wallet.
`FalconClient` (`falcon_client.py:60-70`) holds a global throttle: 60
RPM (`FALCON_MAX_REQUESTS_PER_MINUTE = 60`) + a `Semaphore(1)`.

So 300 enrichments × 1 s/call (60/min throttle) = **5 minutes minimum**,
and with retries it can be 15 min. During those minutes,
`refresh_leaderboard` and `sync_markets` are *blocked*.

`sync_markets` (`:289-384`) is similarly serial — `LIMIT 300` markets,
1–2 Falcon calls + Gamma fallback each, sequential.

**Caching is good** — `FalconClient.query` checks Redis first
(`falcon_client.py:75-85`), 48 h TTL. But `falcon_no_data` wallets are
permanently excluded (`leader_registry.py:159-170`), which is fine but
not surfaced as a positive cache: every cold start re-walks the same
candidates.

The `Semaphore(1)` in `FalconClient` (`falcon_client.py:38`) is the
killer — it serialises **all** Falcon calls system-wide, not just the
ones from one path.

### Estimated bottleneck

**API-rate-limit + serialization.** With Semaphore(1), a 60 RPM throttle
forces 1 s/call no matter how much concurrency the caller wants.

### Latency now (estimate)

| Stage | Time |
|---|---|
| `refresh_leaderboard` | 1–3 s |
| `enrich_leaders` (300 stale) | 5–8 min |
| `sync_markets` (300 stale) | 5–10 min |
| `recategorize_unknowns` | < 1 s |
| **Full cycle** | **10–20 min** |

So with `FALCON_REFRESH_INTERVAL_S = 1800 s` (30 min), each cycle has
~10–20 min of work and ~10–20 min of idle. Adding a leader = wait up to
30 min for it to be enriched.

### Bottleneck rank

**3.** Painful but bounded — leaders trickle in slowly. The hot data
path doesn't depend on this freshness in the second-by-second sense.

### Fix proposals (ordered by ROI)

1. **Replace `Semaphore(1)` with `Semaphore(8)` in `FalconClient`** and
   keep the RPM limiter. Today every Falcon call is single-threaded
   *across the entire process*. The 60 RPM cap is the real ceiling, not
   the semaphore. Gain: ~4–8× concurrent-fanout speed for batches
   (rate-limit becomes the bound, not the lock). Effort: **S**.

2. **Parallelise the `enrich_leaders` loop** with
   `asyncio.gather(*[get_wallet360(w) for w in wallets], return_exceptions=True)`,
   bounded by the new semaphore + the RPM limiter. With 60 RPM and 300
   stale wallets that's still ~5 min, but it overlaps with `sync_markets`
   if you also fan out steps 2 and 3. Gain: 2× cycle wall-time. Effort:
   **S**.

3. **Diff-based leaderboard refresh.** Today `refresh_leaderboard`
   (`:28-81`) re-pulls the top-N every cycle and re-upserts every wallet.
   For a stable top-200 that's wasted writes. Compare against the cached
   list, only upsert deltas. Gain: 80–95% fewer DB writes per cycle.
   Effort: **S**.

4. **Stale-while-revalidate cache for `falcon_no_data`.** Permanently
   marking a wallet excluded is fine; what's missing is a *short-TTL
   negative cache* in Redis so a re-injected wallet (e.g. from the
   profiler's FK upsert) doesn't trigger a Falcon call **every cycle**
   for 30 min waiting for the DB flag. Effort: **S**.

5. **Move `enrich_leaders` and `sync_markets` to background work
   queues** (Redis lists or a tiny `asyncio.Queue` worker pool) so
   they don't block the next `refresh_leaderboard` tick. Cycle becomes
   "schedule work, return; workers drain at 60 RPM." Gain: registry
   responsiveness independent of queue depth. Effort: **M**.

6. **Stop scanning 7-day `trades_observed` in `sync_markets`'s SELECT
   DISTINCT** (`:298-312`). With 90-day retention and ~1M trades, this
   `SELECT DISTINCT t.market_id FROM trades_observed … LIMIT 300` is a
   bitmap scan over a huge index range. Maintain a cheap
   `markets_seen_24h` materialised view refreshed by the WS path, query
   that. Gain: 100× faster `sync_markets` planning. Effort: **M**.

---

## HP-3 — APScheduler cron jobs

### Current behavior

From `src/engine/main.py:135-165` and the `src/engine/jobs/` directory:

| Job | Trigger | Cadence | Coalesce | Max instances | Estimated runtime |
|---|---|---|---|---|---|
| `nightly_batch` | cron 03:00 UTC | daily | yes | 1 | **10–60 min**, see HP-5 |
| `redis_cleanup` | cron 04:00 UTC | daily | yes | 1 | < 30 s |
| `killswitch_sync` | interval | 300 s | yes | 1 | < 100 ms |
| `watchdog` | interval | 30 s | yes | 1 | < 200 ms |
| `refresh_thresholds` | interval | 300 s | yes | 1 | 1–5 s (DB aggregate) |
| `refresh_markets` (in `run_all.py`) | interval | 3600 s | yes | 1 | 1–3 s (HTTP + Redis) |

Scheduler wiring is sound: `Scheduler.add_cron`/`add_interval`
(`scheduler.py:91-148`) sets `coalesce=True, max_instances=1` so
clobbering can't happen *for a given job*. `_safe_run` (`:166-182`)
swallows exceptions per-job.

The risk: `nightly_batch` is **unbounded**. `scripts/batch_runner.py:142-162`
runs 8 sequential steps including `step_backfill_trades` (200 wallets ×
1 Falcon call), `step_refit_hawkes` (200 edges × scipy MLE with 5
restarts), `step_refit_error_models` (LightGBM training), and
`step_cleanup_old_trades` (`DELETE FROM trades_observed`). With
`misfire_grace_time = 600 s` (10 min) but no max-runtime, a runaway
batch can hold the engine event loop for an hour with no alarm.

`refresh_thresholds` (5 min interval) does a DB aggregate over
`leader_profiles` to compute system maturity — at 200 profiles that's
fast, but it's run on the engine event loop with no timeout.

### Estimated bottleneck

**Coroutine-on-shared-loop with no concurrency limits.** APScheduler's
AsyncIOScheduler runs each job on the engine's main loop. While
`nightly_batch` is running its scipy `minimize` calls, no other job ticks
(scipy releases the GIL but blocks the loop) — `watchdog`, `killswitch_sync`,
and `refresh_thresholds` all stall.

### Latency now (estimate)

* All interval jobs: < 200 ms p99 *outside* the nightly window.
* During `nightly_batch`: every interval job slips by minutes.

### Bottleneck rank

**5.** Doesn't block the data path day-to-day. But the
`nightly_batch` overrun risk is real and silent.

### Fix proposals (ordered by ROI)

1. **Move CPU-heavy nightly steps off the engine loop.** Run
   `step_refit_hawkes` and `step_refit_error_models` in a
   `ProcessPoolExecutor` (or a sidecar container) — scipy MLE +
   LightGBM training are exactly the kind of work that should not share
   the asyncio loop. Gain: watchdog + killswitch stay responsive
   through the night. Effort: **M**.

2. **Add `max_runtime_s` enforcement in `_safe_run`.** Wrap with
   `asyncio.wait_for(fn(), timeout=...)`. Set `nightly_batch` to 3600 s,
   intervals to 60 s. Effort: **S**.

3. **Split `nightly_batch` into independent cron jobs.** Today one
   failure midway aborts the rest of the chain. Make
   `step_refresh_registry`, `step_sync_markets`, `step_refit_hawkes`,
   `step_refit_error_models` separate APScheduler entries with
   staggered times (03:00, 03:10, 03:30, 04:30) and per-step timeouts.
   Gain: failure isolation + earlier observability. Effort: **S**.

4. **Add structured timing metrics** (Prometheus histograms) for every
   `_safe_run` invocation. Today the `logger.debug("done in %.2fs")`
   line in `scheduler.py:182` is the only signal. Without histograms
   you can't tell if `refit_hawkes` p99 is creeping up. Effort: **S**.

---

## HP-4 — Dashboard query latency

### Current behavior

`src/api/main.py:559-763` — `_get_terminal_snapshot` is the heart. Every
`/api/terminal-snapshot` (and every WebSocket `tick` push, every
`STATS_PUSH_INTERVAL_S = 1.0` s) calls `_get_terminal_snapshot()`.

The good: it's gated by `_terminal_snapshot_lock` + a 1 s TTL cache
(`TERMINAL_SNAPSHOT_TTL_S = 1.0`, `:559-579`), so concurrent dashboard
requests collapse onto one rebuild.

The bad: each rebuild calls **17 query functions in parallel** via
`asyncio.gather` (`:582-600`). Each acquires its own connection from a
pool of `min_size=2, max_size=10` (`api/main.py:87-91`). That means with
17 fan-outs, the API instantly saturates its pool, and any other
endpoint serving a request waits.

Worse, several of those queries are *very expensive*:

* `queries.overview` (`queries.py:500-665`) — runs **9 separate SQL
  statements** including a 5-table join with `ROW_NUMBER() OVER
  (PARTITION BY market_id)` over `WHERE t.time > NOW() - 20 minutes`.
  Without a covering index on `(time, market_id, is_leader)` the scan
  is wide.
* `queries.market_scanner_rows` (`queries.py:1664-1722`) — `WITH
  latest_books AS (DISTINCT ON … FROM book_quality_snapshots WHERE
  observed_at >= NOW() - INTERVAL '30 minutes')`. With a high-volume WS,
  `book_quality_snapshots` grows fast (it's already inserted per WS
  `book` event — see HP-1). Without `(market_id, token_id, observed_at
  DESC)` index that's a sort.
* `queries.alpha_extras`, `queries.equity_curve`, `queries.wallet_graph`,
  `queries.recent_observed_trades` — each their own multi-CTE query.

The 1 s TTL means even when the dashboard is idle, the `_stats_push_loop`
fires a fresh build every second whenever there's a single connected
client (`api/main.py:766-778`).

`open_positions_with_prices` (`queries.py:1530-1611`) does an N+1
SELECT: one query for open positions, then one Redis GET (or DB
fallback `SELECT … FROM trades_observed`) **per open position** to
fetch the latest price. With 10–20 open positions that's 10–20 sequential
Redis ops per dashboard build.

### Estimated bottleneck

**DB-roundtrip + connection-pool saturation under fan-out.**

### Latency now (estimate)

| Endpoint | p50 | p99 |
|---|---|---|
| `/api/terminal-snapshot` (cache hit) | < 5 ms | < 20 ms |
| `/api/terminal-snapshot` (cache miss, 17-way gather) | 100–300 ms | 800 ms |
| `/api/leaders/{wallet}` and other one-offs | 20–80 ms | 200 ms |

### Bottleneck rank

**4.** Limits dashboard scale, not core data ingestion. But the WS
push loop (1 s) means a busy dashboard *constantly* runs the 17-way
gather, which competes with the engine for DB connections.

### Fix proposals (ordered by ROI)

1. **Bump API DB pool min/max from `2/10` → `5/30`** (`api/main.py:88-91`).
   Today every dashboard tick can saturate the pool; the engine and
   observer pools are correctly sized at `2/10`, but the API serves
   17 concurrent reads per snapshot. Effort: **S**.

2. **Push instead of poll the dashboard.** Replace the 1-s `_stats_push_loop`
   that *unconditionally* rebuilds the snapshot
   (`api/main.py:766-778`) with **event-driven push**: subscribe to
   Redis pub/sub channels (`trades:observed`, `positions:closed`,
   killswitch changes) and broadcast diff-frames to connected WS
   clients. Build the full snapshot only on dashboard reconnect. Gain:
   ~95% fewer DB cycles when the dashboard is open. Effort: **M**.

3. **Move the snapshot build to a background task.** A single coroutine
   rebuilds `_terminal_snapshot_cache` on a Redis-pub/sub trigger
   (debounced to ≥ 1 s); HTTP and WS handlers read the cache,
   never trigger a rebuild. Effort: **M**.

4. **Index audit.** Add (or verify):
   - `trades_observed (time DESC, market_id, is_leader)` for
     `overview` and `market_scanner_rows`
   - `book_quality_snapshots (market_id, token_id, observed_at DESC)`
   - `paper_trades (status, opened_at DESC)` partial on `status='open'`
   - `decision_log (time DESC)` and `(leader_wallet, time DESC)`
   Effort: **S**, but requires running `EXPLAIN (ANALYZE, BUFFERS)` on
   each big query first.

5. **Replace `open_positions_with_prices` N+1.** Pre-cache last-trade
   prices in a Redis hash (one HGETALL per snapshot instead of N GETs)
   and remove the DB fallback from the per-position loop — do it once
   in a single SQL with `LATERAL`. Gain: ~10× faster on snapshots
   with many open positions. Effort: **S**.

6. **Reduce snapshot fan-out.** Merge tightly-coupled queries
   (e.g. `overview` and `equity_curve` both touch `paper_trades`)
   into a single query that returns multiple result sets via `WITH …`.
   Effort: **M**.

---

## HP-5 — Hawkes nightly batch

### Current behavior

`src/graph/hawkes_fitter.py:62-118` — `fit_edge`:

1. Two DB queries per edge: leader trade timestamps + follower trade
   timestamps over `HAWKES_LOOKBACK_DAYS = 30` days.
2. `_fit` (`:120-143`) calls `scipy.optimize.minimize` with **5 random
   restarts** of L-BFGS-B, max 200 iterations each.
3. The likelihood `hawkes_log_likelihood` (`:17-52`) iterates timestamps
   in a **pure Python `for i in range(n)` loop** with `np.exp`/`np.log`
   inside. For n=500 trades × 200 iterations × 5 restarts = 500k Python
   ops per edge.

`run_batch` (`:145-192`) iterates `BATCH_HAWKES_LEADERS = 200` edges
**serially** and *opens a fresh DB connection per fetch and per
update* — 3 connection acquisitions per edge.

### Estimated bottleneck

**CPU + Python overhead in the likelihood inner loop.** L-BFGS-B with
500 ops/iter × 200 iter × 5 restarts × 200 edges = ~200 M ops, plus
600 DB roundtrips.

### Latency now (estimate)

* Per edge: 0.5–3 s.
* Full batch: **2–10 min** at 200 edges.

### Bottleneck rank

**6.** Runs once a day, off the hot path. But it occupies the engine
loop (see HP-3) and gets worse linearly with edge count.

### Fix proposals (ordered by ROI)

1. **Vectorise `hawkes_log_likelihood`.** The recursive `excitation`
   has a closed-form vectorised computation: build the lower-triangular
   `dt[i,j] = t[i]-t[j]` only for `j<i` and use cumulative `np.exp`
   tricks. Or use the `tick.hawkes` library which is C++. Gain:
   **20–100×** for the inner kernel. Effort: **M**.

2. **Drop random restarts to 2 and reduce maxiter to 100.** The
   problem is convex enough in practice; 5 restarts × 200 iter is
   over-engineered. Gain: 2.5× per edge. Effort: **S**.

3. **Run the batch in a `ProcessPoolExecutor`** keyed by edge. Each
   worker fits one edge; main loop coordinates. Gain: ~Ncpu× wall time.
   Effort: **S**.

4. **Reuse a single DB connection for the whole batch** instead of
   `async with get_db() as conn:` inside the loop body
   (`:172-184`). Effort: **S**, mostly mechanical.

5. **Incrementalise.** Today the Hawkes fit is recomputed from scratch
   over a 30-day window. Cache the previous `(mu, alpha, beta)` and
   warm-start subsequent fits — for stable edges this converges in
   ~10 iterations. Gain: 5–10× when edges are stable. Effort: **L**.

---

## HP-6 — Behavior profiler hot path

### Current behavior

`src/profiler/behavior_profiler.py:104-136` — runs **two pubsub
subscribers concurrently**:

* `_subscribe_positions_loop` (every position close) → `on_position_closed`
* `_subscribe_trades_loop` (every observed trade) → `on_leader_trade`

`on_leader_trade` (`:206-251`) is invoked **for every observed trade**.
It does:

1. `_load_profile` — 1 DB SELECT.
2. `_update_decision_process` — pure Python.
3. `_save_profile` — 1 DB upsert into `leader_profiles` (writes the
   *entire* `profile_json` every time).

So **2 DB roundtrips per observed trade**, in addition to whatever
HP-1 already paid. At ~1 trade/s today that's fine; at 10/s it's
contention.

`on_position_closed` (`:138-204`) is heavier: `_load_profile`,
`_count_confirmed_followers`, `_build_error_trade_context`, several
`_update_*`, then `_save_profile` + `error_model.update`. ~3–4 DB ops
per close. Closes are rare (~0.01/s expected) so this is fine.

KDE / size-weighted updates are O(1) per update via the EWMA — **no
quadratic blow-up** despite the CLAUDE.md note about KDE timing. The
KDE is only built periodically, not per-trade. Confirmed.

### Estimated bottleneck

**DB write amplification.** Every leader trade rewrites the whole
`profile_json` blob (could be 10–50 KB) — at 10 trades/s that's a lot
of WAL.

### Latency now (estimate)

* `on_leader_trade`: 2–10 ms per call (pool acquire + 2 roundtrips +
  JSON serialise).
* `on_position_closed`: 10–30 ms per call.

### Bottleneck rank

**7.** Currently fine. Becomes a problem under 10× ingestion load.

### Fix proposals (ordered by ROI)

1. **Coalesce profile writes per wallet.** Hold the dirty-profile in an
   in-memory dict, flush to DB every 5 s or on `positions:closed` event
   (whichever first). Gain: 10–50× fewer DB writes under burst.
   Effort: **M**.

2. **Use partial JSONB updates.** Replace the full `profile_json =
   $2::jsonb` upsert with `profile_json = profile_json || $2::jsonb`
   and only send the changed sub-document. Gain: 3–10× smaller
   payload. Effort: **M**.

3. **Decouple the two subscribers.** Today both pubsub loops live
   under `BehaviorProfiler.start()` and a slow DB write in one starves
   the other. Run them as separate watchdog-supervised tasks. Effort:
   **S**.

---

## HP-7 — Backups (pg_dump)

### Current behavior

`src/backups/dumper.py:53-134` — shells out to `pg_dump --format custom
--compress 9 --no-owner --no-acl`. `BACKUP_HOUR_UTC = 5` (after nightly
batch + Redis cleanup). `BACKUP_PG_DUMP_TIMEOUT_S = 1800` (30 min ceiling).

`pg_dump` in custom format with `--compress 9` is **single-process,
single-CPU**, and walks every table in catalog order. It does **not**
hold an exclusive lock on tables — it uses a transaction snapshot
(`REPEATABLE READ`) that lets writers proceed. So **ingestion is not
blocked**, but:

* `--compress 9` is CPU-heavy and lengthens the dump window
  (compression on the dumping process, not parallel).
* The transaction snapshot pins WAL — if ingestion is heavy, the dump
  prevents `VACUUM` from cleaning up dead tuples until it finishes.
* `BACKUPS_ENABLED = false` by default — currently idle.

### Estimated bottleneck

**CPU (compression) + autovacuum interference window.** Dump duration
grows linearly with DB size. At 50 MB it's <30 s; at 5 GB it'll be
5–10 min.

### Latency now (estimate)

* Dump itself: 30 s to 30 min, scales with DB size.
* Live ingestion lag: 0 (pg_dump uses MVCC).
* Vacuum stall: equal to dump duration.

### Bottleneck rank

**8 (lowest).** No live impact today. Watch for table bloat.

### Fix proposals (ordered by ROI)

1. **Use `--jobs N` with directory format** (instead of custom). Splits
   the dump across N CPUs. Gain: ~Ncpu×. Effort: **S**, but requires a
   shared filesystem upload step.

2. **Drop `--compress` to 5.** Level 9 is ~5% smaller than level 5 but
   2× the CPU. Effort: **S**.

3. **Switch to physical streaming backups (`pg_basebackup` + WAL
   shipping)** for true point-in-time recovery without a per-day
   snapshot window. Effort: **L**, only worth it past 5 GB.

---

## Cross-cutting wins

### Connection-pool sizing

| Component | Current | Recommended | Reason |
|---|---|---|---|
| `engine` | `2 / 10` | `4 / 20` | scheduler jobs + watchdog can fan out |
| `observer` | `2 / 10` | `4 / 20` | every trade does 3–7 DB ops |
| `registry` | `2 / 10` | `2 / 10` | sequential by design |
| `api` | `2 / 10` | `5 / 30` | 17-way snapshot fan-out |

For Redis: today the code uses
`redis_async.from_url(settings.REDIS_URL, decode_responses=True)`, which
gives an unbounded connection pool. On Hetzner with redis-7.2 this is
fine; consider explicit
`max_connections=50` per service to surface bursts as errors instead of
unbounded latency.

### Prepared-statement cache

asyncpg auto-prepares per-connection. The hot statements
(`INSERT INTO trades_observed …`, `SELECT … FROM markets WHERE
market_id = $1`) are good candidates. Verify with
`pg_stat_statements`. If `total_plan_time / total_exec_time > 5%`,
asyncpg's per-connection cache may be cold-starting too often — you can
keep connections warm by **not** acquiring/releasing on every query
(today every `_process_trade` opens 1+ contexts). The micro-batch fix
in HP-1 #3 also fixes this.

### Batch-write windows

Recommended: **flush trades to DB every 100 ms or 200 rows**, whichever
first, via `executemany` against `trades_observed`. Pseudocode:

```python
class TradeWriter:
    def __init__(self):
        self._buf = []
        self._lock = asyncio.Lock()

    async def submit(self, row):
        async with self._lock:
            self._buf.append(row)
            if len(self._buf) >= 200:
                await self._flush()

    async def _flush(self):
        rows, self._buf = self._buf, []
        async with get_db() as conn:
            await conn.executemany(INSERT_SQL, rows)
        # publish redis events post-flush
```

Plus a periodic 100 ms flush task.

### Backpressure when WS outpaces DB

Today there is **none**. The WS coroutine `await`s the full
`_handle_ws_message` chain inline; if Postgres slows to 100 ms/op the
WS event loop just queues messages internally with no upper bound, and
on a real burst it OOMs.

**Recommended:**

1. WS handler enqueues to a **bounded** `asyncio.Queue(maxsize=10_000)`.
2. A consumer task pulls from the queue and runs the heavy work.
3. On `QueueFull`: drop the *oldest* message (book/price events are
   regenerated next tick anyway), increment a `dropped_messages_total`
   counter, alert if > 1% over 1 min.
4. Trade writes (HP-1) **never** drop — they go through the dedicated
   `TradeWriter` whose backpressure signal is "queue full → log warn,
   wait". Different SLOs for different data classes.

### Observability gaps

Missing metrics that we'd need to validate any of these wins:

| Metric | Where | Today |
|---|---|---|
| `trade_observer_lag_s` (data-api ts vs ingest ts) | per-trade histogram | not measured |
| `trade_observer_db_write_seconds` | per-trade histogram | not measured |
| `falcon_request_seconds` (per agent_id) | histogram | one-off `logger.warning` |
| `falcon_cache_hit_ratio` | gauge | inferred from logs |
| `ws_message_queue_depth` | gauge | not present (no queue) |
| `ws_messages_dropped_total` | counter | not present |
| `scheduler_job_duration_seconds` (per `name`) | histogram | `logger.debug` only |
| `db_pool_acquire_seconds` | histogram | not measured |
| `db_pool_in_use` | gauge | not measured |
| `terminal_snapshot_build_seconds` | histogram | only the build_ms field on the snapshot itself |
| `redis_pubsub_lag_s` (publish→consume) | histogram | not measured |

Recommend: **`prometheus_client` (already removed per CLAUDE.md §8 —
re-add it) on a `/metrics` endpoint behind the API container, plus
OpenTelemetry spans on `_process_trade`, `_get_terminal_snapshot`,
`enrich_leaders`, and the nightly batch step boundaries.** Without
these you cannot validate that any of the proposed fixes actually move
the p99.

---

## Phase 1 — ship next week (highest ROI, lowest risk)

1. **HP-1 #1**: Drop the global market sweep poll to 5 s. (Single line
   in `config.py` + verify Polymarket data-api rate limits accept it.)
2. **HP-1 #2**: Parallelise `_backfill_wallet_trades` via
   `asyncio.gather` with `Semaphore(20)`.
3. **HP-1 #4**: Collapse the read-after-write on `markets` in
   `_process_trade`.
4. **HP-1 #5**: Delete `_trade_exists` re-check in dedup hit path.
5. **HP-2 #1 + #3**: `Semaphore(8)` in `FalconClient`; diff-based
   leaderboard upsert.
6. **HP-3 #2**: Add `asyncio.wait_for` timeouts to every `_safe_run`
   invocation.
7. **HP-4 #1**: Bump API pool to `5/30`.
8. **Add basic Prometheus metrics** for `trade_observer_lag_s`,
   `falcon_request_seconds`, `scheduler_job_duration_seconds`,
   `terminal_snapshot_build_seconds`. You can't tune what you can't
   measure.

**Expected end-state:** trade-to-react p50 drops from ~16 s to ~3 s.
Registry cycle drops from 10–20 min to 5–8 min. No new failure modes.

## Phase 2 — month 1

9. **HP-1 #3**: Implement the `TradeWriter` micro-batch + bounded
   `asyncio.Queue`. Backpressure becomes real.
10. **HP-1 #6**: Move `_record_book_metrics` DB inserts off the WS
    loop.
11. **HP-2 #2**: Parallel enrichment + `sync_markets` overlap.
12. **HP-3 #1**: Move scipy/LightGBM steps to `ProcessPoolExecutor`.
13. **HP-3 #3**: Split `nightly_batch` into 4 staggered cron entries.
14. **HP-4 #2**: Replace 1-s WS polling with pub/sub-driven push
    diffs.
15. **HP-4 #4**: Index audit + `EXPLAIN ANALYZE` on the top 5
    snapshot queries.
16. **HP-5 #2 + #3**: Tone down Hawkes restarts; run in process pool.
17. **HP-6 #1**: Coalesce profile writes per wallet.

**Expected end-state:** trade-to-react p50 ~1 s, p99 ~3 s. 10×
ingestion headroom. Dashboard queries unaffected by ingestion load.

## Phase 3 — the "evolved form" (3 months out)

18. **HP-1 #7**: Shard the Polymarket WS connection (2–4 clients,
    fan-in queue). Eliminates head-of-line blocking.
19. **HP-2 #5 + #6**: Background work-queue model for registry.
    Materialised `markets_seen_24h` view.
20. **HP-4 #3**: Snapshot rebuilt by a single background coroutine,
    pub/sub-driven, served from cache to all readers.
21. **HP-5 #1**: Vectorise the Hawkes likelihood (or adopt the `tick`
    library).
22. **HP-5 #5**: Incremental Hawkes warm-starts.
23. **HP-7 #1**: Parallel `pg_dump` with `--jobs N` once DB > 1 GB.
24. **OpenTelemetry tracing end-to-end**: every `trades:observed`
    event carries a trace context propagated through profiler,
    confidence engine, paper trader. You can finally answer "why did
    this signal take 4 s to fire?" without grepping logs.
25. **Latency SLOs in CI**: assertions in load tests that p99
    trade-to-react < 5 s and p99 snapshot build < 200 ms.

**Expected end-state:** sub-second trade-to-react, 100× ingestion
headroom, full distributed tracing, no single coroutine on the hot path.
