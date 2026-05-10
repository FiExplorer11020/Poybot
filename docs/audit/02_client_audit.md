# Database Client Audit — PostgreSQL (asyncpg) + Redis

**Scope**: how the codebase USES the DB clients, not the schema itself.
**Auditor target**: `polymarket-bot/src/{database,observer,registry,control,engine,profiler,graph,api,monitoring,telegram_bot}/`.
**Stack audited**: asyncpg 0.29 / redis-py 5.0.1 (asyncio) / Python 3.11.

---

## Top 5 Quick Wins (P0/P1, effort S)

| # | Title | File | Sev | Effort |
|---|-------|------|-----|--------|
| 1 | `PaperTrader.open_trade` opens trade WITHOUT a transaction — bankroll/DB diverge on partial failure | `src/engine/paper_trader.py:495-525` | P0 | S |
| 2 | `_close_position` does 3 SELECTs + 1 INSERT in one DB session but no `conn.transaction()` — readers see torn state | `src/observer/position_tracker.py:302-364` | P0 | S |
| 3 | `_process_trade` holds a pooled connection across the whole insert + read-back + UPDATE chain with no transaction wrapper | `src/observer/trade_observer.py:924-1014` | P0 | S |
| 4 | Redis SUBSCRIBE shares the SAME client instance as commands — long-lived `pubsub.listen()` keeps a connection out of the pool indefinitely, plus dropped reconnects are silent | `src/observer/position_tracker.py:60-75`, `src/profiler/behavior_profiler.py:104-136`, `src/graph/graph_engine.py:39-54`, `src/engine/{paper_trader,confidence_engine,live_trader}.py`, `src/api/ws_bridge.py:66-92` | P1 | S |
| 5 | `confidence_engine.precompute_redis_cache` writes N SETs sequentially with `await` per wallet — should pipeline | `src/engine/confidence_engine.py:742-773` | P1 | S |

---

## P0 — Critical (data integrity / safety)

### F-01: PaperTrader.open_trade has no transaction; in-memory bankroll updates after each independent statement   [Severity: P0]
File: `src/engine/paper_trader.py:495-549`
Pattern:
```python
async with get_db() as conn:
    row = await conn.fetchrow(
        """INSERT INTO paper_trades ... RETURNING id""", ...
    )
    trade_id = row["id"]
except Exception as e:
    logger.error(f"Failed to open paper trade: {e}")
    return None

self._capital -= size_usdc
open_trade = OpenPaperTrade(...)
self._open_trades.append(open_trade)
await self._persist_state()  # second DB roundtrip via save_state()
await self._record_equity_sample()  # third DB roundtrip
```
Why it matters: insertion of `paper_trades` and the subsequent `portfolio_state` UPDATE happen in DIFFERENT pooled connections (each `get_db()` is a fresh acquire). If the engine crashes between INSERT and `save_state()`, the open trade exists in DB but `portfolio_state.capital` and `consecutive_losses` are stale until the next `_persist_state()`. Same hazard in `close_trade` where two SQL statements (UPDATE paper_trades, UPDATE decision_log) live inside the same `async with get_db()` block — those two ARE atomic at the connection level only because asyncpg auto-commits each statement, but a crash between them still leaves an inconsistent (closed paper trade, dangling decision_log.outcome=NULL) state.
Fix: wrap the `paper_trades` INSERT and the `portfolio_state` UPSERT in a single `async with conn.transaction()` block. Pass the connection to `save_state()`/`record_equity()` so they reuse the same tx. Same fix for `close_trade`: wrap the `paper_trades UPDATE`, the `decision_log` UPDATE, and `_persist_state` together.
Effort: S

### F-02: `position_tracker._close_position` runs read-modify-write across 3 statements without a transaction   [Severity: P0]
File: `src/observer/position_tracker.py:302-364`
Pattern:
```python
async with get_db() as conn:
    market_row = await conn.fetchrow(
        "SELECT category FROM markets WHERE market_id = $1", pos.market_id,
    )
    ...
    trend_row = await conn.fetchrow(
        """SELECT AVG(price) ... FROM trades_observed ...""", ...,
    )
    ...
    await conn.execute(
        """INSERT INTO positions_reconstructed ... VALUES (...) """, ...
    )
```
Why it matters: between the two SELECTs and the INSERT, another writer can change `markets.category` or push trades into `trades_observed`. The `is_contrarian` flag and the row's `category` denormalization can therefore disagree with the canonical state at INSERT time. More importantly, `pnl_usdc` was computed in Python BEFORE these reads; a category/liquidity change mid-flight produces inconsistent denormalized rows. Without a transaction, asyncpg also doesn't pin a snapshot, so under the default READ COMMITTED isolation each statement sees a different view.
Fix: `async with conn.transaction(isolation='repeatable_read'):` around lines 303-364. The position close is a logical atomic unit — closing it as one transaction also guarantees the `positions:closed` Redis publish (line 389) only fires on commit success.
Effort: S

