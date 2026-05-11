# Round 7 — Mempool Watcher + Pre-Signed Order Pool

> **Formal title**: Pre-Confirmation Execution Layer
> **Colloquial name**: The Front Door
> **Prerequisite**: Round 6 ([THE SPINE](ROUND_6_THE_SPINE.md)) — needs
> `src/rpc/`, the local Erigon node, and the `ingestion_daemon` split.
> Round 7 cannot start until Round 6's "RPC primary on local node" gate
> passes.

---

## 1. The thesis — be earlier than confirmation, not earlier than the leader

A naive reading of "trade before the leader" sounds impossible — we
don't read the leader's mind. The achievable goal is more modest and
more powerful:

> **Trade before chain CONFIRMATION**, by watching the leader's
> transaction sit in the mempool for 200ms-2s before it gets mined.

When a leader submits a Polymarket order, their wallet broadcasts the
transaction to Polygon validators. The tx sits in the public mempool
until a validator includes it in a block. That window — between
broadcast and confirmation — is our window. We see the leader's
**intent** before the chain records it as **fact**.

The followers can't see this. They watch confirmed trades, just like
every existing copy-trading bot. By the time their REST polling catches
the leader's trade, our pre-signed order has already filled at the
leader's pre-move price.

The "BEFORE the leader" phrasing in the VISION is slightly imprecise:
we trade before the **chain-confirmed** trade arrives at the followers'
view. Same alpha, different framing.

---

## 2. The Hetzner-specific architecture

### What changes from Round 6

Round 6 already deployed:
- `polymarket-node` (CX31, Erigon pruned) — gives us free access to
  `eth_subscribe('newPendingTransactions')`
- `src/rpc/` — multi-provider RPC abstraction with circuit breaker
- `src/ingestion_daemon/` — supports adding a new daemon trivially

Round 7 adds **one new daemon** on box-1 (the bot) and **zero new
boxes**:
- `polymarket-mempool.service` (systemd unit, ~300 MB memory budget)
- Reuses the existing `src/rpc/` to consume the mempool stream
- Reuses the existing `live_trader` + `killswitch` for order submission

Total infra cost change vs Round 6: **€0**. All compute fits in the
existing host envelope.

### The latency budget

The mempool advantage is measured in milliseconds. Where the time goes:

| Hop | Median | p99 |
|---|---|---|
| Leader tx broadcast → reaches validators' mempool | 50-200 ms | 1 s |
| Mempool propagation to OUR Erigon node | 10-50 ms | 200 ms |
| Erigon → `mempool_listener` daemon (WS) | <5 ms | 20 ms |
| Decode + wallet match | <2 ms | 10 ms |
| Pre-signed order lookup + risk check | <3 ms | 15 ms |
| Order submit to Polymarket CLOB (REST) | 100-300 ms | 1 s |
| Polymarket matches our order | <100 ms | 500 ms |
| **End-to-end: detect → filled** | **~250 ms** | **~3 s** |

Compare to the leader's path:
- Leader broadcast → validator inclusion: 1-3 s (block time + propagation)
- Validator block → followers' REST poll catches it: 5-10 s (after R3's
  5s poll cadence; was 30 s before R3)

