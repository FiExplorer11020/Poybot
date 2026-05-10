# Phase 0 Task C — `markets.liquidity_score` source switch + leakage mitigation

**Audit reference**: `docs/audit/05_ml_pipeline.md` MG-3 + §3.1 (training leakage).

**Owner**: coder agent (Phase 0 Task C).

**Status**: Layer 1 (source switch) + Layer 2 (semantics) shipped. Layer 3
(training leakage) — groundwork only; full feature-store fix deferred to
Phase 3.

---

## 1. What was broken

Per audit MG-3:

1. **Wrong agent**. `markets.liquidity_score` was written by
   `LeaderRegistry.sync_markets` (`src/registry/leader_registry.py:289`)
   from agent **574**'s raw `liquidity` field (`m.get("liquidity")` at
   the old line 348), but every docstring (`src/profiler/CLAUDE.md:172`,
   `src/profiler/error_model.py:83/220`, master `CLAUDE.md:160`/`§6` schema
   comment, `src/registry/CLAUDE.md` Falcon agent table) claims agent
   **575** (Market Insights). Agent 575 was **never called anywhere in
   `src/`** — search confirmed zero references in the codebase prior
   to this task.

2. **24h stale at best**. `sync_markets` only re-fetches markets whose
   `markets.updated_at` is older than 24h. A `liquidity_score`
   refreshed once will not be re-fetched for 24h regardless of how
   stale it actually is. Average lag for the phase-2/3 feature was
   ~12h.

3. **Training leakage** (audit §3.1). `error_model._fetch_training_data`
   reads `markets.liquidity_score` AS-OF-NOW (`COALESCE(m.liquidity_score, 0.5)`,
   `src/profiler/error_model.py:488`). A market that became liquid two
   weeks AFTER a historical position's `open_time` looks liquid in
   training but was illiquid at decision time — classic train/serve skew.

---

## 2. What changed in this task

### Layer 1 — source switch (shipped)

