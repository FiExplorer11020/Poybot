# Redis Pub/Sub Event Schemas

This document is the **operator-facing** reference for every Redis
pub/sub channel the bot uses. Schemas live in
[`src/events/schemas.py`](../src/events/schemas.py) (Pydantic v2,
`extra="forbid"` everywhere). If you add a field to a producer, update
the schema in the same commit — otherwise the consumer side will reject
the event with a `ValidationError` at runtime.

The Pydantic models are the single source of truth. The examples below
are illustrative only.

---

## Channel ↔ Schema map

| Channel                            | Schema                     | Producer(s)                                 | Consumer(s)                                              |
|------------------------------------|----------------------------|---------------------------------------------|----------------------------------------------------------|
| `trades:observed`                  | `TradeObserved`            | `src/observer/trade_observer.py`            | `position_tracker`, `graph_engine`, `confidence_engine`, `ws_bridge` |
| `decisions`                        | `DecisionMade`             | `src/engine/decision_router.py`             | `paper_trader`, `ws_bridge`                              |
| `positions:paper_closed`           | `PositionClosed`           | `src/engine/paper_trader.py`                | `telegram_bot.notifier`, `ws_bridge`                     |
| `system:status`                    | `SystemStatusChanged`      | *(new, reserved — used by future ops/health publisher)* | `ws_bridge`, `telegram_bot.notifier`                     |
| `reconciliation:completed`         | `ReconciliationCompleted`  | *(new, reserved — used by reconciliation job)* | `ws_bridge`, `telegram_bot.notifier`                     |

Other channels in the codebase (`decisions:live`, `decisions:trace`,
`market:price_changes`, `engine:crash`, …) are NOT yet typed by this
module. They will migrate in a follow-up batch — for now they remain
free-form dicts.

---

## `trades:observed` — `TradeObserved`

Emitted by `trade_observer._publish_trade_event` for every dedup-passing
trade the bot ingests (WebSocket or REST). The fields ordered first
(``time`` … ``source``) are the **core contract**; the rest are legacy
enrichment kept for compatibility.

### Required core fields

| Field           | Type                  | Example value                  |
|-----------------|-----------------------|--------------------------------|
| `time`          | `datetime` (ISO 8601) | `"2026-05-18T12:34:56.789+00:00"` |
| `market_id`     | `str`                 | `"0xabc…"`                    |
| `wallet_address`| `str`                 | `"0xLEADER"`                  |
| `side`          | `"BUY" \| "SELL"`     | `"BUY"` (legacy `"buy"` accepted) |
| `price`         | `float`               | serialised as string `"0.65"` |
| `size_usdc`     | `float`               | serialised as string `"100"`  |
| `is_leader`     | `bool`                | `true`                         |
| `source`        | `str`                 | `"websocket" \| "api_market" \| "api_wallet" \| "falcon_trades"` |

### Example payload

```json
{
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
```

---

## `decisions` — `DecisionMade`

Emitted by `decision_router.route` for every non-skip decision. The
PaperTrader consumer (`engine/paper_trader._on_decision_message`)
branches on legacy lower-case actions (`"follow"`, `"fade"`,
`"volume_anticipation"`), so the schema accepts both the canonical
upper-case set AND the legacy set.

### Required core fields

| Field          | Type                                       | Notes |
|----------------|--------------------------------------------|-------|
| `time`         | `datetime`                                 | UTC, ISO 8601 |
| `decision_id`  | `str`                                      | UUID per call to `route()` |
| `market_id`    | `str`                                      |  |
| `action`       | `Literal[OPEN, CLOSE, REDUCE, SKIP, follow, fade, skip, volume_anticipation]` | Canonical or legacy |
| `confidence`   | `float`                                    | Thompson sample value |
| `kelly`        | `float`                                    | Bayesian Kelly fraction |
| `reason`       | `str`                                      | Human-readable |

### Example payload

```json
{
  "time": "2026-05-18T12:35:01.234567+00:00",
  "decision_id": "f9c2…",
  "action": "follow",
  "leader_wallet": "0xLEADER",
  "market_id": "0xmarket1",
  "market_question": "Will X happen by Y?",
  "market_category": "crypto",
  "market_type": "directional",
  "token_id": "0xtoken_yes",
  "side": "buy",
  "price": 0.65,
  "size_usdc": 100.0,
  "kelly": 0.014,
  "kelly_fraction": 0.014,
  "confidence": 0.72,
  "thompson_follow": 0.74,
  "thompson_fade": 0.31,
  "reason": "thompson_follow won the sample",
  "trade_context": {...},
  "signal_audit": {...}
}
```

