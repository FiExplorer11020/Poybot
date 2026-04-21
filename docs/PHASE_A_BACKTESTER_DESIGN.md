# Phase A — Backtester + Coût Modeling — Design Doc

> Objectif : prouver ou réfuter la rentabilité de la thèse FOLLOW/FADE **avant** toute autre
> ingénierie. Sortie attendue : rapport OOS honnête sur 60–90 jours → **Gate 1**
> (Sharpe net > 0.5 ET FADE AUC > 0.60).
>
> Principe directeur : aucune optimisation prématurée, aucune feature nouvelle. On rejoue
> les primitives déjà codées (`confidence_engine`, `error_model`, `behavior_profiler`,
> `risk_manager`) contre des données historiques réelles, avec des coûts réalistes.

---

## 0. État de départ (constaté 2026-04-20)

- DB locale vide sauf `leaders` (200 rows depuis Falcon agent 584).
- Aucun `trades_observed`, `positions_reconstructed`, `paper_trades`, `markets`,
  `follower_edges`, `leader_profiles`, `decision_log`.
- Falcon API accessible (certifi requis côté client), rate limit tight (~1 req/s
  après succession rapide, 429 fréquents).
- Endpoints validés :
  - `556` (trades) : `{proxy_wallet | condition_id, [start_time, end_time]}` — timestamps **epoch seconds en string**.
    Retourne : `timestamp, side (BUY/SELL), price, size, outcome (Yes/No), token_id, condition_id, slug, proxy_wallet, tx_hash`.
  - `572` (orderbook snapshots) : `{token_id, start_time, end_time}` — requis. Peut retourner 0 rows sur marchés illiquides.
  - `568` (candlesticks) : `{token_id, start_time, end_time, interval?}` — même contrainte.
    Payload réel observé : timestamp de bougie sous `candle_time`.
  - `574` (markets metadata) : `{condition_id | market_slug}` — retourne tokens yes/no, dates, volume_total.
  - `581` (wallet 360), `579` (pnl leaderboard), `584` (leaderboard) : déjà intégrés.
  - `575` (market insights) : à tester pour liquidity_score.