### F-03: `trade_observer._process_trade` holds a single connection across 5 SQL statements + nested fn call, no transaction   [Severity: P0]
File: `src/observer/trade_observer.py:924-1014`
Pattern:
```python
async with get_db() as conn:
    await conn.execute("""INSERT INTO markets (...) ON CONFLICT DO NOTHING""", ...)
    inserted_id = await conn.fetchval("""INSERT INTO trades_observed ... ON CONFLICT DO NOTHING RETURNING id""", ...)
    if inserted_id is None: return
    self._inserted += 1
    market_row = await conn.fetchrow(...)
    market_row = await self._repair_market_from_trade_hint(conn=conn, ...)  # may UPDATE markets
    if refined_category: await conn.execute("UPDATE trades_observed SET category=$2 WHERE id=$1", ...)
    if is_leader: leader_row = await conn.fetchrow(...)
```
Why it matters: 5 round-trips share a connection but NOT a transaction. The `markets` upsert + the `trades_observed` insert + the `trades_observed` UPDATE that fixes category should be one atomic unit. A crash mid-sequence (or a transient network blip — asyncpg's `command_timeout=30` will raise) leaves the trade row inserted with `category='unknown'` even though the `markets` stub got refreshed. Worse, the in-memory counter `self._inserted += 1` is incremented before the chain completes, drifting the metric from reality.
Fix: wrap lines 925-1023 in `async with conn.transaction():`. Move `self._inserted += 1` to AFTER the `async with` block exits successfully. Same fix needed for the second `async with get_db() as conn` block at line 1037 that does the Gamma-enrichment UPSERT.
Effort: S

### F-04: Redis pub/sub and command client are the SAME instance — listener can starve commands and silently drop on disconnect   [Severity: P0]
File: `src/observer/main.py:153`, `src/engine/main.py:65`, `src/registry/main.py:24`, `src/api/main.py:94`; consumed by every `pubsub.listen()` call site
Pattern (representative — `position_tracker.py:60-75`):
```python
async def _subscribe_loop(self) -> None:
    pubsub = self._redis.pubsub()
    await pubsub.subscribe(REDIS_TRADES_CHANNEL)
    try:
        async for message in pubsub.listen():
            ...
    finally:
        await pubsub.unsubscribe(REDIS_TRADES_CHANNEL)
```
Why it matters: redis-py's asyncio `client.pubsub()` shares the underlying connection pool with normal commands. With `decode_responses=True` and a single shared `redis_client` (created in each entry-point's `main()`), every long-running subscriber pins one connection from the pool indefinitely (`pubsub.listen()` is an infinite generator). The engine container has 5 such subscribers (`profiler` × 2 channels, `confidence_engine`, `paper_trader`, `graph_engine`, `live_trader`, `telegram_bot`) PLUS commands like `set_overrides`, `publish`, `hincrby` on the same client. Pool exhaustion is real on slow Redis. Worse: when Redis closes the connection (network hiccup, server restart), `pubsub.listen()` raises a `ConnectionError` that is caught by the surrounding `try/except Exception` and re-iterated — but the subscription state is GONE on the new connection. Symptoms: silent message loss on reconnect.
Fix: use a SEPARATE `Redis.from_url(...)` client per pubsub subscriber, or at minimum use `pubsub(ignore_subscribe_messages=True)` + a manual reconnect loop that re-issues `SUBSCRIBE` on connection error. Wrap each `_subscribe_loop` body in `while self._running:` with try/except `ConnectionError`/`asyncio.CancelledError`; on disconnect, sleep + reconnect + re-subscribe. The `position_tracker`, `behavior_profiler`, `graph_engine`, `paper_trader`, `live_trader`, `confidence_engine`, `telegram_bot/notifier`, `ws_bridge` all need this treatment.
Effort: M (per subscriber); S each.

### F-05: Killswitch cache reads return stale state for 2s after every flip (TTL=2) but invalidation is best-effort   [Severity: P0]
File: `src/control/killswitch.py:36, 270-272, 344-352`
Pattern:
```python
REDIS_TTL_S = 2  # short — DB is source of truth, cache just absorbs hot read load
...
async def _invalidate_cache(self) -> None:
    if self._redis is None: return
    try:
        res = self._redis.delete(REDIS_KEY)
        ...
    except Exception as e:
        logger.warning(f"killswitch: redis cache invalidate failed: {e}")
```
Why it matters: killswitch is the authoritative gate for live trading. The flow on flip: write DB → invalidate Redis → write Redis → publish change. If `_invalidate_cache` raises (caught by `try/except`), then `_write_cache` runs anyway with the new value — fine in this code. BUT `_write_cache` happens AFTER `_publish_change`'s broadcast and after the calling worker has accepted the mutation. A reader hitting `get_state()` between DB commit and Redis SET sees the OLD cached value for up to TTL_S seconds. The killswitch_sync job runs every `KILLSWITCH_SYNC_INTERVAL_S` seconds (typically 60-300s) and `_read_cache` is called on every trade attempt; a botched real-execution flip can leak ~2s of trades through. For paper that's tolerable, for live it is not.
Fix: read from DB on every `is_real_execution_enabled()` call (trade gate path). Keep the cache only for `get_state()` dashboard reads. Or use Redis pub/sub on `control:killswitch_changed` to PUSH-invalidate every reader (the channel exists at line 34 but no subscriber consumes it — only the Telegram notifier).
Effort: S

---

## P1 — High (correctness / performance / leak risk)

