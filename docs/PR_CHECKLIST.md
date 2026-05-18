# PR Checklist — Polymarket Trading Bot

> À cocher dans la description de la PR avant demande de review.
> Les règles détaillées sont dans [`CODE_REVIEW_RULES.md`](CODE_REVIEW_RULES.md).
> Si une case est inapplicable, écrire `N/A — raison` à la place de la cocher.

---

## Backend (Python / FastAPI / asyncpg)

### Logique métier
- [ ] Toute nouvelle route HTTP délègue à un service existant ou créé.
      Pas de SQL direct dans `src/api/main.py` (le fichier passe ~4500 LOC,
      on n'en rajoute plus). Le SQL va dans `queries.py`,
      `pillars_queries.py`, `reconciliation_queries.py` ou un nouveau
      module dédié dans `src/api/`.
- [ ] Si je calcule un fait visible à l'écran (compteur 24h, status,
      verdict…), j'ai grep son nom dans tout le repo. Aucun autre site
      ne le **calcule** (les lecteurs sont autorisés). Voir R1.
- [ ] Si je touche `system_status()`, `_build_bot_payload()`, ou
      `terminal_snapshot`, j'ai vérifié que le SQL/source unique
      reste respecté.

### Events Pydantic
- [ ] Tout nouveau payload Redis a un schema Pydantic dans
      `src/events/schemas.py` avec `extra="forbid"`.
- [ ] Tout nouveau producer utilise `model.model_dump_json()`.
      Aucun `json.dumps(dict)` brut sur un canal typé.
- [ ] Tout nouveau consumer utilise `Model.model_validate_json()` (ou
      `model_validate(dict)` si le Subscriber a déjà décodé).
- [ ] Si nouveau canal Redis : ajouté dans `CHANNEL_SCHEMA`,
      `CHANNEL_TO_WS_TYPE`, `WSBridge.start()`, `_RATE_LIMIT_MAX_PER_S`,
      et `tests/events/test_schemas.py`. Le startup-time
      `_assert_channel_coverage()` doit passer.

### Sliding windows / compteurs
- [ ] Si nouveau compteur "X dans les Y dernières secondes/heures" :
      sliding window Redis (ZADD + ZREMRANGEBYSCORE), pas un `INCR` naïf.
      Pattern canonique : `src/observer/trade_observer.py:_update_trades_observed_metric`.
- [ ] La clé Redis (`metrics:*`) a une TTL ou un script de prune.

### Casing canonique
- [ ] Si nouveau champ "status" : valeurs UPPERCASE canoniques
      (`RUNNING`/`STOPPED`/`LIVE`/`DEGRADED`/`DOWN`). Le frontend lit
      avec `.toUpperCase()` mais émettre directement la valeur canonique
      est la règle.
- [ ] Si nouveau Literal dans un model Pydantic : validator
      `mode='before'` pour les producers legacy si nécessaire, sinon
      Literal strict.

### Performance SQL
- [ ] `EXPLAIN ANALYZE` sur toute nouvelle query qui touche une table
      hot (`trades_observed`, `decision_log`, `paper_trades`,
      `book_quality_snapshots`). Le plan ne fait pas de Seq Scan sur
      `trades_observed` (sauf agrégation sur >24h).
- [ ] Si la query agrège sur 24h, elle utilise l'index composite
      `idx_trades_observed_wallet_time` (migration 052) ou équivalent.
- [ ] Si la query est appelée dans une boucle, je l'ai déplacée hors de
      la boucle (anti N+1).

### Cache / TTL
- [ ] Si nouveau helper dans `_HELPER_CACHE_TTLS` : TTL ≥ 3× temps de
      rebuild mesuré. Sinon `_cached_helper` logue
      `cache thrash detected` et le helper devient inutile.
- [ ] Si le helper a un rebuild >60s, je l'ai documenté dans
      `_HELPER_CACHE_TTLS` avec un commentaire sur le profil mesuré.

### Sécurité
- [ ] Pas de clé/token/secret dans le diff (`git diff | rg -i
      'api_key|token|secret|password' --no-line-number`).
- [ ] Toute entrée utilisateur (query param, body) est validée par
      Pydantic au moins.
- [ ] Toute query SQL est paramétrée (`$1`, `$2`…). Aucune
      f-string-into-SQL avec une valeur utilisateur.

---

## Frontend (React / esbuild)

