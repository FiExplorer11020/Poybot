# Session 2026-05-10 — 6 chantiers

Cette session livre l'ensemble des patches associés aux 6 chantiers
discutés. Tout est dans le repo, prêt à être commit / push / redeploy
sur le VM Hetzner.

## TL;DR — déploiement

```bash
# Sur ta machine locale
cd "Polymarket trading bot/polymarket-bot"
git add -A && git commit -m "Session 2026-05-10: DQ + risk cockpit + wallet scanner + inspector + size-weighted profile + dockerfile fix"
git push

# Sur le VM Hetzner (89.167.23.215)
ssh polymarket@89.167.23.215
cd ~/polymarket-bot
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate

# One-shot DQ cleanup (existing 158 leaders stamped 'falcon_no_data')
docker exec -i polymarket_db psql -U postgres polymarket \
  < scripts/cleanup_falcon_no_data_leaders.sql
```

Ensuite ouvre `http://89.167.23.215:8080` et teste :
- l'onglet WALLET GRAPH avec son nouveau toggle Graph / Wallet Scanner
- le nouvel onglet INSPECTOR pour voir le flux brut
- l'onglet RISK & CONFIG : modifier une valeur, cliquer SAVE CONFIG, vérifier qu'elle revient au bon endroit
- l'onglet BOT HEALTH : le compteur "unmapped_tokens" doit avoir baissé drastiquement (~1700 → ~50-200)
- la cohérence visuelle full-width entre tous les onglets

## Chantier 1 — Data Quality (recos a + b combinés)

Objectif : ne plus compter les markets expirés ni les leaders inconnus de
Falcon dans les compteurs DQ, et arrêter de re-traiter ces leaders.

Fichiers touchés :

- `src/registry/leader_registry.py:152-170`
  Quand Falcon ne connaît pas un wallet, on stamp désormais
  `excluded=TRUE, on_watchlist=FALSE` (en plus de `last_refresh=NOW()` et
  `exclude_reason='falcon_no_data'`). Ces wallets sortent immédiatement
  du pool actif.
- `src/registry/leader_registry.py:284-310`
  `sync_markets()` filtre les markets dont `end_date` est passé depuis
  plus de 24h — pas d'enrichissement sur de la donnée morte.
- `src/api/queries.py:data_quality()` (~ligne 1923)
  `unmapped_tokens` ne compte plus que les markets vivants. Nouveau
  champ `unmapped_expired_skipped` exposé pour visibilité côté UI.
- `scripts/cleanup_falcon_no_data_leaders.sql`
  Script one-shot pour rattraper les 158 leaders stampés avant le patch.

Résultat attendu : compteur DQ "unmapped_tokens" passe de ~1707 à
quelques dizaines, "stale_leaders" baisse à ~0 après la première passe
de `enrich_leaders` qui suit le restart.

## Chantier 2 — Wallet Graph enrichi (remplace Market Scanner)

Objectif : supprimer la vue market-centric (sans rapport avec l'edge
wallet-centric du bot) et enrichir le Wallet Graph avec un vrai scanner
de leaders.

Fichiers touchés :

- `static/dashboard/dashboard-app.jsx`
  Onglet `MARKET SCANNER` retiré du nav. Onglet `INSPECTOR` ajouté.
- `static/dashboard/dashboard-tabs.jsx` (`WalletGraph`, ~ligne 1180)
  Toggle [Graph View | Wallet Scanner] dans l'onglet WALLET GRAPH. La
  vue Wallet Scanner affiche pour chaque leader : phase, strategy,
  falcon_score, trades 24h, trades observed, positions resolved,
  win rate, PnL, readiness composite, dernière action décidée.
  Click sur une row → switch vers la vue Graph avec le wallet
  pré-sélectionné dans l'inspector.
- `src/api/queries.py:wallet_graph()` (~ligne 2258)
  Étend la requête avec `win_rate`, `closed_total`, `pnl_total`,
  `trades_24h`, `last_seen`, `last_action`, `last_confidence`
  jointurés depuis `paper_trades`, `trades_observed`, `decision_log`.

