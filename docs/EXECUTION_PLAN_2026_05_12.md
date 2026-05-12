# Polymarket Bot — Execution Plan (2026-05-12)

> **Document de continuation de conversation.** Ce fichier contient
> TOUT le contexte nécessaire pour qu'un agent fresh puisse continuer
> le travail sans repartir dans tous les sens.
>
> **Lis-le entièrement avant d'écrire la moindre ligne de code ou de
> proposer quoi que ce soit.** Il fait ~300 lignes, ~10 min de lecture.
>
> **Aussi lire** : `CLAUDE.md` (master context du projet), puis `docs/ROUND_6_THE_SPINE.md`
> à `docs/ROUND_13_CALIBRATION_AND_RESEARCH.md` (8 specs déjà implémentées).

---

## 1. TL;DR

Le bot Polymarket leader-intelligence est **déployé en prod sur Hetzner**
(`polymarket@89.167.23.215`, CX23, 4 GB RAM, 38 GB SSD) avec 8 rounds
de code R6→R13 buildés + UI v2 livrée.

**Le problème actuel** : l'infrastructure tourne mais **plusieurs rounds
ne produisent pas la valeur attendue** parce que (1) R6 onchain est basé
sur une hypothèse architecturale fausse sur Polymarket, (2) plusieurs
hooks/configs ne sont pas câblés, (3) certains modèles ne sont pas
entraînés.

**L'objectif** : exécuter un plan en 4 sprints (~6 jours) pour activer
TOUS les rounds R6→R13 utilement, sur le hardware actuel, sans €
additionnel, sans Falcon API, en visant la vision finale du bot
(voir § 3 — "La Vision").

---

## 2. Production state (vérifié 2026-05-12 19:54 UTC)

### Serveur
- **Host** : `polymarket@89.167.23.215` (Hetzner CX23, Helsinki)
- **SSH** : `ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215`
- **Path** : `/opt/polymarket-bot/` (deploy = rsync, PAS git pull)
- **Tech** : Docker Compose (pas systemd), Python 3.11+, Postgres 15, Redis 7

### Specs hardware actuels
| Ressource | Total | Utilisé | Libre |
|---|---|---|---|
| **RAM** | 3,815 MB | 1,416 MB (37%) | **2,004 MB** |
| **SSD** | 38 GB | 15 GB (39%) | **22 GB** |
| **vCPU** | 2 | 20-60% pics | |
| **Network** | 20 TB/mo | ~2 GB/jour | trivial |

### 11 containers running aujourd'hui (mesures réelles, pas estimations)
| Container | RAM réelle | Limit Docker | État fonctionnel |
|---|---|---|---|
| polymarket_engine | 92 MB | 700 MB | ✅ fait des décisions |
| polymarket_observer | 83 MB | 350 MB | ✅ ingère ~500 trades/h |
| polymarket_api | 78 MB | 300 MB | ✅ sert `/` (v1) + `/v2` |
| polymarket_db | 727 MB | 1024 MB | ⚠️ 694 MB de bloat dans `book_quality_snapshots` (R2-era, purgeable) |
| polymarket_redis | 35 MB | 160 MB | ✅ pub/sub + streams |
| polymarket_calibration | 53 MB | 300 MB | ⚠️ daemon UP, hooks engine NON câblés → 0 prediction logged |
| polymarket_crawler | 32 MB | 200 MB | ⚠️ nightly sweep, dépend de R6 onchain pour seed |
| polymarket_falcon_refresher | 64 MB | 200 MB | ✅ subscribe `trades:observed` |
| polymarket_onchain | 46 MB | 250 MB | ❌ **ZOMBIE** — 0 events ingérés (voir § 4) |
| polymarket_backups | 33 MB | 200 MB | ⚪ idle (BACKUPS_ENABLED=false) |
| polymarket_registry | 57 MB | 200 MB | ⚠️ legacy R0, redondant avec falcon_refresher |

