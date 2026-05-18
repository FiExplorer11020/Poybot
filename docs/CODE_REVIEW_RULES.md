# Code Review Rules — Polymarket Trading Bot

> Établi 2026-05-19 après le refactor cross-views (batches A5 → A12).
> Ces règles existent pour empêcher la dette qui a motivé le refactor de
> revenir : trois questions étaient répondues à trois endroits différents
> avec trois valeurs différentes (`observed_trades_24h`, `bot_status`,
> `reconciliation.verdict`), et la dashboard pollait des données qui
> avaient déjà un producer event.

Ce document est lu en début de revue. Aucune PR n'est mergée sans avoir
été confrontée aux trois règles.

---

## Règle 1 — Une question, un service

> **Aucun champ visible à l'écran ne doit être calculable à plus d'un
> endroit dans le code.** Si deux endpoints répondent à la même question,
> l'un délègue à l'autre.

### Pourquoi
Avant batch 2, `bot_status` était calculé indépendamment par :

* `src/api/queries.py:system_status` (lit Redis ws TS)
* `src/api/queries.py:portfolio_pipeline_status` (lit le killswitch)
* `src/api/terminal_snapshot.py:_build_bot_payload` (lit `health.websocket_connected`)

Résultat : la sidebar affichait `RUNNING` pendant que le panel pipeline
affichait `UNKNOWN`. La résolution a été de désigner `system_status()` comme
seule source de vérité et de faire converger les deux autres dessus.

### Sous-règles concrètes

1. **Un seul producer par fait.** Si la donnée bouge dans le temps, elle
   a un et un seul endroit où elle est calculée. Ce endroit est nommé
   dans le commentaire en haut du fichier consommateur.

2. **Pour les compteurs 24h en particulier** : un seul producer Redis
   sliding-window (ZADD/ZREMRANGEBYSCORE avec un score = timestamp),
   un seul lecteur côté service. Le pattern canonique est dans
   `src/observer/trade_observer.py:_update_trades_observed_metric` :
   ```
   zset "metrics:trades_observed:zset" → ZCARD → string "metrics:trades_observed_24h" (TTL 90s)
   ```
   `INCR` simple sur un compteur est interdit pour les fenêtres
   glissantes : ça compte depuis le big bang, pas depuis "il y a 24h".

3. **En PR, le reviewer doit grep le nom du fait dans tout le repo.** Si
   le nom apparaît dans plus d'un endroit qui le **calcule** (pas
   seulement le lit), c'est un rejet automatique. Exemple de commande :
   ```bash
   rg -n "observed_trades_24h\s*=" src/
   ```

4. **Champs canoniques** (UPPERCASE) :

   | Fait                     | Producer                              | Valeurs autorisées             |
   |--------------------------|---------------------------------------|--------------------------------|
   | `bot_status`             | `queries.system_status`               | `RUNNING` \| `STOPPED` \| `DEGRADED` |
   | `ws_status`              | `queries.system_status`               | `LIVE` \| `DEGRADED` \| `DOWN` |
   | `observed_trades_24h`    | `trade_observer._update_trades_observed_metric` | int ≥ 0 |
   | `exec_trades_24h`        | `queries.system_status` (SQL `paper_trades`) | int ≥ 0 |
   | `reconciliation.verdict` | `reconciliation_queries`              | `ok` \| `warn` \| `critical` (lowercase legacy) |
   | `execution_mode`         | `terminal_snapshot._build_bot_payload`| `paper` \| `live` \| `dual` |

5. **Les snapshots ne calculent pas, ils composent.** Un fichier
   `*_snapshot.py` ne contient JAMAIS de SQL direct sur une table hot
   (`trades_observed`, `paper_trades`, `decision_log`). Il appelle des
   services dans `queries.py` ou `pillars_queries.py` etc.

### Anti-patterns explicites

```python
# ❌ FORBIDDEN — deux calculs du même fait, le second peut diverger
# Dans queries.system_status:
observed_trades_24h = await redis.get("metrics:trades_observed_24h")
# Dans portfolio_pipeline_status:
observed_trades_24h = await conn.fetchval("SELECT COUNT(*) FROM trades_observed WHERE time > NOW() - INTERVAL '24h'")

# ✅ CORRECT — un calcul, l'autre délègue
async def portfolio_pipeline_status(conn, redis):
    sys = await system_status(conn, redis)  # source unique
    return {"observed_trades_24h": sys["ingestion"]["observed_trades_24h"], ...}
```

---

## Règle 2 — Le serveur pousse, le client subscribe

> **Polling HTTP interdit pour toute donnée qui a un producer event.**
> Si une donnée bouge en temps réel, elle est publiée via Redis pub/sub
> avec un schéma Pydantic typé, relayée par `ws_bridge` au front. Le
> polling reste **uniquement** un filet de sécurité.

