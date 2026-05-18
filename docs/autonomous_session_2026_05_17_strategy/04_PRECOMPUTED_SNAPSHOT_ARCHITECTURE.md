# Pre-computed Snapshot Architecture — Dev Plan

**Trigger**: the in-process `gather()` of 17 fetchers in `/api/v1/live-summary` saturates the pool DB under parallel pressure. Individual queries that finish in 1-5s standalone take 30-60s in parallel. Per-fetcher timeouts patch the symptom (cache stays empty for slow fetchers). The infra-aligned fix is to **move snapshot composition into the maintenance container** (which already exists and does periodic refresh jobs), write the JSON to Redis, and serve the API endpoint from Redis with <10ms latency.

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│ polymarket_maintenance  (existing container, just adding a job)      │
│                                                                       │
│  scripts/maintenance_loop.py                                          │
│    + register: build_live_summary_snapshot (every 30s)                │
│    + sequentially calls the 17 SQL queries (no parallel pressure)     │
│    + builds full JSON dict                                            │
│    + writes Redis: SET "snapshot:live_summary" {...} EX 120           │
│    + writes Redis: SET "snapshot:live_summary:built_at" <epoch>       │
│    + publishes pubsub: "snapshot:live_summary:updated"                │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  Redis
┌──────────────────────────────────────────────────────────────────────┐
│ polymarket_api  (existing, simplified endpoint)                       │
│                                                                       │
│  GET /api/v1/live-summary                                             │
│    1. redis.GET("snapshot:live_summary")                              │
│    2. if absent → 503 {"error":"snapshot_warming_up"} (5s retry)      │
│    3. else → return payload + ETag (built_at hash) + `stale=true`     │
│             if age > 60s                                              │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  Dashboard frontend (no change)
```

### Why this fits the infra

- **No new container** — reuses `polymarket_maintenance`
- **No new tech** — Redis as cache layer is the existing pattern (`book:last`, `metrics:*`, `paper:rejections:*`, `runtime_config:risk`)
- **Same data shape** — frontend code unchanged
- **Decoupling principle** — slow queries isolated from user requests (like the bot's other jobs: backfill_resolved_outcomes, refresh_event_times, etc.)
- **Single writer** — eliminates pool contention; max 1 builder at a time

---

## 2. Detailed File Changes

### 2.1 New module: `src/api/snapshot_builder.py`
Extract the snapshot composition logic from `src/api/main.py` into a standalone module that maintenance can import.

```python
# Exports:
#   async def build_terminal_snapshot(pool, redis_client) -> dict
#       Runs all 17 queries sequentially (or with bounded concurrency=2-3),
#       returns the full dict ready for Redis storage.
#       Uses queries.* functions directly (not _fetch_* helpers which
#       have in-process cache).
#
#   SNAPSHOT_REDIS_KEY = "snapshot:live_summary"
#   SNAPSHOT_BUILT_AT_KEY = "snapshot:live_summary:built_at"
#   SNAPSHOT_TTL_S = 120
#   SNAPSHOT_PUBSUB_CHANNEL = "snapshot:live_summary:updated"
```

Logic:
- Acquire ONE DB pool connection
- Call each query function in sequence (low concurrency — max 2 in parallel for ones that don't conflict)
- Catch per-query exceptions, log, use default value, continue
- Build the dict in the exact shape that `_get_terminal_snapshot` returns today (binary compatible)
- Write to Redis with `EX 120` (auto-expire as safety net)
- Publish pubsub event so WebSocket bridge can fan-out to clients

### 2.2 Maintenance loop hook: `scripts/maintenance_loop.py`
Add a recurring job `build_live_summary_snapshot` that runs every 30s. Pattern matches existing jobs (`refresh_event_times`, `auto_promote_to_watchlist`, etc.).

```python
LIVE_SUMMARY_INTERVAL_S = 30.0

# inside the main loop dispatcher:
if monotonic() - last_run["live_summary"] >= LIVE_SUMMARY_INTERVAL_S:
    try:
        await snapshot_builder.build_terminal_snapshot(pool, redis)
        last_run["live_summary"] = monotonic()
        logger.info(f"live_summary snapshot built in {dur:.2f}s")
    except Exception:
        logger.exception("live_summary build failed")
