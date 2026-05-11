# Phase 3 Round 1 Task C — CDC `trades_observed` → Redis Streams

> **Status note**: the implementing agent was killed mid-run by an Anthropic
> account rate limit. The new module + consumer migrations landed before the
> kill; one consumer migration (graph_engine) was botched and has been
> reverted to the Phase 2 pubsub-only version. This report was written
> post-hoc by the orchestrator.

## What shipped

### 1. `src/control/redis_streams.py`

New utility mirroring the Phase 2D `Subscriber` shape but using Redis
Streams (`XADD` / `XREADGROUP` / `XACK` / `XPENDING` / `XCLAIM`) instead of
pub/sub. Two public classes:

- **`StreamProducer`** — append-only `XADD` with `MAXLEN ~ <retention>`.
  Returns the stream entry-id. Dedicated `redis.asyncio.Redis` connection
  (audit F-04). Auto-reconnect with bounded exponential backoff.
- **`StreamConsumer`** — consumer-group reader with at-least-once
  semantics. Decorator/register API for handlers. On exception: entry stays
  pending, retried up to `max_retries`, then routed to a `.deadletter`
  stream. Periodic `XPENDING` + `XCLAIM` to recover entries from dead
  consumers. Auto-reconnect → re-issue `XGROUP CREATE` (idempotent with
  `MKSTREAM`) → resume `XREADGROUP`.

Public contract closed (replaces the Phase 2D Subscriber's "messages lost
during disconnect window" limitation for any consumer that migrates).

### 2. Dual-write from `trade_observer`

After the Phase 1 `_db_writer_loop._write_batch` commits, the producer
publishes to BOTH the legacy `trades:observed` pub/sub channel AND the new
`trades:stream` Redis Stream. The legacy channel stays in place as a
safety net during soak; subscribers can migrate one at a time.

Stream payload extends the pub/sub payload with `trace_id` (UUID for
end-to-end traceability) and `published_at_ms` (server-side timestamp).

### 3. Consumer migrations

Migrated to `StreamConsumer` (group name in parens):
- `src/profiler/behavior_profiler.py` (`profiler.behavior`)
- `src/engine/confidence_engine.py` (`confidence`)
- `src/engine/paper_trader.py` (`trader.paper`)
- `src/engine/live_trader.py` (`trader.live`)
- `src/api/ws_bridge.py` (`ws_bridge`) — receives both pubsub and stream; idempotent because the dashboard re-polls

Each migrated consumer keeps its legacy `Subscriber` registration as a
safety net (marked `# TODO(phase3-round2): remove pubsub subscription`).

### 4. Telegram notifier and runtime_config

- `src/telegram_bot/notifier.py` — extended to consume the new
  `control:killswitch_changed` Stream where present, falling back to pubsub.
- `src/control/runtime_config.py` — `runtime_config:changed` consumer now
  receives push-invalidations within ~100ms (was 30s TTL).

## Metrics added

```
polybot_stream_published_total{stream}
polybot_stream_consumed_total{stream, group}
polybot_stream_pending_entries{stream, group}
polybot_stream_dead_letters_total{stream, group}
polybot_stream_handler_latency_seconds{stream, group}
polybot_stream_reconnects_total{component, stream}
```

## Tests

- `tests/test_control/test_redis_streams.py` — producer/consumer contract:
  publish→entry id, group create idempotent, XACK on success, XPENDING on
  exception, max-retries→deadletter, reconnect-and-resume-from-last-acked,
  XCLAIM recovers entries from dead consumers.

## Known follow-ups (Round 2)

- **`src/graph/graph_engine.py` reverted** to its Phase 2 pubsub-only
  state. The implementing agent's refactor truncated the `GraphEngine`
  class early and left ~280 lines of dead-code orphan methods nested inside
  `_graph_trade_dedup_key`, causing 6 graph tests to fail with
  `AttributeError: '_update_edge'`. Reverting restored the Phase 2D
  Subscriber wire-up. Stream consumer integration for graph deferred to
  Round 2 — re-apply as additive change rather than refactor.
- **Pubsub deprecation**: every migrated consumer carries a
  `# TODO(phase3-round2): remove pubsub subscription` marker. After a
  one-week soak with `polybot_stream_dead_letters_total` at zero, the
  pubsub subscriptions can be removed and `trade_observer` can stop
  dual-writing.

## Files touched

- `src/control/redis_streams.py` (new)
- `src/observer/trade_observer.py` — dual-write integration after `_write_batch`
- `src/profiler/behavior_profiler.py`, `src/engine/confidence_engine.py`,
  `src/engine/paper_trader.py`, `src/engine/live_trader.py`,
  `src/api/ws_bridge.py`, `src/telegram_bot/notifier.py`,
  `src/control/runtime_config.py` — consumer wire-ups
- `src/monitoring/metrics.py` — 6 metrics added
- `src/graph/graph_engine.py` — **reverted to Phase 2 state** (deferred follow-up)
