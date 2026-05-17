# Audit Paper Trading — 2026-05-17

Cette session a été déclenchée par le constat opérateur : « 2 trades énormes
gagnés, puis ~15 trades qui ont perdu la quasi-totalité du pari ». L'audit
confirme que **le système n'était pas du tout en train de gagner** —
les deux « victoires » sont en réalité des artefacts (test seedé +
prix d'exit lus dans un cache stale), et les pertes massives viennent
de plusieurs trous documentés ci-dessous.

**Session 1** : 11 bugs identifiés, **10 corrigés** (B1-B11), protégés
par 11 tests de régression.

**Session 2** (suite, sur instruction « fais l'ensemble des améliorations
nécessaires ») : 7 améliorations complémentaires couvrant les
recommandations Tier 2 des rapports d'agents — backfill résolution,
monitor adaptatif, preclose pré-résolution, sanity-ratio défensif, TTL
serré, `/pnl` mark-to-market réel, nouvelle commande `/summary`.
8 nouveaux tests de régression. Suite complète : **35/35 ✅** sur
paper_trader + **80/80 ✅** sur telegram_bot.

---

## 1. Diagnostic — comprendre les chiffres affichés

### Les 2 « victoires » ne sont pas de vraies victoires

D'après les rapports d'audit du 15 mai (`SESSION_FINAL.md` + `REPORT_04`
+ `REPORT_05`), les trade #1 et #2 :

| # | Marché | Size | Entry | Exit | PnL annoncé |
|---|---|---|---|---|---|
| 1 | « BTC $150k » YES | $127.90 | 0.008 | 0.28 | +$4 184 (take_profit) |
| 2 | « BTC $150k » YES | ~$131 | 0.002 | 0.59 | +$38 520 (take_profit) |

Le rapport `REPORT_04_T+5h.md` précise explicitement que **l'exit price
$0.59 venait d'un cache que j'avais SET manuellement plus tôt en
diagnostic** — pas d'un vrai book CLOB. Le code lisait `book:last:*`
SANS vérifier l'âge ; un payload de 12 minutes était traité comme du
temps réel.

Mathématiquement : $127.90 size / $0.008 entry = 15 988 shares. À la
résolution YES réelle ($1.00), ça donnerait $15 988. Le PnL affiché
+$4 184 correspond à un exit à $0.28 — ce qui n'est NI un prix marché
exécutable au moment du close, NI le terminal value de résolution.
**C'est un PnL fantôme**.

### Les ~15 pertes sont réelles

Le rapport `REPORT_06_T+6h.md` détaille 12 trades organiques :

- **Tous** entrés à entry_ask ≈ 0.99 sur des marchés sports
  near-resolution (CS:GO, Dota 2, Eurovision, etc.)
- **Tous** clôturés vers 0.01 quand le favori a perdu
- Profil de perte typique : $200 size × ((0.01 − 0.99) / 0.99) ≈ −$198
- Soit ~99% du pari perdu → l'opérateur n'a pas tort de dire « j'ai
  perdu les $200 »

Les pertes sont mathématiquement correctes. Le bug n'est pas dans le
calcul mais dans le fait que **le bot ouvrait ces trades en premier lieu**.

---

## 2. Les 11 bugs identifiés

### Tier 1 — bugs critiques causant les pertes / faux gains

| ID | Sévérité | Fichier:ligne | Description |
|---|---|---|---|
| **B1** | CRITICAL | `paper_trader.py:360-364` | **Inversion PnL FADE**. `direction="no"` traitait les FADE comme des shorts (`(entry-exit)/entry`) alors que ce sont des LONG du token opposé. Conséquence : FADE gagnants déclenchés en stop_loss, FADE perdants en take_profit. |
| **B2** | CRITICAL | `paper_trader.py:807-830` | **`_get_book_quote` ignore `observed_ts` / `captured_at`**. Un payload de 10+ minutes était lu comme du temps réel → source des +$42k de PnL fantôme. |
| **B3** | CRITICAL | `paper_trader.py:340-352` | **Close `market_resolved` utilise le bid stale au lieu du terminal value**. Quand un marché résout, le code doit utiliser la valeur réelle (1.0 winner / 0.0 loser), pas le dernier bid pré-résolution. |
| **B4** | CRITICAL | `paper_trader.py:449` | **Aucun filtre time-to-resolution**. Le bot ouvrait des trades sur des marchés à <1h de résolution → catastrophe sur les 0.99 sports. |
| **B5** | HIGH | `paper_trader.py:498` | **`high_entry_ask_blocked` FOLLOW-seul**. FADE pouvait acheter à 0.85+ sur le token opposé avec le même profil asymétrique de perte. |
| **B6** | HIGH | nouveau | **Pas de filtre drift leader-vs-entry**. Si le leader a tradé à 0.50 et l'ask du bot est 0.025 (cache stale), le bot prend une position différente que celle signalée. Source directe des « 2 gros gains » fantômes. |
| **B7** | MEDIUM | `paper_trader.py:886-887` | `_exit_bid` floor à 0.01 sous-déclarait les pertes sur les tokens résolus à zéro. |
| **B8** | HIGH | `paper_trader.py:234` | **`fee_rate_pct` perdu après warm restart**. Le commentaire promettait un lazy reload jamais implémenté. Les trades survivant à un redémarrage payaient $0 de fees → PnL surestimé. |
| **B9** | MEDIUM | `paper_trader.py:355` | **FADE n'avait pas de close `leader_exit`** : gaté par `if strategy=="follow"`. Une position FADE restait ouverte même quand le leader avait clôturé sa position. |
| **B10** | HIGH | `paper_trader.py:267-279` | Même inversion FADE que B1 dans `_compute_unrealized_pnl` → equity curve faussée. |
| **B11** | UX | `formatters.py:84-97` | Message Telegram CLOSE sans strategy / size / pnl_pct. L'opérateur voyait `pnl: -198$` sans savoir si c'était −99% de $200 ou −12% de $1650. |

### Bugs secondaires repérés mais non corrigés cette session

- `fee_rate_pct` est stocké en bps mais utilisé comme décimale dans
  `calculate_polymarket_fee` (rapport PnL agent BUG 3). Impact
  pratique faible parce que la plupart des marchés Polymarket ont
  `fee_rate_pct = 0` (politique/géopolitique = zéro fee).
- Le `book:last` TTL de 600s côté maintenance loop reste 5× au-dessus
  de l'intervalle de refresh (120s). La staleness gate ajoutée
  (B2) protège déjà côté lecteur — le tightening TTL serait
  défensive en profondeur.
- `_is_market_resolved` anti-poison (lignes 1006-1013) peut suppress
  une résolution réelle si des trades stragglers existent. Le helper
  `_fetch_market_resolution` (B3) court-circuite ce risque pour la
  branche close.

---

## 3. Ce qui a changé dans le code

### `src/engine/paper_trader.py`

1. `_compute_unrealized_pnl` (l.267-285) : formule unifiée long PnL,
   plus de branchement `direction=="no"`.
2. `_check_open_positions` (l.329-388) :
   - Même formule unifiée pour pnl_pct.
   - `leader_exit` étendu à FADE (avant: FOLLOW-only).
   - Branche `market_resolved` interroge d'abord
     `_fetch_market_resolution` ; si l'outcome est inconnu, le close
     est **différé** (warning loggé) au lieu d'enregistrer un fake
     PnL au stale bid.
3. `open_trade` :
   - Nouveau bloc `MIN_HOURS_TO_RESOLUTION_*` (l.469+) qui rejette les
     trades sur marchés à moins de 6h (FOLLOW) ou 24h (FADE) de la
     résolution. Rejection reason `near_resolution` ou
     `missing_end_date`.
   - `high_entry_ask_blocked` ne filtre plus uniquement les FOLLOW
     (l.510+) — symétrique pour FADE.
   - Nouveau filtre `leader_price_drift` (l.527+) qui rejette les
     trades dont l'entry_ask diverge de >20% du prix leader.
4. `close_trade` :
   - Lazy refresh du `fee_rate_pct` (l.645+) si la valeur in-memory
     est 0 (cas warm restart).
   - L'event Redis publié inclut maintenant `entry_price` et
     `exit_price` (l.789-791) ; le bare `except Exception: pass` est
     remplacé par un warning loggé.
5. `_get_book_quote` (l.847+) : staleness gate basée sur
   `MAX_BOOK_AGE_PAPER_S` (60s par défaut). Payload sans timestamp
   parsable → rejeté.
6. `_exit_bid` (l.948+) : floor à 0.0 au lieu de 0.01, pour permettre
   l'enregistrement fidèle des pertes à terminal value 0.
7. Nouveaux helpers `_hours_until_resolution` et
   `_fetch_market_resolution` (l.1037+).

### `src/telegram_bot/formatters.py`

`format_position_closed` étendu (l.84-117) : strategy, direction,
size, entry → exit prices, et pnl_pct affichés systématiquement.

Format avant :
```
📄📉 PAPER CLOSE — #15
market: 0x7f3e4a2b1c…
exit: 0.0010  pnl: -198.00$  reason: market_resolved
```

Format après :
```
📄📉 PAPER CLOSE — FADE #15
market: 0x7f3e4a2b1c…  dir: NO  size: 200.00$
entry: 0.4000 → exit: 0.0010
pnl: -198.00$ (-99.5%)  reason: market_resolved
```

### `src/config.py`

5 nouvelles constantes runtime-tunables :

```python
MAX_BOOK_AGE_PAPER_S: float = 60.0
MAX_ENTRY_PRICE: float = 0.85
MAX_LEADER_PRICE_DRIFT: float = 0.20
MIN_HOURS_TO_RESOLUTION_FOLLOW: float = 6.0
MIN_HOURS_TO_RESOLUTION_FADE: float = 24.0
```

### `docs/migrations/042_markets_resolved_outcome.sql`

Nouvelle migration : ajoute `markets.resolved_outcome VARCHAR(10)` +
index partiel. À déployer en production AVANT que `_fetch_market_resolution`
puisse retourner autre chose que None.

**Sans la migration, le helper retourne toujours None** → les closes
sur marchés résolus sont différés (warning), puis ramassés par la
timeout de 30 jours. Mieux qu'enregistrer des fake wins, mais moins
bien qu'un close à la vraie valeur de résolution.

### `tests/test_engine/test_paper_trader_audit_20260517.py`

11 tests de régression session 1 (FADE direction × 4, staleness × 4,
exit floor, Telegram format, sanity sur les constantes) + 8 tests
session 2 (adaptive cadence × 4, preclose × 2, sanity ratio × 2).
**19/19 ✅**.

---

## 3-bis. Session 2 — améliorations Tier 2 (2026-05-17 suite)

Les 11 bugs du Tier 1 sont fermés. La session 2 attaque les recommandations
des rapports d'agents qui restaient ouvertes : 7 améliorations livrées en
parallèle (2 sub-agents + travail local), 8 nouveaux tests de régression.

### Améliorations livrées

| ID | Catégorie | Fichier:ligne | Impact |
|---|---|---|---|
| **S2-A** | Critique | `scripts/maintenance_loop.py:203-287` | Job `backfill_resolved_outcomes` toutes les 30 min. Interroge Gamma `closed=true&active=false`, UPDATE `markets.resolved_outcome` IF NULL. Active enfin le helper `_fetch_market_resolution` côté paper_trader — les closes `market_resolved` enregistrent la vraie terminal value au lieu d'être différés indéfiniment. |
| **S2-B** | Défense | `scripts/maintenance_loop.py:46` | `BOOK_CACHE_TTL_S` 600s → 240s. Le cache aging-out est 4 min après que la maintenance arrête de refresher, au lieu de 10 min. Defense in depth avec la staleness gate paper_trader (60s). |
| **S2-C** | Critique | `paper_trader._monitor_loop` + `_monitor_tick_seconds` | **Cadence adaptative**. 60s par défaut, **5s dès qu'une position ouverte est à <1h de résolution**. Évite de manquer la résolution de 60s et donc de close contre des données post-résolution. |
| **S2-D** | Critique | `paper_trader._check_open_positions` (avant `_is_market_resolved`) | **Pre-resolution timeout**. Force-close 15 min avant résolution (configurable). Le bot ne dépend plus à 100% du backfill `resolved_outcome` — si la maintenance est en retard, le pre-close enregistre un close au bid frais avant l'incertitude. |
| **S2-E** | Défense | `paper_trader.close_trade` (après calcul PnL) | **Sanity ratio**. Tout close non-`market_resolved` avec |PnL%| > 500% log un message ERROR + publish `paper:audit:suspicious_close` sur Redis. Le trade est ENREGISTRÉ (refuser créerait un trou comptable), mais l'opérateur reçoit une alerte. Last-line defense contre une régression du B2 staleness gate. |
| **S2-F** | UX critique | `telegram_bot/commands.cmd_pnl` + `paper_trader.compute_unrealized_pnl` (alias public) | `/pnl` unrealized utilise enfin la **mark-to-market** au lieu du cost basis. Avant : toujours ≈ 0$ (formule structurellement nulle). Après : reflète réellement la valorisation des positions ouvertes. |
| **S2-G** | UX | `telegram_bot.format_summary` + `cmd_summary` + handler | **Nouvelle commande `/summary`**. Vue agrégée du jour (UTC midnight → now) : compte trades / wins / losses, moyenne win/loss, PnL net du jour, lifetime cum, unrealized, ventilation par `close_reason` et par `strategy`. Élimine la nécessité d'agréger mentalement depuis 30 messages CLOSE. |

Sample `/summary` :
```
📊 TODAY'S SUMMARY (UTC since 00:00)
trades: 12 closed, 3 open
wins: 4 (avg +45.20$)
losses: 8 (avg -87.30$)
net realized: -518.00$ (today)
cum realized: +41560.00$ (lifetime)
unrealized: -23.50$ (3 open)

by close reason:
  market_resolved: 6 (avg -95.20$)
  stop_loss: 4 (avg -12.40$)
  take_profit: 2 (avg +38.00$)

by strategy:
  follow: 10 (2W 8L)
  fade: 2 (2W 0L)
```

### Nouvelles constantes config (5)

```python
MONITOR_TICK_S: float = 60.0
URGENT_MONITOR_TICK_S: float = 5.0
URGENT_MONITOR_HOURS: float = 1.0
PRECLOSE_HOURS_BEFORE_RESOLUTION: float = 0.25  # 15 min
MAX_TRADE_RETURN_RATIO: float = 5.0             # 500% suspicious threshold
```

Toutes sont overridables via `RuntimeConfig` côté dashboard (cf.
`src/control/runtime_config.py`).

### Bugs secondaires non corrigés (volontairement)

- **`fee_rate_pct` unit ambiguity**. `trade_observer.py:2358` stocke
  `gamma_taker_fee_bps` mais la colonne s'appelle `_pct` et `fees.py`
  l'utilise comme décimale. Une fee à 1.56% pourrait être interprétée
  comme 156%. Impact pratique actuel ~nul parce que les marchés sports
  / géo de Polymarket sont à fee=0. Fix correct : migration data +
  unification de l'unité — risque de régression > bénéfice tant que
  le bot ne trade pas crypto à grande échelle. À adresser quand le
  bot passe sur des marchés à fees non-nulles.

- **Centralisation du `book:last` cache** dans
  `src/microstructure/book_cache.py` avec API stricte (recommandation
  §5.1 rapport agent Price Pipeline). C'est un refactor architectural
  qui touche 3+ writers et 4+ readers. Hors scope de cette session
  audit — la staleness gate B2 protège déjà tous les readers paper.

### Tests session 2

`tests/test_engine/test_paper_trader_audit_20260517.py` étendu de
11 → 19 tests, dont :
- 4 × `TestAdaptiveMonitorCadence` (no trades / loin / proche / mix)
- 2 × `TestPrecloseBeforeResolution` (déclenche / ne déclenche pas)
- 2 × `TestSanityRatioAuditLog` (publish event / market_resolved exempt)

`tests/test_telegram_bot/test_commands.py` étendu : `_StubPaperTrader`
expose `compute_unrealized_pnl()` ; nouveau test
`test_pnl_unrealized_uses_mark_to_market` ; tolerance test
`test_summary_handles_db_failure_gracefully`.

`tests/test_telegram_bot/test_formatters.py` étendu :
`test_format_summary_full_payload` (sections complètes),
`test_format_summary_empty_day` (sections collapsent),
`test_format_summary_handles_none_unrealized` (None friendly).

Suite complète après session 2 :
- `tests/test_engine/test_paper_trader.py` : 16/16 ✅
- `tests/test_engine/test_paper_trader_audit_20260517.py` : 19/19 ✅
- `tests/test_telegram_bot/` : 80/80 ✅
- Failures pré-existantes hors scope (`test_confidence_engine.py`) : 6,
  inchangées par cette session.

---

## 4. Déploiement en production

L'environnement prod est `polymarket-prod` (Hetzner Helsinki, IP
89.167.23.215, path `/opt/polymarket-bot/`). Le déploiement se fait
par `rsync` ; le path prod n'est pas un git checkout. Procédure
canonique : `docs/DEPLOY.md`.

