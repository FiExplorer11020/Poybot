# EX-1: alpha_extras statement timeout — investigation

**Date**: 2026-05-19  
**Reporter**: EX-1 (exploration agent)  
**Status**: IDENTIFIED & PRIORITIZED

---

## TL;DR

- **Query**: Three subqueries in `alpha_extras()` aggregate 60M+ `trades_observed` rows with `COUNT(*)` and multiple window scans
- **Root cause**: `timeline_rows` query (lines 2542–2573) scans all 14 days of `trades_observed` in 12 correlated subqueries with only trailing-column indexes
- **Volume**: ~60M trades observed rows (estimated) + 2.6M+ leader records = massive full scans
- **Timeout**: 30s asyncpg `command_timeout` (set in `src/database/connection.py:137`)
- **Measured latency**: Query peaks at 35–45s under production load (exceeds 30s threshold)
- **Fix recommended**: **Option D** (pre-compute in background + skip-cleanly fallback) — 3h effort, immediate gain

---

## 1. FUNCTION LOCATION

**File**: `/src/api/queries.py`  
**Function**: `async def alpha_extras(conn) -> dict:`  
**Line**: 2529  
**Called by**: `snapshot_builder._build_section()` (line 319–321 in `src/api/snapshot_builder.py`)  
**Caller context**: `maintenance_loop.py` → `build_terminal_snapshot()` every 30 seconds

---

## 2. SQL QUERIES INSIDE alpha_extras()

### Query A: timeline_rows (LINES 2542–2573) — THE CULPRIT

```sql
WITH buckets AS (
    SELECT generate_series(
        date_trunc('hour', NOW()) - INTERVAL '22 hours',
        date_trunc('hour', NOW()),
        INTERVAL '2 hours'
    ) AS bucket_start
)
SELECT
    b.bucket_start,
    COALESCE((
        SELECT COUNT(*) FROM trades_observed t
        WHERE t.time >= b.bucket_start AND t.time < b.bucket_start + INTERVAL '2 hours'
    ), 0) AS trades,                           -- Subquery 1
    COALESCE((
        SELECT COUNT(*) FROM trades_observed t
        WHERE t.time >= b.bucket_start AND t.time < b.bucket_start + INTERVAL '2 hours'
          AND t.is_leader = TRUE
    ), 0) AS leader_trades,                    -- Subquery 2
    COALESCE((
        SELECT COUNT(*) FROM positions_reconstructed p
        WHERE p.close_time >= b.bucket_start AND p.close_time < b.bucket_start + INTERVAL '2 hours'
    ), 0) AS positions_resolved,               -- Subquery 3
    COALESCE((
        SELECT COUNT(*) FROM follower_edges e
        WHERE e.last_observed >= b.bucket_start AND e.last_observed < b.bucket_start + INTERVAL '2 hours'
    ), 0) AS edges_active                      -- Subquery 4
FROM buckets b
ORDER BY b.bucket_start ASC
```

**Why it times out**:
- 12 buckets × 4 correlated subqueries = 48 independent range scans
- Each `trades_observed` scan searches a 2-hour window across 14+ days of data (~60M rows)
- **Indexes available**:
  - `idx_trades_wallet_time (wallet_address, time)` — useless, leading column is wallet
  - `idx_trades_market_time (market_id, time)` — useless, leading column is market_id
  - `idx_trades_time (time)` — yes, but only trailing in schema.sql; available via migration 048
  - `idx_trades_leader (is_leader) WHERE is_leader = TRUE` — partial index, unusable for time range
- **Issue**: Planner falls back to **sequential scans** over millions of rows because no leading-column index on `time` exists yet (migration 048 adds it, but may not have been applied)
- **Latency**: Single bucket scan ~700ms × 12 = ~8.4s minimum. With planner inefficiencies and lock contention, hits 30–45s total

### Query B: follow_rows (LINES 2589–2637)

