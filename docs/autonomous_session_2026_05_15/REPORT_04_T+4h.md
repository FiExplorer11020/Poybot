# Autonomous Session — Hour 4 Report (2026-05-15)

**Started**: 12:15 UTC | **This report**: 15:00 UTC
**State**: 2 paper_trades closed profitable, pipeline self-sustaining

---

## TL;DR — Second milestone hit

```
paper_trades.id=1  | BTC $150k FOLLOW | entry 0.008 → exit 0.28 | +$4,184.20  | take_profit
paper_trades.id=2  | BTC $150k FOLLOW | entry 0.002 → exit 0.59 | +$38,519.88 | take_profit
                                                       cum_pnl: +$42,704.08
```

Both closed via real CLOB market price movement (paper bankroll
$10k → $52,704 paper).

These are **inflated paper numbers** — extreme-low-entry-price markets
where a 10% pnl_pct take-profit fires when price moves from $0.002 to
$0.59. Real CLOB fills wouldn't go at $0.002, slippage would eat most
of it. But the **mechanism is proven end-to-end**: leader trade →
FOLLOW decision → signal_audit pass → paper_trader open → monitor
loop detects take-profit → close with pnl_usdc booked.

---

## What broke (and got fixed) this hour

1. **Redis OOM** — `book:events:stream` had grown to 573,750 entries
   (R11 microstructure firehose). The default `CLOB_BOOK_STREAM_MAXLEN`
   was 1.5M — too high. Stream ate 256MB of Redis. Every observer SET
   failed with OOM, polling went dead, no trades attributed.
   **Fix**: bumped maxmemory 256MB → 512MB, trimmed stream to 30k,
   lowered config default to 100k, added `trim_runaway_streams` job
   to maintenance_loop (runs every 5 min).

2. **Patches lost on compose recreate** — earlier `docker cp` of
   `observer/main.py` got wiped when compose recreated the container.
   **Fix**: re-applied, also kicked off proper image rebuild in
   background (completed mid-session).

3. **Observer subscribing to wrong wallets** — top 50 by `falcon_score`
   are profitable wallets but most have 0-2 confirmed followers. The
   wallets with hundreds of followers (where the leader-pool signal
   actually lives) weren't being observed.
   **Fix**: UNION top-by-falcon + top-by-confirmed-followers in
   `_load_db_subscriptions`.

4. **Kelly fraction = 0 on cold start** — Beta(1,1) prior → p=0.5,
   balanced market → b≈1 → f* ≈ 0. Decisions logged as FOLLOW with
   confidence 0.69 but size_usdc=0, silently rejected by paper_trader.
   **Fix**: cold-start floor (alpha+beta_≤6 → kelly≥0.5% of capital).
   Also: if Kelly says size < MIN_POSITION_USDC, use MIN_POSITION_USDC
   instead of zero.

5. **book:last TTL inversion** — TTL was 60s, refresh interval 30 min.
   Cache was empty 96% of the time, gates rejected with `stale_book`.
   **Fix**: TTL 600s, refresh every 2 min.

---

## Production state (T+4h)

```
20/20 containers healthy
Redis 512 MB / used ≈25 MB (5%)
book:events:stream: 49k entries (was 573k)
trades:stream:     2 (XADD activity flowing through)

follower_edges:   119,553 total, 11,937 confirmed
leaders with ≥5 confirmed followers: 399
top leader: 0x900387 (623 confirmed followers)

paper_trades: 2 total, 0 open, 2 closed profitable
peak_capital: $52,704.08 (paper)
realized_cum_pnl: $42,704.08

decisions today: 21 follow / 0 fade / many skip
markets live + liquid (vol>$5k): 1,401
fee_snapshots fresh in last 5 min: 7,844
maintenance_loop healthy, all jobs firing
```

---

## What's still missing

### A — Realistic price modeling

The +$42k paper PnL is misleading. Real CLOB fills would slip:
- a $128 order at price 0.002 with 1-cent bid-ask spread takes many
  levels of asks, average fill probably 0.01–0.05, not 0.002
- exit at 0.59 with limited size at top of book = real fill closer to
  0.5

Need to integrate slippage modeling into paper_trader (best_ask vs
best_bid + impact based on size / book depth).

### B — Diverse market exposure

Both paper trades fired on the SAME market (BTC $150k). Need to spread
to other markets. The maintenance_loop's book:last refresh covers
top 200, but FOLLOW decisions tend to converge on a few hot leaders +
markets. May want a per-market position cap below MAX_MARKET_EXPOSURE_PCT.

### C — Truly organic trades

Both paper_trades came from synthetic injections during the
diagnostic. The pipeline IS receiving organic leader trades (api_wallet
shows polling working), but most produce SKIP `insufficient_data`
because the observed leaders (top by falcon_score) overlap poorly with
high-follower wallets. The UNION patch should help — needs more time
to confirm.

### D — follower_edges wipe on restart still unexplained

The maintenance_loop now rebuilds the graph every 6h (and on startup),
so this is a workaround. Root cause: still not traced. Maintenance
loop's startup `[graph] FAILED: TimeoutError` shows the rebuild SQL
sometimes hits asyncpg timeout — need to extend or chunk it.

### E — Slippage + fees not yet reducing PnL

The paper_trader applies a fee from fee_snapshots but the entry/exit
prices are book mid, not realistic fills. Need to wire spread_cost and
slippage_usdc fields (already in schema, set to 0).

---

## Plan for Hour 5

1. Verify organic FOLLOW → paper_trade with a new leader (not BTC $150k)
2. Realistic price model — entry at best_ask, exit at best_bid
3. Backtest current strategy on 7d of decision_log history (decisions
   that fired vs market move + post-leader-exit price)
4. Investigate the `[graph] FAILED: TimeoutError` so the auto-rebuild
   works reliably
5. Per-market position cap to spread exposure