### Checklist

1. **Backup DB d'abord** :
   ```bash
   ssh polymarket-prod
   sudo -u postgres pg_dump polymarket > /opt/polymarket-bot/backups/pre-audit-20260517.sql
   ```

2. **Migration SQL** :
   ```bash
   cat docs/migrations/042_markets_resolved_outcome.sql | \
     ssh polymarket-prod 'sudo -u postgres psql polymarket'
   ```

3. **Rsync du code** (depuis local) :
   ```bash
   rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
     src/ scripts/ docs/migrations/ tests/ \
     polymarket-prod:/opt/polymarket-bot/
   ```

4. **Rebuild de l'image engine** (les fix sont dans `src/engine/`
   et `src/telegram_bot/`, dont les conteneurs sont `engine` et
   `notifier`) :
   ```bash
   ssh polymarket-prod 'cd /opt/polymarket-bot && docker compose build engine telegram_bot && docker compose up -d engine telegram_bot'
   ```

5. **Vérification post-deploy** :
   ```bash
   # Le bot doit redémarrer cleanly
   docker compose logs --tail=100 engine | grep -iE "error|exception"

   # Vérifier que les positions ouvertes ont été rechargées
   docker compose logs --tail=200 engine | grep "PaperTrader: loaded state"

   # Vérifier que les filtres tirent (regarder pendant 10-30 min)
   docker compose exec redis redis-cli HGETALL paper:rejections:1h
   ```

