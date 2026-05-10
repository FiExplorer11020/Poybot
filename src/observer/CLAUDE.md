# Observer Module — Real-time Trade Observation + Position Reconstruction

**Purpose**: Track every trade by leaders in real-time via dual-source ingestion
(Polymarket WebSocket CLOB + REST polling on data-api every 30 s), deduplicate
across both sources via a Redis 7-day TTL set + a DB-level `UNIQUE INDEX` on
`(wallet, market, time, side, price, size_usdc)`, and reconstruct position cycles
(OPEN → CLOSE) including merge exits. Trades attributed to leaders are
re-published on the `trades:observed` Redis pub/sub channel for downstream
consumers (profiler, graph, confidence engine).

The WS market channel does **not** contain wallet addresses — wallet attribution
comes exclusively from the REST data-api polling. The WS feed informs pricing
and book quality only.

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **websocket_client.py** : Polymarket CLOB WebSocket client with auto-reconnect (exp
  backoff), ping/pong watchdog (drops the connection if no pong in 10 s), market
  filtering (top N most active). Subscribes to the `Market` channel for leader-active
  markets only. Publishes book / price_change / last_trade_price events as Redis pub/sub
  side effects so downstream consumers can stay in sync without each one keeping its
  own WS connection.

- **trade_observer.py** : The ingestion heart. Receives trades from two sources —
  WebSocket (`source='websocket'`) and REST polling on Polymarket's data-api
  (`source='api_market'` for market-scoped polls, `source='api_wallet'` for
  per-wallet polls). Deduplicates first via Redis (7-day TTL set keyed on
  `wallet:market:bucket:side:price:size`), then via a DB-level `UNIQUE INDEX` as a
  safety net (covers Redis flushes / cold starts). Inserts into `trades_observed`
  with the source preserved, then publishes `trades:observed` events on Redis pub/sub.

- **position_tracker.py** : Reconstructs OPEN → CLOSE position cycles per `(wallet,
  market, token)`. Detects merge exits (when a leader buys the complementary token
  to merge YES + NO → $1 USDC). Tracks partial closes — the same position may close
  via several SELL orders. Writes resolved positions into `positions_reconstructed`
  with the `close_method` field set (`sell`, `merge`, or `resolution`).

- **models.py** : Trade, Position, PositionClose dataclasses.

---

## Key Algorithms

### Trade Deduplication (O(1) memory)
Maintain Redis set: `seen_trades:{wallet}:{market}:{day}`
Hash key: `{timestamp_bucket}:{side}:{price}:{size}`
On arrival: check Redis, if duplicate skip, else insert and broadcast to position_tracker.

Bucket = floor(timestamp / 1000) (ms-resolution buckets).
Cleanup: delete daily sets older than 7 days.

### Position Reconstruction
State machine per (wallet, market, token):
```
CLOSED → BUY(price, size)    → OPEN
OPEN   → SELL(price, size)   → CLOSED (exit_method="sell")
OPEN   → MERGE(yes+no)       → CLOSED (exit_method="merge")
OPEN   → RESOLUTION          → CLOSED (exit_method="resolution")
```

Track partial closes: same position may close via multiple SYS_SELL orders over time.
Sum all sells until size_remaining = 0.

### Merge Exit Detection (CRITICAL)
Leader can exit a YES position by buying NO token and merging:
- (YES bought at 0.60) + (NO bought at 0.40) → both cancel → $1.00 received
- Monitor BOTH token_id trades per wallet in same market
- Trigger: observed NO sell by same wallet in same market within 10 minutes of YES buy
  AND size of NO ≈ size of YES → mark as MERGE exit

---

## Critical Pitfalls

1. **WebSocket drops silently**: CLOB WS has no guaranteed delivery. Implement:
   - Ping/pong every 30s (WEBSOCKET_PING_INTERVAL_S from config)
   - Pong timeout = 10s (WEBSOCKET_PONG_TIMEOUT_S)
   - Auto-reconnect on timeout, backfill from Falcon agent 556
   - Always backfill 1 hour of history on reconnect

2. **Merge exits invisible on orderbook**: If a leader bought YES 0.60, then later bought NO 0.40,
   and merged (sold both at $1.00), the orderbook never shows YES being sold. Only the two BUY trades
   are visible. MUST monitor both token_id streams per wallet to reconstruct true exit.

3. **Partial closes not on ledger**: A position might close via 5 separate sells over time.
   Track running sum of SELL quantity. Only mark CLOSED when accumulated sell_size ≥ entry_size.

4. **Fee impact on PnL**: Crypto markets charge up to 1.56% fees. Don't calculate PnL as
   (exit_price - entry_price) * size. Must subtract: fee_usdc = abs(entry_size * entry_fee + exit_size * exit_fee).

5. **Timestamp precision**: Polymarket API may return trades with same timestamp. Use (time, nonce)
   for ordering, or fall back to API trade_id if available. Don't assume unique timestamps.

---

## Testing Approach

- **Unit tests**:
  - Mock WebSocket frames: buy, sell, merge. Verify position state transitions.
  - Test deduplication: inject duplicate frames, verify only one position created.
  - Test merge detection: inject YES buy + NO buy within 10m, verify merge exit detected.
  - Test partial closes: 3 sells totaling entry_size, verify CLOSED on 3rd sell.
  - Test fee calculation: enter at 0.60 with 0.5% entry fee, exit at 0.70 with 0.5% exit fee.

- **Integration tests**:
  - Real WebSocket (simulator or testnet). Subscribe to market, inject test trades, verify DB writes.
  - Falcon agent 556 backfill: fetch 1h of trades, verify no duplicates vs WebSocket cache.
  - Database consistency: verify all positions CLOSED have entry + exit, no orphaned opens.

---

## References
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/`
- Falcon agent 556: Polymarket Trades (backfill on reconnect)
- Database: `trades_observed`, `positions_reconstructed` tables (master CLAUDE.md § 6)
- Constants: `TOP_MARKETS_COUNT`, `WEBSOCKET_PING_INTERVAL_S`, `WEBSOCKET_PONG_TIMEOUT_S` from config.py
- Fee rate from `markets.fee_rate_pct` table
