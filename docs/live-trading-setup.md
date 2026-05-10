# Live Trading — Wallet & Environment Provisioning

Cette procédure documente comment provisionner un wallet Polymarket et configurer
les variables d'environnement pour faire passer le bot du mode paper-only au mode
live (S2.6).

> **Avertissement légal** : Polymarket n'est pas autorisé en France pour les
> résidents français au moment de la rédaction. Le bot tourne déjà sur une
> VM hors juridiction française (Hetzner Helsinki / HEL1, cf.
> [INFRA.md](INFRA.md)). Le mode `LIVE_TRADING_DRY_RUN=true` reste le défaut
> tant que la procédure ci-dessous n'est pas déroulée intégralement.

---

## 1. Étapes manuelles (à faire par Oscar, pas par le bot)

### 1.1 Créer le compte Polymarket
1. Aller sur <https://polymarket.com> depuis un navigateur sur la VM cible
   (pas en France).
2. Connexion via Magic.link (email) ou wallet externe. Magic.link crée un
   **proxy wallet** géré par Polymarket sur Polygon — c'est ce que le SDK
   `py-clob-client` utilise par défaut avec `signature_type=2`.
3. Polymarket génère deux adresses :
   - **EOA (Externally Owned Account)** : la clé privée que vous contrôlez.
     C'est elle qui signe les ordres EIP-712.
   - **Funder (proxy wallet)** : l'adresse Polymarket qui détient les fonds.
     C'est l'adresse à approvisionner et celle à passer en
     `POLYMARKET_FUNDER_ADDRESS`.

### 1.2 Approvisionner en USDC sur Polygon
1. Acheter des USDC sur un exchange (Coinbase, Binance, etc.).
2. Retirer en USDC sur le réseau **Polygon (Matic mainnet)**, chain id `137`,
   directement sur l'adresse Funder du proxy wallet (vue dans Polymarket
   "Deposit"). Pas l'EOA — Polymarket ne tradera qu'avec les fonds présents
   sur le proxy.
3. Garder un peu de MATIC sur l'EOA pour le gas si jamais on doit cancel
   manuellement (le SDK gère le gas pour les ordres normaux).

### 1.3 Récupérer la clé privée de l'EOA
Comment selon le mode de connexion :

- **Magic.link** : Settings → Export Private Key. Sauver dans un manager de
  secrets (1Password, Bitwarden). **Ne jamais committer dans git.**
- **Wallet externe (Metamask, etc.)** : exporter la clé privée du wallet utilisé
  pour signer.

La clé privée est un hex de 64 caractères, optionnellement préfixé `0x`. Le
SDK accepte les deux formats.

