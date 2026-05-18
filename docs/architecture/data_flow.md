# Data Flow — Post-refactor (2026-05-19)

> Diagrammes ASCII des 5 flows principaux après le refactor cross-views.
> Lis ce document avant de toucher un producer Redis, un consumer
> Subscriber, ou un composant qui consomme un slice côté front.
>
> Producer = "celui qui écrit la donnée".
> Consumer = "celui qui la lit".
> WS event = `{type, channel, ts, data}` envoyé à la browser via
> `/ws/live`.

---

## Conventions

* `→` : appel direct ou flux de données
* `[X]` : action (verbe)
* `{X}` : donnée (nom)
* `[Redis: <channel>]` : pub/sub Redis sur ce canal
* `[Postgres: <table>]` : INSERT/UPDATE sur cette table

---

## Flow 1 — Trade observed (peak ~50/s, 100/s capped)

```
[Polymarket CLOB WebSocket]  wss://ws-subscriptions-clob.polymarket.com/ws/
      │
      ▼ {price_change, last_trade_price, book} frames
[observer.websocket_client]
      │
      ▼  [sets ws:market:last_message_ts]   (Redis SET, used by ws_status)
      │
[observer.trade_observer._handle_ws_message]
      │
      │  [dedup via Redis 7d TTL set]
      │  [INSERT trades_observed]            (UNIQUE INDEX as DB safety net)
      │
      ▼  TradeObserved(...).model_dump_json()
[Redis: trades:observed]
      │
      ├──▶ [observer.position_tracker]        (reconstruct OPEN→CLOSE cycles)
      ├──▶ [graph.graph_engine]               (follower edge updates)
      ├──▶ [profiler.behavior_profiler]       (Dirichlet/EWMA/KDE)
      └──▶ [api.ws_bridge._on_typed_event]
                │
                │  [TradeObserved.model_validate_json(raw)]
                │  [rate-limit: token bucket, 100/s, drop log every 10s]
                │
                ▼  WS envelope {type: "trade", channel, ts, data}
           [WebSocket /ws/live] → all connected browsers
                │
                ▼
           [api-client.js._dispatchTyped]
                │
                │  case 'trade':
                │    slice.trades.recent = [norm, ...].slice(0, 200)
                │    slice.paperPnL.observed_trades_24h += 1   (optimistic)
                │
                ▼
           Components subscribed to 'trades' rerender
              (Inspector recent trades, AlphaTerminal firehose)
```

**Backpressure** : si un canal trades:observed dépasse 100/s, le bridge
drop les events excédentaires et logue le drop count toutes les 10s.
L'event reste en DB ; seule la diffusion WS est shed.

**Counter sliding 24h** : en parallèle de la publication WS,
`_update_trades_observed_metric` (appelé inside la même tx) maintient
`metrics:trades_observed:zset` (ZADD + ZREMRANGEBYSCORE par batch). Le
counter `metrics:trades_observed_24h` (TTL 90s) est dérivé via ZCARD et
consommé par `queries.system_status` puis exposé sur le snapshot HTTP.

---

## Flow 2 — Decision routed (peak ~5/min)

```
[engine.confidence_engine.evaluate]
      │
      │  Thompson Sampling FOLLOW vs FADE
      │  Bayesian Kelly shrinkage
      │
      ▼  decision: {action, confidence, kelly, reason, ...}
[engine.decision_router.route]
      │
      ▼  DecisionMade(...).model_dump_json()
[Redis: decisions]            (paper)
[Redis: decisions:live]       (live, non-typé encore — voir dette)
      │
      ├──▶ [engine.paper_trader._on_decision_message]
      │         │
      │         │  [DecisionMade.model_validate_json(raw)]
      │         │  [risk checks via runtime_config]
      │         │  [INSERT paper_trades] if action in (follow, fade)
      │         │
      │         ▼  publish "decisions:trace" (NOT typé — dette)
      │
      └──▶ [api.ws_bridge._on_typed_event] (decisions only, not decisions:live)
                │
                ▼  WS envelope {type: "decision", ...}
           [WebSocket /ws/live] → browsers
                │
                ▼
           [api-client.js._dispatchTyped]
                │
                │  case 'decision':
                │    slice.decisions.recent = [data, ...].slice(0, 200)
                │    slice.decisions.counters[action] += 1
                │
                ▼
           DecisionEngine tab rerenders
```

