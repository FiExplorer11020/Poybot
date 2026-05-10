# Registry Module — Leader Identification via Falcon API

**Purpose**: Discover and enrich the leader watchlist using Falcon API. Maintain a PostgreSQL
registry of influential wallets with their Falcon metrics, dynamic classification, and exclusion rules.

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **falcon_client.py** : Unified async client for all Falcon agents (584, 581, 556, 569,
  574, 575, 568, 572, 579, 585). Handles pagination, retry logic, rate limiting, and
  Redis caching (48h TTL).

- **leader_registry.py** : Maintains the `leaders` table. The `run()` loop performs three
  steps per cycle: (1) `refresh_leaderboard` pulls top N from agent 584 (Falcon Score)
  with PnL leaderboard fallback, (2) `enrich_leaders` calls agent 581 on stale wallets and
  stamps `excluded=TRUE, on_watchlist=FALSE, exclude_reason='falcon_no_data'` for wallets
  Falcon doesn't recognise (so they stop bloating the active pool and the DQ counters),
  and (3) `sync_markets` fills in missing market metadata via agents 574 + Gamma fallback,
  skipping markets whose `end_date` is more than 24h in the past.

- **models.py** : Pydantic dataclasses for Falcon responses, Classification enum,
  Leader schema.

---

## Key Algorithms

### Falcon API caching (Redis, 48h TTL)
Cache key: `falcon:{agent_id}:{hash(params)}`
On hit: return cached JSON, skip HTTP request.
On miss: fetch → parse → validate with Pydantic → cache → return.
Stale fallback: return 48h old cache if Falcon API is down.

### Dynamic Leader Classification
Per-wallet, inferred from behavior + Falcon metrics:
```
classification_json: {
  strategy: "directional" | "structural" | "cognitive"    [inferred from trade velocity]
  influence: "whale" | "top_trader" | "community"         [from Falcon Score + volume]
  horizon: "scalper" | "swing" | "holder"                [from avg holding period]
  copiable: true | false                                  [from execution speed < 1s]
}
```

### Exclusion Rules (EXCLUDE from trading signals)
1. Structural/bot traders: avg execution speed < 1s consistently → `copiable=false`
2. Wallets with < 10 trades observed → `insufficient_data` (observe only, no signals)
3. Falcon Score ≤ 0 → skip entirely
4. Wallet 360 "bot_detected" flag = true → exclude (from agent 581 metrics)
5. **`falcon_no_data`** → permanent exclusion stamp set by `enrich_leaders` when Falcon
   agent 581 returns no metrics. This typically happens for fresh wallets injected via the
   profiler's FK upsert that Falcon's pipeline hasn't picked up yet. The flip to
   `excluded=TRUE, on_watchlist=FALSE` is irreversible by the runtime — recovery requires
   manual SQL (the `cleanup_falcon_no_data_leaders.sql` script does the catch-up for the
   inverse case where rows were stamped before the patch landed).

---

## Critical Pitfalls

1. **Rate limit Falcon API**: Cache everything, respect pagination, don't retry failed requests immediately.
   Use exponential backoff (2s, 4s, 8s max) and circuit breaker.

2. **Proxy wallets**: Polymarket accounts can use proxy wallets. Same human may have 5+ addresses.
   Watch for: (a) similar trade patterns, (b) coordinated timing. Flag for manual review.

3. **Stale Falcon metrics**: Leaderboard updates ~1x per day. Don't re-rank leaders too often.
   Update classifications only when new observable behavior contradicts old Falcon data.

4. **Don't exclude all whales**: Some whales trade directionally and ARE copiable. Only exclude
   if execution speed < 1s (detect via timestamp precision in observed trades).

---

## Testing Approach

- **Unit tests**:
  - Mock Falcon API responses (JSON fixtures).
  - Test caching: hit/miss/expiry, stale fallback on API error.
  - Test classification logic: verify strategy/influence/horizon/copiable assigned correctly.
  - Test exclusion: bot scores, low Falcon scores, insufficient trades.

- **Integration tests**:
  - Real Falcon API calls (with test API key, rate limited).
  - Verify database writes: `leaders` table populated, classifications updated.
  - Test refresh cycle: pull → classify → update → verify no duplicates.

---

## References
- Falcon agents: 584 (Score), 581 (Wallet 360), 556 (Trades), 579 (PnL Leaderboard)
- `FALCON_API_URL`, `FALCON_API_KEY`, `FALCON_CACHE_TTL_S`, `FALCON_REFRESH_INTERVAL_S` from config.py
- Database: `leaders` table schema in master CLAUDE.md § 6
- Classification constants: `INITIAL_LEADER_COUNT`, `MAX_LEADER_COUNT`, `MIN_FALCON_SCORE` from config.py
- All variable names match exactly what's in `src/config.py`