| File | Change |
|---|---|
| `src/registry/models.py` | New `MarketInsights` Pydantic model with `liquidity_score` field (aliases: `liquidity_score` / `normalized_liquidity` / `liquidity`). `extra="allow"` keeps concentration/trend/depth on the model for Phase 3 features. |
| `src/registry/falcon_client.py` | New `get_market_insights(condition_id)` method. Calls agent 575 with `{"condition_id": cid}`, falls back to `{"market_slug": cid}` (mirrors the agent 574 fallback in `sync_markets`). Clamps the returned score to `[0, 1]`: negative → `0`, values >1 are squashed via `tanh(x / 100_000)` so a USD-depth payload doesn't break `_build_features` slot [4]. Returns `None` on Falcon error so the caller can fall through to 574. |
| `src/registry/leader_registry.py` | `sync_markets` now calls `falcon.get_market_insights(mid)` first. On success, writes the 575 score and tags the row `liquidity_score_source='falcon_575'`. On `None`, falls back to the 574 `liquidity` field (or Gamma's `liquidity`) tagged `falcon_574` / `gamma`. When all three sources are empty, the column is left NULL so callers can distinguish "no data" from "zero liquidity". A comment block at the call site cites the audit ID and the documented source. |

### Layer 2 — variable naming + comments (shipped)

- `src/profiler/error_model.py:83` (`predict` docstring) — line now reads
  "from Falcon Market Insights (agent 575), written to
  `markets.liquidity_score` by `LeaderRegistry.sync_markets`
  (Phase 0 Task C fix for audit MG-3)". The previous docstring already
  claimed 575 but was lying; we now tell the reader where the value
  actually comes from in 2026-05.
- `src/profiler/error_model.py:220` (`_build_features` slot [4] comment)
  — adds "sourced via `LeaderRegistry.sync_markets`" so a future
  reader can trace the column back to the registry write path.
- No other code site reads the wrong field — verified via
  `grep -rn 'liquidity_score\|m\.get("liquidity")' src/`. The remaining
  read sites (`src/profiler/behavior_profiler.py`,
  `src/engine/confidence_engine.py`, `src/api/queries.py`,
  `src/observer/trade_observer.py`) all consume the column, not the
  raw agent 574 field, so they pick up the source switch for free.
- The Gamma fallback path in `src/observer/trade_observer.py:1297`
  (`liquidity_score = float(market.get("liquidity") or 0.0)`) was left
  alone: it's a per-trade-arrival enrichment for first-sight markets
  (NOT the periodic refresh), and the row will be overwritten by
  `sync_markets` within the next registry cycle. Tagging it
  retroactively would require touching the same SQL upsert and is
  out of scope for Task C; the row's `liquidity_score_source` will be
  NULL until `sync_markets` lands an authoritative value.

### Layer 3 — training-leakage mitigation (groundwork only)

The full feature-store fix (a `market_liquidity_history(market_id, ts, score)`
table fed by `sync_markets`, queried via an as-of join in
`error_model._fetch_training_data`) is **deferred to Phase 3**. Reasons:

- It requires deciding on retention + sampling cadence (per-minute? per-15-min?
  on-change-only?) which touches the upcoming retention work in Task D.
- It changes the training pipeline's data contract — coordinated migration
  with the eval table proposed in audit §6.

What ships **now** (Task C):

1. **Migration `012_liquidity_score_asof.sql`** adds two columns to
   `markets`:
   - `liquidity_score_updated_at TIMESTAMPTZ` — when the current
     `liquidity_score` was stamped. Distinct from `updated_at`
     (whole-row touch). Once populated for ≥30 days, the as-of read
     in `_fetch_training_data` can use the row's own
     `liquidity_score_updated_at` as a lower-bound timestamp for any
     `pr.open_time` that lies after it.
   - `liquidity_score_source VARCHAR(32)` — provenance tag
     (`'falcon_575'` / `'falcon_574'` / `'gamma'`). Lets the audit
     find rows that bypassed agent 575 and lets a backfill job
     selectively re-refresh `falcon_574` rows when 575 recovers.
   - Both nullable, safe migration on existing rows. An index
     `idx_markets_liq_updated_at` is added for the future as-of
     query.

2. **Leakage marker comment** at `src/profiler/error_model.py:488`
   inside `_fetch_training_data` cites audit §3.1 and MG-3, names the
   Phase 3 follow-up (`market_liquidity_history`), and explains why
   the current code accepts the leakage instead of NULL-ing the
   feature (NULL-ing would regress training samples for phase 2/3
   models). The marker is `# LEAKAGE: liquidity_score is current
   value, not as-of-trade-time. Phase 3 feature-store fix.` — exact
   string the task brief asked for, prefixed with the audit/phase
   identifiers for grepability.

3. No as-of read path is implemented now. The brief explicitly said
   "don't create the as-of read path now; just lay the groundwork" —
   the groundwork is the two new columns + the index.

---

## 3. Migration numbering decision

- Existing migrations: 001 – 010.
- Task D has staked `011_retention_policies.sql` (file already exists
  as a STUB header in the repo — see `docs/migrations/011_retention_policies.sql:1-5`).
- Task C uses **`012_liquidity_score_asof.sql`** per the brief's
  fallback ("if uncertain, use 012 and document why").

---

## 4. Existing-row handling

**Decision**: leave existing rows; let the natural 24h refresh path
drain them.

- Pros: zero risk to live operation. `sync_markets` will overwrite
  every active market within ≤24h after deploy with the correct 575
  score (and the `falcon_575` tag).
- Cons: for the first 24h after deploy, the phase-2/3 error model
  will still read the old (agent 574) score for any market that
  hasn't yet been refreshed.
- Why not `UPDATE … SET liquidity_score = NULL`: the audit said it's
  an option but would force every caller (`behavior_profiler`,
  `confidence_engine`, etc.) to handle `0.5` defaults for ~24h.
  Net feature quality is worse than leaving the (mildly wrong) prior
  value in place.

If the operator wants a faster transition, the migration file
documents the manual one-liner (commented-out, not run automatically):

```sql
UPDATE markets SET updated_at = NOW() - INTERVAL '25 hours'
WHERE end_date IS NULL OR end_date > NOW() - INTERVAL '24 hours';
```

This forces every live market to fall back into the `sync_markets`
selection window on the next registry cycle (`FALCON_REFRESH_INTERVAL_S`
= 1800s = 30 min, per `src/config.py`).

---

## 5. Operational notes

- **Forced full refresh after deploy**: not auto-triggered. See the
  one-liner above. Recommended deploy sequence:
  1. Apply migration 012.
  2. Roll the engine container.
  3. Wait one `FALCON_REFRESH_INTERVAL_S` (~30 min) and confirm
     `SELECT COUNT(*) FROM markets WHERE liquidity_score_source = 'falcon_575'`
     is growing.
  4. If the operator wants the catch-up done within minutes rather
     than 24h, run the manual `UPDATE … SET updated_at = NOW() - 25h`
     during a low-traffic window.
- **Rollback**: dropping the two new columns + the index is safe; no
  code path reads them yet (they're written by `sync_markets` and
  consumed only by the future as-of join).
- **Agent 575 rate limits**: `sync_markets` is bounded to 300 markets
  per cycle (`LIMIT 300` in the SELECT). One call per market =
  ≤300 calls per registry cycle. With `FALCON_REFRESH_INTERVAL_S=1800`,
  that's 600 calls/hour to agent 575. Falcon's per-key budget
  (`FALCON_MAX_REQUESTS_PER_MINUTE`) is the limiting factor;
  `FalconClient._throttle` already enforces it. No new infra change
  required.

---

## 6. Deferred to Phase 3 (not done in this task)

| Item | Reason for deferral |
|---|---|
| `market_liquidity_history(market_id, ts, liquidity_score, source)` time-series table | Touches retention policy (Task D), and requires deciding sampling cadence. |
| As-of read in `error_model._fetch_training_data` (`WHERE ts <= pr.open_time ORDER BY ts DESC LIMIT 1`) | Depends on `market_liquidity_history` existing with ≥30 days of data. The marker comment now makes the call site obvious. |
| Drop the 24h staleness gate in `sync_markets` to 1h for active markets (audit MG-3 fix-c) | Out of scope for Task C; bumps Falcon API load — needs the audit's per-agent budgeting work first. |
| Backfill UPDATE to rewrite existing `liquidity_score_source` on pre-Task-C rows | Not worth the SQL: rows will be overwritten by the natural refresh path within 24h. |
| Wire agent 575 into the per-trade-arrival enrichment in `trade_observer.py:1297` | Different cadence (per-trade hot path), would need its own rate-limit handling. `sync_markets` is the authoritative write site. |

---

## 7. Test coverage added in this task

`tests/test_registry/test_falcon_client.py`:
- `test_get_market_insights_returns_score_from_agent_575`
- `test_get_market_insights_falls_back_to_slug`
- `test_get_market_insights_returns_none_on_empty`
- `test_get_market_insights_returns_none_on_falcon_error`
- `test_get_market_insights_clamps_score_to_unit_interval`
- `test_get_market_insights_clamps_negative_score`

`tests/test_registry/test_leader_registry.py` (within `TestSyncMarkets`):
- `test_sync_markets_writes_575_score_when_available` — proves 575
  wins over 574 and tags `falcon_575`.
- `test_sync_markets_falls_back_to_574_when_575_unavailable` — proves
  the fallback writes the 574 value and tags `falcon_574`.
- `test_sync_markets_writes_null_score_when_no_source_has_data` —
  proves NULL is preserved when nobody has data.
- Existing `test_sync_markets_falls_back_to_gamma_when_falcon_unavailable`
  still passes (default `get_market_insights` mock returns `None`,
  hitting the Gamma fallback path).

---

## 8. Files touched

```
docs/migrations/012_liquidity_score_asof.sql          (new)
docs/audit/phase0/C_liquidity.md                      (this report)
src/registry/models.py                                 (added MarketInsights)
src/registry/falcon_client.py                          (added get_market_insights)
src/registry/leader_registry.py                        (sync_markets switch + provenance write)
src/profiler/error_model.py                            (docstrings + LEAKAGE marker)
tests/test_registry/test_falcon_client.py              (6 new tests)
tests/test_registry/test_leader_registry.py            (3 new tests + fixture update)
```