### F-06: `confidence_engine.precompute_redis_cache` writes N SETs sequentially — should pipeline   [Severity: P1]
File: `src/engine/confidence_engine.py:742-773`
Pattern:
```python
for row in rows:
    ...
    await setter(f"{CACHE_PREFIX}{wallet}", json.dumps(payload), ex=...)
    cached += 1
```
Why it matters: `rows` here is every non-excluded leader (~200-2000). Each `await setter(...)` is one round-trip → ~2000 RTTs per precompute. With a 1ms RTT it's 2s; with prod Redis on the same host but Docker bridge it's measurable. The whole cache-warm sequence is run on boot and on every nightly batch.
Fix: replace with `async with self._redis.pipeline(transaction=False) as pipe: for row in rows: pipe.set(...); await pipe.execute()`. Cuts to a single round-trip.
Effort: S

### F-07: `LeaderRegistry.run()` runs 4 long DB methods in ONE pooled connection while making external Falcon HTTP calls between statements   [Severity: P1]
File: `src/registry/leader_registry.py:491-503`
Pattern:
```python
async with get_db() as conn:
    await self.refresh_leaderboard(conn)   # also calls Falcon API
    await self.enrich_leaders(conn)        # calls Falcon for every wallet
    await self.sync_markets(conn)          # calls Falcon + Gamma
    await self.recategorize_unknowns(conn) # pure SQL
```
Why it matters: `enrich_leaders` (lines 117-192) iterates up to 300 leaders and makes a Falcon HTTP call for each. Each `await self.falcon.get_wallet360(wallet)` is potentially seconds long (the Falcon API caches for 48h but a cache miss is several seconds). During every one of those `await`s, the asyncpg connection is sitting idle but still acquired from the pool (default pool size 10, so ~10% of capacity is parked here for the entire registry cycle). With multiple workers this starves the pool.
Fix: don't hold a DB connection across HTTP awaits. Restructure `enrich_leaders` to (1) fetch the wallet list under one connection, release; (2) call Falcon for each wallet without a connection; (3) re-acquire briefly to UPDATE each row (or batch them into one executemany). Same pattern in `sync_markets` (lines 289-384).
Effort: M

### F-08: `_process_trade`'s second `get_db()` block holds a connection across the whole Gamma HTTP call inside `_fetch_market_metadata_from_gamma`   [Severity: P1]
File: `src/observer/trade_observer.py:1029-1079`
Pattern:
```python
if self._needs_market_enrichment(market_id, market_row):
    try:
        enriched = await self._fetch_market_metadata_from_gamma(market_id, token_id)
    except Exception as exc: ...
    if enriched:
        try:
            async with get_db() as conn:
                await conn.execute(""" INSERT INTO markets ... ON CONFLICT DO UPDATE ... """, ...)
```
Why it's OK here, why other places aren't: this one actually closes the conn before the HTTP call. Good. But `_repair_market_from_trade_hint` (line 989, 1123-1230) accepts an existing `conn` from the parent `_process_trade` and runs an UPDATE on it inside the SAME outer `async with get_db()` block (line 925). That outer block also wraps a `_get_recent_leader_market_ids` call (line 706) — wait no, that's separate. The actual bug here: `_repair_market_from_trade_hint` runs SQL on the connection while the parent loop is in the middle of its insert chain (also without `conn.transaction()`).
Fix: collapse `_repair_market_from_trade_hint`'s SQL into the same transaction as F-03's fix. No external HTTP from inside; this method is pure DB.
Effort: S (subsumed by F-03)

### F-09: `runtime_config.set_overrides` writes Redis AND publishes inside the asyncio lock — slow Redis blocks all readers   [Severity: P1]
File: `src/control/runtime_config.py:167-189`
Pattern:
```python
async with self._lock:
    existing = dict(self._cache.values) if self._cache else {}
    if self._redis is not None and not self._cache:
        try:
            raw = await self._redis.get(REDIS_KEY)
            ...
    existing.update(clean)
    payload = json.dumps(existing)
    if self._redis is not None:
        try:
            await self._redis.set(REDIS_KEY, payload)
            await self._redis.publish(REDIS_PUBSUB_CHANNEL, ...)
```
Why it matters: `self._lock` serializes ALL `_load_overrides` calls (line 116). So one slow Redis write here blocks every other coroutine that tries to refresh its cached overrides. RiskManager's `check_can_trade` calls `get_runtime_config().effective()` on every paper trade — under Redis pressure, all trade attempts queue behind the lock.
Fix: do the Redis I/O OUTSIDE the lock. Take the lock only to mutate `self._cache`. Pattern: build payload → release lock → do Redis I/O → re-acquire briefly to update cache.
Effort: S

### F-10: `_get_market_tokens` caches only positive results; an unmapped market re-queries DB every trade for that market   [Severity: P1]
File: `src/observer/position_tracker.py:420-448`
Pattern:
```python
cached = self._market_tokens.get(market_id)
if cached is not None and (cached[0] or cached[1]):
    return cached
# ... DB query ...
if tokens[0] or tokens[1]:
    self._market_tokens[market_id] = tokens
return tokens
```
Why it matters: the comment at line 432 explains the design intent (re-query unresolved tokens hoping for enrichment). But the call site is `_handle_buy` → `_sibling_token` → `_get_market_tokens` on EVERY incoming trade. For markets with no token mapping (common for fresh markets), every trade triggers a `SELECT token_yes, token_no FROM markets WHERE market_id=$1`. With the observer pulling 50+ trades/min on a market with no mapping, that's 50+ DB queries/min for a market that won't get a mapping until the next observer cycle.
Fix: cache negative results too, with a short TTL (e.g. 60s). Or use the existing `_market_meta_cache` TTL machinery from trade_observer.
Effort: S