```

### 2.3 Simplified API endpoint: `src/api/main.py`
Replace the body of `/api/v1/live-summary` with a Redis GET. Keep the existing ETag conditional-GET logic.

```python
@app.get("/api/v1/live-summary")
async def api_live_summary_v1(request: Request, response: Response):
    raw = await _redis.get(SNAPSHOT_REDIS_KEY)
    built_at = await _redis.get(SNAPSHOT_BUILT_AT_KEY)

    if raw is None:
        # Cold start — snapshot not yet built
        response.status_code = 503
        return {"data": _SKELETON, "warming_up": True}

    age_s = time.time() - float(built_at or 0)
    if age_s > SNAPSHOT_STALENESS_WARN_S:
        # Serve stale with warning header
        response.headers["X-Snapshot-Stale-Age"] = str(round(age_s, 1))

    etag = '"' + hashlib.sha256(raw.encode()).hexdigest()[:16] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    return Response(content=raw, media_type="application/json", headers={"ETag": etag})
```

The background `_snapshot_rebuilder_loop` becomes obsolete and can be removed (or kept as a fallback for development without maintenance container).

### 2.4 Remove (or guard) dead code
- `_snapshot_rebuilder_loop` background task — replace with a no-op stub (or keep as fallback if maintenance container is down)
- `_HELPER_CACHE` in-process cache — keep, still useful for individual endpoint reads (kpis, trades) that don't go through snapshot

### 2.5 New SQL: extract queries used by snapshot
All `queries.*` functions are reused unchanged. No SQL changes required by this refactor (we can still optimize them separately later, but they're no longer the dashboard-blocking path).

### 2.6 WebSocket bridge enhancement: `src/api/ws_bridge.py`
Subscribe to `snapshot:live_summary:updated` pubsub channel. On each event, broadcast to connected WS clients so the dashboard updates in real time without polling.

---

## 3. Sub-agent Assignments

### Agent A — Snapshot Builder Module (new code)
- **Read**: `src/api/main.py:_get_terminal_snapshot` + all `_fetch_*` helpers, understand the data shape
- **Create**: `src/api/snapshot_builder.py` (~300 LOC)
- **Output**: `build_terminal_snapshot(pool, redis_client)` that produces the same dict shape; uses sequential queries (max 3 in parallel via Semaphore for unrelated tables); error handling per section
- **Tests**: `tests/test_api/test_snapshot_builder.py` (8+ tests covering shape, error handling, Redis write, pubsub publish)
- **Files**: only `src/api/snapshot_builder.py` + tests
- **Constraints**: do NOT touch maintenance_loop.py or main.py endpoint — other agents own those

### Agent B — Maintenance Loop Integration
- **Read**: `scripts/maintenance_loop.py`, understand the existing job-scheduling pattern (`backfill_resolved_outcomes`, `refresh_event_times`, etc.)
- **Modify**: `scripts/maintenance_loop.py` — add `build_live_summary_snapshot` job (30s cadence)
- **Tests**: `tests/test_scripts/test_maintenance_snapshot_job.py` (verify the job is scheduled, calls the builder, handles errors)
- **Files**: only `scripts/maintenance_loop.py` + tests
- **Constraints**: depends on Agent A's `snapshot_builder.py` being committed; coordinate by waiting briefly OR use a stub import that lands first

### Agent C — API Endpoint Simplification
- **Read**: `src/api/main.py:api_live_summary_v1` + the helper `_get_terminal_snapshot`
- **Modify**: `src/api/main.py` to:
  - Replace `_get_terminal_snapshot()` call with `redis.get(SNAPSHOT_REDIS_KEY)`
  - Keep ETag conditional-GET intact (just compute ETag from raw bytes)
  - Add 503 + skeleton when key missing
  - Add `X-Snapshot-Stale-Age` header when age > 60s
  - Remove (or comment out) the `_snapshot_rebuilder_loop` background task — replaced by maintenance
- **Tests**: `tests/test_api/test_live_summary_redis_backed.py` (mock Redis: present, absent, stale, 304)
- **Files**: only `src/api/main.py` + tests

### Agent D — WebSocket Bridge Enhancement
- **Read**: `src/api/ws_bridge.py` — understand how it subscribes + broadcasts
- **Modify**: add subscriber on `snapshot:live_summary:updated` channel; on event → broadcast `{"type": "snapshot_updated"}` to all connected clients
- **Frontend impact**: optional — the client can listen and force a refetch
- **Files**: only `src/api/ws_bridge.py` + tests

### Agent E — Validation + Deploy + Rollback Plan
- **Read**: `docs/DEPLOY.md`
- **Output**: 
  - Deploy script: rsync, rebuild api + maintenance, restart in correct order
  - Validation script: curl /api/v1/live-summary, check response time <100ms, check data populated
  - Rollback script: revert the API endpoint to the in-process version if the Redis path is broken
- **Files**: `scripts/deploy_snapshot_redis_refactor.sh` (new), `docs/migrations/...` (none — no DB schema change)

---

## 4. Implementation Order

```
[parallel - Agents A, B, C, D start simultaneously]
  Agent A: snapshot_builder.py module
  Agent B: maintenance_loop hook (stubs Agent A's import for now)
  Agent C: API endpoint simplification (Redis-backed)
  Agent D: WS bridge subscriber

[sequential - after all 4 land]
  Agent E: deploy + validate
```

Each agent commits its own files. Cross-dependencies handled via:
- Agent A defines the function signature first → Agent B can import it
- Agent C's endpoint reads Redis directly → no dependency on Agent A's code, just on the Redis key being populated
- Agent D's subscriber → independent

---

## 5. Acceptance Criteria

- [ ] `/api/v1/live-summary` returns in **<50ms p99** (vs current 200-400ms warm or 30s cold)
- [ ] Snapshot data is **always populated** (no skeleton seen by clients in steady state)
- [ ] Snapshot age **<60s** at all times
- [ ] If maintenance container is down: API returns last-known snapshot with `X-Snapshot-Stale-Age` header
- [ ] If Redis is down: API returns 503 (clean, not 30s hang)
- [ ] No regression on dashboard visual: all sections populated as before
- [ ] WS client receives `snapshot_updated` event after each rebuild → dashboard refreshes ≤ 1s after maintenance writes
- [ ] No new DB queries vs the current snapshot composition (we're just moving WHERE they run)
- [ ] Tests: each agent ships 4+ regression tests covering its module

---

## 6. Risk Mitigation

### Risk: maintenance container crash → snapshot never rebuilds
- **Mitigation**: Redis TTL EX 120 — stale snapshot eventually expires
- **Backup**: API returns 503 explicitly so frontend can show "data refresh paused" indicator
- **Monitoring**: log `snapshot:live_summary:built_at` to Prometheus / log line every cycle

### Risk: Redis full → snapshot write fails
- **Mitigation**: SET with TTL prevents accumulation; snapshot ~200KB × 1 key = trivial vs the 128MB Redis cap
- **Sanity**: log a warning if SET fails

### Risk: snapshot builder takes >30s → cycles overlap
- **Mitigation**: in-process lock in `build_terminal_snapshot` so concurrent calls are serialized
- **Cadence**: 30s build interval; if a build takes 30s+, next one waits

### Risk: shape drift between maintenance build and what API expects
- **Mitigation**: shared shape comes from same `queries.*` functions; tests assert dict keys

### Risk: WebSocket broadcast spam → frontend over-renders
- **Mitigation**: rate-limit broadcasts to 1/2s; frontend already debounces

---

## 7. Files Touched Summary

```
NEW:
  src/api/snapshot_builder.py                                 (Agent A)
  tests/test_api/test_snapshot_builder.py                     (Agent A)
  tests/test_scripts/test_maintenance_snapshot_job.py         (Agent B)
  tests/test_api/test_live_summary_redis_backed.py            (Agent C)
  scripts/deploy_snapshot_redis_refactor.sh                   (Agent E)

MODIFIED:
  scripts/maintenance_loop.py     (+ ~50 LOC for new job)     (Agent B)
  src/api/main.py                 (~ -200 +50 LOC simplify)   (Agent C)
  src/api/ws_bridge.py            (+ ~30 LOC pubsub sub)      (Agent D)
```

No SQL migrations. No frontend changes (binary-compatible payload).

---

## 8. Deploy + Validation Order

1. Build new image (api + maintenance share the same image)
2. Restart MAINTENANCE first — starts publishing snapshot to Redis
3. Wait 30s — first snapshot written
4. Verify `redis-cli GET snapshot:live_summary` returns JSON > 100KB
5. Restart API second — picks up new endpoint code
6. Curl `/api/v1/live-summary` 5 times — all should return in <100ms with populated data
7. Hard-refresh dashboard — should look identical to before
8. Monitor logs for 10 min — no errors

If anything breaks: rollback = revert API to in-process snapshot (keep maintenance job running as no-op until investigation).
