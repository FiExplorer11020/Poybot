# Structural Fix Plan — Live Match Resolution Bug

**Trigger**: 10 paper_trades closed in 24h, **9 losses at -96/98%** despite all prior fixes. Pattern: bot follows leaders on sport markets, market resolves in MINUTES, bot loses 100%. The `MIN_HOURS_TO_RESOLUTION_FOLLOW=6h` filter is conceptually wrong because `markets.end_date` = market expiration (often 7+ days after the actual event), NOT the moment of resolution.

**Status (2026-05-17 ~11:15 UTC)**: Killswitch ACTIVE, sports excluded from whitelist, all open trades closed. Bot is in safe mode until structural fixes deploy.

---

## 1. The Bug Anatomy

Example: Paper #23 (FOLLOW IPL match)
- Market: "Indian Premier League: Punjab Kings vs Royal Challengers Bengaluru"
- `markets.end_date` = 2026-05-24 12:00 UTC (T+169h from trade)
- Filter sees 169h to resolution → passes the 6h gate
- **Actual reality**: match starts within minutes, resolves in ~3h
- Bot opens at 0.52, market resolves NO, exit at ~0.01 → -98%

**Root cause**: `end_date` is the dispute window expiration, NOT the event end. Polymarket allows 7-day dispute period AFTER market resolution. The bot doesn't see resolution time at all.

---

## 2. The 15 Possible Structural Fixes

Ranked by impact × feasibility (each fix is orthogonal — multiple can ship together).

### Tier 1 — Must-have (blocks the bug at the source)

1. **Gamma API event_start_time enrichment** — Polymarket Gamma API likely exposes `event_start_time` or `live=true` flag per market. Enrich `markets` table with `event_start_time` column. Filter on `MIN_HOURS_TO_EVENT` instead of `MIN_HOURS_TO_END_DATE`.

2. **Question regex live-match detector** — parse `markets.question` for live-match patterns:
   - "Map 1 Winner", "Map 2 Winner" → eSports map = 30-45min duration
   - "Half 1", "Quarter 1", "Set 1" → live segment
   - Two team names with "vs" → sport match
   - Date in question (e.g., "May 17") → today's event
   - Time in question (e.g., "8pm EST") → scheduled event
   - Build `is_live_match` boolean column, refresh hourly.

3. **Volume profile signal** — if `volume_24h` ≥ 50% of `volume_lifetime`, the market is in active trading (live event). Add `live_likely=TRUE` flag. Reject FOLLOW on live_likely markets.

4. **Hold-time forced exit for sports** — for any open trade where `category='sports'` AND `holding_period_s > 1800` (30 min): force close at current bid. Avoids holding through full match.

5. **Adaptive stop-loss tightening** — for sport markets: STOP_LOSS = -3% instead of -8%. Sport prices move fast, the 8% gives too much room for catastrophic loss.

### Tier 2 — Defense in depth

6. **Liquidity collapse signal** — when bid-ask spread > 0.30 OR liquidity_score drops by >50% vs 24h ago, the market is pre-resolution. Force close any open position.

7. **Pre-trade match status API** — query Polymarket frontend tags or ESPN/sport-results API. If event status = "in_progress" or "final", reject.

8. **Leader holding-period filter** — per leader, compute median holding period on sport markets. If leader's median < 1h, this leader is a sport scalper — don't try to FOLLOW (too fast for paper trader latency). Skip.

9. **Per-(leader, category) win-rate gate** — if a leader has < 50% win rate on sports specifically, exclude them from sport trading. Same leader may be 80% on crypto.

10. **Mempool resolution detector** — module `src/mempool/` exists. Listen for oracle update transactions on Polymarket conditional tokens. If we see resolution finalizing tx for a market we hold, immediately close.

### Tier 3 — Long-term hardening

11. **Falcon Market Insights enrichment** — Falcon agent 575 returns liquidity, trend, concentration. The `concentration` field may signal "all positions one-sided" = near resolution.

12. **Two-stage category whitelist** — split `sports` into `sports/futures` (long-dated) and `sports/in_play` (live). Whitelist only futures.