### F-11: Trade dedup uses MD5 — collision-resistant but the bucket is 1-second wide — same wallet placing two same-side same-size orders in the same second is dedup'd wrongly   [Severity: P1]
File: `src/observer/trade_observer.py:823-840`
Pattern:
```python
def _dedup_key(self, wallet, market_id, trade_time, side, price, size_usdc) -> str:
    day = trade_time.strftime("%Y%m%d")
    bucket = int(trade_time.timestamp() // 1)  # 1-second buckets
    raw = f"{bucket}:{side}:{price}:{size_usdc}"
    h = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"{DEDUP_KEY_PREFIX}:{wallet}:{market_id}:{day}:{h}"
```
Why it matters: a leader who fires two limit orders of the same size at the same price within the same second (rare but possible — algorithmic traders do this constantly, and the project EXCLUDES them but exclusion happens AFTER detection) gets the same dedup key for both → second one is silently dropped. The DB UNIQUE constraint at line 954 is `(wallet_address, market_id, time, side, price, size_usdc)` and `time` is a TIMESTAMPTZ with microsecond precision — so the DB would accept both. Net effect: Redis dedup is stricter than DB dedup, dropping legitimate trades. Also: MD5 truncated to 12 hex chars = 48 bits. Birthday-paradox collision probability per leader-day = sqrt(2^48) ≈ 16M trades, so MD5 truncation isn't the real issue — the bucket width is.
Fix: bucket on `trade_time.timestamp() * 1000` (millisecond bucket). Or better, use `trade_id` from the data-api response when present (line 90 of `models.py`: `trade_id` is an optional field).
Effort: S

### F-12: `confidence_engine._log_decision` retries with a fallback INSERT on the SAME connection assumption — if the first INSERT failed for connection reasons, the second will too   [Severity: P1]
File: `src/engine/confidence_engine.py:806-865`
Pattern:
```python
try:
    async with get_db() as conn:
        await conn.execute("""INSERT INTO decision_log (..., signal_audit) VALUES (..., $11::jsonb)""", ..., json.dumps(audit_payload))
except Exception as e:
    logger.warning(f"Extended decision log failed, retrying legacy insert: {e}")
    try:
        async with get_db() as conn:
            await conn.execute("""INSERT INTO decision_log (...) VALUES (...)""", ...)
    except Exception as fallback_exc:
        logger.error(f"Failed to log decision: {fallback_exc}")
```
Why it matters: the comment says "fallback to legacy insert" — so this is a schema-version-tolerance hack. The real issue: this is called on EVERY decision (every leader trade fires `_log_decision`). On a transient DB blip, both inserts will fail and the entire decision-log audit trail goes silent. The `except Exception` swallows ALL errors, including ProgrammingError (real schema bug), DataError (bad input), and PostgresConnectionError (transient).
Fix: distinguish error types. Catch `asyncpg.UndefinedColumnError` specifically (the only legitimate trigger for "schema rollback"). For everything else, raise after logging — let upstream decide. Also: `_log_decision` is fire-and-forget on the hot path; consider routing to a write-behind queue rather than inlining a DB roundtrip per decision.
Effort: S

### F-13: `_subscribe_loop` patterns swallow ALL exceptions inside the `async for` — a single bad message kills the iterator silently on some redis-py versions   [Severity: P1]
File: `src/profiler/behavior_profiler.py:108-118`, `src/observer/position_tracker.py:64-74`, `src/graph/graph_engine.py:43-52`, `src/engine/{confidence_engine,paper_trader}.py`
Pattern:
```python
async for message in pubsub.listen():
    if not self._running:
        break
    if message["type"] != "message":
        continue
    try:
        trade = json.loads(message["data"])
        await self.on_trade(trade)
    except Exception as e:
        logger.error(f"GraphEngine error: {e}")
```
Why it matters: `pubsub.listen()` is an `async for` over an iterator that can RAISE on connection drop (ConnectionError, redis.exceptions.ConnectionError). The `try/except` is only around the message processing, not around the iteration itself. When the iterator raises, the `finally` block unsubscribes (which itself may raise — the connection is already gone) and the coroutine returns. The watchdog (`engine/watchdog.py`) restarts it via `factory()`, but the watchdog only checks `state.task.done()` — and a coroutine that returned cleanly (vs raised) is `done()` with no exception, so the watchdog logs "task ended" and restarts, but you've lost any messages between connection-drop and restart-completion.
Fix: wrap the entire `async for` in an outer `while self._running:` retry loop with `try/except (ConnectionError, RedisError):`. Resubscribe explicitly, with backoff. The `live_trader.py` and `telegram_bot/notifier.py` versions use `pubsub.get_message(timeout=1.0)` which is safer but still doesn't reconnect on socket close.
Effort: M

