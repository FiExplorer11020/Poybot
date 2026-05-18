# WS Lag Diagnosis — A7 batch-1 (2026-05-18)

> **Scope**: Investigate the `ws lag 46.2s` / `ws lag 82.9s` shown by the
> V1 terminal topbar on the WalletGraph and ML Progression pages in prod
> (`http://89.167.23.215:8080`).
>
> **Mode**: read-only. No applicative code touched.

---

## TL;DR

The "ws lag X s" label in the V1 topbar is **mis-named**. It does NOT
read `ws:market:last_message_ts` directly; with the current snapshot
shape it falls through to `ingestion.avg_freshness_ms`, which is the
**mean age of the last book_quality_snapshot row across the 60 monitored
markets**. That mean is dominated by 1–8 low-activity markets whose
freshness routinely exceeds 100s, so the label inflates well above the
true WS staleness.

The actual WS feed is healthy: live samples show `clob_lag_ms ≈ 19–289ms`
and `~2872–3527 msgs/min` (~48–59 msg/s sustained). The topbar widget is
the bug, not the WS pipeline.

There IS a real, secondary problem: the maintenance-loop snapshot
rebuild takes **130–161 s** per cycle (target 30s), which compounds the
mis-labelled lag visually (the snapshot served to the browser is itself
80–130s old). This is a snapshot-builder problem, not a WS problem.

**Confidence**: HIGH on the root cause of the user-visible label.
MEDIUM-HIGH on the secondary snapshot-cycle issue.

---

## 1. Pipeline map (read-only)

### 1.1 WS → Redis → DB
```
wss://ws-subscriptions-clob.polymarket.com/ws/market  (4 shards)
  └─ src/observer/websocket_client.py
       PolymarketWSClient._connect_and_run()  (websocket_client.py:353-395)
       └─ `async for raw in ws:`                   (line 366)   ← serial; the next
                                                                  message waits for
                                                                  on_message to return
            ├─ self.messages_received += 1
            ├─ self.last_message_at = time.time()
            ├─ _ws_heartbeat()
            └─ await self._on_message(item)            (line 387 or 389)
                 = TradeObserver._handle_ws_message    (trade_observer.py:1309)

_handle_ws_message  (trade_observer.py:1309-1380):
  1. SET ws:market:last_message_ts  =  now            (line 1320)  ← TIMESTAMP WRITE
                                                                     happens FIRST,
                                                                     before any
                                                                     pipeline work
  2. INCRBY ws:msgs:minute:<bucket>                   (line 1326)
  3. EXPIRE ws:msgs:minute:<bucket>                   (line 1327)
  4. SET observer:ws:last_msg:<channel>               (line 1336)
  5. SET observer:ws:last_msg:any                     (line 1342)
  6. If event_type=='trade':   _process_legacy_ws_trade (line 1349)
     If event_type=='price_change':
       PUBLISH market:price_changes                   (line 1355)
       For each change: SETEX price:<m>:<t>           (line 1373)
     If event_type=='book':   _record_book_metrics    (line 1379) ← BLOCKING
                                                                    inline DB INSERT
                                                                    (see §1.2)

Trade ingestion is non-blocking (writer queue + asyncio batch loop):
  _process_legacy_ws_trade → _process_trade → write_queue.put()
  _db_writer_loop drains 200 rows / 100ms, separate task
                                                    (trade_observer.py:785-870)
```

### 1.2 Book quality persistence — the hot path

```
_record_book_metrics(msg)                         (trade_observer.py:1482-1533)
  ├─ p95 sliding window on book_age_samples
  ├─ SETEX metrics:book_age_p95_s                  (line 1491)
  ├─ SETEX book:last:<market>:<token>              (line 1499)
  └─ await _persist_book_quality_snapshot(...)     (line 1520)
       └─ async with get_db() as conn:             (line 1455)
             await conn.execute(
                 "INSERT INTO book_quality_snapshots (…)" )   (line 1456)
                 ← partial UNIQUE INDEX on (market_id, token_id, source_timestamp)
                   forces a B-tree check per insert (migration 019).
```

This INSERT is **synchronous w.r.t. the WS read loop**. While it
`await`s on a pool connection + Postgres ack, the `async for raw in ws`
of that shard cannot pop the next message off the websockets internal
buffer. The next message's `last_message_ts` SET (step 1) is delayed by
exactly the duration of the inline INSERT path.

### 1.3 Consumers of `ws:market:last_message_ts`

