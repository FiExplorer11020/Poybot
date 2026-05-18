# Event Contract — Reference rapide

> Référence 1-page des 5 canaux Redis typés. La source de vérité est
> `src/events/schemas.py`. Pour le détail operator-facing avec exemples
> JSON, voir `docs/events.md`. Pour les flows complets, voir
> `docs/architecture/data_flow.md`.

---

## Canaux typés (`CHANNEL_SCHEMA`)

| Constante                    | Channel string              | Schema                  | Producer                                          | WS type           |
|------------------------------|-----------------------------|-------------------------|---------------------------------------------------|-------------------|
| `CHANNEL_TRADES_OBSERVED`    | `trades:observed`           | `TradeObserved`         | `src/observer/trade_observer.py`                  | `trade`           |
| `CHANNEL_DECISIONS`          | `decisions`                 | `DecisionMade`          | `src/engine/decision_router.py`                   | `decision`        |
| `CHANNEL_PAPER_CLOSED`       | `positions:paper_closed`    | `PositionClosed`        | `src/engine/paper_trader.py`                      | `position_closed` |
| `CHANNEL_SYSTEM_STATUS`      | `system:status`             | `SystemStatusChanged`   | `src/api/queries.py:_maybe_publish_system_status` | `system_status`   |
| `CHANNEL_RECONCILIATION`     | `reconciliation:completed`  | `ReconciliationCompleted`| `scripts/reconciliation.py`                       | `reconciliation`  |

Constantes et table `CHANNEL_SCHEMA` exportées par `src/events/schemas.py`.
Table `CHANNEL_TO_WS_TYPE` dans `src/api/ws_bridge.py`. La guard
`_assert_channel_coverage()` raise au startup si les deux ne sont pas en
phase.

---

## Schémas — champs requis (core contract)

### `TradeObserved` (chan `trades:observed`)

| Champ            | Type                       | Note                                      |
|------------------|----------------------------|-------------------------------------------|
| `time`           | `datetime` (UTC ISO)       |                                           |
| `market_id`      | `str`                      |                                           |
| `wallet_address` | `str`                      |                                           |
| `side`           | `Literal["BUY","SELL"]`   | `mode='before'` validator upper-case      |
| `price`          | `float`                    | wire = string (Decimal preservation)      |
| `size_usdc`      | `float`                    | wire = string                             |
| `is_leader`      | `bool`                     |                                           |
| `source`         | `str`                      | `websocket` / `api_market` / `api_wallet` / `falcon` |

+ 9 champs Optional legacy (token_id, market_question, market_category,
market_type, wallet_type/status/strategy/horizon/influence).

### `DecisionMade` (chan `decisions`)

| Champ          | Type                                 | Note |
|----------------|--------------------------------------|------|
| `time`         | `datetime`                           |      |
| `decision_id`  | `str`                                | UUID per route() call |
| `market_id`    | `str`                                |      |
| `action`       | `Literal[OPEN, CLOSE, REDUCE, SKIP, follow, fade, skip, volume_anticipation]` | Canonique OU legacy (dette : choisir un vocabulaire) |
| `confidence`   | `float`                              | Thompson sample value |
| `kelly`        | `float`                              | Bayesian Kelly fraction |
| `reason`       | `str`                                |      |

+ 14 champs Optional (leader_wallet, token_id, side, price, size_usdc,
kelly_fraction (alias legacy), thompson_follow/fade, market_question/
category/type, wallet_type/strategy/horizon/influence, trade_context,
context_penalty, strategy_track, economic_model_version, signal_audit).

### `PositionClosed` (chan `positions:paper_closed`)

| Champ                    | Type        | Note                          |
|--------------------------|-------------|-------------------------------|
| `time`                   | `datetime`  |                               |
| `position_id`            | `str`       | str(paper_trades.id)          |
| `wallet_address`         | `str`       | default `"paper_bot"`         |
| `market_id`              | `str`       |                               |
| `pnl_usdc`               | `float`     | Net (after fees)              |
| `close_method`           | `str`       | `leader_exit` / `market_resolved` / etc. |
| `holding_period_seconds` | `int`       |                               |

+ 17 champs Optional (legacy enrichment de paper_trader + observer.position_tracker).

### `SystemStatusChanged` (chan `system:status`)

| Champ        | Type                                  | Note                              |
|--------------|---------------------------------------|-----------------------------------|
| `time`       | `datetime`                            |                                   |
| `bot`        | `Literal["RUNNING","STOPPED"]`        | `mode='before'` upper-case        |
| `ws`         | `Literal["LIVE","DEGRADED","DOWN"]`   | `mode='before'` upper-case        |
| `ingest`     | `dict`                                | open-ended per-source flags       |
| `killswitch` | `bool`                                |                                   |

### `ReconciliationCompleted` (chan `reconciliation:completed`)