- Persistance déjà existante (corrections de l'audit) :
  - Thompson α/β : rehydraté via `leader_profiles.decision_learning` + cache Redis
    (`confidence:leader:{wallet}`).
  - CUSUM : persisté dans `leader_profiles.profile_json.runtime.cusum_state`.

**Conséquence** : le backtester doit être **auto-suffisant** — il ne peut pas s'appuyer
sur la DB pour les données historiques ; il doit pouvoir reconstituer tout l'état à
partir de Falcon + cache local.

---

## 1. Ce que le backtester doit prouver (ou réfuter)

Gate 1 exige deux choses, pas une :

1. **FADE AUC OOS > 0.60** sur la prédiction `P(leader perd | contexte)`.
   Si AUC ≤ 0.60 → la thèse FADE est morte, il faut la retirer du produit.
2. **Sharpe net OOS > 0.5** sur le P&L FOLLOW+FADE combiné, après fees + spread + slippage + lag.
   Si Sharpe ≤ 0.5 → même une thèse FADE valide ne survit pas aux coûts réels.

Les deux doivent passer indépendamment. Si seul (1) passe, c'est un signal statistique
sans traduction économique — la V1 reste non viable.

---

## 2. Architecture (event-driven walk-forward)

Un seul process, pas d'async requis côté event loop principal (on rejoue un fichier).
I/O async uniquement pour le data loader vers Falcon.

```
┌─────────────────────────────────────────────────────────────────────┐
│                   scripts/backtest.py (entrypoint)                  │
│  CLI: --start 2026-01-20 --end 2026-04-20 --wallets N --out FILE    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
            ┌───────────────────┴────────────────────┐
            ▼                                        ▼
┌───────────────────────┐              ┌──────────────────────────────┐
│   src/backtest/       │              │   src/backtest/engine.py     │
│   data_loader.py      │              │   (event loop)               │
│   (Falcon fetch +     │              │                              │
│    local Parquet      │──events────▶│   Pour chaque trade (t, ...):│
│    cache)             │              │    1. detect LEADER entry    │
└───────────────────────┘              │    2. snapshot profile@t     │
            │                          │    3. error_model.predict@t  │
            │                          │    4. confidence.evaluate@t  │
            ▼                          │    5. size via risk_manager  │
┌───────────────────────┐              │    6. open virtual position  │
│   data_cache/         │              │    7. advance price (book)   │
│   *.parquet           │              │    8. close on leader_exit   │
│   (trades, books,     │              │       OR stop/TP/timeout/    │
│    markets)           │              │       resolution             │
└───────────────────────┘              │    9. apply costs → PnL      │
                                       │   10. update Thompson α/β    │
                                       │   11. emit row to results    │
                                       └──────────────────────────────┘
                                                        │
                                                        ▼
                                         ┌──────────────────────────────┐
                                         │  src/backtest/report.py      │
                                         │  - PnL cumulé, Sharpe        │
                                         │  - AUC/Brier FADE predicts   │
                                         │  - Calibration curve         │
                                         │  - Ablations readiness       │
                                         │  - Output: JSON + Markdown   │
                                         └──────────────────────────────┘
```

### 2.1 Principes non-négociables

- **Causalité stricte** : au temps `t`, on n'utilise que des données dont `ts ≤ t − lag`
  où `lag = LIVE_DECISION_MAX_TRADE_AGE_S` (120 s par défaut) pour refléter la latence
  d'observation réelle via polling 60 s. Une erreur courante à éviter : utiliser le
  profil final d'un leader au lieu du profil à `t`.
- **Walk-forward split** : split temporel strict (pas de k-fold aléatoire).
  - Entraînement : `[start, t_split]`
  - Test : `(t_split, end]`
  - Pour le error model Phase 2/3, on re-fit sur fenêtre roulante de 90 j (identique
    au comportement batch_runner). Phase 3 = re-fit 7 j.
- **No lookahead** : aucune utilisation de `condition_id.end_date` dans les features
  au-delà de "temps restant avant résolution" **tel que connu à t**.
- **Coûts obligatoires** : fees + spread + slippage + lag ajoutés avant de considérer
  le PnL comme "net". Un PnL "gross" peut figurer en parallèle pour diagnostic mais
  n'entre pas dans la décision Gate 1.
- **Pas de feature nouvelle** : on rejoue exactement les primitives actuelles
  (`error_model._build_features`, Kelly shrinkage du `risk_manager`, Thompson du
  `confidence_engine`). Si une primitive est incorrecte, on la corrige en amont, on
  ne contourne pas dans le backtester.

---

## 3. Data pipeline

### 3.1 Sélection de l'univers

- **Leaders** : top N (paramétrable, défaut 50) parmi les 200 existants en DB, triés
  par `falcon_score DESC` et **not excluded**.
- **Marchés** : on n'impose pas d'univers *a priori*. On laisse les leaders nous guider :
  tout `condition_id` vu dans leurs trades via agent 556 devient candidat. On filtre ensuite
  ceux pour lesquels `574.volume_total ≥ VOLUME_MIN_USDC` (défaut $50k) pour éviter les
  marchés illiquides où les coûts dominent.
- **Fenêtre temporelle** : `--start`/`--end`. Défaut : 90 derniers jours. 60 premiers
  = warmup (pour que Thompson / CUSUM / error_model aient des posteriors non dégénérés),
  30 derniers = **période d'évaluation OOS**.

### 3.2 Fetching (à écrire dans `src/backtest/data_loader.py`)

- **Trades** (agent 556) : pour chaque leader, fenêtrer par tranches de 7 jours
  (pour éviter `has_more` à pagination unique), paginer offset-based jusqu'à épuisement.
  Utiliser strictement le paramètre `proxy_wallet`; `wallet_proxy` renvoie des trades
  d'autres wallets et contamine le backtest.
- **Markets** (agent 574) : une fois par `condition_id` observé, récupérer metadata +
  `token_yes`/`token_no`/`end_date`/`volume_total`.
- **Orderbook snapshots** (agent 572) : pour chaque `(token_id, jour)` où au moins un
  trade leader existe, tenter un range 24h. Garder snapshot le plus proche de chaque
  entrée/sortie leader pour estimer le spread. Si l'agent retourne vide, fallback =
  spread moyen observé ± 2σ par catégorie (voir §4.1).
- **Candlesticks** (agent 568) : 1h intervals pour `token_id × [t_open - 2h, t_close + 2h]`.
  Permet d'interpoler le prix mid à un instant donné si pas de trade exact.
- **Stockage local** : Parquet par tranche (`data_cache/{agent}/{date}/{shard}.parquet`),
  dédup sur `tx_hash` (trades) ou `(token_id, ts)` (books, candles). Fully re-runnable
  sans retaper Falcon. Dossier à gitignore.

### 3.3 Rate limit et throttling

- Le Semaphore(1) actuel + `max_rpm` config ne suffira pas au volume ici. On passe le
  `FalconClient` avec `max_rpm=60` (≈ 1 req/s), et on parallélise via `asyncio.gather`
  sur un sémaphore dédié de 3–5 (pas plus : 429 vu dès 2 req back-to-back).
- **Checkpointing** : chaque shard réussi est flushé + marqué `done`. Reprise d'un
  run interrompu sans retaper ce qui est déjà caché.

---

## 4. Coût modeling

### 4.1 Spread model (`src/backtest/costs/spread.py`)

**Primaire — orderbook snapshot le plus proche** :
```
spread_bps = (best_ask - best_bid) / mid × 10_000
mid = (best_ask + best_bid) / 2
```
On prend le snapshot `572` dont `|ts_snapshot - ts_trade|` est minimal sur les 5 min
autour de l'entrée/sortie leader.

**Fallback 1 — candlestick intra-bar** : `spread_est = (high - low) × 0.3`
(heuristique, 30% du range intra-bar approxime le spread moyen, à étalonner en §6.2).

**Fallback 2 — constante par catégorie** : si ni orderbook ni candles dispo, on
applique une constante par catégorie de marché :
```
category       median_spread_bps   (à ajuster après §6.2)
crypto         40
politics       60
sports         80
other          100
```

Chaque trade simulé est tagué avec `spread_source ∈ {orderbook, candle, constant}`
pour rapport.

### 4.2 Slippage / price impact (`src/backtest/costs/slippage.py`)

Modèle square-root (conforme à la littérature microstructure + mention CLAUDE.md §7) :

```
impact_bps = k × σ_24h × √(Q / V_24h) × 10_000
```

Où :
- `Q` = taille ordre en USDC
- `V_24h` = volume 24h du token au moment `t` (depuis candles 1h ou market.volume_24h)
- `σ_24h` = vol implicite du prix (std des retours 1h sur la journée)
- `k` = constante calibrée empiriquement. Valeur de départ : `k = 0.5` (valeur typique
  sur marchés binaires faiblement liquides). Étalonnage en §6.2 si on a assez de trades.

Pour une prise de position on paie :
- Entrée : `spread/2 + impact_bps` (cross le book côté ask pour BUY, bid pour SELL)
- Sortie : même chose symétrique

### 4.3 Lag / latence de détection

Le système réel observe les trades leader via polling 60 s. On modélise ça :
- **Lag détection** : `U(30s, 90s)` additionné au timestamp leader réel, avec seed fixe
  pour reproductibilité. Justification : polling 30 s + traitement, dans `[30, 90]`.
- **Prix d'entrée pour le copier** : prix leader + drift entre `t_leader` et `t_leader + lag`.
  Drift estimé depuis candles 1m si dispo, sinon interpolation linéaire sur candles 1h,
  sinon `drift = 0` (optimiste, à signaler dans rapport).

### 4.4 Fees

Le modèle historique ci-dessous est obsolète et ne doit plus être utilisé :

```
fees_usdc = size_usdc × fee_rate_pct × 2
```

Il est remplacé par le module **canonical V1 economics**. Chaque fill de backtest doit
calculer les taker fees avec la formule :

```
fee_usdc = shares * fee_rate * price * (1 - price)
```

Les backtests ne doivent pas recalculer le PnL localement. Ils doivent appeler
`src.economics.pnl` et `src.economics.fees`, avec `economic_model_version` et
`strategy_track=leader_swing` sur chaque résultat.

Phase A ne gate que `leader_swing`. Elle ne doit pas être utilisée pour valider
`micro_reactive`, qui requiert un gate séparé de capture live orderbook avant toute
affirmation de backtest sérieux.

Le rapport Phase A doit contenir : baseline comparison, cost sensitivity, trade
concentration, et preuve que les anciens labels/PnL invalidés sont exclus.

### 4.5 Merge exits & partial fills

- **Merge exits** : on détecte via le heuristique existant (`position_tracker`), avec
  fenêtre stricte 60 s + `size_yes ≈ size_no` (±1%) au lieu du 10 min / approx actuel.
  Pour le backtester seulement — en prod c'est item [H7].
- **Partial fills** : pour la V0 du backtester on assume fill complet au prix inclus
  de l'impact. Tag `partial_fill_modeled=false` dans rapport.

---

## 5. Plan d'exécution (ordre, checkpoints)

On exécute dans cet ordre, chaque étape produit un artefact vérifiable avant de passer
à la suivante :

1. **Étape 1 — Data loader** (~1–2 j)
   - `src/backtest/data_loader.py` (fetch + cache Parquet)
   - `src/registry/falcon_client.py` : ajout `max_rpm` configurable + fix SSL certifi + timeouts aiohttp (fix du [M4] au passage)
   - Smoke test : fetcher 7 jours de trades pour 5 wallets, vérifier Parquet écrit, dédup OK.
   - **Checkpoint 1** : `data_cache/` peuplé, `scripts/backtest_smoke.py` passe.

2. **Étape 2 — Cost models** (~1 j)
   - `src/backtest/costs/{spread,slippage,lag}.py`
   - Tests unitaires avec fixtures orderbook connues.
   - **Checkpoint 2** : pour un trade historique donné, on produit un coût total reproductible.

3. **Étape 3 — Replay engine** (~2–3 j)
   - `src/backtest/engine.py` : event loop + réutilisation directe de
     `confidence_engine.ConfidenceEngine.evaluate()` et `error_model.ErrorModel.predict()`.
   - Adapter nécessaire : `BacktestProfileCache` qui sert les profils **au temps t** (pas
     le profil final). Ça implique de reconstruire les profils en streaming à chaque trade,
     ou de snapshotter les profils tous les 6h.
   - **Checkpoint 3** : rejouer 7 jours sur 5 wallets → trace complète, décisions cohérentes
     avec ce que le live produirait (spot-check manuel de 20 décisions).

4. **Étape 4 — Report** (~1 j)
   - `src/backtest/report.py` : PnL cumulé, Sharpe, AUC FADE, Brier, calibration, breakdown par
     catégorie / par leader / par source de spread.
   - Format : `out/backtest_report_YYYYMMDD.{json,md}`.
   - **Checkpoint 4** : rapport lisible produit sur run pilote 7 j.

5. **Étape 5 — Walk-forward validation error_model [H1]** (~1 j)
   - `src/backtest/validate_error_model.py` : re-fit phase 2 sur rolling 90 j, prédire sur
     les 7 j suivants, computer AUC/Brier. Baseline = predict `P(loss) = base_rate_global`.
   - **Checkpoint 5** : courbe AUC par leader et agrégée, calibration curves.

6. **Étape 6 — Run 90 j + rapport Gate 1** (~4–6 h compute)
   - Fenêtre : [end-90d, end-30d] = warmup, [end-30d, end] = OOS.
   - Output : `out/gate1_report.md` synthétisant Sharpe net OOS et FADE AUC OOS, plus
     ablations sur `FOLLOW_MIN_TRADES ∈ {20, 50, 100}` et `FADE_MIN_CONFIDENCE ∈ {0.65, 0.75, 0.85}`.
   - **Décision Gate 1** : PASS / FAIL + justification.

---

## 6. Risques identifiés et mitigations

### 6.1 Risque — profils rétro-reconstruits biaisés

Reconstruire le profil d'un leader "au temps t" requiert tous ses trades passés. Si un
leader est nouveau dans notre watchlist (apparu à `t_add > start`), on n'a pas d'historique
avant `t_add`. On ne peut pas trader ce leader pendant la période [t_add, t_add + warmup].
Mitigation : le backtester ignore les leaders sans 20 trades observés (`MIN_TRADES_FOR_PROFILE`).
Cela réduit l'univers effectif — à reporter dans Gate 1.

### 6.2 Risque — cost model mal calibré

`k=0.5` pour slippage est un prior, pas une donnée. Si on n'a pas de live fills pour
calibrer, on peut seulement encadrer. Mitigation :
- Borne basse : `k=0.3` (modèle optimiste) → Sharpe "optimiste"
- Borne médiane : `k=0.5` → Sharpe "central" (celui qui compte pour Gate 1)
- Borne haute : `k=1.0` → Sharpe "pessimiste"

Gate 1 passe seulement si **le central > 0.5**. Rapport de sensibilité obligatoire.

### 6.3 Risque — Falcon rate limits trop tight pour 90 j × 50 wallets

Estimation : 50 wallets × 12 fenêtres de 7j = 600 req trades seuls, + 574 par market
(100–500 markets), + 572 orderbook par market par jour (~10k req). À 1 req/s, soit ~3h
de fetch minimum, avec 429 fréquents. Mitigation : cache Parquet irréversible, scheduling
nuit, `--resume` flag.

### 6.4 Risque — biais de survivorship sur la watchlist actuelle

Les 200 leaders actuels sont ceux qui *aujourd'hui* sont dans le leaderboard Falcon. Rejouer
90 jours avec cette liste surestime la performance (on ne voit pas ceux qui ont craché et
sont tombés du leaderboard). Mitigation V0 : on note le biais dans le rapport Gate 1.
Correction propre (V1) : re-fetcher le leaderboard à `t_start` et l'utiliser comme watchlist
initiale, puis la faire évoluer mensuellement — mais ça complique l'architecture et l'agent 584
n'est pas forcément historisé. À réévaluer si Gate 1 PASS marginal.

### 6.5 Risque — over-engineering du backtester

Le backtester est un outil de décision Gate 1, pas un produit. Il doit être correct,
transparent, rejouable — pas performant ni beau. Pas de Kafka, pas de distributed compute,
pas d'UI. Un script + Parquet + Markdown suffisent.

---

## 7. Fichiers à créer

```
src/backtest/
  __init__.py
  data_loader.py           # Falcon fetch + Parquet cache
  engine.py                # Event loop + profile reconstruction
  profile_cache.py         # Profils au temps t (snapshotting)
  costs/
    __init__.py
    spread.py
    slippage.py
    lag.py
    fees.py                # réutilise logique paper_trader
  report.py                # Metrics + Markdown render
  validate_error_model.py  # [H1]
  models.py                # BacktestTrade, BacktestResult dataclasses

scripts/
  backtest.py              # CLI entrypoint
  backtest_smoke.py        # 7-day smoke test

tests/unit/backtest/
  test_costs.py
  test_engine_replay.py    # fixture : 3 leaders, 100 trades, decisions connues

docs/
  PHASE_A_BACKTESTER_DESIGN.md  # ce doc
```

Aucun fichier existant ne doit être modifié en profondeur. Les seuls ajouts hors `src/backtest/` :
- `src/registry/falcon_client.py` : 3 lignes pour injection d'un SSL context configurable
  + timeout (fix [M4]).
- `src/config.py` : constantes `BACKTEST_*` (volume min, k_slippage, lag range, etc.).

---

## 8. Décision d'arbitrage

Une question à trancher avant que j'attaque le code : **périmètre minimal** pour la V0.

Option A (ambitieuse, ~10 j) : tout ce qui est décrit ci-dessus, incluant la reconstruction
fine des profils au temps t et l'orderbook snapshot per-trade.

Option B (minimale, ~4–5 j) : V0 avec profils snapshot à granularité 24h (pas per-trade),
spread = constante par catégorie (skip agent 572), slippage constant `k=0.5`. Permet un
verdict Gate 1 grossier mais **rapide**, avec bornes plus larges.

Option C (Option B + raffinement ciblé, ~7 j) : V0 minimale **si** FADE AUC < 0.60 (thèse
morte, on ne creuse pas), **sinon** on passe à Option A sur les briques qui ont bougé l'AUC.

Je recommande **Option C** : on ne sur-investit pas si la thèse est morte. Dis-moi si tu
valides, je commence par l'étape 1 (data loader) qui est commune aux trois options.