### F-14: `_trade_exists` does a 6-column-WHERE SELECT to detect a Redis dedup false-positive — that's a sequential scan if the UNIQUE INDEX doesn't perfectly match column order   [Severity: P1]
File: `src/observer/trade_observer.py:851-883`
Pattern:
```python
async def _trade_exists(self, market_id, wallet_address, trade_time, side, price, size_usdc) -> bool:
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """SELECT 1 FROM trades_observed WHERE market_id = $1 AND wallet_address = $2 AND time = $3 AND side = $4 AND price = $5 AND size_usdc = $6 LIMIT 1""", ...
            )
            return row is not None
    except Exception:
        return False
```
Why it matters: this runs EVERY time a Redis dedup hits (line 905 in `_process_trade`). Migration 007 added an idempotency UNIQUE index on `(wallet_address, market_id, time, side, price, size_usdc)` (per CLAUDE.md mention) — but this query's WHERE columns are in DIFFERENT order (`market_id` first, `wallet_address` second). PostgreSQL is good at btree column-order rewriting for equality predicates, so this SHOULD use the index — but only if the index is ON those exact columns. If the index is `(wallet_address, market_id, time, ...)` (per CLAUDE.md observer doc), this query benefits from index leading-prefix matching but only for the `market_id` predicate, since the leading column `wallet_address` is NOT first in this WHERE. Worth a query-plan check.
Fix: rewrite WHERE to lead with `wallet_address = $2` so it matches the unique index's leading column. Better: use the index directly via `SELECT id FROM trades_observed WHERE wallet_address=$1 AND market_id=$2 AND time=$3 AND side=$4 AND price=$5 AND size_usdc=$6 LIMIT 1`. Even better: don't run this safety check at all — let the DB UNIQUE constraint reject the duplicate on INSERT (it already does, line 954-955) and trust that path.
Effort: S

### F-15: `risk_manager` uses f-string SQL with `V1_PAPER_TRADE_SQL` interpolated — values come from constants but the pattern invites future SQL injection   [Severity: P1]
File: `src/engine/risk_manager.py:16, 200-206, 218-224, 233-239, 251-258`; same pattern in `src/profiler/{behavior_profiler,error_model}.py`
Pattern:
```python
V1_PAPER_TRADE_SQL = valid_paper_trade_filter()  # returns "economic_model_version = 'v1.0.0' AND invalidated_at IS NULL"
...
row = await conn.fetchrow(
    f"""
    SELECT COUNT(*) AS cnt FROM paper_trades
    WHERE market_id=$1
      AND closed_at >= $2
      AND pnl_usdc < 0
      AND {V1_PAPER_TRADE_SQL}
    """,
    market_id, since,
)
```
Why it matters: `valid_paper_trade_filter()` (in `src/economics/versioning.py:8-12`) ALSO uses f-string interpolation with `ECONOMIC_MODEL_VERSION` from the same package. Today that's a hardcoded string `"v1.0.0"`. But the precedent is dangerous: if anyone later changes `ECONOMIC_MODEL_VERSION` to come from settings (which is env-driven), the filter becomes attacker-controlled SQL via `.env`. A defender reading this code in 6 months will not realize the constant chain is the only thing keeping it safe.
Fix: change the filter helpers to return parameterized predicates (e.g. `economic_model_version = $N`) and pass the version as a real `$` parameter. Or at minimum, add `assert ECONOMIC_MODEL_VERSION in ALLOWED_VERSIONS` at import time.
Effort: M

### F-16: `api/queries.wallet_markets` interpolates `window_days` into INTERVAL clause via f-string   [Severity: P1]
File: `src/api/queries.py:3553, 3562, 3591, 3626`
Pattern:
```python
f"""
... AND t.time >= NOW() - INTERVAL '{window_days} days'
"""
```
Why it matters: `window_days` is typed `int` in the FastAPI handler (`src/api/main.py:814`), so attacker-controlled string injection is blocked by FastAPI's coercion. BUT the CLAUDE.md project rules explicitly forbid string-formatted SQL. And there's no upper bound on `window_days` in the validator (default 30, but 9999999 is accepted), so a malicious caller can ask for `INTERVAL '999999999 days'` — the SQL engine will reject the overflow but you've now spent connection cycles parsing it. Pattern is reused in 4 places in the same query function.
Fix: pass interval as a parameter: `... time >= NOW() - ($N::int * INTERVAL '1 day') ...`. Add `Query(..., ge=1, le=365)` to the FastAPI handler.
Effort: S

### F-17: API `_pool` is sized 2-10 connections but is shared across 22 endpoints, every WS broadcast loop, the snapshot composer (gathers 17 queries in parallel), AND the `/api/v1/live-summary` ETag path   [Severity: P1]
File: `src/api/main.py:87-91, 581-601`
Pattern:
```python
_pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=2, max_size=10)
...
results = await asyncio.gather(
    _fetch_overview_snapshot(),
    _fetch_ml_snapshot(),
    ...   # 17 queries total
    return_exceptions=True,
)
```
Why it matters: each `_fetch_*_snapshot()` does `async with _pool.acquire() as conn:`. The composer fans out 17 queries in parallel; with `max_size=10`, 7 of them will queue. Combined with the stats push loop firing every 1.0s and conditional-GETs polling every 5s, peak demand can routinely touch the pool ceiling. Symptom: random latency spikes on the dashboard.
Fix: bump `max_size` to ~20 for the API pool (engine/observer can stay smaller). Or refactor `_get_terminal_snapshot` to acquire ONE connection and run all 17 queries on it serially — the latency is dominated by the slowest query, not by total CPU. The current parallel pattern is wasteful.
Effort: S

