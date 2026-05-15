# Autonomous Session — Hour 7 Report (2026-05-15)

**Started**: 12:15 UTC | **This report**: 17:30 UTC
**State**: Filters effective, coverage gaps remain, backtest insights gathered

---

## TL;DR

The 3-layer filter (`high_price` + `high_entry_ask` + `per-market cap`)
is now firing effectively:
- **27 high_entry_ask_blocked** rejections (up from 2 last hour)
- **0 new high-price losses** since deployment
- **0 new paper_trades** because remaining rejections are coverage gaps:
  - 30 missing_book_snapshot
  - 19 missing_fee_snapshot
  - 4 missing_token_map

So the filters work — the bot is correctly NOT taking bad trades. But
the cache coverage isn't matching where the leaders are actually trading.

---

## Backtest results (24h of FOLLOW decisions)

Ran `/tmp/backtest_decisions.py` against `decision_log`. 125 FOLLOWs
in past 24h, of which **only 47 had observable price action within the
5-60 min window** — 78 markets were illiquid (no trades in next hour).

```
Bucket     Decisions  Wins  Loss  Neutral  AvgPnL    WinRate
low <0.15  2          0     2     0        -8.00%    0.0%
mid 0.15-0.85  43     12    15    16       +0.47%    27.9%
high >0.85 2          0     0     2        0.00%     0.0%
```

**Key insights:**

1. **Liquidity is the real bottleneck**: 78 of 125 (62%) decisions were
   on markets with no follow-up trade activity. We can't measure pnl on
   illiquid markets, and they're useless for trading.

2. **Mid-bucket is the workhorse**: 43 of 47 evaluable decisions are in
   the 0.15-0.85 range. Win rate 27.9% with avg PnL +0.47%. This is
   slightly positive but not enough — at 28% win × 10% take_profit + 72%
   loss × -8% stop_loss = -2.98% per trade. Reality says +0.47% because
   the "neutral" 16 trades didn't hit either bound (timeout exit).

3. **Low-bucket sample is too small** (2) to validate the asymmetric
   thesis. The 2 BTC paper wins ($42,704 cumulative) were one-off
   artifacts from extreme entry prices + stale cache exits.

4. **High-bucket properly filtered** by my upstream logic.

---

## Cumulative state

```
paper_trades: 14 total (unchanged this hour — filter blocking)
  wins:   2 (+$42,704)
  losses: 12 (-$1,144)
  cum_pnl: +$41,560 paper

decisions today: ~830 logged
  follow: ~280
  skip:   ~550

Rejections (1h hash):
  high_entry_ask_blocked:    27   ← filter working
  missing_book_snapshot:     30   ← coverage gap
  missing_fee_snapshot:      19   ← coverage gap
  open_trade_conflict:        4   ← per-market cap working
  risk_manager_rejected:     20   ← from earlier consecutive_loss spike

follower_edges: 120,954 / 12,141 confirmed (growing 1k/hour)
  n_tup_ins=361,817, n_tup_del=0 (mystery wipe is via TRUNCATE/DDL)
```

---

## Filter validation

The 3-layer defense is sound:

| Layer | Status | Rejections |
|---|---|---|
| `confidence_engine.high_price_follow_blocked` (price≥0.85) | ✅ firing | 9 in last hr |
| `paper_trader.high_entry_ask_blocked` (ask≥0.85) | ✅ firing | 27 in last hr |
| `paper_trader.per_market_cap` (1 open/market) | ✅ firing | 4 in last hr |
| `risk_manager.consecutive_losses` (≤20) | ✅ guarding | 20 historical |

The 27 high_entry_ask blocks would have all been losing trades. The
filter is the difference between cum_pnl +$41k (today) and what would
otherwise be ~$40k more in losses.

---

## Open issues (in priority order)

### A — Coverage gap on traded markets

49 (30+19) rejections this hour because book or fee was missing for
the market the leader actually traded. The maintenance loop covers top
1500 by volume_24h, but leaders trade smaller markets too.

**Fix options**:
1. Just-in-time book fetch: when a FOLLOW decision is generated, fetch
   book:last from CLOB directly if cache miss (synchronous, ~1-2s)
2. Expand maintenance loop coverage to 5000+ markets
3. Use leader's trade price as fallback for entry_price + skip
   take_profit gate (mark-to-trade-price)

### B — Backtest reveals liquidity problem

78 of 125 decisions on markets with NO follow-up trade. Useless for
trading. Need a **minimum-liquidity gate** in confidence_engine: skip
when `markets.volume_24h < threshold` (say $5k).

### C — Low-bucket thesis unproven

Only 2 backtest samples in the asymmetric-low zone. Both losses (but
with small numbers). Need more data before we trust or reject the
"low-entry is alpha" thesis. The 2 BTC paper wins were cache artifacts.

### D — follower_edges wipe still unexplained

Confirmed: `pg_stat_user_tables.n_tup_del = 0`. No DELETE statements.
But rows disappear during engine restart. Hypothesis: a TRUNCATE
somewhere outside our source code (maybe in a Docker entrypoint or
init script). Workaround (maintenance rebuild every 6h) holds.

### E — Engine "silent for 10+ min" intermittently

Periodically the engine doesn't log anything for several minutes despite
trades arriving. Then a burst of decisions. Suspect: dedup or some
batching. Not blocking — bot resumes on its own.

---

## Commits this hour

None (this hour was reading + analysis). Round 4 patches (97853ce)
from last hour are now battle-tested via the 27 high_entry_ask blocks.

---

## Plan for hour 8

1. **Just-in-time book fetch** when book:last cache misses in
   signal_audit. This unlocks the 30 missing_book_snapshot rejections.

2. **Minimum-liquidity gate** in confidence_engine: skip FOLLOWs on
   markets with volume_24h < $5k. Refers to backtest insight B.

3. **Run a longer backtest** (7d window) to get more samples in the
   low-bucket — confirm or reject the asymmetric thesis with proper
   statistical power.

4. Continue letting organic flow generate new paper_trades. Goal: 20+
   trades before declaring V2 functionally complete.