| File                            | Line | Purpose                                  |
|---------------------------------|------|------------------------------------------|
| src/api/main.py                 | 337  | `/api/overview` health block             |
| src/api/queries.py              | 2117 | `data_quality()` snapshot                |
| src/api/queries.py              | 2787 | inspector_snapshot pipeline block        |
| src/api/queries.py              | 4414 | `pipeline_status()` ws_status field      |
| src/api/snapshot_builder.py     | 151  | maintenance-loop snapshot builder        |

All read the key as `max(0.0, now.timestamp() - float(ts))`. The math
is identical everywhere; only the wall-clock at read-time differs.

### 1.4 How the topbar widget computes "ws lag"

```jsx
// static/dashboard/dashboard-app.jsx:371-385
const lag = snapshot?.ingestion?.ws_last_message_age_s
         ?? snapshot?.ingestion?.avg_freshness_ms;
```

The backend currently exposes `snapshot.ingestion.sources[0].lag_ms`
(CLOB WebSocket source, derived from `health.last_message_age_s` —
i.e. the real WS lag) and `snapshot.ingestion.avg_freshness_ms` (the
mean book-snapshot age across the markets table). It does NOT expose
`snapshot.ingestion.ws_last_message_age_s`. So the JS expression
always falls through to `avg_freshness_ms`.

Source for `avg_freshness_ms`: `src/api/terminal_snapshot.py:406`,
`_mean([row['freshness_ms'] for row in market_rows])`. Each
`freshness_ms` = `now - last book_quality_snapshots.observed_at` for
that market (see `market_scanner_rows` in
`src/api/queries.py:1715-1779`).

---

## 2. Question (a) vs (b): when is `last_message_ts` written?

**Answer: (a) — at the very top of `_handle_ws_message`, before any
pipeline work.** See `trade_observer.py:1320`.

> ```python
> async def _handle_ws_message(self, msg: dict) -> None:
>     event_type = msg.get("event_type", "")
>     if self._redis:
>         try:
>             now_ts = time.time()
>             await self._redis.set("ws:market:last_message_ts", str(now_ts), ex=300)
>             ...
> ```

Therefore `last_message_age_s = now - ws:market:last_message_ts`
measures **upstream + asyncio-buffer delay**, NOT the internal Redis
pipeline / DB write cost of the previous message. The interpretation:

* If `last_message_age_s` is high → upstream is slow, OR the previous
  iteration of the WS read loop is still `await`ing on the inline
  book_quality INSERT and the next message is stuck in the websockets
  client buffer.
* If `last_message_age_s` is low (live: 19–289 ms in our samples) but
  the topbar shows a large value → the topbar is NOT reading the WS
  ts; it's reading something else (`avg_freshness_ms`).

---

## 3. Live measurements (taken 2026-05-18 ~22:25–22:31 UTC)

`/api/v1/live-summary` (Redis-backed, fast endpoint):

| Field                                | Value         |
|--------------------------------------|---------------|
| `bot.latency_ms`                     | 19 → 289 ms   |
| `bot.cycle_latency_ms`               | **130s, 161s, 91s** |
| `ingestion.sources[CLOB].lag_ms`     | 19–289 ms     |
| `ingestion.sources[CLOB].messages_last_minute` | 2872 (≈ 48 msg/s) → 3527 (≈ 58 msg/s) |
| `ingestion.updates_last_minute`      | 2872 / 3527 / 1681 |
| `ingestion.avg_freshness_ms`         | **17 753 → 24 045 → 24 045 ms** |
| `ingestion.live_markets`             | 52 / 60       |
| `meta.leaders_active`                | 2 565         |

Per-market freshness distribution (60 rows, snapshot at 22:31:15):

| Stat | Value (ms) |
|------|-----------:|
| min  | 848        |
| p50  | 9 040      |
| p95  | **107 549** |
| max  | 108 274    |
| mean | 24 045     |

Inspector snapshot (`/api/inspector/snapshot`):

| Field                           | Value   |
|--------------------------------|---------|
| `pipeline.ws_last_message_age_s` | 0.0 (clamped — actually <0.5s) |
| `pipeline.ws_msgs_per_min`       | **null** — see note below |
| `pipeline.trades_pubsub_subscribers` | 7    |
| `counters.trades_1h`             | 4 528 (= 1.26 trades/s via REST polling)  |
| `counters.leader_trades_1h`      | 303    |

> **Side-bug**: `inspector_snapshot` reads
> `ws:market:msgs_per_min` (queries.py:2790) but the producer writes
> `ws:msgs:minute:<bucket>` (trade_observer.py:1326). Key mismatch →
> `ws_msgs_per_min` is permanently null in the inspector view. Cosmetic,
> not the root cause of the visible lag.

`/api/overview` was repeatedly timing out at 15s during measurement —
its `COUNT(DISTINCT (market_id, token_id))` on `book_quality_snapshots`
(main.py:1239) is the prime suspect for that, but again not the WS
issue.

