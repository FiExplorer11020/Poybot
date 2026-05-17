# Diagnosis & Plan — Push win rate from 13% to 70%+

**Date**: 2026-05-17 02:00 UTC
**Trigger**: User reports 28% win rate (real: 13.3% on 15 closed paper_trades). Bot has not opened a single trade in >24h because the post-2026-05-17 defensive filters reject 99.8% of FOLLOW decisions.

This document is the single source of truth for the implementation session that follows. All sub-agents read this before touching code.

---

## 1. Ground truth (numbers, no spin)

### paper_trades
- 15 closed, 100% FOLLOW (zero FADE ever)
- **2W / 13L = 13.3% win rate**
- 11 of 13 losses were entries at price = 0.990 → resolved NO → -98.79%
- 2 wins were cache-stale artifacts (documented in AUDIT_PAPER_TRADING_2026_05_17.md)
- Last opened trade: 2026-05-16 18:44 UTC (>24h ago, none since 2026-05-17 fixes)

### Decision flow last 24h
- 1,340 decisions total
- 927 SKIP / 413 FOLLOW / **0 FADE**
- Top SKIP reasons: `low_market_liquidity|vol24h=0` (580×), `wallet_process_too_unstable` (56), `high_price_follow_blocked` (~100)
- Of 413 FOLLOW decisions: **412 blocked by paper_trader filters** → only 1 paper_trade actually opened
- Dominant block: `high_entry_ask_blocked` (66/h × 24h ≈ 1500+)

### Observer & leader registry
- 4,661 total leaders, **only 179 on watchlist** (target was 200-2000, cap 100 in bootstrap)
- 2,496 leaders excluded=f, on_watchlist=f = silent reserve never subscribed
- 5 of top-20 Falcon wallets excluded without reason
- 67/179 active in last 24h (37%)
- Source mix: `source='websocket'` writes 0 rows in last hour (dead path)

### Profiles & resolution
- 1,553 phase 1 / 111 phase 2 / 24 phase 3
- 18,826 reconstructed positions, **0 with close_method='resolution'** ← BUG
- `position_tracker.close_market_positions()` exists but is NEVER called from observer
- `markets.resolved_outcome` never backfilled (S2-A job not running)
- 1,849 markets ended in last 30d, none produced resolution closes

### Achievable edge (positions_reconstructed groundtruth, n=18,826)

| Cohort | n | Win % |
|---|---:|---:|
| All | 18,826 | 56.6% |
| Entry [0.5, 0.7] | 4,215 | 58.0% |
| Entry [0.7, 0.9] | 3,224 | 61.4% |
| Entry [0.9, 1.0] | 3,481 | 62.2% (avg PnL -0.02%) |
| Hold <24h | 7,448 + 8,354 | 54-58% |
| sports × high × short | 323 | 67.8% |
| **Top cohort (≥50 trades, ≥70% winrate) × entry [0.5, 0.9] × <24h** | **1,452** | **83.7%** (+$253k) |
| crypto × mid × short | 215 | **93.0%** |

**Edge exists and is large. Bot currently captures none of it because it trades everyone (no leader prefilter) on bad entries (no entry-price band) for too long (no holding cap).**

---

## 2. Root causes — ranked

### A. Structural bugs (correctness)
1. `close_market_positions()` never invoked → resolution closes lost → Phase 1→2 starved → FADE never ready
2. `decision_log` NUMERIC(5,4) overflow → extended audit lost (`Failed to log decision: numeric field overflow`)
3. `backfill_resolved_outcomes` job not scheduled in maintenance container → `markets.resolved_outcome` always NULL
4. `min_signal_strength` and `kelly_fraction` runtime knobs are **dead** (defined in runtime_config but never read by engine)
5. Stop/take use **bid** for both sides in `_check_open_positions`, while entry uses **ask** → structural spread loss biases toward stop_loss
6. **No filter on leader `side='SELL'`** → bot buys when leader sells (huge directional bug)
7. `paper:rejections:24h` Redis counter never written (only 1h is populated)

### B. Exposure ceiling (signal starvation)
8. `MAX_OBSERVER_WS_TOKENS=100` + `wallet_limit=50` → only 100 of 4,661 leaders watched (2%)
9. 2,496 leaders `excluded=f, on_watchlist=f` never auto-promoted
10. 5 top-Falcon wallets excluded with empty `exclude_reason`
11. Bootstrap `ORDER BY falcon_score DESC` only — bot misses observed-winners not in Falcon top-50
12. `source='websocket'` writes 0 trades — dead WS attribution path

### C. Filter mis-calibration (blocks the edge)
13. `high_entry_ask_blocked` rejects 88% of FOLLOWs at 0.85 — but data shows 0.7-0.9 has 61% win and 0.9+ has 62%; the right move is to **lower the floor** (block low-entry 0.0-0.4 garbage) and **raise the ceiling** (allow up to ~0.92)
14. `MIN_HOURS_TO_RESOLUTION_FADE=24h` too strict — most Polymarket markets resolve in <24h; loosen to 6h
15. `MAX_LEADER_PRICE_DRIFT=0.20` too strict for thin books — loosen to 0.35 OR use absolute Δprice ≤0.10
16. `low_market_liquidity` at $5k vol24h — but 580 SKIPs/24h are `vol24h=0` (data freshness issue, not real zero)
17. No leader maturity/win-rate prefilter — bot trades immature Phase 1 wallets (maturity 0.008) → 14.3% win rate
18. No category whitelist — bot trades `unknown` (43.8% win) and `politics` (33.8% win)