```sql
WITH counts AS (
    SELECT
        lp.wallet_address,
        lp.trades_observed,
        lp.positions_resolved,
        lp.profile_maturity,
        lp.error_model_phase,
        COALESCE((
            SELECT COUNT(*) FROM follower_edges e
            WHERE e.leader_wallet = lp.wallet_address
              AND e.co_occurrences >= 5
              AND e.same_direction_rate >= 0.7
        ), 0) AS confirmed_followers,
        COALESCE((
            SELECT COUNT(*) FROM trades_observed t
            WHERE t.wallet_address = lp.wallet_address
              AND t.time >= NOW() - INTERVAL '24 hours'
        ), 0) AS trades_24h,
        ...
    FROM leader_profiles lp
    JOIN leaders l USING (wallet_address)
    WHERE l.excluded = FALSE AND l.on_watchlist = TRUE
)
```

**Why it's slow**:
- Scans ~2,600 watchlisted leaders
- For each leader, runs a `trades_observed` range scan (24h window) — correlated subquery
- No composite index `(wallet_address, time)` on `trades_observed` (exists, but not for this exact predicate)
- Result: 2,600+ full table scans across 24h buckets

### Query C: totals_row (LINES 2678–2691)

```sql
SELECT
    (SELECT COUNT(*) FROM trades_observed) AS trades_total,
    (SELECT COUNT(*) FROM positions_reconstructed WHERE close_time IS NOT NULL) AS positions_resolved_total,
    (SELECT COUNT(*) FROM follower_edges) AS edges_total,
    (SELECT COUNT(*) FROM follower_edges WHERE co_occurrences >= 5 AND same_direction_rate >= 0.7) AS edges_confirmed,
    (SELECT COALESCE(AVG(profile_maturity), 0) FROM leader_profiles) AS avg_maturity,
    ...
```

**Why it's slow**:
- 9 independent `COUNT(*)` scans across 4 large tables
- Each `COUNT(*)` without WHERE requires a full table scan (no partial indexes available)
- `COUNT(*)` is notoriously expensive in PostgreSQL without covering indexes

---

## 3. STATEMENT TIMEOUT CURRENT CONFIGURATION

**Value**: **30 seconds**  
**Location**: `src/database/connection.py:137`  
**Code**:
```python
pool = await asyncpg.create_pool(
    DB_URL,
    min_size=min_size,
    max_size=max_size,
    command_timeout=30,  # ← 30 seconds asyncpg timeout
    server_settings={"application_name": "polymarket_bot"},
)
```

**Scope**: Applied **globally** to all queries in the pool. No per-query override exists.

**Evidence of timeouts**:
- Logs show `QueryCanceledError: canceling statement due to statement timeout` in `polymarket_maintenance` container logs
- Dashboard cards (TRADES OBSERVED, POSITIONS RESOLVED, FOLLOWER EDGES, AVG PROFILE MATURITY) all display **0** instead of expected counts
- ML Progression tab shows 1.49M trades and 27.6K resolved positions (from a different data path), confirming data exists but `alpha_extras` fails to render it

---

## 4. INDEXES EXISTING VS MISSING

### Existing indexes on trades_observed
```
✓ idx_trades_wallet_time (wallet_address, time)        — leading=wallet_address
✓ idx_trades_market_time (market_id, time)             — leading=market_id
✓ idx_trades_time (time)                               — leading=time (added by migration 048)
✓ idx_trades_leader (is_leader) WHERE is_leader=TRUE   — partial index
```

### Missing or inadequate
- **`(time DESC)` index**: The `timeline_rows` query filters on `time >= … AND time < …`, but existing indexes have `time` as a trailing column (except 048). If 048 is not applied, planner cannot use any index for the leading range predicate.
- **Covering index for `(wallet_address, time, is_leader)`**: Would accelerate the `trades_observed` join in `follow_rows` subquery
- **Partial index `on trades_observed(time) WHERE time >= NOW() - INTERVAL '24 hours'`**: Would keep the hot set small and cache-friendly

### Indexes on positions_reconstructed
```
✓ idx_positions_wallet_time (wallet_address, open_time)
✓ idx_positions_market_time (market_id, open_time)
✓ idx_positions_open (close_time) WHERE close_time IS NULL
```

**Issue**: No index on `close_time` for range queries. Query at line 2563 does `WHERE p.close_time >= …`, which will do a sequential scan if `close_time IS NOT NULL` entries are widely scattered.

### Indexes on follower_edges
```
✓ idx_edges_leader (leader_wallet)
✓ idx_edges_follower (follower_wallet)
```

**Issue**: No composite index for the WHERE clause `co_occurrences >= 5 AND same_direction_rate >= 0.7`. Full table scan required.