---

## 4. Root cause hypothesis

### 4.1 The user-visible label — HIGH confidence

The "ws lag 46.2s / 82.9s" string is the topbar widget reading
`snapshot.ingestion.avg_freshness_ms`, NOT the WS lag. The fall-through
chain in `dashboard-app.jsx:372` is:

```js
snapshot?.ingestion?.ws_last_message_age_s ?? snapshot?.ingestion?.avg_freshness_ms
```

The backend never populates `ingestion.ws_last_message_age_s` in the
snapshot shape (the canonical field is
`ingestion.sources[0].lag_ms`, plus `bot.latency_ms` from the V1
overview path). The widget therefore ALWAYS falls back to
`avg_freshness_ms`, which is the **mean age of the last book snapshot
across 60 markets**, dominated by 1–8 low-activity markets whose
freshness exceeds 100s (one example observed live: an Iran-airspace
market at 53s, msgs/min=0, observations_5m=null — i.e. just an
illiquid market, not a pipeline fault).

The WalletGraph (46.2s) vs ML Progression (82.9s) split is consistent
with **two distinct snapshot reads at different rebuild ages**: the
snapshot itself can be 30–160s old (see §4.2), and the per-market
freshness already-baked-into the snapshot is then read at very
different wall-clock times by the two views. The label compounds
"snapshot age × per-market freshness".

### 4.2 The secondary problem — MEDIUM-HIGH confidence

The maintenance loop rebuilds `/api/v1/live-summary` every 30s
(scripts/maintenance_loop.py:114, `LIVE_SUMMARY_INTERVAL_S = 30.0`),
but `bot.cycle_latency_ms` reports **91 000 – 161 440 ms**. The loop
runs back-to-back rebuilds with NO 30s sleep gap, so the Redis snapshot
served to clients is ~90–160s old in steady state. The dashboard
polls at 3s intervals but the underlying data is itself stale.

Root cause of the 130s+ rebuild: `build_terminal_snapshot()` fans 17
SQL queries through `asyncio.gather` on the maintenance pool (default
`DB_POOL_MAX=25`); one of them (`COUNT(DISTINCT (market_id, token_id))
FROM book_quality_snapshots WHERE observed_at >= NOW() - INTERVAL '15
seconds'` in `api_overview` and similar in `market_scanner_rows`'s
`DISTINCT ON` over the last 30 minutes of `book_quality_snapshots`) is
expensive — `book_quality_snapshots` grows at roughly
`messages_per_min / 60 ≈ 50` rows/s (~ 4.3 M rows/day). Coupled with a
B-tree-backed partial UNIQUE index (migration 019), every insert pays a
deduplication check, and the DISTINCT ON aggregation scans the recent
partition.

### 4.3 What the WS pipeline IS doing well

* `last_message_ts` is set BEFORE any DB work, so the metric does NOT
  conflate upstream lag with internal pipeline lag.
* Trade inserts go through an async write queue + dedicated writer
  loop — they do NOT block the WS read coroutine.
* Live measurements confirm `clob_lag` of 19–289ms — the WS feed
  itself is healthy.

### 4.4 What COULD go wrong but isn't dominant right now

The inline INSERT in `_record_book_metrics → _persist_book_quality_snapshot`
(trade_observer.py:1520) is the only DB write that BLOCKS the WS read
coroutine. At 48–60 msg/s sustained, split across 4 shards (≈ 12–15
msg/s/shard), with an INSERT cost of ~3–10 ms (small table, no triggers,
1 partial UNIQUE index), the shard coroutine spends 36–150 ms per
second on this path. That's NOT saturating, but it means a brief
Postgres latency spike (e.g. a maintenance VACUUM, a heavy concurrent
read on `book_quality_snapshots`) would cascade into a WS read backlog.
Suspected secondary contributor, not the primary visible bug.

---

## 5. Recommendation

### 5.1 Fix the topbar label — TRIVIAL, < 30 min

Two equally valid options for the patching agent:

**(A) Make the label honest** — read the actual WS lag the user expects.
Either populate `snapshot.ingestion.ws_last_message_age_s` from
`health.last_message_age_s` in `src/api/terminal_snapshot.py:_build_ingestion`
(add the field next to `avg_freshness_ms`), OR change the JSX to read
`snapshot?.ingestion?.sources?.[0]?.lag_ms / 1000`. Backend change is
cleaner because it survives field renames. ~10 LOC.

**(B) Rename the label** — change the topbar text from "ws lag" to
"avg market freshness" / "stale" so it matches what's actually shown.
Cosmetic-only. ~3 LOC.