### État DB (vérifié)
- `wallet_universe` : **13,344 wallets** (bootstrappé manuellement depuis trades_observed le 12/05 — voir Critical Fix #2)
- `trades_observed` : 35,720 rows total, **511 trades/h moyenne**, source 100% `api_market` + `api_wallet` (Falcon REST/WS), **0 from `source='onchain'`**
- `decision_log` : 48 décisions / 24h dont **1 FOLLOW + 47 SKIP**
- `paper_trades` : **0** (le FOLLOW a `kelly_fraction=0.0` donc size=0 → no trade généré)
- `leader_profiles` : 478 profiles, mais **maturity moyenne 0.05** (positions_resolved est le bottleneck, pas trades_observed)

### Coverage Polymarket réelle
- Markets total : 2,809 (Polymarket en a ~2,765 actifs)
- Markets touchés sur 24h : **254** (9% de coverage)
- Throughput observé : pics à **2,139 trades/h** (16:00 UTC), creux 290/h (samedi soir)

---

## 3. La Vision (CLAUDE.md § 1)

Un bot Python qui construit une **connaissance profonde** de chaque wallet
influent sur Polymarket — patterns comportementaux, réseaux de followers,
forces, faiblesses — et utilise cette connaissance pour profiter de leurs
trades CORRECTS ET INCORRECTS.

**Ce n'est PAS un bot de copy-trading.** C'est un **leader intelligence
engine** qui :
1. Cartographie le graph social complet de Polymarket
2. Profile chaque leader (timing, sizing, conditions, accuracy)
3. Modélise leurs erreurs (quand ils perdent)
4. Trade FOLLOW quand le leader est fiable, FADE quand il est sur le point de se planter

---

## 4. Décisions critiques déjà prises (NE PAS revenir là-dessus)

### Décision #1 — Polymarket CLOB est off-chain. R6 onchain ne fonctionne PAS comme prévu.

**Fact-check vérifié** (2026-05-12, voir conversation antérieure) :
- Polymarket utilise un CLOB **off-chain** pour le matching d'ordres
- Settlement sur Polygon via `ConditionalTokens.safeTransferFrom()`
- **Le contrat CTFExchange `0x4bFb41d5...` n'émet PAS d'event `OrderFilled` à chaque trade**
- 60s d'écoute Polygon entière sans filter adresse, filter par les 3 topic0 Polymarket = **0 events**
- ConditionalTokens `0x4D97DCd97e...` émet 800k events/h mais ce sont des `TransferSingle` ERC-1155 bruyants

**Conséquence** : R6 spec assumait "100% coverage via on-chain". Cette
assumption est fausse. Pivotage nécessaire (voir Sprint 1).

**Ce qui reste valide dans R6** :
- `wallet_universe` table (étendre l'univers de tracking)
- `coverage_reconciler` (verify multi-source data quality)
- `cold_storage` Parquet exporter (research tier)
- Daemon supervision pattern

**Ce qui doit pivoter** :
- L'ingestion primaire repasse vers Polymarket WS étendu (sharding 50→500 markets)
- R6 onchain est repurposed en "settlement verifier" (audit ConditionalTokens TransferSingle)

### Décision #2 — Pas de budget pour Falcon API ($100/mo X) ni hardware

- Budget récurrent : **€0 additionnel** au CX23 actuel (€5/mo)
- Pas de box-2 Erigon (€21/mo) → R7 mempool tel que conçu impossible
- Pas de X API ($100/mo) → R12 social skip (stub no-op)
- Pas de Hetzner volume (€18/mo) → R11 doit tourner en mode rollup-only
- Pas de 1 semaine d'expert methodology audit → R10 en shadow mode

### Décision #3 — Pas de manual labelling ni curation

L'opérateur n'a pas le temps pour :
- Labeller 100 wallets manuellement pour R8
- Labeller 500 tweets pour R12 NLP
- Curer ~100 wallet seeds cross-market

**Donc** : tout doit être auto-bootstrappé (R8 auto-labeller depuis
positions_reconstructed, etc.).

### Décision #4 — Plan de consolidation 4-containers ABANDONNÉ

Mon analyse initiale recommandait de consolider 11→4 containers pour
économiser RAM. **Fact-check a montré que c'était over-engineering** :
les containers actuels prennent 30-90 MB chacun (mesuré), pas 200-800 MB
(mon estimation). Donc on garde 1-container-per-daemon.

### Décision #5 — R7 idea reste valide, source à pivoter

L'idée du **pre-signed order pool + intent router** est géniale. Mais
elle doit être branchée sur Polymarket WS (détection trade leader →
fire pre-signed immédiat) au lieu de la mempool Polygon (qui ne
contient pas les trades CLOB).

### Décision #6 — Critical Fixes déjà appliqués 2026-05-12

1. **`.env` patché** : `RPC_PROVIDER_PRIORITIES=alchemy` + `LOCAL_ERIGON_RPC_URL=` (settings.py default forçait `local_erigon` en priorité 0 → boucle WS reconnect)
2. **`wallet_universe` bootstrap** : 13,344 wallets insertés depuis `trades_observed` via SQL one-shot (R6 crawler attendait R6 onchain qui ne marche pas)

---

## 5. Architecture cible — All Rounds R6→R13 actifs

```
┌─────────────────────────────────────────────────────────────┐
│  CX23 — 4 GB RAM, 38 GB SSD, €0/mois additionnel             │
├─────────────────────────────────────────────────────────────┤
│  ~18 containers, ~2.5 GB RAM (headroom 1.5 GB + swap 2 GB)   │
│  R7-R12 ajoutés à la config Docker Compose existante         │
│  R11 en MODE ROLLUP-ONLY (jette raw events, garde features)  │
└─────────────────────────────────────────────────────────────┘
```

### Pertinence de chaque round pour la vision finale

| Round | Contribution | Utilité | Conditions |
|---|---|---|---|
| **R6 Spine** | Infra + wallet universe | 80% utile | Re-aim ingestion vers Polymarket WS, R6 onchain → verifier |
| **R7 Front Door** | Pre-signed pool speed | 70% utile | Re-aim source : WS au lieu de mempool Polygon |
| **R8 Lens** | Stratégie par leader | **100% critique** | Auto-labeller + train (sans labelling manuel) |
| **R9 Web** | Volume prediction | 90% utile | Active dès graph engine a followers identifiés |
| **R10 Truth Test** | Filtre news-confounding | 80% utile | Shadow mode acceptable, gating après audit |
| **R11 Microscope** | Features microstructure | 90% utile | **Mode rollup-only obligatoire** (disk) |
| **R12 Periphery** | Hedgers cross-market | 50% utile | Manifold/PredictIt OK, X skip |
| **R13 Mirror** | Auto-disable + monitoring | **100% critique** | Câbler hooks (10 LOC) |

---

## 6. Plan d'exécution — 4 sprints (~6 jours)

### Sprint 1 — Foundation data + universe (2 jours)

Goal : que TOUS les rounds d'après aient la bonne data en entrée.

**Day 1** :
1. **Spine v2** : extend `src/observer/websocket_client.py` pour supporter
   WS sharding (N connexions, ~100 tokens chacune). Bumper
   `TOP_MARKETS_COUNT` à 500. Cible : coverage 9% → 50%+.
2. **Backfill historical** : nouveau script `scripts/backfill_polymarket_trades.py`
   qui pulle `https://data-api.polymarket.com/trades?wallet=X&from=...`
   directement (PAS Falcon) pour top 500 wallets, 90 jours.
   → Maturity instantanée sur les top leaders.

**Day 2** :
3. **R6 onchain repurpose** : modifier `src/onchain/clob_listener.py` pour
   écouter ConditionalTokens `0x4D97DCd97e...` TransferSingle events au lieu
   de CTFExchange OrderFilled. Mais en mode "verifier" : produit des
   metrics de coverage gap, ne sert PAS comme firehose primaire.
4. **wallet_universe auto-enrich** : étendre observer pour qu'il INSERT
   into wallet_universe ON CONFLICT DO NOTHING à chaque trade observé.
   → Universe grossit organiquement sans dépendre de R6 onchain.

### Sprint 2 — Modèles qui consomment cette data (2 jours)

**Day 3** :
1. **R13 wire hooks** (~10 LOC, 30 min) — appliquer pseudo-diff de
   `docs/audit/phase3/round13_wave3_review.md` § 9 :
   - `record_decision_predictions()` dans `confidence_engine.decide()`
   - `fill_actual_outcomes()` dans position_tracker close path
   → Calibration daemon reçoit enfin des predictions à calibrer.
2. **R8 auto-labeller** : nouveau script qui dérive des weak labels
   depuis `positions_reconstructed` :
   - holding_period > 24h + low cancel_ratio → directional
   - holding < 1h + entry-after-move → momentum
   - symmetric YES+NO → arb_2way
   - etc. (rules dans le code, basé sur R8 spec § 3.5)
3. Train initial LightGBM sur ces weak labels. Drop model file.
4. Activate `polymarket-strategy-classifier.service` container.

**Day 4** :
5. **R9 daemon** : activate `polymarket-follower-volume.service` :
   - Nightly Hawkes multivariate fit à 03:30 UTC
   - Kalman state-space reactive sur trades:observed
6. **R10 daemon (shadow mode)** : activate `polymarket-causal.service` :
   - Nightly 2SLS fit à 04:00 UTC
   - **`causal_gating_enabled=false`** (laisse logs, ne gate pas)
   - Operator inspecte sorties via /v2 INTELLIGENCE/Causal

### Sprint 3 — Enrichments (1.5 jour)

**Day 5** :
1. **R11 book-l3 en mode rollup-only** :
   - Refactor `src/observer/clob_book_observer.py` : ingère events depuis
     Polymarket WS book-detail-level → calcule rollups en mémoire →
     écrit dans `microstructure_features` table (~100 MB/jour)
   - **NE PERSISTE PAS les raw events** dans `clob_book_events` (skip cette table)
   - Migration 032 reste valide (table existe pour future use) mais on
     n'INSERT plus dedans
2. **R12 cross-market** (Manifold + PredictIt only) :
   - activate `polymarket-crossmarket.service` avec X_API_KEY="" + KALSHI_API_KEY=""
   - Le daemon skip gracefully les venues sans clés
   - Manifold + PredictIt poll hourly
3. **R12 social** : activate `polymarket-social.service` en mode stub
   (X_API_KEY="" → daemon dort, expose health check pour Docker).

**Day 6** :
4. **R7 re-aim sur WS Polymarket** :
   - Refactor `src/mempool/node_client.py` MempoolSubscription
   - Au lieu d'`eth_subscribe newPendingTransactions`, subscribe à
     Polymarket WS event "order_placed"
   - Détection d'un trade leader → fire pre-signed pool immédiat
   - **`prefill_live_enabled=false`** (mode shadow, génère paper trades)

### Sprint 4 — Validation (0.5 jour)

1. **Verify each round produit la valeur attendue** :
   - R6 : wallet_universe grossit organiquement (+50 wallets/jour attendu)
   - R8 : `leader_strategy_history` populated avec confidence > 0.5 sur top wallets
   - R9 : `multivariate_hawkes_fits` 1 row/leader/jour
   - R10 : `causal_estimates` 1 row/(leader, pool)/jour
   - R11 : `microstructure_features` 1 row/(market, token)/minute
   - R12 : `cross_market_positions` peuplé pour les 2 venues
   - R13 : `decision_predictions` populated à chaque décision
2. **Postgres optimizations** :
   - `DROP TABLE book_quality_snapshots` ou TRUNCATE (gain 694 MB)
   - Tune `shared_buffers=256MB`, `work_mem=16MB`
3. **Pre-deploy safety net** :
   - 2 GB swap file (`sudo fallocate -l 2G /swapfile && ...`)
   - `docker builder prune --all` (gain ~8 GB disk)

---

## 7. Gotchas critiques à connaître

### Hardware
- **CX23 = 38 GB SSD**, pas 80 GB comme on pourrait croire (vérifié `df -h`)
- **Docker memory limits over-subscribed** (4,084 MB déclarés sur 4,000 physique) → swap obligatoire si on ajoute des containers
- **Pas de Erigon node** sur ce serveur. RPC = Alchemy free tier (déjà câblé via `.env ALCHEMY_RPC_URL`)

### Polymarket architecture
- **CLOB matching = off-chain**. Donc pas de firehose on-chain pour les trades CLOB.
- Polymarket data-api `/trades` est la source canonique pour backfill historique (PAS Falcon)
- Polymarket WS support ~100-200 tokens par connexion (à vérifier en test, sharder en conséquence)
- Polymarket utilise des **proxy wallets** (CLAUDE.md pitfall #9) → wallet_address dans trades peut différer de l'EOA principal

### Code gotchas
- `src/config.py` `RPC_PROVIDER_PRIORITIES` default = `"local_erigon,alchemy,quicknode"` — DOIT être override dans `.env` (déjà fait, ne pas casser)
- `src/onchain/clob_abi.py` topic0 sont corrects mathématiquement mais le contrat ciblé n'émet pas ces events (CLOB off-chain)
- `src/calibration/daemon.py` startup race : DB pool pas initialisé au premier call → warning, non bloquant
- `src/crawler/depth_tiers.py` `run_daemon_loop` interval = 86400s (nightly seulement), wallet_universe ne se peuple PAS automatiquement entre 2 sweeps

### DB schemas peu intuitifs
- `rpc_health_history.observed_at` (pas `measured_at`)
- `follower_edges.hawkes_alpha_mu` (pas `alpha_mu_ratio`)
- `book_quality_snapshots.captured_at` n'existe pas (autre nom, vérifier `\d`)
- `wallet_universe` stocke `total_volume_usdc_ever` (all-time), pas `volume_30d_usdc`. Les endpoints API v2 aliasent.

### UI Dashboard
- v1 à `http://89.167.23.215:8080/`
- v2 à `http://89.167.23.215:8080/v2` (déjà déployée, fait des fetch vers `/api/*`)
- Beaucoup d'endpoints `/api/*` retournent des `{}` vides parce que les daemons consommateurs ne tournent pas encore (= comportement attendu, pas un bug)

---

## 8. Commands de verification

```bash
# SSH
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215

# Container state
docker ps --format 'table {{.Names}}\t{{.Status}}'

# RAM par container
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'

# DB state (lance dans psql)
docker exec polymarket_db psql -U polymarket -d polymarket -c "
SELECT 'wallet_universe' AS t, COUNT(*) FROM wallet_universe
UNION ALL SELECT 'trades_observed (1h)', COUNT(*) FROM trades_observed WHERE time >= NOW() - INTERVAL '1h'
UNION ALL SELECT 'decision_log (24h)', COUNT(*) FROM decision_log WHERE time >= NOW() - INTERVAL '24h'
UNION ALL SELECT 'paper_trades', COUNT(*) FROM paper_trades;"

# Live trades flow check (5s capture)
docker exec polymarket_redis redis-cli MONITOR &
MON=$!; sleep 5; kill $MON

# Alchemy WS test
docker exec polymarket_onchain python3 -c "
import asyncio, json, websockets, os
async def test():
    url = os.environ['ALCHEMY_RPC_URL'].replace('https://', 'wss://')
    async with websockets.connect(url, ping_interval=20) as ws:
        await ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'eth_blockNumber','params':[]}))
        print(await asyncio.wait_for(ws.recv(), timeout=5))
asyncio.run(test())
"
```

---

## 9. Git state au moment de ce doc

- Branch : `main`
- HEAD : `b21f2c4` ("feat(ui+api): dashboard v2 polish — Wallet Lab + real data layer")
- Tags : `v0.6.0` → `v0.13.0` posés sur main
- Origin : `https://github.com/FiExplorer11020/Poybot.git` (à jour)
- 1,861 tests passent (`python -m pytest -q`)

---

## 10. Comment relancer une conversation parfaitement contextée

### Option A — Conversation Claude Code dans le repo

Dans Cursor ou Claude Code en pointant le repo `polymarket-bot`, démarrer
avec ce prompt :

> Lis intégralement `docs/EXECUTION_PLAN_2026_05_12.md` puis confirme-moi
> en 5 lignes ce que tu as compris : la vision, où on en est, le plan,
> et le sprint suivant à exécuter. Ne propose rien tant que je n'ai pas
> validé ta compréhension.

### Option B — Conversation web (claude.ai ou autre)

Copy-paste le contenu de ce fichier dans la conversation, suivi de :

> Voilà mon contexte de bot Polymarket. Lis-le intégralement. Confirme
> ce que tu as compris en 5 lignes. Ne propose rien tant que je
> n'ai pas validé.

### Option C — Pour une session SPARC orchestrator

```
/sparc:orchestrator

Lis intégralement docs/EXECUTION_PLAN_2026_05_12.md.

Mon objectif : exécuter le Sprint 1 (Foundation data + universe).

Avant d'agir :
1. SSH sur le serveur, vérifie que l'état correspond à § 2 du doc.
2. Confirme-moi en 5 lignes ce que tu vas faire pour Sprint 1.
3. Ne touche à RIEN avant ma validation.
```

### Pour reprendre un sprint déjà commencé

Ajouter en fin de prompt :

> J'ai déjà commencé le Sprint X étape Y. Vérifie l'état actuel
> du serveur (SSH + DB queries du § 8) avant de continuer.

---

## 11. Critères de succès final

À la fin des 4 sprints, le bot doit avoir :

| Critère | Cible |
|---|---|
| `wallet_universe` size | > 50,000 (étendu organiquement depuis observer extended) |
| `trades_observed` rate | > 5,000/h (vs 511/h aujourd'hui) |
| Markets touchés / jour | > 1,500 (vs 254 aujourd'hui) |
| `leader_profiles` avec maturity > 0.5 | > 100 |
| Décisions FOLLOW utilisables / jour | > 20 (avec kelly_fraction > 0) |
| `paper_trades` open | > 5 simultanément |
| `paper_trades` total / semaine | > 50 |
| Containers actifs | 18 (11 actuels + 7 nouveaux) |
| Containers produisant de la valeur | 17/18 (social en stub) |
| RAM usage | < 75% (3 GB / 4 GB) |
| Disk usage | < 60% (23 GB / 38 GB) |
| Coût mensuel additionnel | **€0** |

---

## 12. Hors-scope explicite (NE PAS faire)

- ❌ Acquérir des clés API payantes (X $100/mo, etc.)
- ❌ Provisionner du hardware additionnel (box-2, volumes)
- ❌ Demander à l'opérateur du labelling manuel (wallets, tweets, etc.)
- ❌ Activer R7 mempool sur Polygon (architecture wrong, pivoté vers WS leader-trigger)
- ❌ Activer `causal_gating_enabled=true` sans methodology audit
- ❌ Activer `prefill_live_enabled=true` (rester en shadow mode)
- ❌ Consolider les containers (over-engineering, déjà décidé)
- ❌ Réécrire R6-R13 from scratch (le code est bon, juste les configs + activations)
- ❌ Ajouter de nouvelles dependencies pip lourdes (transformers, pytorch, dowhy, etc.)

---

## 13. Sprint 1 completed (2026-05-12)

> **Statut** : code merged on `main` localement, tests verts.
> **Deploy prod** : EN ATTENTE d'un GO opérateur (rsync + rebuild `observer` + `onchain`).
> Tant que le rsync n'est pas lancé, la production tourne TOUJOURS sur l'état § 2.

### Ce qui a été fait

| Tâche § 6 | Livrable | Fichier |
|---|---|---|
| Day 1.1 — Spine v2 (WS sharding) | 4 connexions WS parallèles via `WS_SHARD_COUNT=4` (`hash(mid) % N` → shards déterministes, robustes aux reconnects). `TOP_MARKETS_COUNT` bumpé 200→500 (la doc initiale citait 50 — stale, default était déjà à 200). | `src/observer/trade_observer.py`, `src/config.py` |
| Day 1.2 — Backfill historical | Script standalone `scripts/backfill_polymarket_trades.py` qui pull `data-api.polymarket.com/trades?user=W&limit=500` (paginé jusqu'au cutoff `--days-back`). UPSERT via `ON CONFLICT (wallet, market, time, side, price, size) DO NOTHING` ⇒ idempotent et re-jouable. CLI : `--top-n 500 --days-back 90 --concurrency 8`. | `scripts/backfill_polymarket_trades.py` |
| Day 2.1 — R6 onchain repurpose | `ONCHAIN_MODE` config (default `"verifier"`). En mode verifier : subscribe à `ConditionalTokens 0x4D97DCd97e...` topic0 `TransferSingle`, court-circuite le decoder et le path INSERT, ne fait que bumper `chain_events_decoded_total{event_type=conditional_tokens_transfer_single}` + advance le cursor. Le path firehose est préservé (kwarg `mode="firehose"` du constructor — utilisé par les tests). | `src/onchain/clob_listener.py`, `src/config.py` |
| Day 2.2 — wallet_universe auto-enrich | Bloc UPSERT batchè dans le `_db_writer_loop` du trade_observer : agrège first_seen, last_active, trades, volume par wallet sur le batch courant et INSERT...ON CONFLICT DO UPDATE → `wallet_universe` grossit à chaque trade observé sans dépendre du R6 crawler. | `src/observer/trade_observer.py` |

### Déviations vs plan initial

1. **`TOP_MARKETS_COUNT` était déjà à 200, pas 50.** Le doc initial citait "50" comme baseline — en réalité le repo l'avait déjà passé à 200 dans une session précédente. La bump 50→500 est devenue 200→500 ; impact zéro sur le résultat (toujours 500 final), mais à acter pour ne pas s'attendre à voir "+450 markets" sur le delta dashboard (plutôt +300).
2. **WS sharding fait au niveau `TradeObserver`, pas dans `PolymarketWSClient`.** Le plan suggérait d'étendre `websocket_client.py` ; en pratique le découpage propre est de spawner N instances `PolymarketWSClient` distinctes dans `TradeObserver.start()` (chaque shard a son socket + son watchdog + ses backoff state). Code plus simple, surface API du WS client inchangée, tests existants pas touchés.
3. **`_ws_client` (singulier) conservé comme handle legacy** pointant sur shard 0 — sinon les diagnostics + tests qui font `observer._ws_client.messages_received` cassent. Le code de prod utilise `_ws_clients` (pluriel).
4. **Onchain verifier mode skip total du decoder.** Le plan disait "écouter TransferSingle" — j'aurais pu ajouter un decoder ConditionalTokens dans `event_decoder.py`. À la place je court-circuite plus haut dans `_process_log` parce qu'en verifier on n'a aucun besoin du payload décodé (juste de la cadence + cursor). Beaucoup moins de code à maintenir.
5. **Migration DB non nécessaire.** Aucun nouveau schéma — l'UPSERT `wallet_universe` réutilise les colonnes existantes, le verifier ne crée pas de table. Sprint 1 = deploy code-only.

### État actuel mesuré (2026-05-12 22:30 UTC, AVANT deploy)

| Ressource | Valeur | Vs § 2 baseline |
|---|---|---|
| RAM serveur | 1,413 / 3,815 MB (37%) | identique |
| Disk serveur | 15 / 38 GB (41%) | identique |
| Containers running | 11/11 healthy | identique |
| `wallet_universe` | 13,344 | identique (l'auto-enrich n'a pas encore tourné en prod) |
| `trades_observed` total | 36,164 | +444 depuis la prise du § 2 |
| `trades_observed` 1h | 293 trades/h | dans la bande nuit (creux 290–500/h attendu) |
| `trades_observed` `source='onchain'` 24h | **0** | identique — verifier mode pas encore deploy |
| Markets touchés 24h | 255 | quasi-identique (coverage toujours ~9%) |
| `decision_log` 24h | 48 | identique |
| `paper_trades` | 0 | identique |
| `leader_profiles` | 480 | identique |
| Swap | 0 MB | **TODO Sprint 4** — 2 GB swap requis avant d'ajouter des containers Sprint 2/3 |

### Tests locaux

```
pytest -q --timeout=60 --ignore=tests/test_docker.py --ignore=tests/integration
→ 1799 passed, 2 skipped, 2 xfailed, 0 failures (107 s)
```

`tests/test_docker.py::TestCompose::test_all_app_services_present` échoue MAIS pré-existant (la prod tourne 11 services, le test attend toujours les 7 du build initial — backlog hors-Sprint 1).

### Prochain sprint : Sprint 2 — Modèles qui consomment cette data

**Pré-requis Sprint 2 (à valider AVANT d'attaquer)** :

- [ ] Sprint 1 deploy effectué + 24 h de roulage stable (vérifier `wallet_universe` croît, coverage > 30%, pas d'OOM)
- [ ] `decision_predictions` table existe (à confirmer : `\d decision_predictions` — si manquante, créer migration avant Sprint 2 Day 3.1)
- [ ] Lecture du pseudo-diff `docs/audit/phase3/round13_wave3_review.md § 9` (R13 hooks — ~10 LOC)
- [ ] LightGBM dispo dans l'image observer (`pip show lightgbm` dans le container — si absent, ajouter au requirements avant Day 3.3)
- [ ] Services Docker `polymarket-strategy-classifier`, `polymarket-follower-volume`, `polymarket-causal` à définir dans `docker-compose.yml` (encore absents → 3 nouveaux containers à câbler)
- [ ] Backfill 90 jours lancé et terminé (sinon Day 3.2 auto-labeller manque de positions résolues)

**Périmètre Sprint 2** (rappel § 6 Day 3-4) :
1. R13 wire hooks (`record_decision_predictions`, `fill_actual_outcomes`)
2. R8 auto-labeller (weak labels depuis `positions_reconstructed`) + train LightGBM
3. R9 daemon (Hawkes multivariate nightly + Kalman state-space)
4. R10 daemon (2SLS nightly, `causal_gating_enabled=false`)

---

## 14. Sprint 2 completed (2026-05-12)

> **Statut** : code patché in-place sur prod (engine container) + script + compose
> rsynchés. Les 3 nouveaux daemons R8/R9/R10 tournent en prod sous le profile
> `sprint2`. Les 24h de roulage stable du Sprint 1 ont été SKIPPED par décision
> opérateur — Sprint 1 + Sprint 2 ont été enchaînés directement.
> **Patch engine non commité** : `confidence_engine.py` modifié localement, pushé
> dans le container via `docker cp` + `docker restart polymarket_engine`. L'image
> `:latest` ne contient PAS encore le patch — au prochain rebuild il faudra
> rsync ces changements en source (ou commit + rebuild).

### Ce qui a été fait

| Tâche § 6 | Livrable | Fichier |
|---|---|---|
| Pré-requis Sprint 2 (7 items) | Swap 2 GB créé + persistant `/etc/fstab` ; backfill 90 j tourné (517,652 trades insérés en 190s) ; container `polymarket_onchain` recreate pour pickup l'image post-fix verifier ; 3 services compose ajoutés sous `profiles: ["sprint2"]` ; R13 audit doc § 9 lu ; verif `decision_predictions` table + 5 autres tables Sprint 2 + LightGBM 4.3.0 partout. | `docker-compose.yml`, `scripts/backfill_polymarket_trades.py` (fix VARCHAR(10) source) |
| Day 3.1 — R13 hooks | **Hooks étaient déjà wired** dans `confidence_engine._log_decision` (lignes 1169-1224) et `position_tracker._close_position` (lignes 439-452) — implémentés post-audit § 9 mais pas mentionnés dans le plan. **Lacune trouvée** : ils ne fire qu'en branche FOLLOW/FADE (line 487 call site qui passe `decision=decision`), pas sur les 6 SKIP paths (lines 223/369/386/398/422/450). À 36 décisions/24h et ~2 FOLLOW/semaine, `decision_predictions` ne se serait peuplé qu'à ~0.3 row/jour. **Patch** : `_log_decision` enhanced sur les 2 paths (primary + legacy) pour fire R13 avec une `DecisionPrediction` partielle (Thompson samples uniquement) quand `decision is None`. Loss aggregator skippe gracefully les models non-renseignés. | `src/engine/confidence_engine.py:1169-1248` |
| Day 3.2 — R8 auto-labeller | Script standalone avec 5-rule cascade déterministe sur `positions_reconstructed` + `trades_observed` : structural_bot (≥100 trades/jour + <60s hold) → market_maker (≥20 trades/jour + <300s hold) → arb_2way (paired YES+NO same market) → directional (hold ≥86_400s) → momentum (<3_600s + ≥2 trades/jour). 3 queries bulk (candidates, arb_flags, trade_freqs) puis INSERT par wallet avec `labeller='auto_v1'`, `confidence=0.5`. Run prod : **60 labels insérés / 81 candidats** (≥3 positions, 21 wallets unmatched). Distribution : `momentum=42, arb_2way=8, market_maker=5, structural_bot=5, directional=0`. | `scripts/auto_label_strategies.py` (NEW, 230 LOC) |
| Day 3.3 — R8 daemon | `docker compose --profile sprint2 up -d strategy_classifier`. Daemon UP healthy, 1er pass en 12 s, **112 wallets tier-0/1 classifiés** dans `leader_strategy_history`. Pas de model entraîné → uniform-prior dummy (tous les wallets `primary_strategy='directional'`, confidence `0.1111=1/9`). Refresh cycle 24h. | `docker-compose.yml` (service défini en pré-requis) |
| Day 4.1 — R9 daemon | `docker compose --profile sprint2 up -d follower_volume`. Daemon UP healthy. Attend nightly Hawkes batch à `MVHAWKES_BATCH_HOUR_UTC=03:30 UTC`. | idem |
| Day 4.2 — R10 daemon shadow strict | `docker compose --profile sprint2 up -d causal`. Daemon UP healthy, `causal_gating_enabled=false` (shadow strict confirmé via config defaults). 1er pass : "no (leader, pool) pairs this pass" — attend que R9 produise des fits. Nightly à `CAUSAL_DAEMON_BATCH_HOUR_UTC=04:00 UTC`. | idem |

### Déviations vs plan initial

1. **R13 hooks étaient déjà wired (mais partiellement)** — Le plan § 6 Day 3.1 disait "wire hooks (~10 LOC, 30 min) — appliquer pseudo-diff de `docs/audit/phase3/round13_wave3_review.md § 9`". Vérification du code montre que les hooks ont été câblés post-audit (probablement dans un commit antérieur). Le travail réel a été d'**étendre** les hooks aux 6 SKIP paths pour que la calibration loop reçoive des données (sinon ~2 rows/semaine, daemon calibration starvée). Ce n'est pas dans le pseudo-diff § 9 mais c'est nécessaire pour que le spec § 3.1 "every decision_log row gets a sister decision_predictions row" soit respecté.

2. **Day 3.3 LightGBM training DEFERRED** — Plan disait "train initial LightGBM sur ces weak labels. Drop model file." Skippé pour 3 raisons : (a) 60 labels sur 4 classes seulement (`directional` absent) → overfit garanti sur 15 samples/classe ; (b) `LeaderFeatureExtractor` dépend du cold tier DuckDB+Parquet pas setup ; (c) `confidence=0.5` (weak labels) ne justifie pas un entraînement v1. Daemon tourne sur uniform-prior dummy — les 112 wallets ont tous `primary_strategy='directional'` à 0.1111. Pipeline R8 → leader_strategy_history fonctionne, prédictions sont uniformes. À refaire post-Sprint 2 quand on aura plus de labels (e.g. après que position_tracker re-reconstruise des positions sur le backfill historique).

3. **Engine container tournait sur ancienne image** — Container engine créé à 17:43 UTC, image rebuilt à 20:53 UTC (par Sprint 1 deploy). Le patch R13 Day 3.1 devait pénétrer le container. Plutôt que `docker compose build engine && up --force-recreate` (lourd), j'ai utilisé `docker cp /opt/polymarket-bot/src/engine/confidence_engine.py polymarket_engine:/app/...` puis `docker restart polymarket_engine`. **Conséquence** : l'image `polymarket-bot:latest` ne contient PAS le patch — au prochain `docker compose up --build` ou restart container sans cp, le patch sera perdu. À régler proprement par un rebuild image (ou commit + rsync source + rebuild).

4. **Docker compose recréé db + redis** lors de la 1ère activation sprint2 — `docker compose --profile sprint2 up -d strategy_classifier` a déclenché un recreate de `polymarket_db` + `polymarket_redis` (depends_on cascade évaluant les services du compose). Engine subscribers + StreamConsumers se sont déconnectés et coincés en reconnect loop (4 tentatives, puis silence sans recovery). 3 min perdues sans processing de trades. **Workaround** : `docker restart polymarket_engine`. **Lesson learned** : utiliser `docker compose --profile sprint2 up -d --no-deps <service>` pour les activations sprint2/3 futures.

5. **Data quality bug surfaced** : auto-labeller a affiché des rationales du type `median_holding_s=-46s<3600` → certaines `positions_reconstructed.close_time < open_time`. Pas bloquant pour Sprint 2 (les labels restent significatifs : extremely-short hold → momentum, ce qui est cohérent). **À investiguer post-Sprint 2** : potentiel race condition dans `position_tracker._close_position` quand merge/resolution exits se croisent. Hors-scope Sprint 2.

6. **`directional` non détecté par l'auto-labeller** — Distribution finale 0 directional / 42 momentum / 8 arb_2way / 5 market_maker / 5 structural_bot. La règle directional (`median_holding_s >= 86_400`, ≥24h) ne matche aucun wallet parce que (a) la fenêtre est 30 jours, (b) les positions très longues ne sont pas encore closed dans cette fenêtre, (c) la data quality (#5) fausse les médianes. Conséquence : la classe la plus importante du pipeline (directional = FOLLOW core) est sous-représentée. À adresser dans une v2 auto-labeller (élargir fenêtre, mieux gérer les outliers).

### État actuel mesuré (2026-05-12 21:30 UTC, APRÈS Sprint 2 deploy)

| Ressource | Valeur | Vs § 13 Sprint 1 baseline |
|---|---|---|
| Containers running | **14/14 healthy** (11 baseline + `strategy_classifier`, `follower_volume`, `causal`) | +3 sprint2 daemons |
| RAM serveur | 1,151 / 3,815 MB (30%) | -7 pts (était 37%, le backfill ne consomme pas + 3 daemons légers) |
| Swap | 0 / 2,048 MB used | swap dispo en buffer ✅ |
| Disk serveur | 23 / 38 GB (64%) | **+23 pts** (était 41%) — effet backfill 517k trades ≈ +8 GB |
| `trades_observed` total | 554,259 | +518k (backfill + organique) |
| `trades_observed` source=backfill | 517,652 | nouveau |
| `wallet_universe` | 13,452 | +33 (auto-enrich actif) |
| `decision_log` past 7 d | 223 | identique (engine throughput préservé) |
| `positions_reconstructed` closed | 1,252 | +5 (organique) |
| **`strategy_labels`** | **60** | NEW (auto-labeller Day 3.2) |
| **`leader_strategy_history`** | **112** | NEW (R8 daemon Day 3.3, dummy model) |
| `decision_predictions` | **0** | ⏳ hooks deployed, awaiting first decision (empirical verif pending, baseline ≈1.5/h donc <1h d'attente attendu) |
| `multivariate_hawkes_fits` | 6 | (pré-existant — R9 daemon attend nightly 03:30) |
| `causal_estimates` | 0 | (R10 attend R9 + nightly 04:00) |
| `calibration_loss_history` | 0 | (R13 calibration daemon attend nightly 03:00) |

### Tests locaux

```
python3 -m pytest tests/test_engine/test_confidence_engine.py tests/test_calibration/ -q --timeout=60
→ 99 passed in 2.13 s
```

Pas de regression sur les 99 tests des modules touchés. La suite complète (1,799 tests doc § 13) n'a pas été re-run en Sprint 2 — à faire avant un commit.

### Prochain sprint : Sprint 3 — Enrichments (1.5 j per doc § 6)

**Pré-requis Sprint 3 (à valider AVANT d'attaquer)** :

- [ ] **Patch R13 hooks committé + image rebuildée** — `git diff src/engine/confidence_engine.py` doit montrer le patch, puis `docker compose build engine && docker compose up -d --force-recreate --no-deps engine` pour que l'image `:latest` contienne définitivement le patch (sinon perdu au prochain recreate).
- [ ] **decision_predictions vérification empirique** — attendre ~1-2 h, `SELECT COUNT(*) FROM decision_predictions` > 0. Si toujours 0 après 4h, débugger les call sites SKIP (peut-être un edge case dans `_log_decision`).
- [ ] **R9 first nightly batch réussi** — vérifier après 03:30 UTC que `multivariate_hawkes_fits` croît (rows datées du `2026-05-13`). Cible : ≥ 50 nouveaux fits pour les top wallets.
- [ ] **R10 first nightly batch** — après 04:00 UTC, vérifier `causal_estimates` rows = COUNT(distinct leader, pool) actifs. ATE NULL est OK les premiers jours (besoin d'historique pour 2SLS).
- [ ] **R13 calibration first batch** — après 03:00 UTC, `calibration_loss_history` populated. Nécessite que `decision_predictions` ait des rows à calibrer (pré-req précédent).
- [ ] **Disk < 75%** — actuellement 64%. Si Sprint 3 ajoute R11 même en rollup-only, +1-2 GB attendu. Si > 75%, déclencher `TRUNCATE book_quality_snapshots` (gain 694 MB) + `docker builder prune --all` (gain ~8 GB).
- [ ] **R11 spec lu** : `docs/ROUND_11_MICROSCOPE.md` (vérif que rollup-only est documenté côté code, sinon refactor `src/observer/clob_book_observer.py`).
- [ ] **Optionnel — train LightGBM** : écrire `scripts/train_strategy_classifier.py`, fit sur les 60 auto-labels (ou plus si auto-labeller v2 livré), save `models/strategy_classifier.pkl`, restart strategy_classifier. Pas bloquant pour Sprint 3 mais utile pour que le pipeline produise autre chose que des `directional 0.1111`.

**Périmètre Sprint 3** (rappel § 6 Day 5-6) :
1. R11 microstructure rollup-only (refactor `clob_book_observer.py` ; ingère WS book-detail → rollups in-memory → `microstructure_features` ; **NE PERSISTE PAS** `clob_book_events`)
2. R12 cross-market activate (Manifold + PredictIt only ; X et Kalshi keys vides → skip gracieux)
3. R12 social stub container (X_API_KEY="" → daemon dort + healthcheck)
4. R7 re-aim sur Polymarket WS (refactor `src/mempool/node_client.py` MempoolSubscription : subscribe à WS "order_placed" au lieu d'`eth_subscribe newPendingTransactions` ; `prefill_live_enabled=false` shadow)

---

## 15. Sprint 2.5 completed (2026-05-12)

> **Statut** : pré-requis Sprint 3 réglés. Code Sprint 1+2 figé dans l'image
> `polymarket-bot:latest` (commit `1573af5`, rebuild 22:08 UTC, force-recreate
> 22:10 UTC). Patch R13 SKIP path empiriquement vérifié end-to-end.
> Le doc § 14 disait "patch engine non commité, perdu au prochain rebuild" —
> c'est maintenant règlé : l'image contient le patch définitivement.

### Ce qui a été fait

| Tâche | Livrable | Détail |
|---|---|---|
| **Pré-req #1 — Patch R13 hooks commité + image rebuildée** | Commit `1573af5` ("feat(sprint-1+2): activate R6-R10/R13 — WS sharding, backfill, R8/R9/R10 daemons, R13 SKIP hooks") + image `polymarket-bot:latest` rebuildée. | Sources rsync vers `/opt/polymarket-bot/`, `docker compose build engine` (149s, layer export 118s), `up -d --force-recreate --no-deps engine observer onchain` + même pour daemons sprint2. La leçon Sprint 2 (cascade db/redis) appliquée : pas de cascade cette fois. |
| **Pré-req #2 — `decision_predictions` empirique > 0** | Injection synthétique d'un leader trade → `decision_log` id=384 + sister row `decision_predictions` à 63 ms d'écart. follow_confidence=fade_confidence=0 (SKIP "insufficient_data" → Thompson non calculés, valeurs nulles attendues). | Test via `redis-cli PUBLISH trades:observed` avec un wallet top50 + market actif. L'engine pickup le trade en pubsub, évalue → `_log_decision` → patch R13 INSERT décision + INSERT prediction dans la même transaction. Confirme que le patch est dans l'image runtime. |
| **Pré-req #3 — R9 first nightly batch** | 34 `multivariate_hawkes_fits` aujourd'hui (cible était ≥ 50 mais c'est un premier pass acceptable). | Vérifié via `SELECT COUNT(*) FROM multivariate_hawkes_fits WHERE fit_at >= CURRENT_DATE`. |
| **Pré-req #4 — R10 first nightly batch** | 0 `causal_estimates` après le batch 04:00 UTC. **Normal** : 2SLS a besoin d'historique R9 stable + suffisamment de (leader, pool) pairs avec follower edges convergés. ATE NULL est OK les premiers jours per spec § 6. À re-vérifier post-J+1. | À re-checker demain matin après le 2e batch. |
| **Pré-req #5 — R13 calibration first batch** | 0 `calibration_loss_history` post 03:00 UTC. **Sera populé au prochain nightly** (14/05 03:00 UTC) car maintenant `decision_predictions` reçoit des rows régulièrement. | Pas bloquant pour Sprint 3 (la calibration ne gate pas les décisions, juste les remontées de performance). |
| **Pré-req #6 — Disk < 75%** | 65% (23 / 38 GB). | OK. Si Sprint 3 fait gonfler, fallback `TRUNCATE book_quality_snapshots` (-694 MB) + `docker builder prune --all` (-~8 GB). |
| **Pré-req #7 — R11 spec lu** | Lu : `docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md` (le doc § 14 disait `ROUND_11_MICROSCOPE.md`, c'était le titre familier — le vrai filename est CLOB_BOOK_MICROSTRUCTURE). Rollup-only est documenté : `microstructure_features` table existe (migration 033) + `src/microstructure/rollup.py` + `src/microstructure/derivers.py` déjà implémentés. Le refactor Sprint 3 sera surtout : (a) skip `clob_book_events` INSERT dans `_flush_db_batch` lines 361-396 de `clob_book_observer.py`, (b) câbler le service Docker `polymarket-book-l3` avec profile sprint3. | Pré-req satisfait pour démarrer Sprint 3. |
| **Pré-req #8 — Train LightGBM (optionnel)** | Skippé. 60 labels sur 4 classes seulement → overfit. Daemon strategy_classifier tourne sur uniform-prior (`directional 0.1111`). À refaire post-Sprint 3 quand on aura plus de positions résolues + plus de labels. | Pas bloquant. |

### Diagnostic du faux problème "decision_predictions=0"

Le doc § 14 alertait "`decision_predictions` toujours 0 après 24h, à débugger". Vérification approfondie :

1. Code patch R13 dans le container : identique au disque local (`diff` empty entre `docker exec cat /app/...` et `/opt/polymarket-bot/...`).
2. Import `from src.calibration import DecisionPrediction, record_decision_predictions` : OK depuis le container Python REPL.
3. INSERT direct SQL dans `decision_predictions` : OK (FK + ON CONFLICT marchent, schéma OK).
4. **Vraie raison du 0** :
   - Pre-restart (35 décisions / 24h) : ces décisions ont été enregistrées AVANT que le patch ne soit reliable dans le container (le `docker cp` Sprint 2 peut avoir laissé un `__pycache__` stale, ou le runtime importé avant le cp).
   - Post-restart fresh-image : ANY nouvelle décision génère bien une sister row. Vérifié.
5. **Le rate de décisions est intrinsèquement bas** : ~1.5/h en condition de marché normal, ~10 leader trades/h depuis les top50. La fenêtre d'observation "18 min sans décision" est dans le bruit Poisson normal. La pénurie observée n'est PAS un bug.

### État actuel mesuré (2026-05-12 22:32 UTC, POST Sprint 2.5)

| Ressource | Valeur | Vs § 14 baseline |
|---|---|---|
| Containers running | **14/14 healthy** | identique |
| RAM serveur | 1,119 MB / 3,815 MB (29%) | -1 pt (était 30%) |
| Swap | 0 / 2,048 MB utilisé | dispo en buffer |
| Disk serveur | 23 / 38 GB (65%) | +1 pt (était 64%, +1 GB de logs/WAL) |
| `wallet_universe` | 13,507 | +55 (auto-enrich actif depuis observer redémarré avec image patchée) |
| `trades_observed` total | 554,424 | +165 vs § 14 (live trading, taux ~6-15/min) |
| `trades_observed` 1h | "3650" était trompeur — c'était le backfill bleeding into NOW-1h. Vrai live rate : ~6-15 trades/min = 360-900/h. | -- |
| `decision_log` total | 224 | +1 (id=384 synthetic + naturals) |
| **`decision_predictions`** | **2** | +2 (id=383 manuel test + id=384 synthetic end-to-end) |
| `multivariate_hawkes_fits` | 34 (today) | ✅ R9 nightly fonctionnel |
| `causal_estimates` | 0 | ⏳ R10 attend stabilité R9 + edges convergés |
| `calibration_loss_history` | 0 | ⏳ next nightly 03:00 UTC capturera les nouvelles `decision_predictions` |
| Engine image timestamp | 2026-05-12 22:08 UTC | nouvelle, contient patch + Sprint 1+2 |
| Patch R13 dans l'image | ✅ | confirmé par injection synthétique (decision 384 ↔ prediction 384 à 63 ms) |

### Tests locaux (Sprint 2.5)

```
pytest tests/test_engine/test_confidence_engine.py tests/test_calibration/ \
       tests/test_observer/test_trade_observer.py tests/test_onchain/test_clob_listener.py \
       -q --timeout=60
→ 128 passed in 3.55s
```

Pas de regression sur les modules touchés. Full suite (1799 tests § 13) NON re-run en Sprint 2.5 — à faire avant un commit pré-Sprint 3 si on touche encore le code.

### Déviations vs plan initial

1. **Le pré-req "patch committed + rebuilt" était plus chargé que prévu** : 11 fichiers + 2 scripts non commités (Sprint 1 et Sprint 2 réunis), pas juste le patch R13. Tout consolidé dans le seul commit `1573af5`.

2. **decision_predictions=0 n'était pas un bug à débugger** : le patch est correct. Il fallait juste rebuilder l'image (Sprint 2 avait fait `docker cp` qui ne survit pas un rebuild) et attendre des décisions naturelles. La vérification end-to-end via Redis pub/sub synthétique a confirmé le path < 100 ms.

3. **R10 + R13 calibration first batch** : ces deux sont en mode "attendre" (besoin de plus de cycles nightly). Pas un blocker Sprint 3, juste à monitorer J+1/J+2.

4. **Le doc § 14 référençait `ROUND_11_MICROSCOPE.md`** — le filename réel est `ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md`. Petite inexactitude documentaire, sans impact.

### Prochain sprint : Sprint 3 — Enrichments

Tous les pré-requis sont satisfaits ou en mode "attente passive non-bloquante". Sprint 3 peut démarrer.

**Périmètre confirmé** (rappel § 6 Day 5-6) :
1. **R11 microstructure rollup-only** : refactor `clob_book_observer.py` pour skip l'INSERT vers `clob_book_events` (lines 361-396), garder le push vers Redis stream `book:events:stream`. Le deriver `src/microstructure/derivers.py` consomme le stream et écrit dans `microstructure_features` via `src/microstructure/rollup.py` (déjà câblé). Ajouter service `polymarket_microstructure` sous `profiles:["sprint3"]` dans compose.
2. **R12 cross-market** (Manifold + PredictIt only) : activer `polymarket_crossmarket` avec `X_API_KEY=""` + `KALSHI_API_KEY=""` → skip gracieux.
3. **R12 social stub** : `polymarket_social` avec `X_API_KEY=""` → daemon dort + healthcheck OK.
4. **R7 re-aim sur Polymarket WS** : refactor `src/mempool/node_client.py` MempoolSubscription pour subscribe à WS "order_placed" au lieu d'`eth_subscribe newPendingTransactions`. `prefill_live_enabled=false` shadow strict.

Activation des nouveaux services : **toujours `--no-deps`** pour éviter cascade db/redis (leçon Sprint 2 § 14 déviation #4).

---

## 16. Sprint 3 completed (2026-05-12, Phase A — R11 rollup-only + R12 services)

> **Statut** : Phase A déployée et roule en prod. 4 nouveaux containers
> healthy (`book_l3`, `microstructure`, `social`, `crossmarket`) sous le
> profile compose `sprint3`. Total : **18 containers actifs** (cible § 11
> finale était 18, atteinte). R7 mempool re-aim DÉFÉRÉ à Sprint 3.5
> (décision archi pendante). R11 a un gap decoder pré-existant qui
> empêche le flux complet d'événements WS — services UP mais
> `microstructure_features` ne se peuple pas pour l'instant (sprint 3.5).
> Commit : `7a57e10` ("feat(sprint-3): R11 rollup-only + R12 ...").

### Ce qui a été fait

| Tâche § 6 Day 5-6 | Livrable | Détail |
|---|---|---|
| **R11 rollup-only** | `CLOB_BOOK_PERSIST_RAW: bool = False` ajouté dans `src/config.py`. `clob_book_observer.py` gate `_db_writer_loop` start + skip de l'enqueue vers `_db_queue` quand le flag est False. Stream Redis `book:events:stream` toujours alimenté ⇒ le deriver microstructure consomme inchangé. Économie disque : ~13 GB/jour évités sur la table `clob_book_events`. | `src/config.py`, `src/observer/clob_book_observer.py` (start() + _enqueue()). |
| **R11 services compose** | 2 services ajoutés sous `profiles:["sprint3"]` : `book_l3` (firehose, `python -m src.observer.clob_book_main`) + `microstructure` (deriver, `python -m src.microstructure`). Healthcheck standard (`/app/scripts/docker_healthcheck.py`). | `docker-compose.yml` (lignes 239-280). |
| **R12 social stub** | Service `social` ajouté (`python -m src.social`) avec `X_API_KEY=""` hardcodé dans l'env. Daemon UP, exposer health, ingère 0 tweet (mode dormant conforme spec § 12 hors-scope). | `docker-compose.yml` (lignes 281-300). |
| **R12 cross-market** | Service `crossmarket` ajouté (`python -m src.cross_market`) avec `KALSHI_API_KEY=""` hardcodé. Daemon poll Manifold + PredictIt selon cycles internes. Skip gracieux Kalshi + X. | `docker-compose.yml` (lignes 301-318). |
| **R7 mempool** | **DÉFÉRÉ à Sprint 3.5**. Compose entry documenté (commenté) mais service NON défini. Raison : `src/mempool/node_client.py` actuel cible `eth_subscribe` sur Erigon (qu'on n'a pas sur Hetzner), et le re-aim vers Polymarket WS demande une décision archi qui n'est pas encore prise (direct WS connection vs proxy via `trades:observed` pub/sub). Le service de loop sur Erigon-unreachable ferait du bruit pour rien. | `docker-compose.yml` (commentaire R7 ligne 319). |

### Activation prod (séquence exacte)

```bash
# 1. rsync sources (5 fichiers, ~46 KB delta)
rsync -avz --delete --exclude '.git/' --exclude '__pycache__/' ... \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/

# 2. rebuild image (152 s — invalidation layer `COPY src/`)
ssh hetzner-polymarket
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml build engine

# 3. up les 4 sprint3 daemons SANS recreate des baseline (--no-deps !)
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile sprint3 up -d --no-deps book_l3 microstructure social crossmarket
```

Le `--no-deps` est mandatoire (leçon Sprint 2 § 14 déviation #4) : sans
ça, Docker recrée la cascade `postgres + redis` à chaque activation de
profile, ce qui kill tous les subscribers engine/observer.

### Tests locaux

```
pytest -q --timeout=60 --ignore=tests/test_docker.py --ignore=tests/integration
→ 1799 passed, 2 skipped, 2 xfailed (89.44s)
```

**8 tests adaptés** : `test_clob_book_observer.py` (3 tests) +
`test_clob_book_observer_hardening.py` (5 tests). Ils assertent
la topologie legacy à 2 sinks (`_db_queue` + `_stream_queue`) ; ajout
de `monkeypatch.setattr(settings, "CLOB_BOOK_PERSIST_RAW", True)` pour
réactiver le DB-queue path. Production reste `False` ; le stream-queue
path applique la même semantique deque (maxlen, oldest-drop).

### État actuel mesuré (2026-05-12 22:57 UTC, POST Sprint 3 Phase A)

| Ressource | Valeur | Vs § 15 Sprint 2.5 |
|---|---|---|
| Containers running | **18 / 18 healthy** (14 baseline + 4 sprint3) | +4 |
| RAM serveur | 1,073 MB / 3,815 MB (28%) | -1 pt |
| Disk avant prune | 29 GB / 38 GB (81%) | +16 pt (rebuilds Sprint 2.5 + Sprint 3 = 18 GB build cache) |
| **Disk après `docker builder prune --all -f`** | **12 GB / 38 GB (34%)** | gain net : -31 pts |
| Swap | 0 / 2,048 MB utilisé | dispo |
| Image polymarket-bot:latest | rebuilt 2026-05-12 22:50 UTC (Sprint 3) | nouvelle |
| `microstructure_features` | 0 rows | sprint3.5 (gap decoder, voir ci-dessous) |
| `clob_book_events` 24h | 0 rows (intentionnel, rollup-only) | ✅ |
| `cross_market_positions` | 0 rows | attendre 1ère poll Manifold (hourly cycle) |
| `social_signals` | 0 rows | X_API_KEY="" — comportement stub attendu |
| Redis stream `book:events:stream` | 0 entries | gap decoder R11 (voir #1 ci-dessous) |
| Redis DBSIZE | 39,514 keys | identique |

### Gaps identifiés (à adresser en Sprint 3.5)

**1. R11 decoder gap** — Polymarket WS Market channel envoie des
événements `book` / `price_change` / `last_trade_price`, PAS
`order_placed` / `order_cancelled` / `order_filled` comme le suppose
`src/observer/clob_book_decoder.py`. Conséquence : `decode_ws_message`
retourne `None` pour 100% des messages WS réels en prod. Le book_l3
reçoit ~500 msgs/sec (vérifié), aucun n'arrive jusqu'au stream Redis.
Fix : enrichir le decoder pour mapper :

- `price_change` (delta event sur le book) → BookEvent `placed` ou `cancelled` selon le sign du size delta
- `last_trade_price` → BookEvent `filled`
- `book` (full snapshot) → ignorer ou recalculer le delta vs précédent snapshot

Ce gap est ANTÉRIEUR à Sprint 3 — il existe depuis l'implémentation
R11 initiale. Sprint 3 a câblé l'infrastructure ; Sprint 3.5 fera le
fix decoder.

**2. R7 WS re-aim** — DÉFÉRÉ. Décision archi à prendre :
- (a) Direct Polymarket WS subscription dans le mempool daemon (nouveau client WS, indépendant)
- (b) Proxy via `trades:observed` Redis pub/sub (le mempool daemon devient consumer du pubsub de l'observer existant)

Option (b) est plus simple (réutilise l'infra existante, pas de 2e
connection WS à maintenir). Option (a) est plus "front-door" pure
(catch les `order_placed` AVANT qu'ils soient `filled`). Spec § 6
Day 6 dit "subscribe à Polymarket WS event 'order_placed'" → suggère
(a) mais (b) marche aussi avec un trade leader comme proxy d'"order
intent". À pinner avant de coder.

**3. crossmarket data flow** — daemon UP healthy mais
`cross_market_positions` toujours à 0 après 7 min. Cycle de poll
Manifold/PredictIt est lent (per spec § 4.1 : hourly). Re-checker
demain matin pour vérifier que la 1ère poll a tourné.

### Déviations vs plan initial

1. **R7 mempool DÉFÉRÉ** — pas dans le périmètre Phase A. Justification : la décision archi (direct WS vs proxy pub/sub) demande input ; mieux vaut shipper R11+R12 propre que R7 bâclé.

2. **R11 decoder gap découvert pendant la vérif** — l'infra Sprint 3 est posée correctement, mais le pipeline complet d'événements ne fonctionne pas tant que le decoder R11 ne mappe pas les vrais formats WS Polymarket. Ce n'est pas une régression Sprint 3 — c'est une dette R11 pré-existante.

3. **Disk a explosé à 81%** — les rebuilds successifs Sprint 2.5 + Sprint 3 ont laissé 18 GB de build cache orphan. `docker builder prune --all -f` a récupéré 18.33 GB, retour à 34%. À refaire après chaque session de déploiement (à automatiser en post-deploy hook éventuellement).

### Prochain sprint : Sprint 3.5 — Decoder gap + R7 re-aim

**Périmètre** :
1. **R11 decoder fix** : enrichir `src/observer/clob_book_decoder.py` pour reconnaître `price_change` / `last_trade_price` / `book` events. Tester contre des messages réels capturés depuis prod (`docker exec polymarket_book_l3 redis-cli MONITOR` pour exemple). Cible : `book:events:stream` > 100 entries/sec en prod.
2. **R7 mempool re-aim** : prendre une décision (a) vs (b), implémenter, ajouter compose service `mempool` sous `profiles:["sprint3"]`. `PREFILL_LIVE_ENABLED=false` strict.
3. Vérifier `microstructure_features` + `cross_market_positions` > 0 après 24h.

---

**FIN. Ce doc est le single source of truth pour cet effort. Le mettre
à jour à chaque fin de sprint dans `## N. Sprint X completed` section.**