13. **Pattern learning on past losses** — feed the 10 -97% losses into the ReasoningBank, distill the pattern, auto-skip markets matching it.

14. **Decision-log replay backtest** — for any new filter, validate on positions_reconstructed AND the 10 recent loss markets specifically.

15. **Runtime-config propagation fix** — paper_trade #25 opened SPORTS despite `category_whitelist=crypto,macro` being set 7 min before. Investigate the runtime_config read path — caching, propagation, stale snapshot. If not fixed, every future config change is suspect.

---

## 3. Sub-agent Assignments (parallel)

| Agent | Mission | Files | Output |
|---|---|---|---|
| **A** | Gamma API enrichment: discover `event_start_time` field, build extractor, populate `markets.event_start_time` for live markets | `src/registry/falcon_client.py` (read-only), new `scripts/import_gamma_event_times.py`, migration `047_markets_event_start_time.sql` | Field discovered, populated for ≥80% of active markets |
| **B** | Post-mortem of #15-#25: identify common leader IDs, market categories, time-of-day, leader-vs-bot price drift. Distill pattern. | Read-only DB queries | Report ranked by recurrence |
| **C** | Live-match detector filter: implement `is_live_match()` predicate combining regex + Gamma flag + volume profile. Wire into `confidence_engine.evaluate()` AND `paper_trader.open_trade()` | `src/engine/confidence_engine.py`, `src/engine/paper_trader.py`, `src/economics/live_match_detector.py` (new) | 8+ tests, sport rejection rate ≥95% in tests |
| **D** | Adaptive stop-loss + time-forced exit for sport hold. Wire into `_check_open_positions`. | `src/engine/paper_trader.py` | 4+ tests covering 30-min force-close + tighter stop-loss |
| **E** | Backtest the combined fixes (A+C+D) on positions_reconstructed for last 60d. Validate win rate ≥70% on n≥300 with sports re-enabled at safe filter level | `scripts/backtest_strategy_2026_05_17.py` extension | Report: post-fix projected trade volume and win rate |
| **F** | Runtime-config bug: investigate why category_whitelist was not read in time for #25. Cache TTL? Re-read on each evaluate()? | `src/control/runtime_config.py` + `src/engine/paper_trader.py` | Fix + 2+ tests |

---

## 4. Acceptance Criteria

- Backtest n≥500, win rate ≥70% with sports re-enabled
- 0 trades opened on markets with `event_start_time < NOW() + 6h` OR `is_live_match=TRUE`
- All 4 active force-close mechanisms tested (Gamma resolved_outcome, hold-time cap, liquidity drop, leader_exit)
- Runtime-config propagation verified: setting `category_whitelist=X` instantly rejects category Y trade on next evaluation

---

## 5. Acceptance Tests (binary)

- [ ] `markets.event_start_time` populated for all sport markets active in last 24h
- [ ] `is_live_match` predicate returns TRUE for "IPL: Punjab vs Bengaluru" (live match in our test set)
- [ ] `is_live_match` predicate returns FALSE for "US Presidential Election 2028" (long-dated)
- [ ] FOLLOW signal on a live_match market is rejected with reason `live_match_blocked`
- [ ] Open sport trade at T+0, force-close at T+30 min with reason `holding_cap_sport`
- [ ] Stop-loss for sport position fires at -3% (vs -8% for non-sport)
- [ ] Backtest on last 60d: sport cohort win rate ≥65% (vs 13% today)
- [ ] Runtime-config category_whitelist change is reflected in next paper_trade evaluation (max 30s delay)

---

## 6. Risk

- New filters too aggressive → no trades on crypto+macro+sports = 0 trades
  - Mitigation: backtest before re-enabling sports
- Gamma API doesn't expose event_start_time → fallback to regex-only detection
  - Mitigation: combine multiple signals (volume + regex + concentration)
- Some sport markets ARE long-dated futures (e.g., "Who wins Champions League 2027?") — don't reject those
  - Mitigation: regex specifically targets "Map 1", "Half 1", date in question, etc., not generic "vs"