Recommendation: **(A) backend-side**, with a fallback. The
`ws_last_message_age_s` semantic ("how stale is the WS feed itself")
is the more useful operator signal; `avg_freshness_ms` is dominated by
low-activity markets and is not a pipeline-health indicator.

### 5.2 Fix the snapshot rebuild cycle — MEDIUM, ~2–4 h

Out of scope for a "WS lag" diagnosis but worth flagging. The 130–160s
rebuild blocks the dashboard from showing fresh data regardless of how
fast the WS is. Two levers:

* The `COUNT(DISTINCT (market_id, token_id))` in `api_overview`
  (main.py:1239) and the `DISTINCT ON` LATERAL in `market_scanner_rows`
  (queries.py:1719) over the last 30 min of `book_quality_snapshots`
  are the prime candidates. With ~4.3 M rows/day, a 30-minute window
  scans ~90 K rows on every snapshot rebuild. Move to a 1-min rolling
  Redis hash maintained by the observer, or persist a `latest_book`
  table updated on each insert.
* `book_quality_snapshots` has no partitioning. With 4.3 M rows/day
  and 90-day retention, that's ~390 M rows total. Range partitioning by
  `observed_at` (daily) would let DISTINCT ON queries prune ~99% of the
  scan.

### 5.3 Defensive: bound the inline book INSERT — LOW-MEDIUM, ~1 h

To insulate the WS read coroutine from Postgres latency spikes, move
the `book_quality_snapshots` insert behind a bounded async queue (same
pattern as `_db_writer_loop` for trades). Drop on full + counter, never
block the WS read. ~50 LOC. Reduces tail latency under DB contention
but doesn't change steady-state behaviour.

---

## 6. Risk of NOT fixing

* **The label**: operators continue mis-reading dashboard health.
  Visible 46/83s "ws lag" suggests broken ingest, but the bot is
  actually receiving 50–60 msg/s in real time. Risk = wasted on-call
  hours chasing a phantom + erosion of trust in the dashboard.
* **The 130s snapshot cycle**: decisions / risk / portfolio panels are
  effectively 2-3 min behind reality. Acceptable today (paper-only
  mode, no live trading gated on snapshot freshness), but BLOCKING for
  any future live-trading mode where a halt decision needs sub-30s
  surface.
* **The defensive book-INSERT change**: not currently a problem.
  Latent risk under future PG load (large analytic query, vacuum
  on 390M-row `book_quality_snapshots` partition). Low priority until
  any of those triggers fire.

---

## 7. Files referenced (absolute paths, all read-only here)

| Path                                                          | Lines        | Why it matters                                   |
|---------------------------------------------------------------|--------------|--------------------------------------------------|
| `polymarket-bot/src/observer/trade_observer.py`               | 1309-1380    | `_handle_ws_message` — WS timestamp write        |
| `polymarket-bot/src/observer/trade_observer.py`               | 1482-1533    | `_record_book_metrics` — inline DB INSERT        |
| `polymarket-bot/src/observer/trade_observer.py`               | 1417-1480    | `_persist_book_quality_snapshot` — actual INSERT |
| `polymarket-bot/src/observer/websocket_client.py`             | 353-395      | WS read loop — serial async-for                  |
| `polymarket-bot/src/api/terminal_snapshot.py`                 | 390-443      | `_build_ingestion` — exposes lag fields          |
| `polymarket-bot/src/api/terminal_snapshot.py`                 | 406          | `avg_freshness_ms = mean(market.freshness_ms)`   |
| `polymarket-bot/src/api/queries.py`                           | 1715-1773    | `market_scanner_rows` — heavy DISTINCT ON        |
| `polymarket-bot/src/api/queries.py`                           | 2117, 2789, 4414 | All three readers of `ws:market:last_message_ts` |
| `polymarket-bot/src/api/queries.py`                           | 2790         | **Bug**: reads `ws:market:msgs_per_min` (wrong key) |
| `polymarket-bot/src/api/main.py`                              | 1239         | `COUNT(DISTINCT) … book_quality_snapshots` (slow) |
| `polymarket-bot/src/api/snapshot_builder.py`                  | 151          | Snapshot builder reads `ws:market:last_message_ts` |
| `polymarket-bot/static/dashboard/dashboard-app.jsx`           | 369-385      | Topbar "ws lag" widget — the user-visible label  |
| `polymarket-bot/scripts/maintenance_loop.py`                  | 114, 1340-1354 | `LIVE_SUMMARY_INTERVAL_S=30s` & rebuild call    |
| `polymarket-bot/docs/migrations/019_book_quality_snapshots_unique.sql` | all          | Partial UNIQUE index on the inserted table       |

---

*Diagnosis only — no applicative code modified. A7 batch-1, 2026-05-18.*
