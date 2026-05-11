# Phase 3 Round 1, Agent D — Ingest Health Watchdog

## Problem

The user reports that data-acquisition pauses of 10–30 min are appearing
and going unnoticed. Phase 1 added Prometheus metrics but no alert rules
and no auto-recovery. This task builds the observability + auto-recovery
layer.

## Source Taxonomy

Every ingestion entry point is a "source" tracked by the
`IngestHealthMonitor` singleton. Heartbeats are O(1) dict writes on the
success path of each source — never on the failure path, so a sustained
failure surfaces as a stale `last_heartbeat_at`.

| Source               | Hot path                                                                | Heartbeat call site                               |
|----------------------|-------------------------------------------------------------------------|---------------------------------------------------|
| `ws_market_feed`     | Polymarket CLOB WebSocket message loop                                  | `src/observer/websocket_client.py::_connect_and_run` |
| `rest_data_api`      | `data-api.polymarket.com` 200 OR 304                                    | `src/observer/trade_observer.py` (both backfills) |
| `falcon_leaderboard` | Falcon agent 584 / 579 (PnL leaderboard alias)                          | `src/registry/falcon_client.py::query`            |
| `falcon_wallet360`   | Falcon agent 581                                                        | same                                              |
| `falcon_markets`     | Falcon agents 574 + 575                                                 | same                                              |
| `falcon_trades`      | Falcon agent 556                                                        | same + `trade_observer._backfill_from_falcon`     |
| `redis_pubsub`       | `src/control/redis_pubsub.py::Subscriber._consume_once`                 | per received message (shared infra)               |
| `redis_streams`      | Reserved — Agent C's StreamConsumer auto-registers on first heartbeat   | follow-on                                          |

## Threshold Table — Defaults & Rationale

Every threshold is env-overridable via `INGEST_THRESHOLD_<SOURCE>_S`.
Severities: `warning = >threshold`, `critical = >2 × threshold`.

| Source               | Default (s) | Rationale                                                                                                                 |
|----------------------|------------:|---------------------------------------------------------------------------------------------------------------------------|
| `ws_market_feed`     |          60 | Active markets always emit *something*; 60 s of silence = real WS drop, not a quiet book. Ping/pong is 30 s.              |
| `rest_data_api`      |          30 | Poll cadence is 5 s (HP-1); missing 6 cycles is real. Lower would flap on a single failed cycle.                          |
| `falcon_leaderboard` |        2100 | Refresh interval = 1800 s (30 min); allow one normal cycle + 5 min slop.                                                  |
| `falcon_wallet360`   |        7200 | Enrichment is bursty (only fires when leaders go stale). 2 h is the observed normal cadence.                              |
| `falcon_markets`     |       86400 | `sync_markets` only refreshes daily-stale rows; >24 h silence means the daily job is broken.                              |
| `falcon_trades`      |         600 | Trade backfill on reconnect should be fast. 10 min covers a slow reconnect.                                               |
| `redis_pubsub`       |         300 | Internal channel — at least one of {profiler, paper_trader, graph, telegram, dashboard WS bridge} should always emit.     |

Trade-off: thresholds too low → alert fatigue (Falcon would flap every
30 min). Too high → user's reported pain persists (10 min outage stays
invisible). The table above is the operational compromise:
**`rest_data_api`** at **30 s** catches the user's 10-min outage class;
**`falcon_*`** at the next-cycle-plus-slop avoids alerting on healthy
sleep.

## Recovery Decision Tree

```
gap detected (now - last_heartbeat > threshold) AND not in_gap
└── transition to in_gap=True, log WARNING, increment gap counter
    ├── ws_market_feed   → force_reconnect() on PolymarketWSClient
    ├── rest_data_api    → alert operator (Telegram) + log; NO eager retry
    ├── falcon_*         → alert operator (Telegram); NO retry (rate-limit hostile)
    ├── redis_pubsub     → alert operator; Subscriber.restart() at the call site
    └── (cooldown check) — within RECOVERY_COOLDOWN_S? skip + bump counter

heartbeat arrives while in_gap=True
└── log INFO "gap closed after Xs", set in_gap=False, increment
    ingest_recovery_success_total{source}
```

**Critical constraint**: Falcon-source recovery is alert-only. Auto-
retrying the very Falcon endpoint that is already 429-ing us — or worse,
unreachable for unrelated reasons — would worsen the incident. The
recovery dispatcher publishes to `ingest:gap` on Redis; the Telegram
notifier formats and sends, with a per-source cooldown
(`INGEST_ALERT_COOLDOWN_S`, default 300 s) so a multi-hour outage
doesn't paginate operators.

