# Phase 2 Task D — Dedicated Redis pub/sub clients with reconnect+resubscribe

**Audit ticket**: F-04 in `docs/audit/02_client_audit.md` (Client P0 #2).

## The problem

Every subscriber in the codebase used the SAME `redis.asyncio.Redis`
instance as command callers, and the run loop looked like:

```python
self._pubsub = self._redis.pubsub()
await self._pubsub.subscribe("trades:observed")
async for msg in self._pubsub.listen():
    try:
        ...
    except Exception:
        continue
```

Two correctness bugs, one architectural smell:

1. **Shared client.** `pubsub.listen()` pins one pool connection forever.
   Six subscribers in the engine container plus the API's `ws_bridge`
   meant 7+ permanently-occupied pool slots fighting commands.
2. **Silent message loss on disconnect.** When Redis dropped the
   connection, `listen()` raised inside the `async for`. The
   `try/except` only wrapped message handling — the iterator itself
   died, the surrounding `try/except Exception` re-entered the loop,
   but the SUBSCRIBE registration was gone. Any message published in
   the gap window was lost without a single log line.
3. **No reconnect contract.** The watchdog restarted the coroutine on
   `task.done()`, but a coroutine that returned cleanly after a silent
   re-subscribe failure is `done()` with no exception — the watchdog
   logged "task ended" and restarted, accepting the loss.

For `trades:observed` (the channel that drives every decision and every
follower-edge update), this was the most user-visible reliability bug
in the Redis layer.

## The fix

### New utility — `src/control/redis_pubsub.py`

`Subscriber(redis_url, name=…)` owns its own `redis.asyncio.Redis`
connection (disjoint from the command client). Channels are bound via
`register("channel", handler)` or the `@subscriber.handler("channel")`
decorator. On `start()`:

- Opens the dedicated client (or accepts an injection for tests).
- Spawns one task that runs `_run_loop()`.
- `_run_loop` enters `_consume_once()` inside a `while self._running:`
  guarded by exponential backoff `(1, 2, 4, 8, 16, 30s cap)`.
- `_consume_once()` issues SUBSCRIBE for every registered channel,
  then polls `get_message(timeout=1.0)` until a stop signal or I/O
  error. On exit it cleans up with bounded `wait_for(unsubscribe, 2s)`.
- On `ConnectionError` / `TimeoutError` / `OSError`, the outer loop
  bumps the reconnect counter (labelled `conn_error|timeout|other`),
  sleeps, and re-enters — re-issuing SUBSCRIBE.
- Handler exceptions are caught per-message and bump
  `polybot_redis_subscriber_handler_errors_total` but do NOT trigger
  reconnect.

### Subscriber sites refactored (8)

All sites now hold a private `Subscriber` and expose async `_on_*`
handlers instead of the old `async for pubsub.listen()` body:

| Site | Channels | Subscriber name |
|------|----------|-----------------|
| `src/graph/graph_engine.py` | `trades:observed` | `graph.engine` |
| `src/observer/position_tracker.py` | `trades:observed` | `observer.position_tracker` |
| `src/profiler/behavior_profiler.py` | `positions:closed`, `trades:observed` | `profiler.behavior` |
| `src/engine/confidence_engine.py` | `trades:observed` | `engine.confidence` |
| `src/engine/paper_trader.py` | `decisions` | `engine.paper_trader` |
| `src/engine/live_trader.py` | `decisions:live` | `engine.live_trader` |
| `src/telegram_bot/notifier.py` | 6 alert channels | `telegram.notifier` |
| `src/api/ws_bridge.py` | `trades:observed`, `decisions`, `positions:paper_closed` | `api.ws_bridge` |

### `runtime_config:changed` push-invalidation

Audit Red Flag #6: the channel existed (every `set_overrides` publishes
on it) but nothing subscribed. `RuntimeConfig.start_pubsub()` now wires
a Subscriber that invalidates the in-memory cache on every flip,
dropping propagation from the 30s TTL to <100ms. Wired in both
`src/engine/main.py` and `src/api/main.py` lifespans.

### Metrics (`src/monitoring/metrics.py`)

Four new metrics, additive with Task C:

- `polybot_redis_subscribers_active` (Gauge)
- `polybot_redis_subscriber_reconnects_total{subscriber, reason}`
- `polybot_redis_subscriber_messages_total{subscriber, channel}`
- `polybot_redis_subscriber_handler_errors_total{subscriber, channel}`

## Tests

`tests/test_control/test_redis_pubsub.py` — 17 tests, all passing in <1s:

- Registration (4): decorator form, duplicate-channel rejection, post-start
  registration rejection, empty-handler-set rejection.
- SUBSCRIBE issued for every channel + JSON payload decoded.
- **Reconnect + resubscribe on `ConnectionError`** — uses a hand-rolled
  `_FakePubsub` whose message queue is partitioned by SUBSCRIBE session,
  so the second message is unreachable unless the reconnect path
  actually re-issues SUBSCRIBE.
- Handler exception does NOT kill the loop; next message still arrives.
- Bad JSON does not kill the loop.
- `stop()` cancels task + closes owned redis client; idempotent.
- Backoff is bounded (`>= 0.5s` first, `<= 30s` cap, monotonic).
- Two subscribers on the same channel both receive every message.
- Health counters increment correctly.
- End-to-end RuntimeConfig invalidation via pub/sub.

The existing `tests/test_telegram_bot/test_notifier.py` (10 tests) passes
unchanged — the notifier's constructor still accepts `redis_client=` and
threads it through to `Subscriber.start(redis_client=...)` so fakeredis
fixtures keep working.

## Public contract

> Messages published while a Subscriber is alive are delivered, modulo
> the disconnect window. Messages published during the reconnect backoff
> are LOST.

Phase 3 closes the gap with Redis Streams + a server-side cursor (an
ADR will land separately). For Phase 2 we accept the gap and surface it
via `polybot_redis_subscriber_reconnects_total` — any sustained reconnect
rate is now visible on the dashboard.

## Surprises

- **fakeredis pub/sub has no retention.** Tests must wait for
  `Subscriber.is_connected` before publishing or messages silently drop.
  Added `_await_connected()` helper in the test file.
- **`pubsub.listen()` swallows the stop flag** — the iterator only
  rechecks after the next message. I switched to
  `get_message(timeout=1s)` for predictable shutdown latency.
- **Notifier's existing tests pass a fakeredis instance** as the
  pubsub client. Subscriber's `start(redis_client=...)` injection
  exists exclusively for this case; production never uses it.

## Touched files

- New: `src/control/redis_pubsub.py`, `tests/test_control/test_redis_pubsub.py`
- Modified subscribers: `src/graph/graph_engine.py`,
  `src/observer/position_tracker.py`,
  `src/profiler/behavior_profiler.py`,
  `src/engine/{confidence_engine,paper_trader,live_trader}.py`,
  `src/telegram_bot/notifier.py`, `src/api/ws_bridge.py`
- Wiring: `src/control/runtime_config.py`,
  `src/engine/main.py`, `src/api/main.py`
- Metrics: `src/monitoring/metrics.py` (+4)
