# WalletGraph v3 — Audit du code existant à supprimer/garder

## À SUPPRIMER (vague de cleanup)

| Fichier | Lignes | Contenu | Pourquoi |
|---|---|---|---|
| static/dashboard/dashboard-tabs.jsx | 1875–2518 | WalletGraph V1 SVG (main component + layout logic) | SVG force-layout remplacé par HellBorn/BubbleMaps universe graph |
| static/dashboard/dashboard-tabs.jsx | 2519–2890 | WalletProfileSection, Stat, et helpers (sub-components du WalletGraph V1) | Sous-composants du WalletGraph V1 |
| static/dashboard/dashboard-tabs.jsx | 3123–3163 | V2LabToggle component (localStorage.poybot_v2_lab toggle) | Le toggle lab ne servira plus après suppression du V2 |
| static/dashboard/v2/WalletGraph.jsx | 1–647 | Cosmograph WebGL v2.0 (complete file) | Implémentation Cosmograph à supprimer |
| static/dashboard/v2/ | (entire dir) | LivePortfolio.jsx + theme.css + primitives/ + portfolio/ | Dossier complet V2 (~2457 LOC) |
| templates/dashboard.html | 156–165 | Cosmograph CDN load + ESM script | Script import Cosmograph: `https://esm.sh/@cosmograph/cosmograph@1.4.2` |
| templates/dashboard.html | 112–202 | V2 lab overlay init block | Conditionnelle de chargement des fichiers V2 sur `localStorage.poybot_v2_lab === '1'` |

## À GARDER (data backend + endpoints)

**Backend endpoints (API)**:
- `/api/wallet/{wallet}/markets` — GET, retourne `{markets: [...], category_breakdown: [...], total_trades, distinct_markets}` (src/api/main.py:1428)
- `/api/wallet/{wallet}/profile` — GET, retourne `{sizing, accuracy, entry_patterns, follower_impact, wallet360_json, ...}` (src/api/main.py:1461)
- Snapshot field `wallet_graph` (cache 30s) — retourne `{nodes: [...], edges: [...], stats: {leaders, followers, edges_total, edges_confirmed}}`

**Backend queries** (src/api/queries.py):
- `wallet_graph(conn, max_leaders=3000)` — lignes 2713–2900, retourne nodes + edges pour le graph
- `wallet_profile(conn, wallet_address)` — lignes 3291–3350, retourne behavioural profile JSON
- `wallet_markets(conn, wallet_address, window_days=30, limit=20)` — lignes 4086+, retourne liste des marchés du wallet

**Database schema** (no changes):
- `leader_profiles` (wallet_address, profile_json, profile_maturity, error_model_phase, trades_observed, positions_resolved)
- `follower_edges` (leader_wallet, follower_wallet, co_occurrences, hawkes_alpha_mu, follow_probability, avg_delay_s, same_direction_rate, trapped_rate)
- `trades_observed` (wallet_address, market_id, token_id, time, side, price, size_usdc, category)

## Schéma des données retournées par wallet_graph()

**Nodes** (array):
```json
{
  "id": "0x...",                    // wallet address
  "label": "0x....",                // shortened for display
  "role": "leader" | "follower",
  "falcon_score": 0.85,
  "phase": 1 | 2 | 3,
  "maturity": 0.72,
  "trades_observed": 156,
  "positions_resolved": 89,
  "win_rate": 0.54,
  "pnl_total": 1234.56,
  "trades_24h": 3,
  "last_action": "follow" | "fade",
  "classification": "directional",
  "top_categories": [{category: "crypto", pct: 0.45, trades: 70}, ...]
}
```

**Edges** (array):
```json
{
  "source": "0x...",                // leader
  "target": "0x...",                // follower
  "p_follow": 0.72,                 // follow_probability
  "hawkes_alpha_mu": 1.24,
  "co_occurrences": 18,
  "same_direction_rate": 0.89,
  "trapped_rate": 0.45,
  "avg_delay_s": 45
}
```

**Stats**:
```json
{
  "leaders": 2628,
  "followers": ~28000,
  "edges_total": ~48000,
  "edges_confirmed": ~5000
}
```

## Palette globale existante (dashboard-components.jsx, ligne 26)

```javascript
const C = {
  amber:  '#e8a020',  // Golden yellow (phase 3, active, alerts)
  green:  '#28a84e',  // Success (wins, profits, active trades)
  red:    '#c93545',  // Loss, warnings
  blue:   '#3d7dc8',  // Info (phase 2)
  purple: '#7855c0',  // Phase 1 (beta)
  
  // Neutrals
  text:   '#c4ccd8',  // Primary text
  dim:    '#3a4558',  // Secondary text
  dim2:   '#6b7a94',  // Tertiary, labels
  white:  '#eef2f8',  // Bright/highlights
  
  // Backgrounds
  bg:     '#070809',  // Body background (near-black)
  panel:  '#0c0e12',  // Card/panel background
  panel2: '#101318',  // Secondary panel (darker)
  border: '#1a2030',  // Primary border
  border2: '#252d3e'  // Secondary border (lighter)
};
```

