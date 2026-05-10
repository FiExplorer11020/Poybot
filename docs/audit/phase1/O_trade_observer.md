# Phase 1 Task O — Trade Observer Hot Path

> **Audit traceability**: HP-1 in `docs/audit/04_perf_hotpaths.md`. The single
> highest-leverage perf change in Phase 1.
> Goal: bring median leader-trade-to-react latency from ~16s down to ~2-3s.
>
> **Status**: code, tests, instrumentation landed. This summary doc was written
> post-hoc after the implementing agent was killed mid-doc-write. Code is the
> source of truth; this file points at it.

---

## Change at a glance

Four sub-changes, all in `src/observer/trade_observer.py` + `src/config.py`:

1. **Poll cadence**: `TRADE_OBSERVER_POLL_INTERVAL_S` 30 → **5** (with new `_MIN=1` / `_MAX=60` validators).
2. **HTTP conditional GET**: `_last_etag` captured from REST responses, sent back as `If-None-Match` on the next cycle. 304 short-circuits the cycle.
3. **Micro-batch DB writes**: `_process_trade` now enqueues a `_TradeRecord` to a bounded `asyncio.Queue` (`TRADE_OBSERVER_QUEUE_MAX=10_000`). A dedicated `_db_writer_loop` drains the queue in batches of up to `TRADE_OBSERVER_BATCH_MAX=200` rows or `TRADE_OBSERVER_BATCH_FLUSH_MS=100` ms, whichever comes first. Each batch commits as one `async with conn.transaction():`.
4. **Backpressure**: producer uses `asyncio.wait_for(queue.put(...), timeout=1.0)`. On timeout the trade is dropped and `observer_queue_drops_total{reason="queue_full"}` increments. Better to drop than to deadlock the WS coroutine.

Plus full Prometheus instrumentation against the Task M contract.

---

## New config constants

| Name | Default | Bounds | Purpose |
|------|---------|--------|---------|
| `TRADE_OBSERVER_POLL_INTERVAL_S` | **5** (was 30) | `[POLL_MIN, POLL_MAX]` | REST poll cadence on `data-api.polymarket.com/trades` |
| `TRADE_OBSERVER_POLL_INTERVAL_S_MIN` | 1 | int | Lower validator bound |
| `TRADE_OBSERVER_POLL_INTERVAL_S_MAX` | 60 | int | Upper validator bound |
| `TRADE_OBSERVER_QUEUE_MAX` | 10_000 | int | Bounded `asyncio.Queue` capacity (trade records) |
| `TRADE_OBSERVER_BATCH_MAX` | 200 | int | Max rows per `executemany`/multi-row INSERT |
| `TRADE_OBSERVER_BATCH_FLUSH_MS` | 100 | int | Soft flush deadline if batch hasn't filled |

All env-overridable. Validators in `src/config.py` lines ~362-378.

---

## Design notes

### Why bound the queue at 10_000

At 5s poll + bursty market activity, observed peak rate is in the
hundreds of trades/sec. 10_000 gives ~10 seconds of buffer at a sustained
1k trades/sec — well above realistic peak. Beyond that, dropping is correct:
the WS coroutine must not block, and pipeline backpressure is signaled to
the operator via `observer_queue_drops_total`.

### Why 200 rows / 100 ms batch window

The audit estimated 5-10× ingestion-throughput gain from micro-batching.
With ~30 trades/sec sustained, 200 rows fills in ~7s — way over the
100ms flush deadline. So in steady state every batch is **flush-timed**,
not size-capped, giving a max-100ms tail latency on writes. Under burst,
size-capped batches at 200 rows commit in a single transaction roundtrip
instead of 200 individual roundtrips.

### Producer/consumer ordering

- `_process_trade` (producer): dedup-check (Redis) → category inference → enqueue.
  All Redis ops + in-memory work; **no DB roundtrip** in the producer path.
- `_db_writer_loop` (consumer): drains queue → `async with conn.transaction()`
  → `executemany` (or per-row ON CONFLICT DO NOTHING fallback) → emit metrics
  → publish `trades:observed` to Redis **after** commit.

Pub/sub ordering preserved from Phase 0 (publish outside the tx, after commit).

### Failure modes

- **Batch UniqueViolation** (intra-batch dupes that bypassed Redis): writer
  falls back to per-row inserts with `ON CONFLICT DO NOTHING`. No row loss.
- **Hard DB failure**: dedup keys are cleared for the failed batch so retries
  can succeed on next ingestion; the batch is **abandoned** rather than retried
  in-place. Trade durability is best-effort by design — durability would
  require WAL/Kafka-style replay which is Phase 3 (CDC).
- **Writer task crash**: `_db_writer_loop` wraps each iteration in a broad
  `try/except` that logs and continues. Only `CancelledError` exits the loop.

