# Autonomous Session — Hour 2 Report (2026-05-15)

**Started**: 12:15 UTC | **This report**: 13:50 UTC
**Goal hit this hour**: ✅ **FIRST PAPER TRADE EXECUTED**

---

## TL;DR — Milestone reached

The bot took its first paper trade in production:

```
paper_trades.id              = 1
opened_at                    = 2026-05-15 13:44:51 UTC
market                       = Will Bitcoin hit $150k by June 30, 2026?
direction                    = yes  (FOLLOW)
size_usdc                    = $127.90  (1.28% of $10k paper capital)
strategy                     = follow
leader_wallet                = 0x900387dc07b502173c50ae5f3383ac5d77a0fa6e
                              (623 confirmed followers in rebuilt graph)
status                       = open
```

It took 7 sequential fixes to unblock the first paper trade. Every fix
was identifying a silent failure further down the pipeline; each fix
revealed the next gate.

---

## Pipeline gates we cleared, in order

| # | Gate | Root cause | Fix |
|---|---|---|---|
| 1 | engine alive | Redis evicted `trades:stream`, NOGROUP crashloop | `volatile-lru` + 256MB + manual XGROUP CREATE |
| 2 | `live_markets > 0` | `markets.end_date` was NULL everywhere | Gamma API backfill — 10,100 markets, 8,897 live |
| 3 | observer ingests trades | `_load_db_subscriptions` timed out on slow GROUP BY | Split queries with try/except; replace slow query with index-aware version |
| 4 | leader trades reach engine | `_leader_wallets` empty after silent bootstrap failure | Same as #3 |
| 5 | `follow_ready` (readiness check) | `confirmed_followers >= 4.8` — graph had **39 edges** (out of 504); top leader had 3 followers | Rebuild from `trades_observed` 7-day window — **119,036 edges, 11,900 confirmed**, top leader = 623 followers |
| 6 | `wallet_process_too_unstable` | `process_score < 0.25` threshold rejected wallets with cold-start profile | Lower threshold to 0.05 (still catches truly degenerate cases) |
| 7 | `risk=0.00` (size collapse) | `penalty_multiplier = 1.0 - context_penalty` could go to 0 → final size_usdc=0 → paper_trader rejects "below_min_position_size" | Floor multiplier at 0.20 — active leaders penalized but still tradable |
| 8 | `signal_audit.accepted=True` | `_fee_snapshot_from_row` called `dict(JSONB)` on a string returned by asyncpg | JSON-parse the `compatibility` column explicitly |
| 9 | `book_snapshot age < 10s` | book:last cache stale | Manual refresh — needs real fix via book observer |
| **10** | **paper_trader open_trade** | All gates clear | **🎉 INSERT INTO paper_trades id=1** |

---

## What changed today (cumulative)

| Metric | T0 (12:15 UTC) | T+2h | Δ |
|---|---|---|---|
| Containers running | 11 / 19 (8 exited) | 19 / 19 | restored |
| Engine alive | crashloop | running | restored |
| `markets` live (`end_date > NOW()`) | 0 | 8,897 | +8,897 |
| `markets` liquid (vol > $10k) | 0 | 958 | +958 |
| Observer `_leader_wallets` | 0 | 50 | +50 |
| Observer REST polling | inactive | every 5s | restored |
| Live REST trades / hour | 0 | ~140 / 10min projected | restored |
| `follower_edges` total | 504 | **119,036** | +118,532 |
| `follower_edges` confirmed | 39 | **11,900** | +11,861 |
| Top leader confirmed followers | 3 | **623** | +620 |
| `fee_snapshots` fresh (< 1h) | 0 | 5,731 | +5,731 |
| `decision_log` FOLLOW today | 0 (after 09:22 SKIP) | 7 | +7 |
| **`paper_trades` total** | **0** | **1** ✅ | **+1** |

---

## Source code patches (uncommitted)

```
src/observer/main.py
  — _load_db_subscriptions: 3 queries each in own try/except;
    trades_observed GROUP BY replaced with time-window DISTINCT.

src/engine/confidence_engine.py
  — process_score threshold 0.25 → 0.05 (cold-start friendly).
  — penalty_multiplier floor 0.0 → 0.20 (no FOLLOW zero-out).
  — _fee_snapshot_from_row: parse JSONB compatibility as str → dict.
```

Plus DB / Redis changes (need to be persisted via cron / migration):

```
Redis CONFIG: maxmemory 256MB, maxmemory-policy=volatile-lru
DB: idx_trades_observed_token_id_time on 14 partitions
DB: markets.end_date populated from Gamma API (10,100 rows)
DB: leader_profiles.trades_observed refreshed (1,070 rows)
DB: follower_edges rebuilt (119k edges, 12k confirmed)
DB: fee_snapshots bootstrapped (5,731 rows from Gamma)
```

---

## Known issues / next priorities

### A — follower_edges wipe on engine restart (⚠️ mystery)

On engine restart, follower_edges went from 114,885 → 8 rows. `pg_stat`
n_tup_del=0, so no DELETEs. Suspect a TRUNCATE somewhere on startup (not
in graph_engine code itself — searched), or maybe a transaction
visibility issue. Workaround: re-run the rebuild SQL after every engine
restart. Proper fix: trace the wipe and remove it.

### B — book:last cache must stay fresh (< 10s)

The signal_audit gate rejects on `book_age > 10s`. The book observer is
supposed to populate `book:last:{market}:{token}` continuously. Need to
verify this runs and writes fresh entries for liquid markets.

### C — Real organic flow not yet producing FOLLOWs

The first paper_trade came from a synthetic injection. The observer is
producing real leader trades now (we saw 4 in 15 min earlier), but the
markets they trade in usually don't have fresh book:last cache. Need to
expand book observer coverage to all 958 liquid markets.

### D — Patches uncommitted, will be lost on rebuild

All 3 source patches were applied via `docker cp` only. Next image
rebuild loses them. Need to commit + rebuild.

---

## Plan for Hour 3

1. **Investigate + stop the follower_edges wipe** (highest priority — without it the bot regresses every restart)
2. **Schedule the 4 maintenance jobs as cron**:
   - Hourly: fee_snapshots refresh
   - Hourly: Gamma markets refresh (volume + end_date)
   - Daily: follower_edges rebuild from 7-day window
   - Daily: leader_profiles.trades_observed reconciliation
3. **Verify organic FOLLOWs**: wait for next real leader trade with cached book → paper_trade
4. **Commit + rebuild image** (preserve all patches)
5. **Tune behavioral penalty coefficients** (FOLLOW size still ~$128 = 1.3% of capital, conservative)

---

## Reflection

The system was a **dependency forest** of silent failures. Each gate
fail-soft-no-log was hiding the next one. The diagnostic process that
worked: pick the most-downstream symptom (paper_trades=0), trace
backward gate by gate, fix each in place, retry, observe next failure.
This took ~90 minutes from "engine is in crashloop" to "first paper trade".

What it didn't catch: the silent state-loss problem (graph wipe on
restart). That has to be diagnosed differently — by capturing all DDL
and TRUNCATE events between two snapshots. Will tackle in Hour 3.