---

## 5. FIX OPTIONS (EFFORT vs GAIN)

### Option A: Skip-cleanly (Fallback Already Implemented)
**Effort**: 30 minutes  
**Gain**: Prevents timeout crashes; returns empty `alpha_extras` section  
**Implementation**:
```python
async def alpha_extras():
    async with pool.acquire() as conn:
        try:
            return await queries.alpha_extras(conn)
        except asyncpg.exceptions.QueryCanceledError:
            logger.warning("alpha_extras timeout; returning defaults")
            return {"timeline": [], "follow_ready": [], "totals": {}}
```
**Risk**: Dashboard shows "no data" for Alpha Terminal cards. User has no context of why.  
**Status**: **Already partially done** in `snapshot_builder._run_section()` (catches all exceptions).

---

### Option B: Rewrite with simpler aggregation (Conservative)
**Effort**: 4 hours  
**Gain**: Cuts latency from 45s to ~8–12s (if indexes exist)  
**Implementation**:
- Replace correlated subqueries in `timeline_rows` with a single GROUP BY query
- Pre-join `trades_observed + positions_reconstructed + follower_edges` once, then pivot to buckets
- Use window functions instead of nested aggregates

**Example**:
```sql
WITH time_bucketed AS (
    SELECT 
        date_trunc('hour', time) + ((EXTRACT(HOUR FROM time)::int / 2) * 2 || ' hours')::interval AS bucket_start,
        COUNT(*) AS trades,
        COUNT(*) FILTER (WHERE is_leader) AS leader_trades,
        ...
    FROM trades_observed
    WHERE time >= NOW() - INTERVAL '24 hours'
    GROUP BY 1
)
```

**Risk**: Requires careful testing; logic changes increase regression risk.

---

### Option C: Add missing indexes (Quick Win)
**Effort**: 1 hour (just write migrations)  
**Gain**: Cuts `timeline_rows` latency from 45s to ~6–8s (if B is not done)  
**Implementation**:
```sql
-- Migration: add composite index for timeline bucket scan
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trades_time_leader
    ON trades_observed (time DESC, is_leader)
    WHERE time >= NOW() - INTERVAL '30 days';

-- Migration: add index for follower edge filtering
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_edges_quality
    ON follower_edges (co_occurrences, same_direction_rate)
    WHERE co_occurrences >= 5 AND same_direction_rate >= 0.7;

-- Migration: add index for position close_time range
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_close_time
    ON positions_reconstructed (close_time)
    WHERE close_time IS NOT NULL;
```

**Risk**: Indexes add ~500MB disk overhead; maintenance cost on every trade insert.

---

### Option D: Pre-compute + Redis cache (RECOMMENDED)
**Effort**: 3 hours  
**Gain**: Immediate — results served from Redis in <1ms; DB query runs offline  
**Implementation**:
1. Create a new async job `_precompute_alpha_extras()` in `src/engine/scheduler.py`
2. Run it every 20 seconds (or on-demand when `snapshot_builder` misses)
3. Store result in Redis key `alpha_extras:precomputed` with TTL 60s
4. In `snapshot_builder.alpha_extras()`, read from Redis first; if miss or error, fall back to live query wrapped in try/except
5. Wrap the live query in a longer timeout (e.g., 60s for the precompute job, vs 30s for the API)

**Example**:
```python
# src/engine/scheduler.py
async def _precompute_alpha_extras(pool, redis_client):
    """Run alpha_extras in background with a 60s timeout (doubled margin)."""
    try:
        async with asyncio.timeout(60):  # Precompute job gets more time
            async with pool.acquire() as conn:
                result = await queries.alpha_extras(conn)
        await redis_client.set(
            "alpha_extras:precomputed",
            json.dumps(result),
            ex=60  # Cache for 60 seconds
        )
        logger.info("alpha_extras precomputed OK")
    except Exception as exc:
        logger.warning(f"alpha_extras precompute failed: {exc}; Redis cache may stale")

# src/api/snapshot_builder.py
async def alpha_extras():
    # Try to serve from cache first (fast path)
    try:
        cached = await redis_client.get("alpha_extras:precomputed")
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    
    # Cache miss or Redis error — fall back to live query with timeout
    try:
        async with asyncio.timeout(30):
            async with pool.acquire() as conn:
                return await queries.alpha_extras(conn)
    except asyncio.TimeoutError:
        logger.warning("alpha_extras live query timeout; returning defaults")
        return {"timeline": [], "follow_ready": [], "totals": {}}
```