| Champ         | Type                                  | Note                              |
|---------------|---------------------------------------|-----------------------------------|
| `time`        | `datetime`                            |                                   |
| `verdict`     | `Literal["ok","warn","critical"]`     | lowercase (pillar API contract)   |
| `delta_abs`   | `float`                               | USDC absolu de drift              |
| `sample_size` | `int`                                 | nombre de trades évalués          |

---

## Slices côté frontend (`api-client.js`)

| Slice            | WS event qui le met à jour                                                  | Composants principaux                              |
|------------------|------------------------------------------------------------------------------|----------------------------------------------------|
| `systemStatus`   | `system_status` (+ mirror depuis `reconciliation`)                          | Sidebar (BOT/WS chips), Topbar, BootstrapBanner    |
| `paperPnL`       | `trade` (optimistic obs counter), `position_closed` (optimistic PnL)        | Sidebar (Win Rate / PnL), AlphaTerminal KPI strip  |
| `trades`         | `trade`                                                                       | Inspector, AlphaTerminal firehose                  |
| `decisions`      | `decision`                                                                    | DecisionEngine tab                                 |
| `positions`      | `position_closed`                                                             | LivePortfolio (closed_recent)                      |
| `reconciliation` | `reconciliation` (+ propage dans `systemStatus`)                             | Sidebar RECON chip, Inspector PaperTruth panel     |

Subscription côté composant :
```jsx
const { useLiveStoreSlice } = window;
const trades = useLiveStoreSlice('trades') || { recent: [] };
```

Le composant ne rerender que si **ce slice** change.

---

## Anti-drift — règle d'or

```
Producer:    Event(...).model_dump_json()
                 └─▶ redis.publish(CHANNEL_*, json)

Bridge:      raw → ChannelSchema.model_validate_json(raw)
                 └─▶ WS envelope {type, channel, ts, data}

Consumer:    raw → Schema.model_validate_json(raw)   (Pydantic, jamais data.get)
```

JAMAIS :

```python
# FORBIDDEN
redis.publish("trades:observed", json.dumps({"side": "buy", ...}))
data = json.loads(raw)
side = data.get("side")
```

---

## Ajouter un nouveau canal

1. Déclarer la classe Pydantic dans `src/events/schemas.py` avec
   `extra="forbid"` et un docstring listant les producers et consumers.
2. Déclarer la constante `CHANNEL_NEW_THING = "new:thing"`.
3. Ajouter dans `CHANNEL_SCHEMA` :
   ```python
   CHANNEL_SCHEMA[CHANNEL_NEW_THING] = NewThing
   ```
4. Ajouter dans `ws_bridge.CHANNEL_TO_WS_TYPE` :
   ```python
   CHANNEL_TO_WS_TYPE[CHANNEL_NEW_THING] = "new_thing"
   ```
5. Enregistrer dans `WSBridge.start()` :
   ```python
   self._subscriber.register(CHANNEL_NEW_THING, self._on_typed_event)
   ```
6. Ajouter dans `_RATE_LIMIT_MAX_PER_S` (default 100).
7. Côté front, ajouter dans `SLICES` (api-client.js) et un case dans
   `_dispatchTyped`.
8. Tests :
   * `tests/events/test_schemas.py` round-trip + `extra="forbid"` drift.
   * `tests/test_api/test_ws_bridge.py` consumer dispatch + rate-limit.
9. Doc :
   * Une ligne dans la table ci-dessus.
   * Section dédiée dans `docs/events.md`.
   * Flow diagram dans `docs/architecture/data_flow.md`.

`_assert_channel_coverage()` raise au startup si l'une des étapes 3-5
est skipped.

---

## Dette : canaux non-typés (à migrer)

Ces canaux sont publish en `json.dumps(dict)` brut. Aucune validation
côté consumer. À migrer dans une PR follow-up (voir
`docs/review/2026-05-18_post_fix.md` §Dette priorité haute) :

| Channel                       | Producer                                      | Consumer principal           |
|-------------------------------|-----------------------------------------------|------------------------------|
| `graph:follower:confirmed`    | `graph/graph_engine.py:361`                   | telegram notifier            |
| `positions:closed`            | `observer/position_tracker.py:541`            | (reconstruct path)           |
| `registry:leader:added/excluded` | `registry/leader_registry.py:505`         | telegram notifier            |
| `profiler:drift:detected`     | `profiler/error_model.py:506`                 | telegram notifier            |
| `profiler:phase:upgraded`     | `profiler/error_model.py:523`                 | telegram notifier            |
| `engine:watchdog:restarted`   | `engine/watchdog.py:411`                      | telegram notifier            |
| `engine:ingest:gap`           | `engine/main.py:73`                           | telegram notifier            |
| `decisions:trace`             | `engine/paper_trader.py:174`                  | debug/observability          |
| `decisions:live`              | `engine/live_trader.py:603`                   | LiveTrader self-loop, audit  |
| `engine:risk:breaker_tripped` | `engine/risk_manager.py:181`                  | telegram notifier            |