### F-18: `_purge_orphan_heartbeats` uses `SCAN` correctly but doesn't bound the total runtime — a Redis with 100k keys will block the cleanup job for minutes   [Severity: P1]
File: `src/engine/jobs/redis_cleanup.py:48-74`
Pattern:
```python
while True:
    cursor, keys = await redis_client.scan(cursor=cursor, match=match, count=200)
    for key in keys:
        try:
            ttl = await redis_client.ttl(key)
        except Exception:
            continue
        if ttl == -1:
            ...
    if cursor == 0:
        break
```
Why it matters: `SCAN` is non-blocking but `await redis_client.ttl(key)` is one round-trip per key — with `count=200` and 100 iterations, that's tens of thousands of RTTs. The job runs at a cron hour; if it hasn't finished by the next tick, APScheduler's `coalesce=True` skips one. Acceptable in the steady state but degrades with key growth.
Fix: pipeline the TTL+DELETE pair: for each batch of 200 keys, queue 200 `pipe.ttl(key)` then a final `pipe.execute()`; iterate the response, queue deletes in a second pipeline.
Effort: S

### F-19: `live_trader.open_trade` writes a `live_trades` row in a tx, hands off to OrderManager which does its own DB writes, then writes a follow-up UPDATE in a SEPARATE tx — three independent atomicity domains for one logical trade   [Severity: P1]
File: `src/engine/live_trader.py:232-295`
Pattern:
```python
async with get_db() as conn:                         # tx 1: insert pending
    live_trade_id = await conn.fetchval("""INSERT INTO live_trades ...""", ...)
outcome = await self._order_manager.place_for_position(...)   # arbitrary number of tx 2..N (live_orders inserts)
async with get_db() as conn:                         # tx N+1: finalize
    await conn.execute("""UPDATE live_trades SET status='open', entry_price=$2 ...""", ...)
```
Why it matters: if the engine dies between the OrderManager's last `live_orders` row and the final UPDATE, `live_trades` stays in `status='pending'` forever, even though orders ARE filled on chain. On restart `_reload_open_trades` only loads `status='open'` (line 145), so the pending row is invisible to monitoring.
Fix: at restart, scan for `status='pending'` rows and reconcile with `live_orders` join — if any `live_order` is `filled`, promote `live_trades` to `open`. Or use the DB to drive: a trigger that sets `status='open'` when a live_orders row goes to `filled`.
Effort: M

---

## P2 — Medium (cleanup / hardening)

### F-20: `_get_recent_leader_market_ids` does the same query in two branches — the only difference is whether to call `update()` or `replace()` on the bounded set   [Severity: P2]
File: `src/observer/trade_observer.py:732-778`
Why it matters: code duplication, two near-identical SQL blocks, easy to drift out of sync.
Fix: extract a helper that fetches the rows; call it once, branch on whether to `update` or `replace`.
Effort: S

### F-21: `BehaviorProfiler._save_profile` does an INSERT into `leaders` with `ON CONFLICT DO NOTHING` followed by an INSERT into `leader_profiles` with `ON CONFLICT DO UPDATE` — no transaction   [Severity: P2]
File: `src/profiler/behavior_profiler.py:498-536`
Why it matters: similar to F-01 — two statements that should be atomic. If a crash hits between them, you have a `leaders` row but no `leader_profiles` row, which the next pass will paper over via the same idempotent path. So this one is mostly OK in practice. Still: wrap in `conn.transaction()`.
Effort: S

### F-22: `error_model._save_model` and `_save_runtime_profile` are separate UPDATEs called sequentially in `_upgrade_phase` — should be one UPDATE   [Severity: P2]
File: `src/profiler/error_model.py:307-353`
Why it matters: two round-trips, two UPDATEs on the same row, no transaction. They almost-always run together (`_upgrade_phase` -> `_save_model` -> `_save_runtime_profile`).
Fix: combine into one UPDATE that sets all 4 columns (`error_model_phase`, `error_model_blob`, `profile_json`, `economic_model_version`). Or wrap in `conn.transaction()`.
Effort: S

### F-23: `GraphEngine._update_edge` does SELECT + INSERT-ON-CONFLICT-UPDATE — race between the SELECT (which reads `co_occurrences`) and the INSERT (which writes `co_occurrences = $3`)   [Severity: P2]
File: `src/graph/graph_engine.py:215-279`
Pattern:
```python
async with get_db() as conn:
    row = await conn.fetchrow("""SELECT co_occurrences, ... WHERE leader_wallet=$1 AND follower_wallet=$2""", ...)
    if row is None: new_count = 1
    else: new_count = (row["co_occurrences"] or 0) + 1
    ...
    await conn.execute("""INSERT INTO follower_edges ... ON CONFLICT (leader_wallet, follower_wallet) DO UPDATE SET co_occurrences = EXCLUDED.co_occurrences ...""", ...)
```
Why it matters: between SELECT and INSERT, another GraphEngine instance (e.g. on a different worker) can update the row. Both instances read `co_occurrences=5`, both compute `new_count=6`, both write `co_occurrences=6`. One increment is lost. With a single GraphEngine instance per deployment this doesn't bite, but the architecture allows multiple workers.
Fix: use `INSERT ... ON CONFLICT ... DO UPDATE SET co_occurrences = follower_edges.co_occurrences + 1, follow_beta_a = follower_edges.follow_beta_a + $X, ...` — push the increment into the DB. Eliminates the SELECT entirely.
Effort: M

