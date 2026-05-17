# Data Optimization Plan — 2026-05-17 round 2

**Trigger**: 30h+ since last paper_trade despite the 84.72% backtest validation.

**Root cause** (newly quantified): the new gates require leaders with ≥30 internal resolved positions, but **only 80 of 1810 leaders pass that bar** (4.4%). And those 80 aren't currently active. Meanwhile the bot SITS on a treasure trove of unused data:

- **6,897 orphan open positions** in `position_tracker_state` (positions opened but never closed — most are on markets that resolved without the resolution flowing back to us)
- **4,518 expired markets still marked `active=TRUE`** in `markets` (pollutes every liquidity / category query)
- **5,247 leaders with `wallet360_json` populated** (Falcon's full PnL+winrate stats already pulled, but never integrated into the Bayesian gate)
- **1,442 leaders in 0-4 resolved bucket** (immature internally, but most have external Falcon track records)

**Strategy**: harvest existing data + import Falcon priors + adapt thresholds tier-by-tier. Expected to **multiply qualifying leaders by 5-10x** without weakening the 70%+ win-rate target.

---

## 1. Quantified state (2026-05-17 ~02:00 UTC)

| Bucket (internal `positions_resolved`) | Leaders |
|---|---:|
| 0-4 | 1,442 |
| 5-9 | 95 |
| 10-19 | 80 |
| 20-29 | 46 |
| 30-49 | 47 |
| 50+ | 100 |
| **Total** | **1,810** |
| **Pass current gate (≥30 resolved AND ≥0.55 winrate)** | **80** |

| Leaders by Falcon Score (non-excluded) | Count | Watched |
|---|---:|---:|
| 50+ | 39 | 24 |
| 20-50 | 317 | 116 |
| 5-20 | 23 | 6 |
| null (no Falcon data) | 2,651 | 25 |

**Falcon data**: 5,247 leaders have populated `wallet360_json`. The Wallet 360 endpoint exposes (from CLAUDE.md §5) 60+ metrics including PnL leaderboard position, total trades, win count.

**Infrastructure debt**:
- `position_tracker_state`: 6,897 rows (open positions that never closed)
- `markets WHERE end_date < NOW() AND active=TRUE`: 4,518 rows (data hygiene)

---

## 2. The 3 new levers

### Lever A — DATA HARVEST (unlock 6,897 + 4,518)

Walk Gamma's closed-markets endpoint, for each market that's `closed=true active=false` in Gamma but `active=TRUE end_date<NOW()` in our DB:

1. `UPDATE markets SET active=FALSE, resolved_outcome=<yes|no>` (from `outcomePrices`)
2. Trigger `position_tracker.close_market_positions(market_id, outcome)` for all rows in `position_tracker_state`
3. Each closed position writes to `positions_reconstructed` with `close_method='resolution'`, terminal value 0 or 1
4. This **feeds the Beta posteriors** → leaders move from Phase 1 → Phase 2 → Phase 3 organically

Expected outcome: hundreds of new Phase 2/3 leaders within minutes.

### Lever B — FALCON PRIOR INTEGRATION (unlock 5,247 leaders)

Currently the Beta posterior for each leader is `Beta(α=internal_wins, β=internal_losses)` — starts at `Beta(1,1)` and only grows from observed closures.

New: for the 5,247 leaders with `wallet360_json`:
- Extract `total_trades`, `win_count` (or compute from PnL track)
- Define **`effective_resolved`** = `MAX(internal_resolved, falcon_resolved * 0.5)` — discount external by 50% to weight observed > reported
- Define **`effective_winrate`** = Bayesian fusion: `Beta(α_internal + 0.5*α_falcon, β_internal + 0.5*β_falcon).mean()`
- Confidence engine uses `effective_resolved` and `effective_winrate` against the gates

This lets the bot trade Falcon-validated leaders BEFORE we've observed 30 of their own closures — which is the right call because their performance is independently measured.

### Lever C — TIER-BASED THRESHOLDS

Tighter gates for unverified leaders, looser for top-Falcon (the latter have external validation):

| Tier | Criteria | `min_resolved` | `min_winrate` |
|---|---|---:|---:|
| **A** | Falcon score ≥ 50 OR ≥5 confirmed follower edges | 10 | 0.50 |
| **B** | Falcon score ≥ 20 OR ≥3 confirmed edges | 20 | 0.55 |
| **C** | Else | 30 | 0.55 |

The 39 tier-A wallets get a fast track. Falcon Score is the leaderboard ranking — by construction they're externally validated.

---

## 3. Additional ideas explored (not previously attempted)

### Decision-learning seeding (Lever D)

`decision_learning` table seeds Thompson posteriors. Currently empty for cold-start leaders → Beta(1,1) → exploration.

Replay the 1,340 historical decisions through the NEW strategy gates against `positions_reconstructed` outcomes. For each decision a virtual leader would have made, write a `decision_learning` row with the historical W/L outcome. The Thompson posterior starts the Phase 2 with REAL prior, not uniform.

### Follower-graph as quality signal (Lever E)

`follower_edges` has 23,550 confirmed edges. A leader with ≥5 confirmed followers is socially validated. For the 963 such leaders, treat the social signal as an alternative quality gate:
- Promote `min_resolved` to 15 (vs 30)
- Add `min_confirmed_followers ≥ 5` as an OR alternative to winrate gate

### Markets hygiene (Lever F)

Sweep + mark 4,518 expired-active markets as `active=FALSE`. Removes them from the `low_market_liquidity` SKIP miss-attribution (a market in `markets` table with stale `volume_24h=0` is currently skipping, but it's not even an active market).

### Synthetic candlestick exit prices (Lever G — speculative)

For the 6,897 orphan positions on markets that resolved long ago, if `resolved_outcome` isn't in Gamma anymore, fall back to Polymarket's candlestick API (Falcon agent 568, daily candles) — the close price of the last candle before resolution is a fair exit price. Better than discarding the position.

### Adaptive observation throttling (Lever H — defensive)

The previous round saturated DB at 400 wallets. Now that max_connections=500, we have headroom. Re-attempt MAX_OBSERVER_WS_TOKENS=600 (between 400 and 800) and measure: more wallets observed = more trade signals = more decisions.

---

## 4. Execution plan (3 parallel agents)

### Agent A — Data Harvest Operator
- Build one-shot `scripts/backfill_gamma_resolutions_2026_05_17.py` that walks Gamma `closed=true active=false` for last 90 days, UPDATEs markets.resolved_outcome + active=FALSE, calls `position_tracker.close_market_positions` for orphans, writes a summary report
- Also: cleanup expired-active markets (Lever F)
- Adjust `scripts/maintenance_loop.py` to run this sweep every 30 min (currently the job exists but only closes the FUTURE — doesn't backfill historical orphans)
- Files: `scripts/backfill_gamma_resolutions_2026_05_17.py` (new), `scripts/maintenance_loop.py`
- Tests: `tests/test_scripts/test_backfill_gamma_resolutions.py` (new)

### Agent B — Falcon Prior + Adaptive Thresholds (Levers B+C)
- Add migration `046_leader_external_stats.sql` with new columns `leader_profiles.external_resolved_count`, `external_wins`, `external_losses`, `external_source` (default 'falcon_wallet360')
- New script `scripts/import_falcon_external_stats_2026_05_17.py` that walks `leaders.wallet360_json`, extracts win_count + total_trades, populates the new columns
- In `confidence_engine.evaluate()`: compute `effective_resolved` and `effective_winrate` (Bayesian fusion 50% weight on external) — replace the current internal-only check
- Add tier classification: tier A/B/C based on falcon_score AND confirmed_followers
- Three thresholds (tier-specific) replace the single `MIN_LEADER_RESOLVED_FOR_FOLLOW` / `MIN_LEADER_WINRATE_FOR_FOLLOW` constants
- Files: `src/engine/confidence_engine.py`, `src/config.py`, `src/control/runtime_config.py`, `docs/migrations/046_leader_external_stats.sql` (new), `scripts/import_falcon_external_stats_2026_05_17.py` (new)
- Tests: `tests/test_engine/test_confidence_engine_falcon_prior.py` (new)

### Agent C — Backtest validation + Decision-learning seeding (Levers D+E)
- Build `scripts/seed_decision_learning_2026_05_17.py`: replay decision_log + positions_reconstructed → INSERT decision_learning rows for matched outcomes
- Re-run `scripts/backtest_strategy_2026_05_17.py` with Falcon-prior + tier-based logic, report projected trade volume and win rate per tier
- Validate: ≥70% win-rate target holds at projected trade volume ≥10/day
- Files: `scripts/seed_decision_learning_2026_05_17.py` (new)
- Tests: `tests/test_scripts/test_seed_decision_learning.py` (new)

---

## 5. Acceptance criteria

- ≥500 leaders qualifying for FOLLOW after Levers A+B+C (vs 80 today)
- ≥10 paper_trades opened per 24h in production
- Backtest validation: win rate ≥70% on n≥500 with new (Falcon-prior + tier) gate logic
- All Beta posteriors no longer at uniform Beta(1,1) (decision_learning seeded for top 200 leaders)
- 0 orphan positions on markets where Gamma reports closed=true
- 0 markets active=TRUE with end_date < NOW() - 24h