### Pourquoi
Avant A8/A9, chaque tick du WS bridge envoyait un `snapshot_updated`
trigger et le front rappelait `/api/v1/live-summary`. Sous 100 trades/s en
peak, ça déclenchait 100 refetch/s d'un payload de 200 KB. Le rebuild du
snapshot prenait 5-11s. Le dashboard était figé pendant que les requêtes
empilaient sur le pool. Le refactor a déplacé tous les deltas vers les 5
canaux typés et le polling adaptatif (60s sain / 10s dégradé) est devenu
le filet de sécurité.

### Sous-règles concrètes

1. **Si une donnée bouge en temps réel, elle a un producer event.**
   * Le producer construit la payload via la classe Pydantic dans
     `src/events/schemas.py`.
   * Le producer appelle `model.model_dump_json()` (et JAMAIS
     `json.dumps(dict)`).
   * Le producer publie sur le canal via la constante `CHANNEL_*`
     déclarée dans le même module (pas une string literal).

2. **Polling HTTP autorisé uniquement comme :**
   * **Filet de sécurité** : `/api/v1/live-summary` est encore polled
     toutes les 60s (sain) ou 10s (WS silencieux >60s ou disconnect).
     Voir `api-client.js:pickInterval`.
   * **Data analytics non-streamée** : table `markets`, equity timeline,
     wallet graph layout. Ces données changent lentement (>60s) ou sont
     coûteuses à diffuser. Polling acceptable mais avec ETag/304.

3. **Côté front, les composants subscribent via slice.**
   * `useLiveStoreSlice('<name>')` pour les composants qui ne consomment
     qu'une partie du store. Re-render granulaire.
   * `useLiveStore()` (global) est toléré pendant la migration mais
     marqué pour cleanup. Nouveaux composants doivent utiliser la slice.
   * Pas de `useEffect(() => fetch(...), [])` au mount sauf data
     analytics non streamée.

4. **`ws_bridge._assert_channel_coverage()` au démarrage.** Tout canal
   ajouté à `CHANNEL_SCHEMA` doit aussi être ajouté à `CHANNEL_TO_WS_TYPE`
   et au handler `WSBridge.start()`, sinon le bridge raise au démarrage.
   Cette guard est non-négociable.

5. **Rate-limit par canal.** Chaque canal typé a un bucket à 100/s. Le
   `trades:observed` peak à ~50/s en prod, c'est une safety cap, pas une
   throttle de routine. Si un canal commence à drop régulièrement
   (`WSBridge: rate-limit dropped events`), c'est un bug producer.

### Anti-patterns explicites

```javascript
// ❌ FORBIDDEN — polling sur une donnée qui a un producer event
useEffect(() => {
  const id = setInterval(async () => {
    const r = await fetch('/api/recent-trades');
    setTrades(await r.json());
  }, 2000);
  return () => clearInterval(id);
}, []);

// ✅ CORRECT — slice subscription, WS pousse les deltas
const trades = useLiveStoreSlice('trades');
// rerender automatique quand le WS bridge dispatche un event 'trade'
```

```python
# ❌ FORBIDDEN — publish raw dict sans validation
await redis.publish("trades:observed", json.dumps({
    "time": str(trade.time),
    "side": trade.side,
    "extra_field": "oops",  # le consumer ne validera pas, drift silencieux
}))

# ✅ CORRECT — Pydantic model avec extra="forbid"
event = TradeObserved(
    time=trade.time, side=trade.side, ...
)
await redis.publish(CHANNEL_TRADES_OBSERVED, event.model_dump_json())
```

---

## Règle 3 — Le schéma est en code

> **Tout payload qui traverse une frontière (Redis, WS, HTTP) a une
> classe Pydantic ou TypedDict avec `extra="forbid"`.** Les `dict[str,
> Any]` qui transitent entre modules sont une dette qu'on ne crée plus.

### Pourquoi
Avant A5, sept producers publiaient des dicts opaques sur Redis. La
documentation `docs/events.md` listait l'attendu mais rien ne le checkait.
Quand `paper_trader` a renommé `kelly_fraction` → `kelly`, le consumer
Telegram a continué de lire la vieille clé et a affiché `None` pendant
deux semaines avant qu'on s'en rende compte. Le schéma typé avec
`extra="forbid"` rend ce genre de drift impossible : le validator du
consumer raise une `ValidationError` au premier message déviant.

### Sous-règles concrètes

1. **Producers utilisent `model.model_dump_json()`, jamais
   `json.dumps(dict)` brut** pour les canaux typés. Les canaux typés
   actuels sont déclarés dans `CHANNEL_SCHEMA` (`src/events/schemas.py`).

2. **Consumers utilisent `Model.model_validate_json(raw)`** (ou
   `model_validate(dict)` si Subscriber a déjà décodé), jamais `data: Any`
   suivi de `data.get(...)`.

3. **`extra="forbid"` sur tous les models.** C'est ce qui catch le drift.
   Si vous devez relaxer pour un cas legacy, ajoutez le champ comme
   `Optional` au model — n'enlevez jamais la forbid.

