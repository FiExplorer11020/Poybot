# Phase 1 — Task M: Metrics Foundation

**Status**: landed
**Owner**: Phase 1 Task M
**Consumers (parallel work)**: Phase 1 Task O (observer hot path), Phase 1 Task F (Falcon parallel backfill)

This document is the **single source of truth** for the Prometheus metric
contract. The audit (`docs/audit/04_perf_hotpaths.md`) is explicit: "Phase 1
also adds Prometheus histograms — without them no fix can be validated." Tasks
O and F instrument their hot paths against the names and labels listed here.

## Wire-up

- Module: `src/monitoring/metrics.py`
- Endpoint: `GET /metrics` on the FastAPI dashboard (`src/api/main.py`)
- Registry: default `prometheus_client.REGISTRY` (single-process; multiprocess
  is an explicit non-goal for Phase 1)
- Content-type: `text/plain; version=0.0.4; charset=utf-8` (provided by
  `prometheus_client.CONTENT_TYPE_LATEST`)
- Auth: **none** in Phase 1 (LAN-only scrape). Phase 2 must add bearer-token
  auth + rate-limit before this is exposed beyond the prod LAN.

Helper:

```python
from src.monitoring.metrics import export_latest
payload, content_type = export_latest()  # bytes, "text/plain; ..."
```

## Contract — exact metric names

### Trade observer hot path (consumed by Task O)

| Metric                                  | Type      | Labels                                               | Notes |
|-----------------------------------------|-----------|------------------------------------------------------|-------|
| `polybot_trades_ingested_total`         | Counter   | `source` (`ws`/`rest`/`backfill`), `result` (`inserted`/`deduped`/`failed`) | One increment per trade event |
| `polybot_trade_ingestion_latency_seconds` | Histogram | `source` (`ws`/`rest`/`backfill`)                  | Buckets: 10ms → 30s; arrival → DB commit |
| `polybot_ws_disconnects_total`          | Counter   | `reason` (`ping_timeout`/`server_close`/`exception`/`reconnect`) | Includes intentional reconnects |
| `polybot_db_write_batch_size`           | Histogram | none                                                 | Buckets: 1, 5, 10, 25, 50, 100, 200, 500, 1000 |
| `polybot_db_write_latency_seconds`      | Histogram | none                                                 | Buckets: 5ms → 5s; one observation per `executemany` |
| `polybot_observer_queue_depth`          | Gauge     | none                                                 | Set every enqueue/dequeue |
| `polybot_observer_queue_drops_total`    | Counter   | `reason` (`queue_full`/`shutdown`)                   | Backpressure visibility |

### Falcon API (consumed by Task F)

| Metric                              | Type      | Labels                                        | Notes |
|-------------------------------------|-----------|-----------------------------------------------|-------|
| `polybot_falcon_calls_total`        | Counter   | `agent` (`574`/`575`/...), `result` (`ok`/`empty`/`rate_limited`/`error`/`timeout`) | One increment per request, after the call resolves |
| `polybot_falcon_call_latency_seconds` | Histogram | `agent`                                     | Buckets: 50ms → 30s |
| `polybot_falcon_concurrency`        | Gauge     | none                                          | Inc on enter / dec on exit (use a context manager) |

### Redis pub/sub (any publisher)

| Metric                          | Type    | Labels                                  |
|---------------------------------|---------|-----------------------------------------|
| `polybot_redis_publishes_total` | Counter | `channel`, `result` (`ok`/`error`)      |

### Killswitch (Phase 0 wired the strict path; we measure consultation rate)

| Metric                                  | Type    | Labels                                 |
|-----------------------------------------|---------|----------------------------------------|
| `polybot_killswitch_strict_path_total`  | Counter | `result` (`enabled`/`disabled`/`error`) |

### Build info (best-effort)

| Metric              | Type | Labels                                    |
|---------------------|------|-------------------------------------------|
| `polybot_build_info` | Info | `version` (from `pyproject.toml`), `git_sha` (short) |

If git is unavailable or the package isn't installed, the labels fall back to
`unknown`. The Info metric is best-effort and never breaks import.

## Importing in Tasks O and F

```python
# Tasks O / F MUST use these exact symbols. Renames break the contract.
from src.monitoring.metrics import (
    trades_ingested_total,
    trade_ingestion_latency_seconds,
    ws_disconnects_total,
    db_write_batch_size,
    db_write_latency_seconds,
    observer_queue_depth,
    observer_queue_drops_total,
    falcon_calls_total,
    falcon_call_latency_seconds,
    falcon_concurrency,
    redis_publishes_total,
    killswitch_strict_path_total,
)
```

Histogram timing pattern:

```python
with trade_ingestion_latency_seconds.labels(source="ws").time():
    ...  # work to be timed
```

## Non-goals (deferred)

- Grafana dashboards — Phase 1 Round 3
- Multiprocess registry — not needed for the current single-process API
- Auth on `/metrics` — Phase 2 (TODO is in `src/api/main.py`)
- Instrumenting the hot paths themselves — Tasks O and F own those

## Tests

- `tests/test_monitoring/test_metrics.py` covers: clean import, contract types,
  `export_latest()` payload, FastAPI route 200 + content-type, no-DB/Redis
  invariant on scrape.