---

## 3. Implementation plan — 4 phases in parallel

### Phase 1 — Correctness Fixes (no behavior change unless bug-driven)
- Fix `close_market_positions()` invocation: wire to WS `market_resolved` event in `observer/main.py` AND add periodic Gamma `ended-markets` sweep in maintenance loop
- Fix `decision_log` NUMERIC(5,4) overflow via migration to NUMERIC(7,4) (or clamp at insert time)
- Verify/restart `backfill_resolved_outcomes` job in maintenance loop
- Wire `min_signal_strength` into `confidence_engine.evaluate()` (SKIP when confidence below)
- Wire `kelly_fraction` into `_kelly_size` (multiply final size by knob)
- Use **mid** instead of bid for stop/take checks in `_check_open_positions`
- Add `side='SELL'` filter: refuse FOLLOW on a sell-side leader trade
- Fix `paper:rejections:24h` write path

### Phase 2 — Exposure Increase (multiply signal volume)
- `MAX_OBSERVER_WS_TOKENS` 100 → 800, `wallet_limit` 50 → 400
- Auto-promote any wallet with ≥5 confirmed follower edges to `on_watchlist=TRUE` (maintenance job)
- Clear 5 accidentally-excluded top-Falcon wallets (DB UPDATE)
- Add observed-win-rate selector to bootstrap UNION (3rd source alongside falcon_score and follower-pool)
- Document or remove dead `source='websocket'` path (out of scope to fix WS attribution this session)

### Phase 3 — Edge Selection (the strategy upgrade)
- **Leader prefilter**: in `confidence_engine.evaluate()`, after readiness, SKIP unless `positions_resolved ≥ 30 AND posterior_winrate ≥ 0.60` (target wallet cohort showing edge)
- **Entry-price band**: in `paper_trader.open_trade`, reject if `entry_ask < 0.40 OR entry_ask > 0.92`. Promote bounds to RuntimeConfig (`MIN_ENTRY_PRICE`, `MAX_ENTRY_PRICE`).
- **Category whitelist**: skip markets with `category NOT IN (sports, crypto, macro)`. RuntimeConfig: `category_whitelist` (default {sports, crypto, macro}).
- **Holding cap**: hard close at `holding_period_s > 86400` in `_check_open_positions`
- **B4 FADE loosen**: `MIN_HOURS_TO_RESOLUTION_FADE` 24 → 6 (since FADE close logic was fixed in B9)
- **B6 drift loosen**: `MAX_LEADER_PRICE_DRIFT` 0.20 → 0.35
- **Liquidity gate**: prefer `markets.volume_24h_observed` (from trades_observed last 24h sum) instead of `markets.volume_24h` (stale Gamma)
- All new constants in `src/config.py` AND `src/control/runtime_config.py` for dashboard tunability

### Phase 4 — Backtest Validation
- Build/extend `scripts/backtest_strategy.py`:
  - Loads `positions_reconstructed` joined with `markets`, `leader_profiles`
  - Applies the new filter stack (Phase 1+3) as predicates
  - Replays with parameterizable `policy`: `min_leader_resolved`, `min_leader_winrate`, `entry_min`, `entry_max`, `max_hold_s`, `category_whitelist`
  - Reports: total positions matched, win rate, avg PnL%, cumulative PnL, win rate by cohort
- Run on production data; iterate Phase 3 thresholds until win rate ≥ 70% on n ≥ 500

### Phase 5 — Deploy
- pg_dump backup
- Apply SQL migration (`decision_log` widen + indices for new selector queries)
- rsync src/ + scripts/ + docs/migrations/
- `docker compose build engine observer maintenance && docker compose up -d engine observer maintenance`
- Reset Redis rejections counters, watch logs 15 min

---

## 4. Acceptance criteria

- Backtest on production positions_reconstructed (last 60d, n ≥ 500) shows ≥ 70% win rate with new filter stack
- No regression in test suite (`pytest tests/test_engine/`)
- Engine logs show new filters firing (rejections counters update)
- After deploy, first 50 paper_trades show win rate trending toward 70% (binomial CI overlaps)
- If first 50 paper_trades show win rate <60%, iterate Phase 3 thresholds and re-deploy

---

## 5. Files touched (expected)

```
src/engine/confidence_engine.py        ; min_signal_strength, kelly_fraction, leader prefilter
src/engine/paper_trader.py             ; entry band, category whitelist, holding cap, side filter, spread fix
src/engine/risk_manager.py             ; (read-through to runtime_config) — no change needed
src/control/runtime_config.py          ; new knobs: category_whitelist, min_entry_price, min_leader_winrate
src/config.py                          ; new defaults
src/observer/main.py                   ; observer cap, market_resolved wiring, observed-winrate selector
src/observer/position_tracker.py       ; verify close_market_positions still works
src/observer/market_events.py          ; wire market_resolved to position_tracker
scripts/maintenance_loop.py            ; auto-promote job, backfill_resolved_outcomes verify
scripts/backtest_strategy.py           ; NEW — strategy backtest CLI
docs/migrations/043_decision_log_widen.sql  ; NEW
docs/migrations/044_unexclude_falcon_top.sql ; NEW
tests/test_engine/test_paper_trader_strategy_2026_05_17.py ; NEW regression tests
tests/test_engine/test_confidence_engine_strategy.py        ; NEW regression tests
tests/test_observer/test_position_tracker_resolution.py     ; NEW regression tests
```