## Alert Routing

Three layers of throttle in front of the operator:

1. **`RECOVERY_COOLDOWN_S`** (default 60 s): how often the
   IngestHealthMonitor will fire its recovery callback for the same
   source. Prevents the 10 s watchdog tick from re-firing on every loop
   while a gap is open.
2. **`INGEST_ALERT_COOLDOWN_S`** (default 300 s): how often the
   TelegramNotifier will broadcast an `ingest:gap` alert for the same
   source. Independent of layer 1 — even if the watchdog fires only
   once per gap, multiple consecutive gaps would still each generate
   one alert without this gate.
3. **`TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE`** (default 20): global
   leaky-bucket on the bot. Last line of defence against any storm.

Alert rules live in `docs/monitoring/alerts.yml` (7 rules):

| Rule                          | Severity  | Pain it detects                              |
|-------------------------------|-----------|----------------------------------------------|
| `IngestSourceStale`           | warning   | any source silent >5 min                      |
| `IngestSourceDown`            | critical  | any source silent >30 min (the user's pain)   |
| `TradeIngestionLatencyHigh`   | warning   | p95 trade-ingestion > 10 s                    |
| `ObserverQueueBackpressure`   | warning   | queue drops > 0                               |
| `FalconRateLimitHits`         | warning   | 429s observed                                 |
| `SubscriberReconnectStorm`    | warning   | pub/sub reconnect rate > 0.1/s                |
| `DeadLetterStreamGrowing`     | critical  | Agent C stream deadletter rate > 0/s (10m)    |

The list also has an `IngestRecoveryNotProgressing` rule (warning) that
flags "we keep recovering but the source never returns" — useful to
distinguish flap from sustained outage.

## Grafana Dashboard Sketch

Single dashboard, one row per concern:

* **Ingestion freshness** — `polybot_ingest_seconds_since_last_event`
  per source, log scale Y, threshold lines at default + 2× default.
* **Gap counters** — `rate(polybot_ingest_gaps_total[5m])` stacked by
  severity, one panel per source.
* **Recovery success/failure** — ratio of
  `ingest_recovery_success_total` over
  `ingest_recovery_attempts_total{result="triggered"}` — should be
  near 1.0 over any 24 h window.
* **Active breaches** — `polybot_ingest_threshold_breaches_active`
  table, one row per source, color RED when active.
* **Cross-reference panels** — Falcon 429s, observer queue depth, WS
  disconnects, subscriber reconnects, stream deadletter rate. These
  give the why for an active breach without a separate dashboard hop.

## Coordination Notes

* Heartbeat insertions in `websocket_client.py`, `trade_observer.py`,
  `falcon_client.py`, `redis_pubsub.py` are one-liners and additive.
  Agent A's freshness watchdog and Agent B's smart client write to the
  same metrics module without name collisions (verified by inspection
  of `src/monitoring/metrics.py`).
* `Subscriber.restart()` was added to `src/control/redis_pubsub.py` for
  per-subscriber recovery; the engine container's bootstrap currently
  uses the alert-only path because cross-subscriber orchestration is
  out of scope.

## Files Touched

| Path                                                                    | Change                                                                  |
|-------------------------------------------------------------------------|-------------------------------------------------------------------------|
| `src/monitoring/ingest_health.py`                                       | NEW — central IngestHealthMonitor + singleton accessor                  |
| `src/monitoring/metrics.py`                                             | +5 metrics                                                              |
| `src/observer/websocket_client.py`                                      | heartbeat + `force_reconnect()`                                         |
| `src/observer/trade_observer.py`                                        | heartbeat at REST 200/304 + Falcon trades success                       |
| `src/registry/falcon_client.py`                                         | heartbeat per agent_id on 200/304                                       |
| `src/control/redis_pubsub.py`                                           | heartbeat per message + `Subscriber.restart()`                          |
| `src/telegram_bot/notifier.py`                                          | new `ingest:gap` channel + per-source cooldown                          |
| `src/telegram_bot/formatters.py`                                        | `format_ingest_gap`                                                     |
| `src/engine/main.py`                                                    | bootstrap + recovery callback wiring                                    |
| `docs/monitoring/alerts.yml`                                            | NEW — 7 Prometheus alert rules                                          |
| `tests/test_monitoring/test_ingest_health.py`                           | NEW                                                                     |
| `tests/test_telegram_bot/test_ingest_alerts.py`                         | NEW                                                                     |