**Net lead time**: 1-10 seconds ahead of every follower (and ahead of
the leader's own confirmation). On hot markets that's enough for a
50-200 bps mid-price move.

---

## 3. Component breakdown

### 3.1 `src/mempool/node_client.py` — Mempool subscription

```python
class MempoolSubscription:
    """Subscribes to Erigon's eth_subscribe('newPendingTransactions',
    {fromAddress: [watched leader addresses]}) — Erigon supports the
    filtered subscription extension that public providers don't.

    With the watched-address filter, we get ~10-100 tx/sec firehose
    (only tx from our ~2000 watched wallets, not the full mempool's
    1000+ tx/sec).

    Yields decoded tx events:
      MempoolTx(
        tx_hash, from_wallet, to_contract, gas_price, gas_limit,
        nonce, calldata, received_at, replaces=Optional[tx_hash]
      )
    """
    async def stream(self) -> AsyncIterator[MempoolTx]: ...
```

**Edge case**: tx replacement. The leader can replace a pending tx with
a higher gas price (same nonce). Our watcher must track replacement
chains — only the LAST tx in a nonce chain actually gets mined. If we
react to an obsolete tx, we trade against a stale intent.

```python
class NonceTracker:
    """Per-wallet nonce chain tracking. When a new tx arrives with
    nonce N for wallet W, look up any pending tx with same (W, N) and
    mark it as replaced. We act on the LIVE leader of each chain.
    """
```

### 3.2 `src/mempool/tx_decoder.py` — Polymarket CLOB tx decoding

The CLOB contract's `matchOrders` and `fillOrder` functions take
calldata containing the order parameters. We decode against the
contract ABI to extract: market, token, side, size, price.

```python
class CLOBTxDecoder:
    """Decodes pending tx calldata against the Polymarket CTF Exchange
    contract ABI. Returns a structured LeaderIntent or None if the tx
    doesn't target a function we care about.

    Functions we decode:
      - fillOrder(order, signature, fillAmount, salt)
      - matchOrders(takerOrder, makerOrders, ...)
      - cancelOrder(order)

    Returns:
      LeaderIntent(
        wallet, market_id, token_id, side, size_usdc, price,
        order_type,            # FOK | GTC | GTD
        intent_received_at,    # when our node saw the tx
        expected_block,        # next Polygon block
      )
      or None if calldata is opaque / for an irrelevant function.
    """
```

**Edge case**: encoded data may use proxy contracts (UMA dispute,
Polymarket adapters). The decoder must understand the proxy and unwrap
the underlying call. We pin to the current Polymarket contract version;
alerts fire on decode-failure spikes (suggesting a contract upgrade).

### 3.3 `src/mempool/wallet_index.py` — Bloom-filter wallet matcher

```python
class WatchedWalletIndex:
    """O(1) membership test for ~2000 watched leader wallets.

    Backed by a 32 KB bloom filter (1% false-positive rate at 2000
    entries). Updates from the wallet_universe table every 5 minutes —
    leaders promoted to tier 0/1 (per Round 6 AdaptiveDepth) get added,
    demotions get a periodic rebuild.

    Why a bloom filter: at 1000 tx/sec in the unfiltered mempool, even
    a hash-set lookup adds up. The bloom check is ~50 ns; a Postgres
    lookup is 1+ ms. The false-positive case (1%) wastes one decode
    attempt — cheap.
    """
```

If we want to skip the bloom and use Erigon's filtered subscription
(which has 99% identical effect with zero false positives), we still
keep the bloom as a defense-in-depth — Erigon could miss a filter
update during a restart.

### 3.4 `src/mempool/event_emitter.py` — Publish to Redis Stream

Per the Phase 3 R1 contract, every cross-module event flows through
Redis Streams. Mempool detections publish to a new stream:

```
Stream:    mempool:leader_intent
Group(s):  prefill_router (Round 7 consumer)
           paper_shadow   (R7 paper-trading shadow consumer, for
                          calibrating the strategy without firing real
                          orders during the soak)
```

The payload extends the existing `trade event` schema with two new
fields:
```json
{
  "intent_id": "<uuid>",
  "wallet": "0x...",
  "market_id": "...",
  "token_id": "...",
  "side": "buy",
  "size_usdc": "1234.56",
  "price": "0.6234",
  "intent_received_at_ms": 1234567890123,
  "tx_hash": "0x...",
  "nonce": 42,
  "replaces": null,
  "expected_block": 12345678,
  "trace_id": "<uuid>"   // for end-to-end correlation
}
```

### 3.5 `src/execution/prefill/pool.py` — Pre-signed order pool

The pre-signed pool is the latency-saver. Order signing against the
EOA's private key takes ~50ms — too slow on the hot path. We sign in
advance and warehouse the signatures.

```python
class PreSignedPool:
    """Maintains a pool of pre-signed CLOB orders, keyed by
    (market_id, token_id, direction, size_bucket).

    Pool sizing target: 4 orders per (top-100 market × 2 token × 2
    direction × 4 size buckets) = ~3200 orders alive at any moment.
    Each signature is valid for 5 minutes (configurable); the pool
    auto-rotates expired signatures via a background task.

    Operations:
      warm(markets) -> generates pre-signed orders for all
                       (market, direction, size) combinations
      fire(intent: LeaderIntent) -> picks a matching pre-signed
                       order, fires it via py-clob-client, returns
                       the FilledOrder or PoolMiss reason
      expire_stale() -> rotates expired signatures
      stats() -> pool size per market, miss rate, etc.

    State: in-memory dict; rebuilt from leader_universe + market list
    on daemon start. No DB persistence (signatures are time-limited
    anyway).
    """
```

**Why size buckets, not exact size**: pre-signing for every possible
size is combinatorially infeasible. We sign for {500, 2000, 10000,
50000} USDC and pick the largest bucket ≤ leader's size. Slight
mismatch is acceptable — we're already getting most of the alpha by
being first.

**Edge case**: signatures expire. The rotation task runs every 30s;
each signature is valid for 5 minutes; rotation cycle is well within
the safety margin.

### 3.6 `src/execution/prefill/intent_router.py` — Detect → fire path

```python
class IntentRouter:
    """Consumes mempool:leader_intent stream entries, validates against
    runtime risk limits + killswitch, picks a pre-signed order from the
    pool, fires it via py-clob-client.

    Risk validation (in order, fail-fast):
      1. KILLSWITCH STRICT PATH (bypass_cache=True per Phase 0 R2 B)
      2. Leader's confidence engine recommends FOLLOW or volume-anticipation
         (Round 8 / Round 9 outputs) — not all leaders trigger us
      3. Within current_capital * MAX_POSITION_PCT
      4. No cooldown violation
      5. Pool has a matching order

    On success:
      - Fire the pre-signed order via py-clob-client
      - Log to decision_log with action='prefill_intent' + intent_id
      - Publish to trades:stream as source='prefill' (cross-source
        reconciler from Round 6 will catch it)
      - Record in mempool_observations:
          intent_id, leader_wallet, expected_block, fired_at_ms,
          fill_status, fill_block (filled later when chain confirms)

    On miss:
      - Log the reason (pool_miss, risk_blocked, killswitch_off, ...)
      - polybot_prefill_misses_total{reason} counter
      - No order fires — better to miss than to fire blind
    """
```

### 3.7 Shadow mode — first 30 days

Live execution from mempool intent is high-risk. We deploy in **shadow
mode** for 30 days:
- `IntentRouter` runs against the stream
- Risk checks fire
- BUT: instead of `live_trader.open_trade()`, calls
  `paper_trader.open_trade()` only
- A "shadow_intent" branch in `decision_router.py`
- We compare paper outcomes against what live would have done
- After 30 days of clean shadow execution, flip to live

The shadow mode is a `decision_router` config flag, not a separate
code path. Switch is one runtime config knob away.

---

## 4. Migration sequence

| Migration | Round | Purpose |
|---|---|---|
| 024 | 7.1 | `mempool_observations` table — intent_id, latency tracking, fill correlation |
| 025 | 7.2 | `live_orders` extension — `intent_id` FK to `mempool_observations` |

```sql
-- Migration 024 (sketch)
CREATE TABLE mempool_observations (
    intent_id UUID PRIMARY KEY,
    wallet_address VARCHAR(100) NOT NULL,
    market_id VARCHAR(100) NOT NULL,
    token_id VARCHAR(100) NOT NULL,
    side VARCHAR(4) NOT NULL,
    size_usdc NUMERIC(20, 2) NOT NULL,
    intent_received_at TIMESTAMPTZ NOT NULL,
    tx_hash VARCHAR(100) NOT NULL,
    nonce BIGINT NOT NULL,
    replaces_tx_hash VARCHAR(100),
    expected_block BIGINT,
    fired_at TIMESTAMPTZ,
    fire_result VARCHAR(20),  -- 'filled' | 'pool_miss' | 'risk_blocked' | 'killswitch_off' | 'shadow'
    confirmed_at TIMESTAMPTZ,
    confirmed_block BIGINT,
    latency_ms_to_fire INTEGER,
    latency_ms_to_confirm INTEGER
);
CREATE INDEX idx_mempool_obs_wallet_time ON mempool_observations (wallet_address, intent_received_at DESC);
CREATE INDEX idx_mempool_obs_tx ON mempool_observations (tx_hash);
```

---

## 5. New Prometheus metrics (Round 7 contributes ~12)

```
polybot_mempool_subscriptions_active{provider}
polybot_mempool_tx_received_total{source}        # source: erigon|fallback
polybot_mempool_tx_decoded_total{result}         # decoded|not_clob|decode_failed
polybot_mempool_wallet_matches_total
polybot_mempool_replacement_chain_length         # histogram

polybot_prefill_pool_size{market, direction}
polybot_prefill_pool_misses_total{reason}        # no_match, expired, ...
polybot_prefill_pool_signing_seconds             # background sign latency

polybot_intent_router_decisions_total{result}    # filled, pool_miss, killswitch_off, ...
polybot_intent_router_latency_seconds            # intent_received -> fire_complete

polybot_mempool_intent_to_confirm_seconds        # fire -> chain confirm
polybot_mempool_shadow_vs_live_pnl_diff_usdc     # during shadow mode
```

---

## 6. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| 3.1 — `node_client.py` + Erigon filtered subscription | 0.5 |
| 3.2 — `tx_decoder.py` (ABI + edge cases) | 0.5 |
| 3.3 — `wallet_index.py` | 0.25 |
| 3.4 — `event_emitter.py` (stream wiring) | 0.25 |
| 3.5 — `prefill/pool.py` | 0.75 |
| 3.6 — `prefill/intent_router.py` (incl. risk integration) | 0.75 |
| Migration 024/025 + tests | 0.5 |
| 3.7 — Shadow-mode infrastructure | 0.25 |
| Documentation + audit doc | 0.25 |
| **Total** | **~4 weeks** + 30-day shadow soak (parallel, doesn't block dev) |

### Dependencies

**Hard dependencies**:
- Round 6 complete + Erigon node at chain-head + `src/rpc/` shipped
- `KillswitchService.is_real_execution_enabled(bypass_cache=True)` (Phase 0 R2 B) — already shipped, this is the safety net

**Soft dependencies**:
- Round 8 strategy classifier: not a hard dep, but the IntentRouter's
  "leader confidence ok" check is much smarter with R8 outputs available
- Round 9 follower-pool: same — the volume-anticipation sizing wants
  Kalman forecasts

For R7 V0 we can use the current Beta-Binomial confidence as the gate
and refine after R8/R9 ship.

### Risk: 4/5

| Risk | Severity | Mitigation |
|---|---|---|
| Gas-price war: leader replaces tx with higher gas before we fire | High | NonceTracker tracks replacement chains; we only fire against the LIVE leader of each chain. Worst case our pre-fire trades against the obsolete intent. Bound: position size limit means worst-case loss is ~1 % of bankroll per false trigger. |
| Signature expiry mid-fire (race) | Medium | 5 min validity vs 30 s rotation; safety margin is 10×. If we ever hit a stale signature, we miss the fire — annoying, not catastrophic. |
| Polymarket CLOB rate-limits our REST submission | Medium | Pre-flight: measure submission rate during shadow soak; if we hit limits, throttle via Phase 1's adaptive token bucket pattern. |
| Mempool spying detection by leaders | Low | Sophisticated leaders may use private-mempool relays (Flashbots equivalent on Polygon). We can't see those tx. Acceptable — we still cover the public-mempool majority. |
| CLOB contract upgrade breaks tx_decoder | Medium | Alert on `polybot_mempool_tx_decoded_total{result="decode_failed"}` spike; pin ABI version; ops runbook for emergency decoder update. |

### Acceptance criteria

- p50 intent→fire latency < 250 ms in production load test
- p99 intent→fire latency < 3 s
- 30-day shadow soak: shadow paper PnL is positive AND has a Sharpe
  > 1.0 (i.e., the strategy actually works on paper before going live)
- `polybot_intent_router_decisions_total{result="filled"}` >
  `polybot_intent_router_decisions_total{result="killswitch_off"}` in
  steady state (i.e., the killswitch isn't constantly blocking us)
- `polybot_mempool_intent_to_confirm_seconds` p50 ≤ Polygon's average
  block time (2 s) — proves we're consistently AHEAD of confirmation

---

## 7. Rollout plan

### Phase 7.A — Read-only mempool + shadow logging (weeks 1-2)
1. Deploy `polymarket-mempool.service` daemon, no order firing yet
2. All detected intents logged to `mempool_observations` with
   `fire_result='shadow'`
3. Validate latency budget via the new metrics — must show p50 < 250 ms
   end-to-end (intent_received → would-have-fired-at)
4. **Gate**: 7 days of clean shadow logs, latency budget met

### Phase 7.B — Pre-signed pool warming + paper firing (weeks 2-3)
1. Pool generator runs (`PreSignedPool.warm`); pool size monitored
2. `IntentRouter` enables paper-trading branch (calls `paper_trader`
   instead of `live_trader`) on detected intents
3. Compare paper PnL from prefill path vs paper PnL from existing
   FOLLOW path — they should be comparable; prefill should be earlier
   = slightly better
4. **Gate**: 14 days of paper firing with no anomalies, Sharpe ≥ existing
   FOLLOW path

### Phase 7.C — Live capital under shadow gate (weeks 4-6)
1. Flip `prefill_live_enabled=true` in runtime config, but with very
   small position sizes (e.g., 0.1 % of bankroll per trade)
2. Killswitch stays in strict-path mode
3. Telegram alerts on every live prefill order
4. Operator monitors closely; can revert via runtime config knob
5. **Gate**: 30 days of small-live with positive PnL, no killswitch
   trips, no anomalies

### Phase 7.D — Full live (week 6+)
1. Lift position-size cap to normal (governed by RiskManager only)
2. Continue monitoring `polybot_mempool_shadow_vs_live_pnl_diff_usdc`
   as a calibration metric
3. **No gate** — at this point the system is in normal operation

---

## 8. What this round explicitly does NOT do

- **Does NOT implement private-mempool integration** (Flashbots-on-Polygon,
  if such a thing existed). The architecture is open-mempool only.
- **Does NOT introduce its own signing key**. Uses the existing bot
  trading key (provisioned per `docs/live-trading-setup.md`).
- **Does NOT change the killswitch or live-trader architecture**. The
  IntentRouter is a new caller of `live_trader.open_trade()`, not a
  replacement.
- **Does NOT route every leader trade**. The IntentRouter's risk gate
  + strategy filter (R8 when available) decides; many leaders will
  bypass the prefill path entirely.
- **Does NOT add a second machine**. Reuses the Round 6 infra topology
  (box-1 + box-2).

---

## 9. The non-obvious gains

1. **A perfect latency benchmark for the rest of the system**. The
   end-to-end latency budget surfaced in section 2 is a per-hop
   accountability table. Any future regression in our stack
   (Postgres slow, Redis stall, py-clob-client upgrade) shows up
   immediately as a degradation in `polybot_intent_router_latency_seconds`.

2. **The shadow-mode soak doubles as A/B testing infrastructure**.
   The `mempool_observations` table records every intent with
   `fire_result='shadow'` — comparing those paper PnLs against the
   non-prefill paper PnL gives us a clean experimental design for
   ANY future strategy hypothesis. The framework is reusable.

3. **Replacement-chain tracking surfaces leader behavior we couldn't
   see before**. Sophisticated leaders sometimes deliberately spam
   replacement tx as a deception tactic against follower bots. The
   NonceTracker's chain-length histogram is a fingerprint of this
   behavior — useful as a feature for the strategy classifier (R8).

4. **The pre-signed pool is a kill-switch superset**. If we want to
   instantly halt all live trading without touching the killswitch,
   we can flush the pool. The pool gives us a second control surface.

---

## 10. The single sentence

> Round 7 makes us **trade against intent, not confirmation** — watch
> the leader's transaction sit in the mempool for 200ms-2s before the
> chain seals it, fire a pre-signed order in the gap, be done before
> the followers' REST poll catches up.
