# Autonomous Session — Hour 1 Report (2026-05-15)

**Started**: ~12:15 UTC. **This report**: 13:15 UTC.
**Goal**: Reach first paper_trade + first profitable runs.

---

## TL;DR

Major silent failure chain unblocked. The bot had been "running" for ~16h
but producing **zero useful work** because of cascading silent failures in
DB queries, Redis policy, and Polygon API gating. We now have an
**operational data pipeline** for the first time today.

| Metric | T0 | Now | Δ |
|---|---|---|---|
| Containers EXITED | 8 / 19 | 0 / 19 | restored |
| Redis `trades:stream` | NOT EXIST | exists + 3 groups | recreated |
| `live_markets` (`active=TRUE AND end_date > NOW()`) | **0** | **8,897** | +8,897 |
| `markets` with `volume > $10K` | 0 | 958 | +958 |
| Observer `_leader_wallets` loaded | 0 | 50 | +50 |
| Observer REST polling | not firing | firing every 5s | restored |
| `paper_trades_total` | 0 | 0 | ⏳ waiting first cycle |

---

## Silent failures identified + fixed

### 1. Engine crashloop (NOGROUP Redis)

**Symptom**: `polymarket_engine Exited (0)` for ~30 min. 7 other consumer
containers also Exited.

**Root cause**: Redis `maxmemory-policy=allkeys-lru` evicted the
`trades:stream` key after observer had been idle. The
`StreamConsumer._run_loop()` doesn't recreate the stream/group on NOGROUP
errors — it only does that on Connection/Timeout errors.

**Fix applied**:
- Bumped Redis `maxmemory` 128 → 256 MB (live `CONFIG SET`, persisted)
- Changed policy `allkeys-lru` → `volatile-lru` (only evicts TTL'd keys
  — streams are now safe from LRU)
- Manually recreated `trades:stream` + 3 consumer groups (`confidence`,
  `profiler.behavior`, `graph`)
- Restarted all 8 exited containers

**Open**: `redis_streams.py:_run_loop()` should call `_ensure_group()`
on NOGROUP errors. Patch staged but not committed yet.

### 2. `markets.end_date IS NULL` for all 3,544 active markets

**Symptom**: Every gate that filters on `end_date > NOW()` returned 0
markets → no live markets for FOLLOW/FADE routing.

**Root cause**: `sync_markets` in registry depends on Falcon API which
returns 401 (expired key). Gamma API was never wired as a fallback.

**Fix applied**:
- New script `/tmp/backfill_markets_gamma.py` runs in the engine
  container, pages through `gamma-api.polymarket.com/markets` (100 per
  page, ordered by 24h volume desc), UPSERTs into `markets` table
- One-shot run: **10,100 markets fetched, 9,369 inserted, 731 updated**
- Now: **8,897 markets with `end_date > NOW()`**, 958 with `volume > $10K`

**Open**: schedule this as an hourly cron in the engine scheduler
(currently one-shot). Otherwise volume_24h goes stale within hours.

### 3. Observer `_leader_wallets=0` (the headline bug)

**Symptom**: `Observer bootstrap: 0 leader wallets, 100 market tokens` —
which means `TradeObserver._backfill_loop()` short-circuits on
`if not self._leader_wallets: return`. **No trades ever get attributed to
leaders**, so `is_leader=TRUE` never fires, so the confidence engine
never gets a signal.

**Root cause**: `_load_db_subscriptions` runs three queries in one
try/except. The middle one
(`SELECT token_id FROM trades_observed ... GROUP BY ... ORDER BY MAX(time)`)
takes **57–81 seconds** because GROUP BY + ORDER BY MAX can't use an
index on a 14-partition table. asyncpg default `command_timeout=30s` →
exception → whole bootstrap fails silently → `wallets={}` returned.

**Fix applied (source change)**:
- Split the 3 queries into individual try/except blocks in
  `src/observer/main.py::_load_db_subscriptions`
- Replaced the slow query with a time-windowed version:
  `SELECT DISTINCT token_id FROM trades_observed WHERE time >= NOW() - INTERVAL '24 hours' LIMIT 250`
  (uses the existing `(time DESC)` index — sub-second)
- Markets query now orders by `volume_24h DESC` for better priority
- Patched via `docker cp` (image rebuild pending)

**Result**: Observer now logs ~50 `cursor missing for source=api_wallet;
bootstrapping at now - 300s` → polling loop running.

### 4. Polymarket data-api 403 (red herring)

**Symptom**: `urllib.urlopen("https://data-api.polymarket.com/trades")` →
HTTP 403 Forbidden from inside container.

**Root cause**: Polymarket's CDN blocks the default `python-urllib/3.11`
User-Agent. `aiohttp` works (`aiohttp/3.x.y` UA accepted).

**Verdict**: Observer uses `aiohttp` everywhere, so this never affected
production — only my diagnostic test. Documented for posterity.

---

## What's next (Hour 2)

1. **Verify first paper_trade triggers** — wait for next leader trade →
   trace `trades:observed` → confidence_engine → DecisionRouter →
   paper_trader. Need to confirm `signal_audit.accepted=True` (book +
   fee snapshots fresh).

2. **Fix `risk=0.00` zeroing FOLLOWs**: yesterday's 18 FOLLOWs all had
   `kelly_fraction>0` but `context_penalty` zeroed final `size_usdc`.
   Most penalties came from "aggressive_scale_in" and "burst_trading"
   patterns which are common in active leaders — re-tune those penalty
   coefficients in `_reason_penalty_from_profile`.

3. **fee_snapshots hourly refresh job** — current snapshots captured at
   2026-05-14 19:55 UTC, so they expire (`max_fee_age_s=24h`) at
   19:55 UTC today. Need to add a cron in `src/engine/scheduler.py`.

4. **Patch `redis_streams._run_loop`** to call `_ensure_group()` on
   NOGROUP errors — prevents this whole class of cascade.

5. **Start synthetic stress test**: inject a fake leader trade via Redis
   pub/sub to verify the full pipeline end-to-end.

---

## Production state (live snapshot)

```
SSH ok, 19/19 containers running healthy
Redis: 48 MB / 256 MB, volatile-lru policy
DB: 4.5 GB used, healthy
trades_observed total: 554,765 (mostly backfill from yesterday)
leaders.excluded=FALSE: 1,561 (200 on_watchlist, 696 explicitly excluded)
leader_profiles.maturity > 0.5: 1
decision_log past 24h: 95 (last live FOLLOW: yesterday 19:25 UTC)
paper_trades: 0
fee_snapshots: 1,360 (1 capture at 2026-05-14 19:55 — expires in ~7h)
microstructure_features: still flowing (~140/h post fix from yesterday)
multivariate_hawkes_fits today: 50
causal_estimates: 0 (waiting J+2/J+3 convergence)
```

---

## Code changes pending commit

```
src/observer/main.py      — split 3 bootstrap queries (silent failure fix)
+ docker-side patches:
  redis maxmemory 256MB, maxmemory-policy=volatile-lru
  markets.end_date populated (10,100 rows from Gamma)
  trades_observed_*_token_id_time idx on 14 partitions
```

These need a proper commit + image rebuild + force-recreate. Until then
they survive only on the running container's filesystem and may be lost
on next deploy.