La vue `MarketScanner` reste dans le code (legacy) — elle a été
réorganisée pour servir de fallback debug, accessible uniquement via
le sous-onglet "Markets (legacy)" si on rebranche l'item de nav.

## Chantier 3 — Bug full-width corrigé

Cause : aucun max-width CSS, mais le wrapper du tab actif n'avait
pas d'`width` explicite — selon le contenu de chaque tab, le navigateur
laissait shrink différemment.

Fix : `static/dashboard/dashboard-app.jsx:196-202`
Le wrapper du tab actif est passé en `display: flex, width: 100%` avec
un container intermédiaire flex-column qui force chaque tab à occuper
toute la largeur. Tous les onglets (ML Progression, Live Portfolio,
Decision Engine, Risk Config, Bot Health) sont maintenant alignés sur
la même largeur que Alpha Terminal.

## Chantier 4 — Pipeline coherence : pondération size_usdc

Audit refait : `behavior_profiler.py` utilisait déjà `size_usdc` pour
l'EWMA et `size_ratio` (input du modèle d'erreur). En revanche, les
priors Dirichlet sur les catégories et les Beta posteriors sur l'accuracy
étaient en +1 par trade, sans tenir compte de la conviction (un trade
de $50k pesait pareil qu'un de $50).

Patches :

- `src/profiler/behavior_profiler.py:_size_weight()` (nouveau, ~ligne 866)
  Convertit `size_usdc` en poids dans `[0.5, 3.0]` via
  `sqrt(size / ewma_size)`, clampé. Compression sub-linéaire pour qu'un
  whale unique ne domine pas le prior.
- `src/profiler/behavior_profiler.py:_update_dirichlet()` (~ligne 891)
  Accepte maintenant `size_usdc` et incrémente la concentration α par
  le poids calculé. Compatibilité ascendante : si `size_usdc<=0` ou
  `ewma_size==0`, fallback vers le +1 historique.
- `src/profiler/behavior_profiler.py:_update_accuracy()` (~ligne 940)
  Beta posterior `(beta_a, beta_b)` également pondéré. Les compteurs
  raw `wins`/`losses` restent en +1 (utilisés pour l'affichage).
- `src/profiler/behavior_profiler.py:on_position_closed()` (~ligne 165)
  Ordre changé : `_update_sizing` AVANT `_update_dirichlet` /
  `_update_accuracy` pour que le baseline EWMA soit déjà à jour quand
  on calcule le poids.

Effet : un leader qui place un trade $50k en "geopolitics" verra sa
préférence pour cette catégorie monter ~3× plus vite qu'avant. Les
Thompson samples convergent plus vite vers la vraie préférence du
leader. Les nouveaux profils héritent immédiatement du comportement ;
les profils existants en cours convergent au fur et à mesure des
nouvelles résolutions.

Améliorations identifiées MAIS non livrées (cf. section "Next") :
- pondération `size_usdc` dans `same_direction_rate` des follower_edges
- attribution `source` (websocket/api_market/api_wallet) dans le profil
- pondération par `liquidity_score` pour les markets très peu liquides

## Chantier 5 — Risk & Config Option 2 (cockpit fonctionnel)

Le cockpit était read-only avec `config_mutable=False` codé en dur.
Maintenant il modifie réellement les seuils du RiskManager au runtime.

Fichiers ajoutés :

- `src/control/runtime_config.py` (nouveau)
  Singleton `RuntimeConfig` qui charge les défauts depuis `settings`,
  persiste les overrides dans Redis (`runtime_config:overrides`),
  publie les changements sur `runtime_config:changed` (pub/sub),
  cache 30 s en mémoire. Validation par clé (`ALLOWED_KEYS`) + bounds
  (`BOUNDS`) — toute écriture hors-périmètre est rejetée.

Fichiers touchés :

- `src/config.py:113-130`
  Ajout de `KELLY_FRACTION`, `MAX_DRAWDOWN_STOP_PCT`,
  `MAX_CONCURRENT_POSITIONS`, `MAX_CONSECUTIVE_LOSSES`,
  `MAX_RECENT_LOSSES_PER_MARKET` (étaient hardcodés dans
  `risk_manager.py`).
- `src/engine/risk_manager.py:46-100`
  `check_can_trade` lit ses seuils depuis `runtime_config.effective()`
  au lieu d'utiliser des littéraux. Nouvel async `apply_size_async`
  qui en fait autant ; `apply_size` synchrone conservé pour compat.
- `src/api/main.py`
  `init_runtime_config(_redis)` au lifespan startup. Nouveaux endpoints
  `GET /api/risk/config` (lecture défauts + bounds + valeurs effectives)
  et `POST /api/risk/update` (validation + persistence + invalidation
  cache snapshot). `runtime.config_mutable` flippé à `True`.
- `src/engine/main.py:13,69`
  `init_runtime_config(redis_client)` aussi côté engine pour que les
  overrides soient lus par RiskManager dans le process engine
  (le module API et le module engine partagent le même Redis donc
  les écritures sont visibles partout).
- `src/api/terminal_snapshot.py:_build_risk_config()`
  Lit les valeurs effectives via le paramètre `runtime_overrides` au
  lieu de retourner les défauts.
- `static/dashboard/api-client.js`
  `botControl(cmd)` et `updateConfig(edits)` câblés sur les vrais
  endpoints. Plus de `READ_ONLY_ERROR`.
- `static/dashboard/dashboard-tabs.jsx:RiskConfig`
  Champs réorganisés en 3 sections : Sizing & Kelly, Circuit Breakers,
  Position Management. Champs obsolètes (entry_threshold, spread_cap,
  fee_bps, max_holding_seconds) retirés. Nouveaux champs exposés :
  max_consecutive_losses, max_recent_losses_per_market, fade_size_ratio.

Sécurité : la validation server-side rejette tout `risk_per_trade_pct`
hors `[0.001, 0.10]`, `max_drawdown_stop_pct` hors `[0.05, 0.50]`, etc.
Impossible de mettre le bot dans un état ouvertement dangereux via le
dashboard. Les écritures sont loguées avec l'`actor` (par défaut
`dashboard`).

## Chantier 6 — Observabilité : nouvel onglet INSPECTOR

Objectif : voir en temps réel ce que reçoit le serveur et ce qu'il
décide, sans avoir à SSH.

Fichiers ajoutés :

- `src/api/queries.py:inspector_snapshot()` (nouveau, ~ligne 2465)
  Renvoie en un appel : raw_trades (last 80 avec tous les champs +
  question + category jointe), decisions (last 50 du decision_log),
  source_mix (5 min, par source avec ratio leader/non-leader), counters
  (trades 1h, leader_trades 1h, decisions 1h, actionable 1h, closes 1h),
  pipeline (Redis health, WS lag, msgs/min, pubsub subscribers).

Fichiers touchés :

- `src/api/main.py:944` — endpoint `GET /api/inspector/snapshot?limit=N`
- `static/dashboard/dashboard-app.jsx` — onglet INSPECTOR dans le nav
- `static/dashboard/dashboard-tabs.jsx:Inspector` (nouveau composant)
  - 6 KPI cards (trades 1h, leader trades, decisions, actionable, closes, WS lag)
  - Toolbar avec filtre wallet (all/leader/non-leader) + filtre source
    (all/websocket/api_market/api_wallet) + auto-refresh toggle 3s
  - Stream raw trades (table monospace, dernières 80, ID + time + wallet
    + side + price + size + source + market)
  - Source mix avec ProgressBar par source
  - Pipeline Health (Redis, WS lag, msgs/min, pubsub subscribers)
  - 20 dernières décisions avec leur reason et confidence

Ce qui est **prévu mais pas encore livré** (foundations posées) :
- Pipeline Trace par décision (modal détaillé qui montre les inputs
  Thompson + Kelly de chaque décision). Le `decision_log` table stocke
  déjà la plupart des champs nécessaires. Il faudra étendre pour
  ajouter un champ `trace_json` snapshot des features et un endpoint
  `GET /api/decisions/{id}/trace`.
- Endpoint Prometheus `/metrics` exposant throughput, queue depths,
  Falcon API rate limit usage. Hors scope cette session.

## Chantier bonus — Dockerfile

Le `docs/migrations/` n'était pas copié dans l'image runtime, ce qui
cassait `setup_db.py` dans le container.

Fix : `Dockerfile:85-90`
Ajout de `COPY --chown=polymarket:polymarket docs/migrations/ ./docs/migrations/`.

## Récap fichiers modifiés (15 patches)

```
M  Dockerfile
M  scripts/cleanup_falcon_no_data_leaders.sql              (nouveau)
M  src/api/main.py
M  src/api/queries.py
M  src/api/terminal_snapshot.py
M  src/config.py
M  src/control/runtime_config.py                            (nouveau)
M  src/engine/main.py
M  src/engine/risk_manager.py
M  src/profiler/behavior_profiler.py
M  src/registry/leader_registry.py
M  static/dashboard/api-client.js
M  static/dashboard/dashboard-app.jsx
M  static/dashboard/dashboard-tabs.jsx
M  docs/SESSION_2026-05-10_RUNBOOK.md                       (ce fichier)
```

## Tests rapides post-deploy

```bash
# 1. POST /api/risk/update (test cockpit)
curl -X POST http://89.167.23.215:8080/api/risk/update \
  -H 'Content-Type: application/json' \
  -d '{"edits":{"max_drawdown_stop_pct":0.15,"actor":"smoke-test"}}'
# Attendu: {"config":{"risk_per_trade_pct":0.02,"...","max_drawdown_stop_pct":0.15,...}}

# 2. GET /api/risk/config
curl http://89.167.23.215:8080/api/risk/config
# Attendu: { config: {...}, allowed_keys: {...}, bounds: {...} }

# 3. GET /api/inspector/snapshot
curl http://89.167.23.215:8080/api/inspector/snapshot?limit=20
# Attendu: { raw_trades:[...], decisions:[...], source_mix:[...], counters:{...}, pipeline:{...} }

# 4. DQ counter
curl -s http://89.167.23.215:8080/api/data-quality | python3 -m json.tool | head -20
# Vérifier: markets.unmapped_tokens << 1700, leaders.stale_refresh ~ 0
```

## What's next (non livré cette session)

Ces items méritent une session dédiée :

1. **Pipeline Trace par décision** — étendre `decision_log` avec
   `trace_json BYTEA`, capturer les inputs/outputs de chaque étape
   (Thompson sample, Kelly calc, gates passed/failed). Endpoint
   `GET /api/decisions/{id}/trace`. Modal dans Decision Engine.

2. **Endpoint Prometheus `/metrics`** — pour scraper en externe
   (Grafana Cloud, UptimeRobot avec content match). Throughput WS,
   p50/p95/p99 latency, queue depths, Falcon rate limit usage.

3. **Source attribution dans behavior_profiler** — exploiter le champ
   `source` (websocket vs api_market vs api_wallet) pour ajuster la
   confidence selon la latence d'attribution. Un trade attribué via
   polling REST a 30s de retard, ce qui peut nous faire fade des
   moves déjà éventés.

4. **Liquidity-weighted profile updates** — pondérer les Beta posteriors
   par `liquidity_score` du market. Un trade dans un market à
   $1k de liquidité est moins informatif qu'un trade dans un market
   à $1M.

5. **Activation backups R2** — créer le bucket Cloudflare R2, ajouter
   les credentials au `.env`, flip `BACKUPS_ENABLED=true`. Doc déjà
   dans `docs/backups.md`.

6. **UptimeRobot** — point de monitoring externe sur `/healthz` et
   sur le delta `last_trade_age_s` (pour détecter une coupure WS qui
   ne tue pas le process).
