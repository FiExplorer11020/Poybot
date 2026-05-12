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

**FIN. Ce doc est le single source of truth pour cet effort. Le mettre
à jour à chaque fin de sprint dans `## 7. Sprint N completed` section.**
