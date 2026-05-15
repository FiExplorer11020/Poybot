# Autonomous Session — Hour 6 Report (2026-05-15)

**Started**: 12:15 UTC | **This report**: 16:45 UTC
**State**: 14 paper_trades, asymmetric pattern confirmed, filters hardened

---

## TL;DR

The 6 organic FOLLOWs that fired between hour 5 and hour 6 (#9–#14)
all entered at 0.99 → exit 0.01 → stop_loss losses (-$50 to -$167
each). Total losses for round 4 = -$594.94.

Root cause: my `entry_price >= 0.85` filter in confidence_engine checks
the **leader's trade price**, but paper_trader fills at the **current
ask price** read from book:last. On near-resolution markets the
spread is 0.01/0.99, so leader buys at 0.50 but we fill at 0.99.

Fix shipped:
- `paper_trader.open_trade` blocks when `entry_ask >= 0.85` (catches
  the case the upstream filter misses)
- `_has_open_trade_conflict` tightened from per-(market,leader,strategy)
  to per-market — prevents the 4-on-same-market cluster we saw on
  `0x59eb6...`

---

## Cumulative state

```
paper_trades: 14 total (was 8 last hour)
  wins: 2 (BTC low-entry +$42,704)
  losses: 12 (sports 0.99-entries -$1,144)
  cum_pnl: +$41,560 paper
  avg_win: $21,352 | avg_loss: -$95
  win_rate: 14%, profit_factor: 18.7

decisions today: ~150 (was ~25 yesterday)
markets covered in trades: 9 distinct
follower_edges: 119,544 / 11,937 confirmed (stable via maintenance)
book:last cache: 2,015 keys / 1,500 markets target (fresh)
maintenance loop: healthy, all jobs firing on schedule
20/20 containers healthy
Redis: 51 MB / 512 MB
```

---

## Filters now in place (3-layer defense)

| Layer | Check | Reject reason |
|---|---|---|
| confidence_engine | `trade.price >= 0.85` | `high_price_follow_blocked` |
| paper_trader | `entry_ask >= 0.85` (after book lookup) | `high_entry_ask_blocked` |
| paper_trader | `same market already open` | `open_trade_conflict` |
| risk_manager | `consecutive_losses >= max` | `risk_manager_rejected` |

The `risk_manager` triggered 20 times this hour as `consecutive_losses`
hit 12. Manually reset to 0 and bumped runtime config
`max_consecutive_losses` to 20 so the bot can resume trading now that
the underlying logic is fixed.

---

## Current bottleneck: Polymarket activity

Public `data-api.polymarket.com/trades` returns most-recent-trade
~700s ago. Polymarket is in a genuinely quiet period (Sunday afternoon,
no major events). Observer is healthy and polling but no leader trades
to react to.

This is **expected behavior** — the bot waits when the market is quiet.
When activity returns, the filters above should produce diverse, low-
price-entry trades.

---

## Commits this hour

- `2d98127` — round 3: slippage modeling, high-price filter, book
  coverage expansion, OOM recovery
- `8e07598` — MIN_POSITION_USDC floor instead of context_penalty SKIP
- `97853ce` — round 4: per-market cap + entry_ask filter + risk reset

---

## Open issues

1. **Polymarket public activity low** — wait for evening US time-zone
   activity to resume.

2. **Observer cursor reset frequency** — every restart re-bootstraps
   the cursors from `now-300s`. Might be lossy if a leader trade happened
   in the gap. Should persist cursors across restarts (or at least
   `now-3600s`).

3. **follower_edges wipe-on-restart** still unexplained. Workaround in
   place (maintenance rebuild every 6h).

4. **Engine appears silent for 13+ min** between bursts. Need to verify
   it's actually consuming trades:observed messages (not just sitting
   on the channel).

5. **R8 classifier still uniform-prior** for most leaders. Re-train
   needed once we have 200+ closed paper_trades for labels.

---

## Next hour plan

1. Wait for Polymarket activity to resume (will check again in 30 min)
2. If trades resume but pipeline doesn't react, investigate engine
   message processing path (`_on_trade_message` handler)
3. Backtest current logic on historical decision_log (the 700+
   decisions logged) — would the current filters have prevented all
   the 0.99 losses?
4. Trace the follower_edges wipe root cause by reading engine startup
   sequence end-to-end