**Utilisation dans le dashboard**:
- `C.amber` — Phase 3 leaders, active signals, important metrics
- `C.blue` — Phase 2 leaders
- `C.purple` — Phase 1 (beta) leaders
- `C.green` — Wins, profits, successful followers
- `C.red` — Losses, errors, warnings
- `C.dim2` — Labels, secondary info
- `C.text` — Primary content text

## Total LOC à supprimer

| Component | Lines | Notes |
|-----------|-------|-------|
| WalletGraph V1 SVG (dashboard-tabs.jsx) | 644 | Main component + layout engine |
| WalletProfileSection + helpers (dashboard-tabs.jsx) | ~370 | Sub-component + Stat, SectionLabel, etc. |
| V2LabToggle (dashboard-tabs.jsx) | 41 | localStorage.poybot_v2_lab toggle |
| WalletGraph.jsx (Cosmograph v2) | 647 | Complete V2 implementation |
| LivePortfolio.jsx (v2) | ~284 | V2 portfolio overlay |
| theme.css (v2) | ~384 | V2 styling |
| Primitives + portfolio sub-components (v2) | ~1142 | Reusable UI blocks for V2 |
| **Subtotal (frontend JSX)** | **~3512** | |
| Cosmograph CDN + v2-lab init (dashboard.html) | ~92 | HTML script tags + conditional loader |
| **Total suppressible lines** | **~3604** | Frontend + template |

## Surprises trouvées

1. **Cosmograph ESM module load timing** — L'import Cosmograph en dashboard.html (ligne 160) utilise un script module qui s'exécute **après** que les V2 fichiers sont chargés. Il y a une race condition potentielle si window.Cosmograph n'est pas prêt à temps ; le code polled déjà (v2/WalletGraph.jsx, ligne 280–293) mais c'est fragile.

2. **V2 lab gating dans localStorage** — Le toggle persiste en localStorage (`poybot_v2_lab`), mais il n'y a aucune autre référence au flag V2 ailleurs dans le backend. Aucune mutation d'API basée sur le lab, juste du chargement/rebinding côté client. Safe à supprimer.

3. **WalletProfileSection réutilisé seulement par WalletGraph V1** — C'est un sous-composant purement V1 ; aucune autre utilisation. Suppression propre une fois WalletGraph V1 disparu.

4. **Pas d'autres dépendances Cosmograph trouvées** — Le recherche complète d'`/static/` et `src/api/` montre qu'aucun autre fichier n'importe Cosmograph. Safe de supprimer le fichier v2/ entièrement.

5. **Backend queries sont réutilisables** — Les endpoints `/api/wallet/{wallet}/markets`, `/api/wallet/{wallet}/profile`, et le field snapshot `wallet_graph` ne dépendent **jamais** de la version du frontend. Ils peuvent servir le nouveau HellBorn/BubbleMaps universe graph sans modification.

---

## Checklist de cleanup

- [ ] Supprimer lignes 1875–2518 de dashboard-tabs.jsx (WalletGraph V1)
- [ ] Supprimer lignes 2519–2890 de dashboard-tabs.jsx (WalletProfileSection + helpers)
- [ ] Supprimer lignes 3123–3163 de dashboard-tabs.jsx (V2LabToggle)
- [ ] Supprimer le répertoire entier `static/dashboard/v2/`
- [ ] Supprimer lignes 112–202 de templates/dashboard.html (V2 lab init block)
- [ ] Supprimer lignes 156–165 de templates/dashboard.html (Cosmograph CDN script)
- [ ] Mettre à jour l'export final (dashboard-tabs.jsx, ligne 3427) pour retirer LabGates, LivePortfolio, WalletGraph (v1)
- [ ] Keeper: `src/api/queries.py` (wallet_graph, wallet_profile, wallet_markets)
- [ ] Keeper: `src/api/main.py` endpoints `/api/wallet/{wallet}/markets` et `/api/wallet/{wallet}/profile`
- [ ] Keeper: `dashboard-components.jsx` palette C.* (aucune modification requise)

---

## Notes architecturales pour V3

**Data contract maintenu** — Le nouveau HellBorn/BubbleMaps engine doit consommer le même schéma de nodes/edges :
- Nœuds avec `id`, `falcon_score`, `phase`, `maturity`, `trades_24h`, `top_categories`
- Arêtes avec `source`, `target`, `follow_probability`, `co_occurrences`, `same_direction_rate`

**Visual inheritance** — Le nouveau engine doit respecter la palette C.* existante :
- Phase colors: purple (P1), blue (P2), amber (P3)
- Node halo/glow: nodes leaders = bright, followers = muted
- Edge opacity/width: `α ∝ follow_probability`, `width ∝ log(co_occurrences)`

**Backend remains untouched** — Aucune modification requise à la base de données, aux requêtes, ou aux endpoints API.