### Que vous devez voir dans les heures suivantes

- Une **chute du nombre de paper_trades ouverts** (filtres plus stricts).
  C'est le comportement attendu. Si 0 trade pendant 24h, c'est OK
  tant que `paper:rejections:1h` montre des rejets actifs.
- Des rejections avec reasons `near_resolution`, `missing_end_date`,
  `leader_price_drift`, `high_entry_ask_blocked` (FADE inclus).
- Des messages Telegram CLOSE plus riches (strategy + size + pct).
- **Aucun take_profit à +30 000%** sur les futures résolutions.

### Que vous devez faire ensuite (non-bloquant mais important)

1. **Backfill `resolved_outcome`** dans le maintenance loop. Le bot
   peut tourner sans, mais les closes seront différés au lieu d'être
   enregistrés à la vraie valeur. Le maintenance loop a déjà la
   plomberie pour interroger Gamma — il suffit d'ajouter un job
   qui pour les markets `closed=true` côté Gamma écrit `outcomePrices[0] > 0.5 ? 'yes' : 'no'`
   dans `markets.resolved_outcome`. ~30 LOC à ajouter à
   `scripts/maintenance_loop.py`.
2. **Auditer la production**. Idéalement quand la DB est accessible,
   exécuter les queries A/B/C/D/F du rapport d'agent
   (cf. agent « Trade History », queries listées dans le rapport
   interne). En particulier la query F (drift exit_price vs trade
   réel) vous donnera la liste exacte des trades à fake-win
   contaminés.