---

## `positions:paper_closed` — `PositionClosed`

Emitted by `paper_trader._close_trade` after each paper trade exit. The
Telegram notifier and the dashboard consume this channel directly.

### Required core fields

| Field                  | Type                | Notes                       |
|------------------------|---------------------|-----------------------------|
| `time`                 | `datetime`          | UTC ISO 8601                |
| `position_id`          | `str`               | Stringified `paper_trades.id` |
| `wallet_address`       | `str`               | Defaults to `"paper_bot"` if no leader attached |
| `market_id`            | `str`               |                              |
| `pnl_usdc`             | `float`             | Net PnL after fees           |
| `close_method`         | `str`               | `"leader_exit" \| "market_resolved" \| ...` |
| `holding_period_seconds`| `int`              | `int(close - open).total_seconds()` |

### Example payload

```json
{
  "time": "2026-05-18T13:00:00+00:00",
  "position_id": "42",
  "wallet_address": "0xLEADER",
  "market_id": "0xmarket1",
  "pnl_usdc": 12.34,
  "close_method": "leader_exit",
  "holding_period_seconds": 3600,
  "trade_id": 42,
  "leader_wallet": "0xLEADER",
  "pnl_pct": 4.6,
  "direction": "yes",
  "size_usdc": 100,
  "entry_price": 0.65,
  "exit_price": 0.70,
  "close_reason": "leader_exit",
  "strategy": "follow",
  "strategy_track": "production",
  "economic_model_version": "v3",
  "gross_pnl_usdc": 13.0,
  "size_shares": 153.85,
  "loss_reasons": [],
  "context_penalty": 0.0
}
```

---

## `system:status` — `SystemStatusChanged`

Reserved for a future health publisher. Emitted whenever the bot
lifecycle (running/stopped, WebSocket health, killswitch state)
transitions.

### Required fields

| Field         | Type                                 |
|---------------|--------------------------------------|
| `time`        | `datetime`                           |
| `bot`         | `Literal["RUNNING", "STOPPED"]`     |
| `ws`          | `Literal["LIVE", "DEGRADED", "DOWN"]` |
| `ingest`      | `dict` (per-source flags)            |
| `killswitch`  | `bool`                               |

### Example payload

```json
{
  "time": "2026-05-18T12:00:00+00:00",
  "bot": "RUNNING",
  "ws": "LIVE",
  "ingest": {"websocket": "ok", "rest": "ok", "falcon": "degraded"},
  "killswitch": false
}
```

---

## `reconciliation:completed` — `ReconciliationCompleted`

Reserved for the paper-truth reconciliation job (see
[`project_paper_trading_truth.md`](../../.claude/projects/-Users-oscargrima-Documents-Claude-Projects-Polymarket-trading-bot/memory/project_paper_trading_truth.md)
memory). Emitted after each reconciliation run by the maintenance
loop.

### Required fields

| Field         | Type                                |
|---------------|-------------------------------------|
| `time`        | `datetime`                          |
| `verdict`     | `Literal["ok", "warn", "critical"]` |
| `delta_abs`   | `float`                             |
| `sample_size` | `int`                               |

### Example payload

```json
{
  "time": "2026-05-18T03:00:00+00:00",
  "verdict": "warn",
  "delta_abs": 125.50,
  "sample_size": 42
}
```

---

## Anti-drift contract

1. **Every producer MUST** build the event via the Pydantic model and
   call `.model_dump_json()`. Direct `json.dumps(dict)` of the payload
   is **forbidden** for the channels above.

2. **Every consumer MUST** validate via `MyModel.model_validate(data)`
   (where `data` is the already-decoded dict from the Subscriber) or
   `MyModel.model_validate_json(raw)` for raw bytes/string.

3. **Adding a field**:
   * Add the field (Optional + default) to the Pydantic model in
     `src/events/schemas.py`.
   * Add it to the producer call site.
   * Update the consumer if it needs to read the new field.
   * Add an assertion in `tests/events/test_schemas.py` if the new
     field has structural meaning (Literal, validator, …).

4. **Removing a field**:
   * Remove the field from the model only AFTER no producer or
     consumer reads it.
   * Run the full test matrix (`pytest tests/events/ tests/test_observer
     tests/test_engine tests/test_api/test_ws_bridge_snapshot_event.py`).

5. **`extra="forbid"` is non-negotiable** — it's the whole reason this
   module exists. If you need to relax it, you're probably modelling
   the wrong thing.