---

## Flow 3 — Position closed (peak ~1/min, paper)

```
[engine.paper_trader._close_trade]
      │
      │  [exit price via PriceOracle (book → gamma → fail)]
      │  [audit log INSERT close_audit_log]
      │  [UPDATE paper_trades SET closed_at, exit_price, pnl_usdc]
      │
      ▼  PositionClosed(...).model_dump_json()
[Redis: positions:paper_closed]
      │
      ├──▶ [telegram_bot.notifier]          (formats + sends Telegram alert)
      ├──▶ [engine.feedback_loop]           (updates leader accuracy posteriors)
      └──▶ [api.ws_bridge._on_typed_event]
                │
                ▼  WS envelope {type: "position_closed", ...}
           [WebSocket /ws/live] → browsers
                │
                ▼
           [api-client.js._dispatchTyped]
                │
                │  case 'position_closed':
                │    slice.positions.closed_recent = [data, ...].slice(0, 100)
                │    slice.paperPnL.total += data.pnl_usdc   (optimistic)
                │    slice.paperPnL.exec_trades_24h += 1
                │
                ▼
           LivePortfolio rerenders, AlphaTerminal KPI strip updates
```

**Note** : la table `close_audit_log` est la source de vérité du
PriceOracle pilier (`pillars_queries._check_oracle`). Chaque close émet
une row avec `oracle_source ∈ {book, gamma, fallback, fail}`.

---

## Flow 4 — System status changed (event-driven, debounced 30s)

```
[api.queries.system_status(conn, redis)]   (called on every snapshot rebuild)
      │
      │  computes bot_status, ws_status, ingestion.* canoniquement
      │
      ▼  [_maybe_publish_system_status_change]
            │
            │  signature = f"{bot_status}|{ws_status}"
            │  if signature == redis.get("system:status:last_emit"):
            │      return   # no transition
            │
            ▼  SystemStatusChanged(...).model_dump_json()
       [Redis: system:status]
            │
            │  redis.set("system:status:last_emit", signature, ex=30)
            │
            ├──▶ [api.ws_bridge._on_typed_event]
            │         │
            │         ▼  WS envelope {type: "system_status", ...}
            │    [WebSocket /ws/live]
            │         │
            │         ▼  [api-client.js._dispatchTyped]
            │                slice.systemStatus = merge(...)
            │                ▼
            │           Sidebar rerenders (BOT/WS chips)
            │
            └──▶ [telegram_bot.notifier]
                       │
                       ▼  Telegram alert "Bot status changed: RUNNING → STOPPED"
```

**Debounce 30s** : évite le spam si bot_status flap (RUNNING ↔ DEGRADED
toutes les 30s sous WS instable). Le Redis key debounce est `set ex=30`
donc le prochain transit après 30s est ré-émis.

**Idempotence** : `system:status:last_emit` est lu/écrit à chaque appel
de `system_status`. Si deux workers FastAPI tournent en parallèle, le
second voit déjà la signature du premier et skip. Côté trade-off : la
debounce est partagée donc rate-limitée naturellement.

---

## Flow 5 — Reconciliation completed (event-driven, 1× / 30 min)

```
[scripts/reconciliation.py]   (cron job from APScheduler)
      │
      │  fetch Gamma quotes for resolved markets (last 24h paper closes)
      │  compare paper_trades.exit_price vs Gamma quote
      │  INSERT paper_close_divergences rows for drift > $1
      │
      ▼  compute summary
         {verdict: ok|warn|critical, delta_abs, sample_size}
      │
      ▼  ReconciliationCompleted(...).model_dump_json()
[Redis: reconciliation:completed]
      │
      ├──▶ [api.ws_bridge._on_typed_event]
      │         │
      │         ▼  WS envelope {type: "reconciliation", ...}
      │    [WebSocket /ws/live]
      │         │
      │         ▼  [api-client.js._dispatchTyped]
      │                slice.reconciliation = data
      │                slice.systemStatus.reconciliation = data   (mirror)
      │                ▼
      │           Sidebar RECON chip rerenders (OK/WARN/Δ$X)
      │           Inspector PaperTruth panel rerenders
      │
      └──▶ [telegram_bot.notifier]
                 │
                 ▼  Telegram alert if verdict ∈ (warn, critical)
                    + drift drilldown link
```