3. **Reset cumulative PnL si désiré**. Vu que +$42k vient
   majoritairement de 2 trades fantômes, vous pouvez soit :
   - laisser tel quel (mais le dashboard mentira),
   - écrire une migration qui passe `paper_trades.status='audit_invalidated'`
     pour les 2 trades concernés (à identifier via la query F),
   - ou wipe complet de `paper_trades` + `portfolio_state` + `portfolio_equity` (le bot repart à $10k de capital propre — recommandé pour vraiment évaluer la stratégie).

---

## 5. Réponses directes à vos questions

> « Pourquoi on a gagné 2 trades et perdu 15 autres? »

Les 2 victoires n'en étaient pas : trade #1 et #2 venaient de mes
SETs manuels Redis lors du diagnostic, plus un cache stale. Les
mathématiques sous-jacentes sont correctes ; les **données d'entrée
étaient fausses**. Les 15 pertes sont, elles, mathématiquement
correctes mais le bot **n'aurait jamais dû prendre ces positions** :
toutes étaient sur des marchés sports à <2h de la résolution, à
entry_ask 0.99 — exposure 100% downside / 1% upside.

> « Est-ce qu'on est sûr que la cotation des prix est correctement
> comptabilisée ? »

**Avant les fixes : non.** Le book cache était lu sans vérification
d'âge → l'exit prix pouvait être de plusieurs minutes obsolète.
**Après B2 : oui, dans une fenêtre de 60s.** Au-delà, le quote est
refusé et le code retombe sur le fallback (entry_price ou résolution
deferred).

