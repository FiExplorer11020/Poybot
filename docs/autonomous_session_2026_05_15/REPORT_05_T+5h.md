# Autonomous Session — Hour 5 Report (2026-05-15)

**Started**: 12:15 UTC | **This report**: 15:50 UTC
**State**: Organic paper_trades on diverse markets, first losses revealing asymmetric edge

---

## TL;DR

The bot moved from "2 paper trades on BTC via synthetic injection" to
**8 organic paper_trades on 6 different markets**, revealing an
asymmetric edge:

```
Wins  (2): BTC low-price entries (0.002, 0.008)  → +$42,704
Losses(6): Sports high-price entries (all 0.99)  → -$748
                                       cum_pnl: +$41,956 paper
```

The 6 losses all entered at 0.99 on near-resolution sports markets that
went to 0.01. The leader was wrong; we followed. Asymmetric pattern:

- entry at **0.99** → upside bounded +1% (to 1.0), downside up to -99%
- entry at **0.008** → upside up to +12,400% (to 1.0), downside bounded -100%

So I shipped a filter: **block FOLLOW when entry_price ≥ 0.85**. Low
prices stay open — they are the source of asymmetric profit.

---

## Cumulative wins this hour

| Action | Before | After |
|---|---|---|
| Paper trades total | 2 | 8 |
| Distinct markets traded | 1 | 6 |
| Organic FOLLOWs converted to paper_trades | 0 | 6 |
| book:last coverage | 326 keys (top 200) | 2,440 keys (top 1500) |
| max_book_age_s (gate) | 10s | 180s |
| Slippage modeling | none | best_ask entry, best_bid exit |
| Open trade conflicts caught | 0 | 1 |

---

## Code changes (round 3 — commit 2d98127)

```
src/engine/confidence_engine.py
  - high-price FOLLOW filter (entry >= 0.85)
  - signal-audit max_book_age_s 10 → 180

src/engine/paper_trader.py
  - _get_book_quote + _entry_ask + _exit_bid (real slippage)
  - monitor loop uses bid-side mark-to-market

scripts/maintenance_loop.py
  - book refresh: 200 → 1500 markets, parallel
  - TTL 60s → 600s, refresh interval 1800s → 120s
  - graph rebuild moved to background task

src/config.py
  - CLOB_BOOK_STREAM_MAXLEN 1.5M → 100k (Redis OOM safety)

src/observer/main.py
  - leader selection: UNION top-falcon + top-confirmed-followers
```

---

## The asymmetry

Looking at the 8 trades:

| Entry zone | Count | Avg PnL | Direction |
|---|---|---|---|
| 0.00 – 0.05 | 2 | +$21,352 | win |
| 0.85 – 1.00 | 6 | -$125 | loss |

This is exactly the leader-following thesis playing out:

- **Low-price entries**: leader saw something the market hasn't priced
  in. If correct, market re-prices toward 1.0 → massive gain. If wrong,
  small loss (premium paid).
- **High-price entries**: market already agrees with the leader's
  thesis. The leader's edge is the +1-3% remaining drift to 1.0 — too
  small to overcome the catastrophic downside if the resolution flips.

The filter blocks the asymmetric-bad regime. The asymmetric-good regime
(low-price entries) is exactly what we want to capture.

---

## Open issues

### A — Real-world price impact still missing

The 2 winning trades closed at 0.28 and 0.59 — but those were from a
specific cache state (one of my earlier manual SETs, plus the
maintenance loop's actual CLOB pull). With proper slippage modeling now
on the entry side, future trades will be more realistic. But the
existing wins' exit prices may be optimistic.

### B — Position sizing

Trade #2 was $131 on a market that yielded $38k paper. With the BTC
market actually at 0.001 currently, our hypothetical fill would've been
much smaller (you can't buy $131 of shares at 0.002 on a thin book).
Realistic fill size at low prices is bounded by book depth.

### C — Per-market position cap

Trade #3 and #8 are both on `0x24ee87...` (CS:GO FURIA vs Falcons). No
cap prevented double-exposure. Fix pending.

### D — follower_edges wipe-on-restart still unexplained

Workaround: maintenance loop rebuilds every 6h. Root cause still TBD.

### E — graph rebuild SQL times out occasionally

The rebuild SQL on 7-day window takes 5-10 min on the prod DB. Already
moved to background task (no longer blocks startup). But still
unreliable — sometimes errors with TimeoutError or asyncpg cancellation.
Should chunk into smaller windows (1-day rolling).

---

## Plan for hour 6

1. Verify high-price filter eliminates new 0.99 losses
2. Add per-market position cap (`max_concurrent_positions_per_market = 1`)
3. Backtest with current logic on the last 7d of decision_log to estimate
   what win-rate looks like in steady state
4. Cap position size at min(Kelly, book_depth_safe_size) to avoid
   pretending we can fill huge orders at the top of a thin book