### ETag handling

`self._last_etag: str | None = None` on the instance. After a 200 response
the writer captures `response.headers.get("ETag")` (or `Last-Modified` as
fallback). The next request includes `If-None-Match: <etag>`. A 304 response
is treated as "no new trades, skip cycle" — saves bandwidth and rate-limit
budget at the 5s cadence.

If the upstream server doesn't return ETag headers, the first miss is
logged at DEBUG and the feature degrades to plain polling. No hard failure.

---

## Instrumentation (consumes Task M's metrics)

```python
from src.monitoring.metrics import (
    trades_ingested_total,
    trade_ingestion_latency_seconds,
    ws_disconnects_total,
    db_write_batch_size,
    db_write_latency_seconds,
    observer_queue_depth,
    observer_queue_drops_total,
    redis_publishes_total,
)
```

Emit sites:

| Metric | When |
|--------|------|
| `trades_ingested_total{source,result}` | After each batch commit; `source ∈ {ws, rest, backfill}`, `result ∈ {inserted, deduped, failed}` |
| `trade_ingestion_latency_seconds{source}` | Per-record: `time.monotonic() - record.event_ts_s` measured AFTER DB commit returns. This is the headline metric — it proves HP-1's ~16s → ~2-3s claim. |
| `ws_disconnects_total{reason}` | In `websocket_client.py` at every disconnect site |
| `db_write_batch_size` | Per batch flush (histogram, buckets 1..1000) |
| `db_write_latency_seconds` | Per batch flush (commit elapsed) |
| `observer_queue_depth` | Updated by writer loop each iteration |
| `observer_queue_drops_total{reason}` | When `wait_for(queue.put)` times out |
| `redis_publishes_total{channel,result}` | After each `trades:observed` publish |

Import-guard fallback exists for the edge case where `src/monitoring/metrics.py`
hasn't been imported yet (no-op stubs). Should never trigger in practice now
that Task M has landed.

---

## Tests

New file: `tests/test_observer/test_trade_observer_phase1.py`. Coverage:

1. `_process_trade` enqueues to `_write_queue` instead of writing inline.
2. `_db_writer_loop` flushes at 200 rows.
3. Flushes at 100ms even with <200 rows.
4. Queue-full drops the trade and increments `observer_queue_drops_total`.
5. ETag round-trip: mock returns ETag on first call, 304 on second.
6. Batch transaction failure rolls back (no partial inserts).

Existing tests in `tests/test_observer/test_trade_observer.py` were updated:
the architectural change in `_process_trade` (no longer writes inline) meant
~30 prior tests that asserted `conn.execute.assert_called` had to be rewritten
as `queue.put.assert_called`. Net coverage maintained.

**Test run**: `pytest tests/test_observer/ -q` → **62 passed, 0 failed.**

---

## Expected freshness gain

Per the audit (HP-1 summary):

| Stage | Before | After |
|-------|--------|-------|
| Poll cadence (median wait) | 15s (half of 30s) | 2.5s (half of 5s) |
| Backfill serial → parallel | ~1.6 min for 200 wallets | ~5s (Task F) |
| DB write batching | 3–7 roundtrips/trade | 1 roundtrip/200 trades |
| **Median leader-trade-to-react** | **~16s** | **~2–3s** |

The metrics needed to **prove** this (`trade_ingestion_latency_seconds`) are
now emitted. Run for 10 minutes against production data, scrape `/metrics`,
plot p50/p99. That's the validation hook.

---

## Deferred to Phase 2+

- **Logical-replication CDC out of `trades_observed`** — Phase 3, replaces the
  in-memory pub/sub fan-out with durable consumer groups via Redis Streams.
- **Adaptive backpressure** — current drop policy is binary (queue full →
  drop). A smarter policy would prioritize leader trades over non-leader
  trades on drop. Tracked for Phase 2 along with the dedicated-pubsub-client
  refactor (audit F-04).
- **`copy_records_to_table` vs `executemany`** — current implementation uses
  multi-row INSERT with `ON CONFLICT DO NOTHING`. asyncpg's `copy_records_to_table`
  is faster for large batches but doesn't support `ON CONFLICT`. If batch sizes
  routinely exceed 500, revisit.
- **ETag persistence across restarts** — `_last_etag` is instance-scoped; lost
  on restart. Trivial to add to Redis if we want sub-cycle freshness right
  after deploy.

---

## Files touched

- `src/observer/trade_observer.py` (+~700 lines: queue, writer loop, batch insert, ETag, metrics)
- `src/config.py` (+5 constants + 1 validator)
- `tests/test_observer/test_trade_observer_phase1.py` (new)
- `tests/test_observer/test_trade_observer.py` (updated for queue contract)