### 1.4 Vérifier les credentials (optionnel mais recommandé)
Sur la VM (en mode dry-run d'abord), depuis un REPL Python :

```python
import asyncio
from src.engine.clob_client_wrapper import CLOBClientWrapper

async def check():
    w = CLOBClientWrapper(
        clob_url="https://clob.polymarket.com",
        chain_id=137,
        private_key="0x…",
        funder_address="0x…",
        dry_run=False,
    )
    print(await w.get_midpoint("0x…token_id…"))

asyncio.run(check())
```

Si le client construit sans erreur et que `get_midpoint` retourne un float
dans (0, 1), les credentials sont OK.

---

## 2. Variables d'environnement

Toutes vivent dans `.env` (pas committé) ou injectées par le runtime
(systemd, Docker secrets, etc.). Tous les défauts sont safe : si une
seule de ces vars manque, le bot reste en mode shadow.

| Variable                          | Type   | Défaut                          | Effet                                                                    |
|-----------------------------------|--------|---------------------------------|--------------------------------------------------------------------------|
| `LIVE_TRADING_DRY_RUN`            | bool   | `true`                          | `false` autorise les vrais ordres. Ne passe PAS à `false` sans `POLYMARKET_PRIVATE_KEY`. |
| `POLYMARKET_CLOB_URL`             | string | `https://clob.polymarket.com`   | Endpoint REST CLOB.                                                       |
| `POLYMARKET_CHAIN_ID`             | int    | `137`                           | Polygon mainnet.                                                          |
| `POLYMARKET_PRIVATE_KEY`          | string | `""`                            | Clé EIP-712. Vide ⇒ le wrapper force `dry_run=True` même si le flag est `false`. |
| `POLYMARKET_FUNDER_ADDRESS`       | string | `""`                            | Adresse du proxy Polymarket (= l'adresse qu'on a approvisionnée).        |
| `LIVE_SLIPPAGE_BPS`               | int    | `50`                            | Marge limite (50 bps = 0,5 %) ajoutée au mid pour BUY, soustraite pour SELL. |
| `LIVE_ORDER_TIMEOUT_S`            | int    | `30`                            | Si l'ordre n'est pas filled au bout de N secondes, on cancel + reprice.  |
| `LIVE_ORDER_MAX_RETRIES`          | int    | `3`                             | Nombre max de tentatives reprice avant d'abandonner.                     |
| `LIVE_FILL_POLL_INTERVAL_S`       | float  | `2.0`                           | Période de polling REST CLOB pour la détection de fill.                  |

### Exemple `.env` minimal

```
LIVE_TRADING_DRY_RUN=true
POLYMARKET_PRIVATE_KEY=0xabc…
POLYMARKET_FUNDER_ADDRESS=0xdef…
```

Avec ces trois lignes, le bot tournera encore en mode shadow (le flag
prime), mais il sera prêt à basculer en `false` quand tout sera validé.

---

## 3. Procédure de bascule shadow → live

> **Faire dans cet ordre, jamais l'inverse.**

1. **Phase 1 — Shadow sur live data**
   - `LIVE_TRADING_DRY_RUN=true`, clé privée renseignée, funder vide.
   - Le bot lit le CLOB (mid, orderbook), prend des décisions, mais
     n'envoie aucun ordre. Toutes les "exécutions" sont loggées avec
     `state='shadow'` dans `live_orders` et `live_trades`.
   - Laisser tourner ≥ 24 h. Vérifier que les rows s'accumulent et que
     les PnL hypothétiques (calculés post-hoc) correspondent au paper
     trader.

2. **Phase 2 — Smoke test live, taille minimale**
   - Approvisionner 5–10 USDC sur le funder.
   - `LIVE_TRADING_DRY_RUN=false`, `MIN_POSITION_USDC=1.0` temporairement.
   - Sur un seul marché peu liquide, déclencher manuellement un ordre.
   - Vérifier dans Polymarket que la position apparaît, puis la closer.
   - Comparer ce qui est dans `live_trades` à ce qui s'est passé chez
     Polymarket — si OK, on est bon pour la phase 3.

3. **Phase 3 — Production**
   - Restaurer `MIN_POSITION_USDC` à la valeur normale.
   - Approvisionner le funder à hauteur du capital alloué au bot.
   - Suivre via les logs et le dashboard. Le killswitch S1.1 reste
     l'arrêt d'urgence.

---

## 4. Sécurité opérationnelle

- **Ne jamais** mettre `POLYMARKET_PRIVATE_KEY` dans git, dans un Dockerfile,
  ou dans une URL.
- Stocker la clé dans un secret manager (Doppler, AWS SM, GCP SM,
  Hashicorp Vault). Sur le VM Hetzner actuel, la clé est lue depuis
  `/opt/polymarket-bot/.env` (mode 600, owner `polymarket`). Migrer vers
  Docker secrets ou systemd `LoadCredential=` avant de monter le capital
  alloué au bot.
- Limiter le solde sur le funder à ce qui est nécessaire pour 24–48 h de
  trading. Ce n'est pas un wallet de stockage.
- Le killswitch (`src/control/killswitch.py`) doit être en `ON` avant tout
  démarrage live. Il est en `OFF` par défaut au boot.
- Garder la trace des ordres effectifs dans `live_orders` permet une
  reconciliation manuelle a posteriori si un fill est manqué.

---

## 5. Diagnostic

| Symptôme                                                       | Cause probable                                            | Fix                                                       |
|---------------------------------------------------------------|-----------------------------------------------------------|------------------------------------------------------------|
| `place_limit_order` retourne `success=False, error="EOA …"` | EOA pas autorisée à signer pour ce funder                 | Vérifier que l'EOA est bien celle créée par Magic.link / Metamask. |
| Ordre placé mais jamais filled                                | Limit price trop conservateur, le book a bougé             | Augmenter `LIVE_SLIPPAGE_BPS` ou réduire `LIVE_ORDER_TIMEOUT_S` pour reprice plus vite. |
| `dry_run` reste `True` malgré le flag                         | `POLYMARKET_PRIVATE_KEY` vide → force shadow              | Renseigner la clé.                                         |
| `bad_midpoint` dans `live_orders.error_message`               | Marché illiquide ou résolu                                | Filtrer en amont (RiskManager).                            |