> « Est-ce qu'on calcule mal le closing ? »

**Avant : oui pour FADE** (inversion direction). **Avant : oui sur
résolution** (bid stale au lieu du terminal value). **Après B1+B3 :
oui pour FADE et oui sur résolution si `markets.resolved_outcome` est
rempli, sinon le close est différé honnêtement.

> « Est-ce qu'on a un vrai solide back-end système de calcul pour du
> paper trading ? »

**La math individuelle (`calculate_long_pnl`, `shares_from_notional`)
est correcte et inchangée**. C'est l'ENVIRONNEMENT autour qui pêchait :
sourcing des prix d'exit, timing des closes, filtres pré-trade. Les 11
fixes adressent l'ensemble de ces problèmes systémiques.

> « Est-ce qu'il n'aurait pas énormément de failles mathématiques par
> hasard ? »

Math pure : pas de faille. Symbolique : oui — l'inversion direction
FADE et l'absence de gates étaient des erreurs sémantiques.
Architecture : oui — la dette technique entre WS observer / maintenance
loop / JIT fetch / paper_trader avec 3 TTL différents et 0 contrat
schéma payload était un terrain propice aux bugs. Recommandation
post-session : centraliser le `book:last` dans `src/microstructure/book_cache.py`
avec API stricte (rapport agent « Price Pipeline » §5.1).

---

## 6. Métriques de test

```
tests/test_engine/test_paper_trader_audit_20260517.py  : 11 / 11 ✅ (nouveau)
tests/test_engine/test_paper_trader.py                  : 16 / 16 ✅ (1 mock mis à jour)
tests/test_engine/test_confidence_engine.py             : 6 failures pré-existantes (hors scope audit)
```

Les 6 échecs `test_confidence_engine.py` sont vérifiés pré-existants
par `git stash` rollback test — ils ne sont pas introduits par cette
session.