### F-24: `LeaderRegistry.refresh_leaderboard` uses `executemany` (good!) but follows it with a separate `UPDATE ... WHERE NOT (wallet_address = ANY($1))` — these two statements should be one transaction   [Severity: P2]
File: `src/registry/leader_registry.py:64-78`
Why it matters: a crash between the upsert and the watchlist-clear leaves stale wallets on the watchlist for one cycle.
Fix: `async with conn.transaction(): await conn.executemany(...); await conn.execute("UPDATE leaders SET on_watchlist=FALSE WHERE ...")`.
Effort: S

### F-25: `_safe_off_state` returns a fresh `KillswitchState` on every infra failure, but the `updated_at` is `now()` — confuses observability metrics   [Severity: P2]
File: `src/control/killswitch.py:355-362`
Why it matters: when the DB is down, `get_state()` returns SAFE-OFF with a fresh `updated_at`. The dashboard then shows "killswitch flipped 2 seconds ago" every refresh, even though nothing flipped — the user will think the system is unstable.
Fix: cache the safe-off state with a fixed `updated_at` (e.g. process start time) — only update it when DB comes back and confirms a real flip.
Effort: S

### F-26: `WSBridge._consume_loop` doesn't handle reconnect; if Redis drops, the only recovery is a full API restart   [Severity: P2]
File: `src/api/ws_bridge.py:66-92`
Why it matters: the dashboard goes silent on Redis hiccup until uvicorn is restarted. Pubsub-on-shared-client (F-04) makes this worse.
Fix: same fix as F-13 — wrap the `async for` in a reconnect loop.
Effort: S

### F-27: API endpoints use `_pool.acquire()` directly via shorthand `async with _pool.acquire() as conn:` 22 times — but the helper `_conn()` defined at line 125 is unused dead code   [Severity: P2]
File: `src/api/main.py:125-129`
Why it matters: `async def _conn(): return _pool.acquire()` is defined but no caller uses it. Dead code that misleads future readers about the connection pattern.
Fix: delete `_conn()` (5 lines).
Effort: S

### F-28: `redis_async.from_url(decode_responses=True)` is fine for all the JSON paths, but the killswitch's `_publish_change` calls `self._redis.publish(...)` and then awaits the result — on a `decode_responses=True` client this returns an int, not bytes; the existing `inspect.isawaitable` guard is unnecessary on redis-py 5.x asyncio (always awaitable)   [Severity: P2]
File: `src/control/killswitch.py:269-272, 318-321, 338-340, 348-351`
Why it matters: not a bug, but the `inspect.isawaitable` checks suggest someone wasn't sure if the client was sync or async. With `redis.asyncio` the API is always-async; the checks are dead defensive code.
Fix: remove the `inspect.isawaitable` paths — they hide real bugs (e.g. a sync redis instance being passed in by mistake would silently work in production but fail tests).
Effort: S

### F-29: `_handle_ws_message` calls `self._redis.incrby` then `self._redis.expire` as two round-trips — should be one pipeline   [Severity: P2]
File: `src/observer/trade_observer.py:362-375`
Why it matters: every WS message (price_change, book, trade) does two Redis RTTs. With high WS throughput (~100/s on busy markets), that's 200 redis ops/s for accounting alone. Should be 100.
Fix: `async with self._redis.pipeline() as pipe: pipe.incrby(...); pipe.expire(...); await pipe.execute()`.
Effort: S

### F-30: `_record_book_metrics` does up to 4 Redis writes serially (setex, setex, then `_persist_book_quality_snapshot` which uses a fresh DB connection) — same pipeline opportunity   [Severity: P2]
File: `src/observer/trade_observer.py:511-562`
Why it matters: every WS book event hits Redis 2-4 times serially.
Fix: pipeline the Redis writes.
Effort: S

### F-31: `_subscribe_loop`'s `pubsub.unsubscribe()` in the `finally` block can hang if the connection is already broken — should have a timeout   [Severity: P2]
File: most `_subscribe_loop` implementations
Why it matters: shutdown can hang if Redis is unreachable when stop is called.
Fix: `await asyncio.wait_for(pubsub.unsubscribe(...), timeout=2.0)` with a try/except around it.
Effort: S

---

## P3 — Low (style / forward-looking)

### F-32: `_thompson_state` is rebuilt per-process; cache hit-rate optimization opportunity via Redis-backed shared state   [Severity: P3]
File: `src/engine/confidence_engine.py:79`
Why it matters: each engine restart re-seeds Thompson from `decision_learning` blob. With multiple workers (future scaling), each maintains its own state and they diverge. The `precompute_redis_cache` already exists to push the state to Redis — but no consumer reads it on the hot path; only the `_seed_thompson_from_cache` path does (called once per wallet on first signal).
Fix: long-term, write Thompson updates THROUGH to Redis on every `update_thompson` call.
Effort: L

### F-33: `_fetch_training_data` does TWO `conn.fetch` calls inside the same `async with get_db()` — fine, but the third query for follower_edges is also there with no transaction   [Severity: P3]
File: `src/profiler/error_model.py:474-557`
Why it matters: read-only queries don't STRICTLY need a transaction, but for consistency snapshot semantics across the three queries (positions, observed trades, follower edges), a `conn.transaction(isolation='repeatable_read')` would freeze a consistent view for training. Without it, the LightGBM training set can be slightly mismatched (e.g. an edge confirmed mid-fetch).
Fix: wrap in `conn.transaction(isolation='repeatable_read')`.
Effort: S

