# `/ws/live` WebSocket Contract

This document is the **front-end facing** reference for the live
WebSocket fan-out at `/ws/live`. It documents what the browser
receives and how to handle it. Server side: `src/api/ws_bridge.py`.

Pair with [`docs/events.md`](events.md), which documents the underlying
Redis pub/sub schemas — the WS payloads here are just thin envelopes
over those Pydantic models.

---

## Envelope shape

Every message sent on `/ws/live` is JSON with one of two shapes.

### Typed delta (new, A8+)

```json
{
  "type":    "trade" | "decision" | "position_closed" | "system_status" | "reconciliation",
  "channel": "<redis_channel>",
  "ts":      1715000000.123,
  "data":    { ... }
}
```

* `type` — high-level event class. Front consumers should `switch` on
  this value to dispatch to the right slice of state.
* `channel` — raw Redis channel name. Provided so debugging the
  producer/consumer chain doesn't require chasing the type→channel map.
* `ts` — wall-clock seconds at the moment of fan-out (NOT the publisher
  time, which lives inside `data.time`). Use to drop stale events under
  reconnect.
* `data` — the Pydantic-serialised event payload (`model_dump(mode="json")`).
  Shape is fixed by the schema in `src/events/schemas.py`. All fields
  are JSON-safe primitives (datetime → ISO 8601, decimals → strings).

### Legacy refetch trigger (deprecated, removed in A9)

```json
{
  "type": "snapshot_updated",
  "ts":   1715000000.123
}
```

* Emitted whenever the maintenance container writes a fresh snapshot to
  Redis (every ~30s, plus on demand).
* Carries no payload — the client is expected to `GET /api/v1/live-summary`
  on receipt.
* Will be retired in A9 once every front-end slice consumes typed
  deltas directly. Until then, every typed delta is emitted **in
  parallel** with the next snapshot trigger, so a slow front-end migration
  cannot break the dashboard.

---

## Type ↔ channel ↔ schema map

| `type`            | Redis channel              | Pydantic schema           | When the browser sees it |
|-------------------|----------------------------|---------------------------|--------------------------|
| `trade`           | `trades:observed`          | `TradeObserved`           | Every dedup-passing trade ingested (WS or REST). Up to ~50/s in prod, capped at 100/s on the bridge. |
| `decision`        | `decisions`                | `DecisionMade`            | Every routable decision (open / close / reduce / skip + legacy follow/fade). |
| `position_closed` | `positions:paper_closed`   | `PositionClosed`          | Every paper-trade exit (sell / merge / resolution). |
| `system_status`   | `system:status`            | `SystemStatusChanged`     | Health transitions: bot running, WS health, ingest sources, killswitch. |
| `reconciliation`  | `reconciliation:completed` | `ReconciliationCompleted` | After each paper-truth reconciliation run (Gamma vs internal). |
| `snapshot_updated`| `snapshot:live_summary:updated` | *(none — trigger only)* | Maintenance wrote a fresh live-summary; legacy refetch path. |

Anti-drift: `src/api/ws_bridge.WSBridge._assert_channel_coverage()` raises
on startup if `CHANNEL_TO_WS_TYPE` and `CHANNEL_SCHEMA` diverge. Tests in
`tests/test_api/test_ws_bridge.py` mirror the assertion for fast feedback.

---

## Example payloads

### `trade`

```json
{
  "type": "trade",
  "channel": "trades:observed",
  "ts": 1715000000.123,
  "data": {
    "time": "2026-05-18T12:34:56.789012+00:00",
    "market_id": "0xmarket1",
    "wallet_address": "0xLEADER",
    "side": "BUY",
    "price": "0.65",
    "size_usdc": "100",
    "is_leader": true,
    "source": "websocket",
    "token_id": "0xtoken_yes",
    "market_question": "Will X happen by Y?",
    "market_category": "crypto",
    "market_type": "directional",
    "wallet_type": "leader",
    "wallet_status": "active",
    "wallet_strategy": "directional",
    "wallet_horizon": "swing",
    "wallet_influence": "whale"
  }
}
```

Notes:
* `price` and `size_usdc` are **strings** by design — the legacy producer
  stringifies them for Decimal precision. Parse with `parseFloat` before
  arithmetic.
* `side` is canonical `"BUY"`/`"SELL"`. Legacy lower-case is normalised
  upstream.

### `decision`

```json
{
  "type": "decision",
  "channel": "decisions",
  "ts": 1715000000.234,
  "data": {
    "time": "2026-05-18T12:35:01.234567+00:00",
    "decision_id": "f9c2-…",
    "market_id": "0xmarket1",
    "action": "follow",
    "confidence": 0.71,
    "kelly": 0.015,
    "reason": "thompson_follow won the sample",
    "leader_wallet": "0xLEADER",
    "token_id": "0xtoken_yes",
    "side": "buy",
    "price": 0.65,
    "size_usdc": 100.0,
    "thompson_follow": 0.74,
    "thompson_fade": 0.31
  }
}
```

Notes:
* `action` may be canonical (`OPEN`/`CLOSE`/`REDUCE`/`SKIP`) or legacy
  (`follow`/`fade`/`skip`/`volume_anticipation`). Front code must handle
  both for the duration of the migration.