4. **Casing tolerance.** Pour rester non-breaking sur les producers
   legacy, les Literal enum-like (`side`, `action`, `bot`, `ws`) ont un
   validator `mode='before'` qui upper-case les inputs. Les nouveaux
   producers doivent **émettre directement la valeur canonique** (pas
   compter sur le validator pour normaliser).

5. **Ajouter un nouveau canal** :
   * Déclarer la classe Pydantic dans `src/events/schemas.py` avec
     `extra="forbid"`.
   * Déclarer la constante `CHANNEL_*` dans le même module.
   * Ajouter dans `CHANNEL_SCHEMA` (le module bridge le pickup auto).
   * Ajouter dans `ws_bridge.CHANNEL_TO_WS_TYPE`.
   * Enregistrer dans `WSBridge.start()` via
     `self._subscriber.register(CHANNEL_*, self._on_typed_event)`.
   * Ajouter dans `_RATE_LIMIT_MAX_PER_S` (default 100).
   * Ajouter un test round-trip dans `tests/events/test_schemas.py`.
   * Ajouter un test consumer dans `tests/test_api/test_ws_bridge.py`.
   * Mettre à jour `docs/architecture/event_contract.md`.

6. **Ajouter un champ à un canal existant** :
   * Optional + default sur le model.
   * Update producer call site.
   * Le consumer ne change pas s'il ne lit pas le champ.
   * Pas besoin de tour de l'écosystème de tests.

7. **Renommer un champ** : grand danger. Procédure :
   * Ajouter le nouveau champ Optional sur le model (`new_name: float
     | None = None`).
   * Le producer émet **les deux** champs pendant N déploiements.
   * Migrer tous les consumers vers `new_name`.
   * Retirer l'ancien champ. Tests verts.
   * Mettre à jour `events.md` et `event_contract.md`.

### Anti-patterns explicites

```python
# ❌ FORBIDDEN — dict opaque qui traverse une frontière
async def publish_decision(self, decision: dict) -> None:
    await self._redis.publish("decisions", json.dumps(decision, default=str))

# ✅ CORRECT — Pydantic model en interface
async def publish_decision(self, decision: DecisionMade) -> None:
    await self._redis.publish(CHANNEL_DECISIONS, decision.model_dump_json())
```

```python
# ❌ FORBIDDEN — consumer Any-typed, drift silencieux
async def _on_decision_message(self, raw: bytes, _channel: str) -> None:
    data = json.loads(raw)
    action = data.get("action")  # le producer a renommé en "act", silence radio
    if action == "follow":
        ...

# ✅ CORRECT — validation Pydantic, raise loud sur drift
async def _on_decision_message(self, raw: bytes, _channel: str) -> None:
    try:
        decision = DecisionMade.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning(f"PaperTrader: dropped malformed decision: {exc}")
        return
    if decision.action == "follow":
        ...
```

---

## Application — checklist de revue

Pour chaque PR, le reviewer pose ces trois questions :

1. **R1** : grep le nom de chaque nouveau fait dans `src/`. Plus d'un
   site qui le **calcule** = rejet.

2. **R2** : la donnée est temps-réel ? Alors un canal Redis typé existe ?
   Le front la consomme via slice subscription ? Polling = filet de
   sécurité ?

3. **R3** : tout payload Pydantic, `extra="forbid"`, producer en
   `model_dump_json`, consumer en `model_validate_json` ?

Voir aussi `docs/PR_CHECKLIST.md` pour la grille détaillée par section.

---

## Exceptions documentées

* **Inspector tab** : a un `useEffect(fetch)` au mount pour `/api/inspector/snapshot`. C'est de la data analytics drilldown qui rebuilds en
  5-11s ; mettre ça sur WS coûterait plus que ça ne rapporte.
* **Equity timeline** : polling toutes les 60s sur `/api/portfolio/equity-curve-v2`. Données lentes (1 point/min), pas de producer event.
* **Wallet Graph layout** : recomputé côté serveur toutes les 5 min, polled. Le graph layout n'est pas un fait temps-réel.
* **`decisions:trace`, `decisions:live`, `market:price_changes`,
  `engine:crash`, etc.** : canaux pas encore typés. À migrer dans un
  follow-up batch (voir `docs/review/2026-05-18_post_fix.md` §Dette).

---

## Métriques de santé du contrat

À surveiller en prod (sont déjà loggés) :

* `WSBridge: rate-limit dropped events` — un producer dépasse 100/s.
* `WSBridge: dropped malformed event` — un producer s'est mis à émettre
  un payload non-conforme.
* `WSBridge: typed handler called on unmapped channel` — drift de
  configuration entre `CHANNEL_SCHEMA` et `CHANNEL_TO_WS_TYPE`.

Aucun de ces warnings ne doit apparaître plus d'une fois par jour en
prod. S'ils apparaissent, c'est une régression de R3.