### F-34: `_fetch_market_metadata_from_gamma` opens a fresh `aiohttp.ClientSession()` for every call — should reuse a session   [Severity: P3]
File: `src/observer/trade_observer.py:1253-1305`, also `src/observer/trade_observer.py:626-635` (`_backfill_from_data_api`)
Why it matters: not a DB issue but related to the "long-running connections" theme — every Gamma lookup costs a TLS handshake.
Fix: keep a class-level `aiohttp.ClientSession` opened in `start()`, closed in `stop()`.
Effort: M

### F-35: Migration 007's idempotency UNIQUE INDEX is documented in CLAUDE.md but the inserts at `_process_trade` use `(wallet_address, market_id, time, side, price, size_usdc)` ordering for the `ON CONFLICT` clause, while migration 007's index column order isn't visible from this audit — verify alignment   [Severity: P3]
File: `src/observer/trade_observer.py:954`
Why it matters: `ON CONFLICT (wallet_address, market_id, time, side, price, size_usdc) DO NOTHING` will only match an index of EXACTLY that column set. If migration 007 created the index with different column ordering, the constraint won't fire and duplicates leak in.
Fix: confirm migration 007's `CREATE UNIQUE INDEX` statement uses the same column list. (Out of scope for client audit — schema audit job.)
Effort: S (verification only)

### F-36: `paper_trader._is_market_resolved`, `_leader_exited_recently`, `_has_open_trade_conflict`, `_has_recent_reentry_conflict`, `_get_current_price`, `_get_fee_rate`, `_get_opposite_token` — every paper-trade-eligibility check opens its own DB connection   [Severity: P3]
File: `src/engine/paper_trader.py:738-917`
Why it matters: opening a paper trade currently does up to 7 DB round-trips (one per check). For a heavy decision burst these queue against the pool.
Fix: collapse the eligibility checks into ONE SQL CTE that returns all the booleans at once.
Effort: M

### F-37: `_get_current_price` reads Redis price cache but falls back to `SELECT price FROM trades_observed ORDER BY time DESC LIMIT 1` — that ORDER BY is expensive without a `(market_id, token_id, time DESC)` covering index   [Severity: P3]
File: `src/engine/paper_trader.py:738-762`
Why it matters: called every 60s per open trade in `_check_open_positions`. With 10 open trades, that's 10 queries/min hitting `trades_observed`. The cache should normally serve, but in the 5-minute window after an outage the DB fallback is hammered.
Fix: pre-warm the Redis price cache on engine boot (`SELECT DISTINCT ON (market_id, token_id) ... ORDER BY time DESC` once, write all entries with a generous TTL).
Effort: M

### F-38: Connection pool size mismatch across services — engine, observer, registry all use `settings.DB_POOL_MIN`/`MAX` but the API hardcodes 2-10   [Severity: P3]
File: `src/api/main.py:87-91` vs all `*/main.py`
Fix: route the API through `settings.DB_POOL_MIN`/`MAX` too, or set API-specific bounds.
Effort: S

### F-39: `GraphEngine._hydrate_recent_trades` warm-start does 2 passes over the same row set in Python — should be one   [Severity: P3]
File: `src/graph/graph_engine.py:75-99`
Why it matters: minor inefficiency; iterates `rows` once to fill the deque, then again to detect leaders for each non-leader row. With deque size capped at 1000, this is fine, but it's mildly hot on warm-start.
Fix: combine into one pass.
Effort: S

### F-40: `RuntimeConfig._cache` is a single in-memory dict guarded by an asyncio.Lock — but the `invalidate_cache()` method (line 194) is sync and just sets `self._cache = None`, which races with a coroutine in the middle of `_load_overrides`   [Severity: P3]
File: `src/control/runtime_config.py:194-197`
Why it matters: race window is small (microseconds) and the consequence is a redundant Redis fetch — not a correctness issue.
Fix: take the lock in `invalidate_cache()` (make it async), or swap with `self._cache = _CachedOverrides({}, 0.0)` to keep the type stable.
Effort: S

---

## Cross-cutting observations

1. **No `conn.transaction()` ANYWHERE outside `killswitch.py`.** Every other module relies on auto-commit-per-statement. For multi-statement writes that's wrong — see F-01, F-02, F-03, F-21, F-22, F-23, F-24.
2. **Pub/sub on shared client.** Six subscribers in the engine container all share one `redis.asyncio.Redis` instance with command callers. See F-04 — this is the single biggest reliability risk in the Redis layer.
3. **Bulk operations are loops, not pipelines.** Only `LeaderRegistry.refresh_leaderboard` uses `executemany`. `precompute_redis_cache`, `_purge_orphan_heartbeats`, the WS message accounting paths all loop with sequential `await`s.
4. **No N+1 SELECT-in-loop pattern was found in production code paths.** The closest is `enrich_leaders` (Falcon HTTP per wallet, but each is one SQL UPDATE), which is correct given the rate-limit constraint. Good.
5. **f-string SQL is rare.** Only `api/queries.py` (4 INTERVAL injections, F-16), `risk_manager`, and `profiler/{behavior,error}_model` (F-15) use f-string composition, and all interpolated values are numeric or constants. Acceptable today, fragile tomorrow.
6. **Connection-leak risk on exception is zero**: every `get_db()` is `async with` — asyncpg releases on `__aexit__`. This part of the codebase is clean.
7. **`fakeredis` boundary**: `inspect.isawaitable` checks in killswitch (F-28) are defensive against fakeredis returning sync values. Tests probably depend on this. Keep until fakeredis is replaced.