### `position_closed`

```json
{
  "type": "position_closed",
  "channel": "positions:paper_closed",
  "ts": 1715000000.345,
  "data": {
    "time": "2026-05-18T13:00:00+00:00",
    "position_id": "42",
    "wallet_address": "0xLEADER",
    "market_id": "0xmarket1",
    "pnl_usdc": 12.34,
    "close_method": "leader_exit",
    "holding_period_seconds": 3600,
    "strategy": "follow",
    "entry_price": 0.65,
    "exit_price": 0.70,
    "size_usdc": 100.0,
    "pnl_pct": 4.6
  }
}
```

### `system_status`

```json
{
  "type": "system_status",
  "channel": "system:status",
  "ts": 1715000000.456,
  "data": {
    "time": "2026-05-18T12:00:00+00:00",
    "bot": "RUNNING",
    "ws": "LIVE",
    "ingest": {"websocket": "ok", "rest": "ok", "falcon": "degraded"},
    "killswitch": false
  }
}
```

### `reconciliation`

```json
{
  "type": "reconciliation",
  "channel": "reconciliation:completed",
  "ts": 1715000000.567,
  "data": {
    "time": "2026-05-18T03:00:00+00:00",
    "verdict": "warn",
    "delta_abs": 125.50,
    "sample_size": 42
  }
}
```

### `snapshot_updated` (legacy)

```json
{
  "type": "snapshot_updated",
  "ts":   1715000000.678
}
```

---

## Consumer guidance

A minimal front consumer looks like this::

```js
const ws = new WebSocket(`${baseUrl}/ws/live`);

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  switch (msg.type) {
    case "trade":
      tradeSlice.appendTrade(msg.data);
      break;
    case "decision":
      decisionSlice.appendDecision(msg.data);
      break;
    case "position_closed":
      portfolioSlice.applyClose(msg.data);
      break;
    case "system_status":
      healthSlice.set(msg.data);
      break;
    case "reconciliation":
      reconciliationSlice.set(msg.data);
      break;
    case "snapshot_updated":
      // Legacy refetch path — schedule a debounced fetch of /api/v1/live-summary.
      snapshotSlice.scheduleRefetch();
      break;
    default:
      console.warn(`WS: unknown type=${msg.type}`, msg);
  }
};
```

Reconnect strategy: on `onerror` / `onclose`, reconnect with exponential
backoff (1s → 30s capped). On reconnect, the next `snapshot_updated`
event will trigger a full refetch of `/api/v1/live-summary` — that's
how the front catches up on events that landed in the disconnect gap.

---

## Rate limits

The bridge enforces a **per-channel** broadcast rate limit (token bucket,
1-second window, broadcast-wide — NOT per client).

| Channel                    | Cap (msg/s) | Drop policy |
|----------------------------|-------------|-------------|
| `trades:observed`          | 100         | Drop excess, count, log every 10s |
| `decisions`                | 100         | Drop excess |
| `positions:paper_closed`   | 100         | Drop excess |
| `system:status`            | 100         | Drop excess |
| `reconciliation:completed` | 100         | Drop excess |
| `snapshot:live_summary:updated` | n/a    | 2-second debounce (different mechanism) |

Why 100/s when `trades:observed` peaks at ~50/s? The cap is a safety net
against an upstream burst (backfill, replay, runaway producer); under
normal load nothing should ever be dropped. Drops are surfaced via
loguru as::

    WARNING WSBridge: rate-limit dropped events in last 10s: {"trades:observed": 152}

If you ever see this in prod logs, either an upstream is misbehaving or
the cap needs raising — both are operator-visible signals, not silent
data loss.

---

## Fallback strategy

If the typed broadcast pipeline crashes mid-event (serialisation bug,
unexpected exception), the bridge falls back to emitting a bare
`snapshot_updated` so the front-end at least refetches the snapshot.
This is the same channel the dashboard already polled in pre-A8 mode,
so the fallback is transparent.

Schema validation failures (drift between producer and consumer)
**do NOT** trigger the fallback — they are logged at WARNING level and
the event is dropped. The expectation is that the operator will see the
WARNING and update either the producer or `src/events/schemas.py` to
match. See [`docs/events.md` § Anti-drift contract](events.md#anti-drift-contract).

---

## Open items / future work

* The bridge subscribes to the 5 channels in `CHANNEL_TO_WS_TYPE` plus
  the legacy snapshot trigger. Adding a new typed channel requires
  updating BOTH `CHANNEL_SCHEMA` (in `src/events/schemas.py`) AND
  `CHANNEL_TO_WS_TYPE` (in `src/api/ws_bridge.py`) — the
  `_assert_channel_coverage()` startup check enforces this.
* A9 will remove the `snapshot_updated` parallel emit once the front-end
  consumes typed deltas exclusively. The legacy refetch path still
  exists at that point for cold-start cases (initial page load), just
  not for streaming.
* Per-client rate limiting (vs the current broadcast-wide bucket) would
  give a slow client less impact on others. Not needed today — broadcasts
  are bounded by the slowest client's `send_text` latency, but slow
  clients are evicted from `_connections` rather than blocking the loop.