### LiveStore
- [ ] Mon composant lit via `useLiveStoreSlice('<name>')`, pas
      `useLiveStore()` global, sauf back-compat documentée (à éliminer
      au prochain passage).
- [ ] Pas d'`useEffect(() => fetch(...), [])` au mount sauf data
      analytics non-streamée (Inspector, equity timeline, wallet layout).
- [ ] Si je rajoute un slice : déclaré dans `SLICES` côté store, hydraté
      dans `_hydrateSlicesFromSnapshot`, mis à jour dans `_dispatchTyped`
      pour le bon canal WS.

### Tab keep-alive
- [ ] Si je touche `dashboard-app.jsx`, le pattern `NAV.map +
      display: flex/none + visitedTabsRef` est préservé. Pas de retour à
      `<ActiveTab />` conditionnel (perd l'état des autres tabs).

### Re-render
- [ ] J'ai vérifié dans le browser console (`window.__LIVESTORE_DEBUG__
      = true`) que mon composant ne se re-render pas sur un slice qu'il
      ne consomme pas.
- [ ] Pas d'objet inline dans un `style={}` ou prop qui n'a pas besoin
      d'être recréé à chaque render (provoque rerender enfants).

### Accessibilité / UX
- [ ] Tout chip cliquable a un `title` ou aria-label.
- [ ] Tout état "loading / error / empty" est rendu (jamais de blanc
      sans explication).
- [ ] Couleurs : utiliser `C.green/amber/red/blue/purple/dim2/text` (palette
      `dashboard-components.jsx`), pas de hexcode hard-codé dans le composant.

---

## Tests

### Cross-view consistency
- [ ] Si j'ai modifié un champ exposé dans plusieurs views, j'ai ajouté
      un test dans `tests/test_api/test_terminal_snapshot.py` qui
      vérifie que les deux views remontent la même valeur.
- [ ] Si j'ai modifié `system_status`, le test
      `test_system_status_returns_canonical_fields` passe et couvre les
      nouveaux champs.

### Schemas Pydantic
- [ ] Test round-trip pour tout nouveau model : producer construit →
      `model_dump_json()` → `model_validate_json()` → tous les champs
      préservés.
- [ ] Test de drift : un payload avec un champ inattendu doit raise
      `ValidationError` (grâce à `extra="forbid"`).

### WS bridge
- [ ] Tout nouveau type d'event a un test dans
      `tests/test_api/test_ws_bridge.py` qui vérifie : (1) le canal est
      bien souscrit, (2) le payload typé est forwarded correctement,
      (3) un payload malformé est dropped sans crasher le bridge.

### Front
- [ ] Si je touche `api-client.js`, le test `tests/dashboard/test_livestore.mjs`
      passe (9/9). Sinon, je l'ai mis à jour.

### Suite complète
- [ ] `npm test` vert (137/137 attendu, 1 test pré-existant cassé
      `test_redis_unreachable_returns_degraded` est toléré, voir review note).
- [ ] `npm run lint` vert.
- [ ] `npm run build` vert (l'esbuild du dashboard build en <50ms).

---

## Documentation

- [ ] Si j'ai ajouté un canal Redis : `docs/events.md` ET
      `docs/architecture/event_contract.md` updatés.
- [ ] Si j'ai changé un flow majeur : `docs/architecture/data_flow.md`
      updaté (le diagramme ASCII).
- [ ] Si j'ai ajouté ou changé un champ exposé au dashboard :
      `docs/ws_contract.md` updaté.
- [ ] Pas de nouveau `*.md` à la racine du repo (`/docs/` ou rien).

---

## Sécurité (pre-merge)

- [ ] `git diff --stat` ne montre pas de fichier `.env`, `*.key`,
      `*credentials*.json`.
- [ ] `git log -p HEAD~5..HEAD | rg -i 'BEGIN.*PRIVATE KEY|api_key=|token='`
      ne sort rien.
- [ ] Si la PR touche le killswitch, l'audit log, le reconciliation,
      ou le live trading : revue manuelle d'un second relecteur exigée.

---

## Sign-off

Reviewer : tu as confronté la PR aux 3 règles de
`CODE_REVIEW_RULES.md` ? (R1: une question / un service · R2:
serveur pousse · R3: schéma en code)

Auteur : j'ai vérifié que cette checklist est cochée ou justifiée
case par case. Aucune coche silencieusement skippée.
