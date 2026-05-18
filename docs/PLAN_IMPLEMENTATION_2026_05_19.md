# PLAN D'IMPLÉMENTATION — Polymarket Leader Bot
**Date** : 2026-05-19
**Auteur** : Claude (synthèse de 5 agents d'investigation + diagnostic live prod)
**Objectif** : déverrouiller la prise de positions paper, livrer les capabilités manquantes pour atteindre 70%+ win rate, et reposer le pipeline sur des fondations alignées avec la vision "leader intelligence engine".

---

## 0. DIAGNOSTIC VÉRIFIÉ EN PRODUCTION (live le 18/05 23:06 UTC)

### 0.1 Faits chiffrés (24h glissantes prod)
| Métrique | Valeur | Verdict |
|---|---|---|
| `observed_trades_24h` | 2 483 | Data flow OK |
| `leaders_active` | 2 546 | Mapping OK |
| Décisions émises | 8 270 | Engine évalue |
| Skip | 8 112 (98.1%) | Trop |
| FOLLOW émises | 113 | Engine peut décider |
| FADE émises | 45 | Engine peut décider |
| **Trades paper insérés** | **0** | **BLOCAGE** |
| `open_positions` | 0 | |
| `total_pnl` (historique) | -2 920 $ | Trades passés tous perdants |
| `cycle_latency_ms` | 88 145 | Cycle de 88 s — anormal |

### 0.2 SMOKING GUN
`/api/v1/live-summary` retourne :
```json
"meta": {
  "readiness_blockers": ["missing_fee_snapshot", "missing_token_map", "stale_book"]
}
```
→ Le `signal_audit` gate (`src/economics/gates.py:92-158`) rejette **100% des décisions FOLLOW/FADE** émises par le confidence_engine, parce que la table `fee_snapshots` est **dormante** en prod (le job `bootstrap_fee_snapshots` du maintenance_loop ne tourne pas ou son filtre `volume_24h > 500 AND active=TRUE` exclut les marchés leader). C'est **LA** cause directe du 0-trade.

### 0.3 Distribution des `skip` (échantillon N=50 récent)
| Reason | % |
|---|---|
| `live_match_blocked\|signal=gamma_flag` | 24% |
| `low_market_liquidity` (vol24h<80$) | 24% |
| `stale_trade>600s` | 24% |
| `high_price_follow_blocked` (p>0.92) | 12% |
| `insufficient_data` | 6% |
| `leader_excluded` | 6% |
| `fade_confidence_too_low` | 2% |
| `cold_start_zero_resolved` | 2% |

### 0.4 Lab gates ON en prod (violation memory)
```
strategy_conditional_confidence_enabled: true   (R8)
volume_anticipation_enabled: true               (R9)
causal_gating_enabled: true                     (R10)
prefill_live_enabled: true                      (R7)
```
Le memory user dit explicitement *"V2 = lab gated OFF, ne pas migrer"*. Ces 4 gates downgradent silencieusement les décisions FOLLOW.

### 0.5 5 Piliers paper trading (état réel)
| Pilier | Statut | Détail |
|---|---|---|
| PriceOracle | ❌ ok=false | "no quotes in 24h" — dead-path OU health-check buggy |
| Reconciliation | ✅ ok=true | mais 0 closes 24h |
| Backfill | ❌ ok=false | 3 116 résolus / 7 704 pending |
| Spread gates | ✅ ok=true | mais 0 activity |
| Audit log | ✅ ok=true | mais 0 rows |
| **Overall** | **❌ false** | Wirage OK mais 4 gaps réalisme |

### 0.6 Capabilités manquantes critiques par rapport à la vision user
| # | Capabilité | Statut | Localisation |
|---|---|---|---|
| 1 | `follower_impact` (volume induit / price impact / followers activés) | ❌ Jamais calculé | `behavior_profiler.py:100, 1153` (init à 0, jamais update) |
| 2 | Microstructure/social/cross-market dans le moteur | ❌ Modules existent mais pas importés | `grep "from src.social\|microstructure\|cross_market" src/engine/` → 0 résultat |
| 3 | Exit momentum (trailing stop adaptatif) | ❌ 100% stops/take-profit statiques | `paper_trader.py:715-739` |
| 4 | `leader.horizon` consulté à l'exit | ❌ Calculé mais ignoré | `paper_trader.py:518-687` n'utilise jamais classification_json |
| 5 | Strategy classifier feed engine | ⚠️ Gaté OFF par défaut | `runtime_config.py:47` (mais ON en prod actuellement — bug) |
| 6 | Coverage leaders | ⚠️ 800/2369 = 34% | `observer/main.py:33` |
| 7 | Onchain feed connecté au moteur | ❌ Publie sur stream non consommé | `onchain/clob_listener.py:549` |

### 0.7 Bugs réalisme paper trading
| Bug | Impact | Localisation |
|---|---|---|
| `fee_rate_pct` bps↔decimal | Sur markets crypto, fees ×10000 too high | `trade_observer.py:2483` |
| Slippage exit non modélisé | Close au mid au lieu du bid (overstate 15-25%) | `paper_trader.py:1264-1270` |
| Merge YES+NO=$1 non implémenté | Manque option d'exit naturelle | `paper_trader.py` (absent) |
| 3 spread thresholds non coordonnés | 0.20 / 0.30 / 0.50 sans single source | `paper_trader.py`, `price_oracle.py:195`, `gates.py` |

### 0.8 Cross-layer mismatches (engine émet, paper rejette)
| # | Mismatch | Coût |
|---|---|---|
| 1 | `MIN_ENTRY_PRICE` 0.30 (settings) vs 0.40 (paper_trader getattr) | Décisions [0.30, 0.40) rejetées au paper |
| 2 | `live_match_blocked` double check | Charge DB + double-log |
| 3 | `category_not_whitelisted` au paper only | Wastes Kelly + signal_audit |
| 4 | `leader_sell_side` au paper only | Wastes Thompson updates |
| 5 | `signal_audit` rejeté → engine émet quand même | Le smoking gun ci-dessus |

---

## 1. STRATÉGIE GLOBALE

### 1.1 Philosophie alignée sur la vision user
Le user veut : *"un bot qui prend tous les trades qu'il aura identifiés comme potentiellement intéressants"*, *"qui comprend la conjoncture des marchés en les observant"*, *"prendre une position en conséquence avec un exit parfait à la fin du momentum"*.

Trois principes refactor :
1. **Hard gates → sizing penalties** (sauf killswitch, capital, anti-fraude). Un trade "moyennement intéressant" sera *réduit*, pas *rejeté*.
2. **Connaissance leader EXPLOITÉE** (horizon, strategy, follower_impact) à chaque étape : entry sizing, holding period, exit trigger.
3. **Conjoncture market** alimentée par microstructure/social/cross-market déjà codées mais non câblées.

### 1.2 Périmètre du plan
6 phases, ~30 fix individuels, ~6-10 jours de travail (avec swarm). Aligné avec les modules existants (pas de re-design Big Bang). Respect strict de :
- Architecture DDD bounded contexts (`/src/{observer,engine,profiler,graph,...}`)
- Async-everywhere
- Pydantic models pour I/O externe
- Migrations SQL versionnées
- Pas de secret hardcoded
- Tests TDD London (mock-first)
- Files <500 LOC (les violations existantes ne sont pas aggravées)

### 1.3 Ce que ce plan NE fait PAS (hors scope)
- Re-design Big Bang du confidence_engine (1900+ LOC) → fix chirurgical
- Passage live trading (`TRADING_MODE=live`) → reste paper
- Refonte UI/dashboard (sauf surfaces nécessaires)
- Migration BDD vers TimescaleDB ou autre
- Refonte du système de migrations
- Implémentation phase 3 LightGBM (déjà coded, attente data)

---

## 2. PHASES & PRIORITÉS

### Phase P0 — UNBLOCK (Jour 1, urgent, ~4-6h)
**Objectif** : passer de 0 à >10 trades paper / 24h en débloquant les top-3 skips.

| # | Fix | Effort | Impact |
|---|---|---|---|
| P0-1 | **Bootstrap `fee_snapshots`** : élargir filtre `volume_24h > 500` → `volume_24h > 0 OR last_observed_trade < 24h`. Forcer une première passe au boot pour TOUS les markets actifs avec leader trades observés. | 2h | **CRITIQUE — résout le smoking gun** |
| P0-2 | **Désactiver R7/R8/R9/R10 gates en prod** : `causal_gating_enabled=false`, `volume_anticipation_enabled=false`, `strategy_conditional_confidence_enabled=false`, `prefill_live_enabled=false` via Redis. Aligner avec memory. | 15min | Restaure baseline |
| P0-3 | **Relâcher `LIVE_DECISION_MAX_TRADE_AGE_S`** de 600 → 1200s (latence p95 mesurée 40-60s par agent data — 600s marge insuffisante avec 24% skip). | 15min | Réduit `stale_trade` skip |
| P0-4 | **Relâcher `low_market_liquidity` floor** $5 000 → $1 000 (24% des skips sur vol24h<80$ qui sont en fait des leaders dans des markets neufs/thin). | 15min | Réduit `low_market_liquidity` skip |
| P0-5 | **`live_match_block` : sortir du `gamma_flag` seul** : exiger AT LEAST 2 signaux sur (gamma_flag, regex, volume) avant de bloquer. | 30min | Réduit `live_match_blocked` de 24% à ~5% |
| P0-6 | **Aligner `MIN_ENTRY_PRICE`** : supprimer le `getattr(settings,"MIN_ENTRY_PRICE",0.40)` fallback dans paper_trader.py:1043, utiliser uniquement runtime_config (0.30). | 10min | Restaure cohorte [0.30, 0.40) |

**Critère de sortie P0** : `actionable_1h > 5` ET `paper_trades` insertions/24h > 10. Inspection via `/api/inspector/snapshot` à T+1h post-deploy.

### Phase P1 — SIMPLIFY DECISION TREE (Jour 2, ~6-8h)
**Objectif** : réduire 35 gates → 8 hard gates + N sizing penalties.

| # | Fix | Effort |
|---|---|---|
| P1-1 | **Architecture context_penalty** : étendre l'embryon `paper_trader.py:894` en module `src/engine/sizing_penalties.py`. Définir 12 penalty multipliers (mid_liquidity, near_resolution, high_price, mid_spread, low_winrate, late_cycle, etc.). | 3h |
| P1-2 | **Migrer 12 gates "soft" en penalties** : `low_market_liquidity` (sous $1k → ×0.5, sous $500 → ×0.2), `high_price` (0.85-0.92 → ×0.7), `near_resolution` (6-12h → ×0.6), `live_match_partial` (1 signal → ×0.3), `wallet_process_unstable`, etc. | 2h |
| P1-3 | **Cross-layer cleanup** : déplacer `leader_sell_side`, `category_whitelist` pré-filtre au confidence_engine (avant Kelly). Supprimer redondances paper_trader (`live_match_blocked` paper-side garde pour défense en profondeur mais log différent). | 1h |
| P1-4 | **Fix engine émission sur `signal_audit.accepted=False`** : confidence_engine doit short-circuit + log `signal_audit_rejected_at_engine` au lieu d'émettre une décision rejetée downstream. | 30min |
| P1-5 | **Diluer tier_c gate** : si `falcon_external_resolved > 10` ET `winrate > 0.5`, on passe Tier C automatiquement (Bayesian fusion stronger). | 1h |
| P1-6 | **Hard gates conservés (8)** : killswitch · leader_excluded · `signal_audit_critical` (token_map/book vraiment manquants) · `min_position_size` · `insufficient_paper_capital` · `risk_manager_killswitch` · `open_trade_conflict` · `market_resolved`. | inclus |

**Critère de sortie P1** : `actionable_1h > 15`, distribution skip rationnelle (aucun reason > 40%).

### Phase P2 — CONNECTER LA CONJONCTURE MARKET (Jour 3-4, ~8-12h)
**Objectif** : le bot "comprend les marchés en les observant" via microstructure + social + cross-market.

| # | Fix | Effort |
|---|---|---|
| P2-1 | **`src/engine/market_context.py`** nouveau module — agrégateur de features external au confidence_engine. Lit `microstructure.book_features`, `social.pulse_snapshots`, `cross_market.correlations` depuis BDD (read-only, cached 30s). | 3h |
| P2-2 | **Wire `market_context` dans `_build_trade_context`** confidence_engine.py:1156. Ajoute features : `book_imbalance`, `social_pulse_score`, `cross_market_corr_max`, `microprice_drift`. | 2h |
| P2-3 | **Sizing boost / penalty** : si `book_imbalance` aligned avec leader direction → ×1.3 ; si `social_pulse_score` opposé → ×0.7 ; si `cross_market_corr` indique transfert → ×1.2. | 1h |
| P2-4 | **Câbler `chain:trades:stream`** (onchain CLOB) → graph_engine + profiler. Réduit latence p95 40-60s → <5s pour transactions Polygon. Modif : `graph_engine.py` + `behavior_profiler.py` subscribe additionnel. | 3h |
| P2-5 | **Active strategy_conditional weights** comme features (pas comme gate) : le primary_strategy du leader devient un feature dans `market_context`, pas un hard multiplier sur Thompson. | 1h |
| P2-6 | **Bootstrap strategy_classifier** : si LightGBM model absent, fallback à `directional` par défaut (avec confidence=0.5) au lieu de NaN dans features.py. Garantit que les multipliers ne sont jamais skipés. | 1h |

**Critère de sortie P2** : `actionable_1h > 25`, premières positions paper auto-ouvertes avec un sizing modulé par conjoncture.

### Phase P3 — FOLLOWER IMPACT & LEADER HORIZON (Jour 4-5, ~8h)
**Objectif** : exploiter la connaissance leader pour entry sizing et exit timing.

| # | Fix | Effort |
|---|---|---|
| P3-1 | **Implémenter `follower_impact` réel** : sur chaque close_time d'une `positions_reconstructed` leader, batch job qui mesure `avg_volume_induced` (volume followers dans next 5min), `avg_price_move` (mid_after - mid_before / mid_before sur 15min), `followers_activated` (count distinct followers same-direction). Persiste dans `leader_profiles.profile_json.follower_impact`. | 3h |
| P3-2 | **Migration SQL `052_leader_follower_impact.sql`** : 3 nouvelles colonnes typées sur `leader_profiles` (avg_volume_induced NUMERIC, avg_price_move NUMERIC, followers_activated INTEGER) pour query rapide sans parser JSONB. | 30min |
| P3-3 | **Kelly sizing exploite follower_impact** : si `avg_volume_induced > $10k` ET `followers_activated > 5` → boost ×1.5. Sinon baseline. | 1h |
| P3-4 | **Exit adaptatif sur `leader.classification_json.horizon`** : nouveau fichier `src/engine/exit_strategy.py`. holding_cap = scalper:30min / swing:6h / holder:24h. Lit `classification_json.horizon` au open_trade et stamp dans `paper_trades.leader_context.horizon`. | 2h |
| P3-5 | **Trailing stop momentum-aware** : nouveau check dans `_check_open_positions`. Si pnl > +5%, active trailing stop à -2% du peak (pour scalper) / -4% (swing) / -6% (holder). Implémente comme une 6e condition d'exit avant stop_loss statique. | 2h |
| P3-6 | **Partial leader_exit** : au lieu de close full position quand leader exits, si leader trim partiel (>50% reste open), trim 50% du paper et stamp `partial_leader_exit_observed`. | 1h |

**Critère de sortie P3** : trades paper ouverts avec sizing varié (×0.3 à ×1.5 baseline) ET au moins 1 exit déclenché par trailing stop.

### Phase P4 — RÉALISME PAPER TRADING (Jour 5-6, ~6h)
**Objectif** : PnL paper crédible sur markets crypto (fees) et sur exits réalistes.

| # | Fix | Effort |
|---|---|---|
| P4-1 | **Fix `fee_rate_pct` bps↔decimal** : dans `trade_observer.py:2483` convertir `gamma_taker_fee_bps / 10000.0` AVANT le INSERT. Migration data backfill `053_fees_decimal_normalize.sql` pour rows existantes. | 1h |
| P4-2 | **Slippage exit modelé** : `paper_trader.close_trade` calcule `slippage_usdc = shares × (mid - best_bid) × slippage_factor` et passe à `calculate_long_pnl`. Factor = `min(0.5, size / book_depth)`. | 1.5h |
| P4-3 | **Merge YES+NO=$1 exit option** : nouvelle fonction `paper_trader._check_merge_exit` : si user a position YES, et le complement NO est buyable à un prix tel que (entry_yes + ask_no) < $1 - frais, exit via merge théorique. Stamp reason `merge_arb_exit`. | 2h |
| P4-4 | **Single source spread threshold** : nouvelle constante `MAX_PAPER_SPREAD: float = 0.20` dans config.py. Remplace les 3 thresholds (0.20/0.30/0.50). Le `book_wall_max_spread=0.50` reste comme fallback hard (= panic level). | 30min |
| P4-5 | **Fix PriceOracle `no quotes in 24h`** : investiguer pourquoi `oracle.quotes_24h=0` malgré exit closes existants. Probablement le query SQL du pillar check (`pillars_queries.py`) compte le mauvais signal. Soit fix le check, soit fix l'instrumentation. | 1h |

**Critère de sortie P4** : reconciliation report montre delta < $5 sur trades crypto, et oracle pillar ok=true.

### Phase P5 — COVERAGE & STABILITÉ (Jour 6-7, ~6h)
**Objectif** : élever le bot de 34% à 80%+ de coverage leader, stabiliser cycle latency.

| # | Fix | Effort |
|---|---|---|
| P5-1 | **Lift `MAX_OBSERVER_WS_TOKENS` 800 → 2000** + scale `REGISTRY_BACKFILL_CONCURRENCY` 20 → 40. Test load via `scripts/test_connectivity.py`. | 1h |
| P5-2 | **DB pool scaling** : passer `asyncpg` pool size de 20 → 40 et `max_size` → 80 dans `database/connection.py` pour absorber le doublement de polling. | 30min |
| P5-3 | **Diagnostiquer `cycle_latency_ms=88145`** (88s !) : profile `scheduler.py` + `watchdog.py`. Probablement un coroutine bloquant. Output trace via `loguru` + remediation. | 2h |
| P5-4 | **Diagnostiquer `oracle pillar no quotes`** (déjà inclus P4-5). | inclus |
| P5-5 | **Auto-promote logic** : si un wallet hit `tier_c_min_resolved=20 AND winrate>=0.5` mais pas yet `on_watchlist=true`, auto-promote au prochain refresh. Modif `leader_registry.py:auto_promote_to_watchlist`. | 1h |
| P5-6 | **Health-check enrichi** : `scripts/health_check.py` doit logger `meta.readiness_blockers` + `actionable_1h` + `cycle_latency_ms`. | 1h |

**Critère de sortie P5** : coverage leaders > 1500 active, `cycle_latency_ms` < 5000.

### Phase P6 — TESTS & DEPLOY (Jour 7-8, ~6h)
**Objectif** : régression-tests + déploiement VM + monitoring J+1.

| # | Fix | Effort |
|---|---|---|
| P6-1 | **Tests unitaires nouveaux modules** : `tests/test_engine/test_market_context.py`, `test_exit_strategy.py`, `test_sizing_penalties.py`, `test_follower_impact_job.py`. Cible ~80% coverage. | 3h |
| P6-2 | **Test E2E full-stack** : `tests/test_e2e/test_leader_to_paper_close.py`. Mock leader trade → assert paper open → mock leader exit → assert paper close. | 2h |
| P6-3 | **Fix 6 failures `test_confidence_engine.py`** (déjà flag par audit S2 18/05, hors scope mais bloque la CI). | 1h |
| P6-4 | **Lint + ruff** clean run. | 30min |
| P6-5 | **Deploy script** : `scripts/deploy_2026_05_19.sh` qui rsync, run migrations, restart containers, et runs smoke-tests post-deploy. | 1h |
| P6-6 | **Monitoring J+1** : créer `scripts/monitor_post_deploy.py` qui run pendant 24h post-deploy et alerte sur drift skip distribution / 0-trade pattern récurrent. | 1h |

**Critère de sortie P6** : CI green, deploy réussi, >50 trades paper sur premier 24h post-deploy avec distribution skip cohérente.

---

## 3. PLAN D'EXÉCUTION VIA SWARM PARALLÈLE

Conformément au CLAUDE.md projet (`Concurrency: 1 MESSAGE = ALL RELATED OPERATIONS`), l'exécution se fait par vagues parallèles d'agents Task. Topology hierarchical-mesh, max 8 agents en flight.

### Vague 1 — P0 unblockers (parallèle, 1 message, ~4h wall-clock)
- **Agent A1 (`backend-dev`)** : P0-1 bootstrap `fee_snapshots` + maintenance_loop changes. Touche `scripts/maintenance_loop.py:147-180`.
- **Agent A2 (`backend-dev`)** : P0-2 désactiver R7/R8/R9/R10 gates via `runtime_config` updates. Touche `src/control/runtime_config.py` + crée script `scripts/disable_lab_gates_2026_05_19.py`.
- **Agent A3 (`coder`)** : P0-3, P0-4, P0-5, P0-6 — config + paper_trader cleanup. Touche `config.py`, `paper_trader.py`, `live_match_detector.py`.
- **Agent A4 (`tester`)** : tests régression pour les 6 fixes. Touche `tests/test_economics/test_live_match_detector.py`, `tests/test_engine/test_paper_trader.py`.

### Vague 2 — P1 decision tree simplification (parallèle, 1 message, ~8h wall-clock)
- **Agent B1 (`architecture`)** : design `sizing_penalties.py` module + interfaces. Crée `src/engine/sizing_penalties.py`.
- **Agent B2 (`coder`)** : implémentation des 12 penalty multipliers + intégration confidence_engine.
- **Agent B3 (`coder`)** : cross-layer cleanup (P1-3, P1-4) + `signal_audit` short-circuit.
- **Agent B4 (`tester`)** : tests `test_sizing_penalties.py` + tests régression cross-layer.

### Vague 3 — P2 market context wiring (parallèle, 1 message, ~12h wall-clock)
- **Agent C1 (`backend-dev`)** : `market_context.py` module + wirings DB read-only.
- **Agent C2 (`coder`)** : intégration `_build_trade_context` + sizing boost/penalty.
- **Agent C3 (`coder`)** : câblage `chain:trades:stream` → graph + profiler.
- **Agent C4 (`ml-developer`)** : bootstrap strategy_classifier fallback `directional`.
- **Agent C5 (`tester`)** : tests `test_market_context.py` + tests intégration.

### Vague 4 — P3 follower_impact + exit strategy (parallèle, 1 message, ~8h wall-clock)
- **Agent D1 (`backend-dev`)** : job `follower_impact` (cron) + migration SQL 052.
- **Agent D2 (`coder`)** : `exit_strategy.py` + trailing stop + partial leader_exit.
- **Agent D3 (`coder`)** : Kelly sizing exploite follower_impact.
- **Agent D4 (`tester`)** : tests `test_follower_impact_job.py` + `test_exit_strategy.py`.

### Vague 5 — P4 réalisme paper (parallèle, 1 message, ~6h wall-clock)
- **Agent E1 (`coder`)** : fix `fee_rate_pct` bps↔decimal + migration 053.
- **Agent E2 (`coder`)** : slippage exit modeling + merge YES+NO exit option.
- **Agent E3 (`coder`)** : single-source spread threshold consolidation.
- **Agent E4 (`coder`)** : fix PriceOracle pillar health check.

### Vague 6 — P5 coverage + stability (parallèle, 1 message, ~6h wall-clock)
- **Agent F1 (`performance-engineer`)** : diagnose `cycle_latency_ms=88145` bottleneck + remediation.
- **Agent F2 (`backend-dev`)** : DB pool scaling + WS tokens 800→2000.
- **Agent F3 (`coder`)** : leader_registry auto-promote enrichment.
- **Agent F4 (`coder`)** : health_check.py enrichment.

### Vague 7 — P6 tests + deploy prep (parallèle, 1 message, ~6h wall-clock)
- **Agent G1 (`tester`)** : tests E2E `test_leader_to_paper_close.py`.
- **Agent G2 (`tester`)** : fix 6 failures `test_confidence_engine.py`.
- **Agent G3 (`reviewer`)** : code review final + lint.
- **Agent G4 (`coder`)** : deploy script + monitor_post_deploy.

### Vague 8 — Deploy + monitoring (sequential, hors swarm, ~2h)
- Backup BDD prod (pg_dump → snapshot local).
- SSH VM → git fetch → run migrations 052, 053 → docker compose restart engine + observer + maintenance.
- Smoke tests post-deploy (10 minutes).
- Surveillance 4h via `/api/inspector/snapshot` + alerte si actionable_1h < 5.

---

## 4. TESTS & VALIDATION

### 4.1 Critères de succès par phase (récap)
| Phase | Critère |
|---|---|
| P0 | actionable_1h > 5 ET paper_trades/24h > 10 |
| P1 | aucun skip reason > 40% |
| P2 | sizing variance observée (min/max ratio > 3x) |
| P3 | ≥ 1 exit déclenché par trailing stop |
| P4 | reconciliation delta < $5 sur crypto trades, oracle pillar ok=true |
| P5 | coverage > 1500 active leaders, cycle_latency < 5000ms |
| P6 | CI green, J+1 monitoring stable |

### 4.2 Tests unitaires à ajouter (count: 8 nouveaux fichiers, ~120 nouveaux tests)
```
tests/test_engine/
├── test_market_context.py       (~15 tests)
├── test_exit_strategy.py        (~25 tests)
├── test_sizing_penalties.py     (~20 tests)
├── test_follower_impact_job.py  (~15 tests)
└── test_engine_cross_layer.py   (~10 tests cross-layer alignment)

tests/test_economics/
├── test_fee_rate_normalize.py   (~10 tests bps↔decimal)
└── test_slippage_exit.py        (~10 tests)

tests/test_e2e/
└── test_leader_to_paper_close.py (~15 tests)
```

### 4.3 Tests régression existants à valider
- `tests/test_engine/test_paper_trader.py` (toutes failures clear)
- `tests/test_engine/test_confidence_engine.py` (6 failures à fix → 0)
- `tests/test_control/test_price_oracle.py`
- `tests/test_scripts/test_reconciliation.py`
- `tests/test_scripts/test_backfill_resolved_outcomes_robust.py`

### 4.4 Validation BDD post-deploy (queries)
```sql
-- 1. fee_snapshots populée
SELECT COUNT(*), MAX(captured_at) FROM fee_snapshots;
-- Attendu : > 500 rows, captured_at < 1h ago

-- 2. paper_trades flux
SELECT DATE_TRUNC('hour', opened_at) AS h, COUNT(*) FROM paper_trades
WHERE opened_at > NOW() - INTERVAL '24h' GROUP BY 1 ORDER BY 1;
-- Attendu : monotone non-décroissant après deploy

-- 3. distribution actions decision_log
SELECT action, COUNT(*), MIN(time), MAX(time) FROM decision_log
WHERE time > NOW() - INTERVAL '4h' GROUP BY 1;
-- Attendu : follow/fade > 5% chacun

-- 4. follower_impact populated
SELECT COUNT(*) FROM leader_profiles
WHERE (profile_json->'follower_impact'->>'avg_volume_induced')::numeric > 0;
-- Attendu : > 50 rows

-- 5. exit_strategy stamping
SELECT close_reason, COUNT(*) FROM paper_trades
WHERE closed_at > NOW() - INTERVAL '24h' GROUP BY 1;
-- Attendu : variety incl. trailing_stop, leader_exit, take_profit
```

---

## 5. PLAN DE DÉPLOIEMENT VM

### 5.1 Pré-deploy checklist
- [ ] CI green sur `main` (incluant fixes test_confidence_engine.py)
- [ ] Code review complet (Agent G3)
- [ ] Backup BDD prod : `pg_dump polymarket > snapshots/pre_deploy_2026_05_19.sql`
- [ ] Backup `runtime:config` Redis : `redis-cli HGETALL runtime:config > snapshots/pre_deploy_runtime_2026_05_19.txt`
- [ ] Snapshot état actuel `/api/inspector/snapshot` → `snapshots/pre_deploy_inspector_2026_05_19.json`

### 5.2 Procédure deploy (canonique, alignée avec `docs/DEPLOY.md`)
```bash
# 1. SSH VM
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
cd /opt/polymarket-bot

# 2. Sauver état (idempotent côté serveur)
docker exec polymarket_db pg_dump -U polymarket -d polymarket > /var/backups/polymarket-bot/pre_2026_05_19.sql
docker exec polymarket_redis redis-cli SAVE
docker exec polymarket_redis cp /data/dump.rdb /data/pre_2026_05_19_dump.rdb

# 3. Rsync code (depuis local)
# Local-side :
rsync -avz --delete --exclude='.venv' --exclude='node_modules' \
  --exclude='.git' --exclude='data_cache' \
  /Users/oscargrima/Documents/Claude/Projects/Polymarket\ trading\ bot/polymarket-bot/ \
  polymarket@89.167.23.215:/opt/polymarket-bot/

# 4. Run migrations 052, 053
ssh hetzner-polymarket
cd /opt/polymarket-bot
for m in docs/migrations/052_*.sql docs/migrations/053_*.sql; do
  docker exec -i polymarket_db psql -U polymarket -d polymarket < $m
done

# 5. Rebuild images si Dockerfile/requirements changent
docker compose -f docker-compose.yml -f docker-compose.prod.yml build engine observer maintenance

# 6. Restart en cascade pour minimiser downtime
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps engine
sleep 30 && curl http://localhost:8080/healthz  # vérif
docker compose ... up -d --no-deps observer
sleep 30 && curl http://localhost:8080/healthz
docker compose ... up -d --no-deps maintenance

# 7. Smoke tests
curl http://localhost:8080/api/control/state  # killswitch ON
curl http://localhost:8080/api/risk/config | jq .config.causal_gating_enabled  # false
curl http://localhost:8080/api/health/pillars | jq .overall_ok  # true
curl http://localhost:8080/api/v1/live-summary | jq .meta.readiness_blockers  # []

# 8. Monitor 4h
python3 scripts/monitor_post_deploy.py --duration 4h --alert-on actionable_1h_below=3
```

### 5.3 Rollback strategy
Si à T+30min `actionable_1h == 0` OU au moins une smoke test fail :
```bash
# Rollback code
cd /opt/polymarket-bot
git reset --hard <pre_deploy_sha>  # ou rsync inversé depuis snapshot

# Rollback DB si migrations problématiques
docker exec -i polymarket_db psql -U polymarket -d polymarket < /var/backups/polymarket-bot/pre_2026_05_19.sql

# Rollback runtime_config
docker exec -i polymarket_redis redis-cli HMSET runtime:config $(cat /tmp/pre_deploy_runtime_2026_05_19.txt)

# Restart all
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart
```

### 5.4 Monitoring J+1 → J+7
Métriques à tracker :
- `actionable_1h` (target > 5, alarme < 2)
- `paper_trades_24h` (target > 30)
- `cycle_latency_ms` (target < 5000)
- Distribution skip reasons (aucun reason > 40%)
- `oracle pillar ok=true`
- `reconciliation delta` (target < $5)

Alertes via Telegram (déjà configurées sur `notifier`).

---

## 6. RISQUES & MITIGATIONS

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| `fee_snapshots` boot crash si trop de markets | M | H | Batch par 50 markets, retry par lot, ne pas tout charger à la fois |
| Cross-layer cleanup casse paper_trader paths existants | M | H | Tests régression + canary deploy (1 conteneur d'abord) |
| Trailing stop trop agressif → close pré-momentum | M | M | Calibration sur backtest 30j avant deploy |
| `MAX_OBSERVER_WS_TOKENS=2000` sature DB pool | M | H | Scale DB pool en même temps (P5-2), monitoring connect count |
| `causal_gating_enabled=false` augmente faux positifs | L | M | Suivre win rate post-deploy, ré-enable si dégradation |
| `slippage exit` réduit PnL apparent | H | L | C'est l'objectif (réalisme) — communiquer comme amélioration |
| Migrations 052/053 lock tables longtemps | L | H | Wrap dans `CREATE INDEX CONCURRENTLY`, test sur DB shadow |
| Deploy script échoue mid-restart | L | H | Healthcheck entre chaque container restart |

---

## 7. MÉTRIQUES DE SUCCÈS POST-DEPLOY (J+7)

| KPI | Baseline (18/05) | Target J+7 | Stretch |
|---|---|---|---|
| `paper_trades` / 24h | 0 | > 30 | > 100 |
| `actionable_1h` | 2 | > 10 | > 25 |
| `win_rate` (sur trades fermés) | n/a | > 40% | > 55% |
| `coverage leaders active` | 800 / 2369 | > 1500 / 2500 | > 2000 / 2500 |
| `cycle_latency_ms` | 88 145 | < 5 000 | < 1 000 |
| `oracle pillar ok` | false | true | true |
| `5 pillars overall_ok` | false | true | true |
| Skip dominé par 1 reason | oui (3 à 24%) | non (max 25%) | non (max 15%) |
| Réconciliation delta paper | inconnu | < $5 / trade | < $1 / trade |
| FOLLOW : FADE ratio | 71:29 | 60:40 | 50:50 |

À J+30, attente d'atteindre **70% win rate** sur cohorte > 200 trades fermés (objectif user).

---

## 8. POST-PLAN (HORS SCOPE MAIS À TRACKER)

- ADRs (Architecture Decision Records) pour 5 décisions majeures de ce plan
- Refacto `confidence_engine.py` (1900 LOC) en sous-modules
- Live trading enablement (TRADING_MODE=live) après 30j paper validé
- Phase 3 LightGBM error model (quand 500+ resolved/leader)
- Calibration loop Round 13 (`decision_replay`, `loss_aggregator`, `auto_disable`)
- Tracing distribué (Tempo/Jaeger)
- HTTPS/auth dashboard
- UptimeRobot config

---

## ANNEXE A — Ordre canonique des gates (pour référence pendant l'exécution)

### Engine `evaluate()` ordre actuel (35 gates, à devenir 8 hard + N penalties)
1. is_leader
2. excluded
3. stale_trade
4. high_price_follow_blocked
5. low_market_liquidity
6. cold_start_zero_resolved
7. insufficient_data
8. live_match_blocked
9. leader_resolved_too_low / leader_winrate_too_low
10. wallet_process_too_unstable
11. fade_confidence_too_low / fade_edge_too_low
12. follow_error_risk_too_high
13. below_min_signal_strength
14. signal_audit (missing_token_map / missing_fee_snapshot / stale_fee / missing_book / stale_book)

### Paper `open_trade()` (15 gates supplémentaires)
15. book_wall_spread
16. leader_sell_side
17. stale_decision
18. signal_audit accepted check
19. below_min_position_size
20. insufficient_paper_capital
21. live_match_blocked (redondant)
22. category_not_whitelisted
23. open_trade_conflict
24. recent_reentry_conflict
25. market_resolved
26. missing_end_date / near_resolution
27. risk_manager_rejected (killswitch / drawdown / consec_losses / market_exposure)
28. risk_manager_zero_size
29. high_entry_ask_blocked
30. low_entry_ask_blocked
31. leader_price_drift

### Cible post-P1 (8 hard gates)
1. killswitch
2. leader_excluded
3. signal_audit_critical (token_map + fresh book)
4. min_position_size
5. insufficient_paper_capital
6. open_trade_conflict
7. market_resolved
8. risk_manager_emergency (drawdown OR critical exposure)

Le reste devient des **multiplicateurs de sizing** (`context_penalty`, range 0.0 à 1.5), avec floor = `MIN_POSITION_USDC=50`. Un trade très peu intéressant aura sizing × 0.2, mais sera quand même pris si > $50 → c'est la philosophie "take all interesting trades".