**Backfill** : si la reconciliation détecte des phantom closes (pre-UMA
exits), les rows sont écrites dans `paper_close_divergences` et le
dashboard Inspector tab les surface en drill-down. Le verdict
"critical" (>= $250 absolute drift) déclenche un Telegram CRITICAL.

---

## Flow 6 — HTTP polling fallback (60s steady / 10s degraded)

```
[api-client.js loop]
      │
      │  every pickInterval():
      │    if connectionState !== 'connected':       → 10s
      │    elif Date.now() - lastTypedDeltaAt > 60s: → 10s
      │    else:                                      → 60s
      │
      ▼  GET /api/v1/live-summary  + If-None-Match: <etag>
[api.main.live_summary]
      │
      │  TTL cache (5s) hit?    → return 200 body OR 304
      │  Else:
      │    [parallel gather]
      │      ├─ health snapshot         (helper cached)
      │      ├─ system_status           (canonical bot/ws/ingestion/maturity)
      │      ├─ reconciliation_summary  (cached 30s)
      │      ├─ pillars_status          (parallel gather, 80ms)
      │      ├─ data_quality            (cached 60s)
      │      └─ ... 12 other helpers
      │    [terminal_snapshot.build_snapshot(...)]
      │    [cache result for 5s, emit etag]
      │
      ▼  {data: snapshot, ts, etag}
[browser]
      │
      ▼  [api-client.js processBootstrap → _hydrateSlicesFromSnapshot]
         notifies all 6 slices (coarse but correct on cold start)
```

**Pourquoi le polling est encore là** : filet de sécurité si le bridge
WS bug. Les 6 slices se réhydratent du HTTP snapshot, donc le dashboard
récupère après un downtime du producer WS. En steady state, le polling
ne ramène jamais de nouveauté (le WS pousse) — il sert juste à confirmer
la fraîcheur.

**ETag/304** : le snapshot a un cache TTL 5s. Si rien n'a changé entre
deux polls (et donc l'etag est inchangé), on évite 200 KB de JSON et le
parse côté browser.

---

## Composition récapitulative

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Producers                                                              │
│  ─────────                                                              │
│  trade_observer        ──┐                                              │
│  decision_router       ──┤                                              │
│  paper_trader          ──┼─▶ [Redis pub/sub, 5 typed channels]          │
│  queries.system_status ──┤                                              │
│  reconciliation.py     ──┘                                              │
│                                                                         │
│  + 7 legacy publishers (NOT typed yet — see DETTE in review note)       │
│    graph_engine, position_tracker, leader_registry, error_model,        │
│    watchdog, engine.main (ingest_gap), risk_manager, live_trader        │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Consumers                                                              │
│  ─────────                                                              │
│  ws_bridge ──▶ /ws/live ──▶ api-client.js ──▶ 6 slices ──▶ components   │
│  telegram_bot.notifier (18 channels, sortant)                           │
│  paper_trader._on_decision_message (consumes decisions)                 │
│  profiler/graph_engine (consume trades:observed)                        │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  HTTP fallback (filet de sécurité)                                       │
│  ─────────────────────────────────                                       │
│  /api/v1/live-summary (TTL 5s + ETag)                                   │
│  /api/inspector/snapshot (TTL 30s)                                      │
│  /api/health/pillars (TTL 30s, parallel pillar checks)                  │
│  /api/portfolio/equity-curve-v2 (polled 60s, slow data)                 │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Anti-drift guards en place

1. **`ws_bridge._assert_channel_coverage()`** au startup : raise si
   `CHANNEL_SCHEMA` (keys) ≠ `CHANNEL_TO_WS_TYPE` (keys).
2. **Pydantic `extra="forbid"`** sur tous les models : un producer qui
   sneak un champ inattendu fait drop le message côté consumer avec
   `dropped malformed event`.
3. **`mode='before'` validators** sur les Literal enum-like : tolèrent
   le legacy casing (`buy`/`BUY`) sans accepter `bouy` ou autre typo.
4. **Test `test_ws_bridge_typed_event_round_trip`** : valide que les 5
   canaux font le trajet producer → bridge → ws envelope intact.
5. **Test `test_schemas_extra_forbidden`** : valide qu'un champ
   inattendu raise `ValidationError`.

À ajouter (cf review note) : test round-trip producer Pydantic ↔
consumer Pydantic pour chaque canal en intégration (le test actuel
mocke le Subscriber).