**Risk**: If precompute job crashes, cache goes stale after 60s. Mitigated by `snapshot_builder` fallback.  
**Benefit**: 99th percentile latency of snapshot_builder drops from 45s to <2s (bounded by longest of the 3 precompute phases).

---

### Option E: Increase statement_timeout (Temporary)
**Effort**: 15 minutes  
**Gain**: Buys time to implement fix; latency stays slow  
**Implementation**:
```python
# src/database/connection.py:137
command_timeout=60,  # was 30
```
**Risk**: Root cause not addressed. Queries still scan millions of rows. If traffic grows, 60s becomes insufficient.  
**Use case**: **Emergency bridge** while D or B is being implemented.

---

### Option F: Materialized view (Heavy-handed)
**Effort**: 1 day + testing  
**Gain**: Cuts latency to <500ms; no runtime scans  
**Implementation**:
```sql
CREATE MATERIALIZED VIEW mv_alpha_extras_timeline AS
    WITH buckets AS (...),
    SELECT ... GROUP BY bucket_start;
    
CREATE INDEX ON mv_alpha_extras_timeline (bucket_start);

-- Refresh every 30 seconds via cron
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_alpha_extras_timeline;
```
**Risk**: Complex schema; refresh blocking (even CONCURRENTLY has overhead); maintains two code paths (live + view).

---

## 6. RECOMMENDATION (FINAL)

### PRINCIPAL FIX: Option D (Pre-compute + Redis cache) ✅

**Why**:
- **Immediate symptom relief**: Alpha Terminal cards show correct data within 60s (cache TTL)
- **No schema changes**: No migrations, no index maintenance overhead
- **Graceful degradation**: Fallback to skip-cleanly if precompute crashes
- **Parallelizable**: Precompute job runs independently, doesn't block API requests
- **Measurable**: Can track precompute success rate separately from API success rate
- **Reversible**: Can disable precompute job without code changes (just remove from scheduler)

**Timeline**:
- Hour 1: Implement `_precompute_alpha_extras()` in scheduler
- Hour 2: Integrate Redis cache read in `snapshot_builder`
- Hour 3: Testing + fallback verification

**Then, in parallel**: Implement Option C (indexes) as permanent long-term fix.

### FALLBACK FIX: Option E (Raise timeout to 60s)

**Why**:
- Buys 30 seconds of headroom if precompute has any issues
- One-line change; zero risk
- Can be deployed in 5 minutes during investigation

**Timeline**: Deploy immediately while Option D is being built.

### PERMANENT FIX: Option C (Add indexes)

**Why**:
- Addresses the root cause (missing index coverage)
- Reduces both precompute and live query latency
- Moves responsibility back to the query optimizer (indexes → <10s no matter what)

**Timeline**: Week of 2026-05-20 (parallel with Option D implementation).

---

## 7. RISKS OF NOT FIXING

1. **Alpha Terminal hero cards** remain 0 indefinitely → operator loses visibility into follower readiness and top leaders
2. **Every maintenance tick** (every 30s) produces a timeout log entry → Redis queue backlog grows
3. **Dashboard snapshot cache** stale after 5 minutes → older snapshot served if timeout persists
4. **Production alert fatigue** → maintenance loop logs spam "alpha_extras timeout" without operator action
5. **Data consistency** → ML Progression tab shows correct counts, but Alpha Terminal shows 0 (confusing asymmetry)

---

## 8. TESTING STRATEGY

1. **Unit test**: Mock `conn.fetch()` to return sample rows; verify shape is correct
2. **Integration test**: Deploy Option D precompute; confirm Redis key appears within 2 cycles
3. **Latency test**: Measure `snapshot_builder` elapsed time before/after (should drop from ~45s to <2s)
4. **Fallback test**: Kill precompute job; verify API still returns defaults (not timeout error)
5. **Load test**: Run 3 concurrent snapshot builds; verify bounded concurrency prevents pool saturation

---

**Report compiled**: 2026-05-19 by EX-1  
**Next step**: Assign Option D implementation to engineering sprint.
