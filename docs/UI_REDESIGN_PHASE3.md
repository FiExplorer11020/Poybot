# UI Redesign — Phase 3 (Post R6→R13)

> **Status**: Design proposal • Date: 2026-05-12 • Author: orchestrator
> **Scope**: complete refonte of the React dashboard to surface every
> R6→R13 deliverable (8 rounds, +31 metrics, +21 migrations, +5 daemons,
> +253 tests). Existing UI was designed for the R0→R5 era.
> **Not in scope**: implementation. This document defines target
> architecture + per-tab wireframes + roadmap. Implementation lives in
> follow-up PRs against `static/dashboard/`.

---

## 1. Executive summary

The dashboard was built for the R0→R5 bot — a leader-follower
intelligence engine on top of Falcon+REST. R6→R13 added six new
architectural layers:

| Round | Codename | UI surface needed |
|---|---|---|
| R6 | The Spine | wallet universe browser, on-chain ingestion health, cold-tier explorer, daemon supervisor |
| R7 | The Front Door | mempool intent feed, NonceTracker replacement chains, pre-signed pool inventory, intent-router decision log |
| R8 | The Lens | strategy fingerprint per leader (9 classes), labelling workspace, cluster explorer, drift heatmap |
| R9 | The Web | multivariate Hawkes α matrix, follower-pool Kalman state, volume forecast graph |
| R10 | The Truth Test | causal vs Hawkes scatter, IV diagnostics, instrument event timeline, counterfactual replay UI |
| R11 | The Microscope | L3 book firehose, microstructure features per market, per-wallet signature |
| R12 | The Periphery | social signals feed, NLP intent classification, cross-market operator resolver |
| R13 | The Mirror | per-model calibration loss, drift z-score gauges, auto-disable status, research notebook launcher |

The current 8-tab navigation **physically cannot host** these new
surfaces without massive vertical compression. The refonte preserves
the 8-tab top-level (muscle memory) but **redefines four of them** and
**adds three brand-new tabs** in exchange for **collapsing three
existing tabs** into a single multi-pane "Execution" surface.

**Design principles preserved**:
- Dark terminal aesthetic (monospace, yellow/green/red/blue/purple accents)
- KPI strip across the top of every page (5–8 KPIs)
- Left sidebar with system status + paper/live toggle at the bottom
- Status pills (RUNNING / DRY RUN / latency / UTC)
- Dense tables with sortable columns
- Action drawer on the right side of cockpit pages
- Audit log at the bottom of every page that mutates state

**Design principles added**:
- "Gated feature" pattern — when a runtime flag is OFF, the section
  shows a clearly-labelled "GATED — operator must enable in OPERATIONS"
  banner instead of greyed-out data.
- "Cross-round breadcrumb" — pages reference upstream / downstream
  rounds explicitly (e.g., the Lens shows "feeds → Causal Layer (R10)").
- "Deferred fix" callout — when a wave-3 reviewer flagged a missing
  hook (R13 engine wiring), the UI renders a banner with the pseudo-diff
  link.

---

## 2. Audit of current UI

### 2.1 Existing 8-tab inventory

| Tab | Component | Hosts | LOC |
|---|---|---|---|
| 1. ALPHA TERMINAL | `AlphaTerminal` | exec-summary KPIs + live snapshot | ~400 |
| 2. ML PROGRESSION | `MLProgression` | profile maturity, phase 1/2/3 distribution, learning trajectory | ~500 |
| 3. WALLET GRAPH | `WalletGraph` | leader→follower graph + scanner table | ~500 |
| 4. LIVE PORTFOLIO | `LivePortfolio` | open positions table + per-position P&L | ~300 |
| 5. DECISION ENGINE | `DecisionEngine` | decisions grouped by leader with action counters | ~350 |
| 6. INSPECTOR | `Inspector` | raw trades + source mix + pipeline health + recent decisions | ~400 |
| 7. RISK & CONFIG | `RiskConfig` | mutable risk cockpit + bot control + audit log | ~250 |
| 8. BOT HEALTH | `BotHealth` | data quality issues + ingestion sources + market-level health | ~250 |

Total: ~2750 LOC of JSX across `dashboard-tabs.jsx`. The components
are reasonably composed (most cards reusable) but the **navigation
mental model** is tied to the R0–R5 architecture.

### 2.2 What still works (preserve)

- Terminal aesthetic — extends naturally to the new layers
- KPI strip pattern — fits all new tabs unchanged
- Sidebar — `SYSTEM` status block is universal
- Status pills + breadcrumb header
- Audit log row pattern (used in Risk & Config) — port to Operations
- Action drawer / right-side cockpit
- Bot control card (Start / Stop / Pause)
- Emergency kill switch

### 2.3 What's outdated post-R6

1. **`INSPECTOR` and `DECISION ENGINE` overlap** with each other and
   neither shows R7 intent-router decisions or R10 causal gate decisions.
2. **`ML PROGRESSION`** shows the old 3-phase progression (Beta /
   BayesianLogReg / LightGBM) — completely silent about R8 strategy
   classifier, R9 multivariate Hawkes, R13 calibration loss.
3. **`WALLET GRAPH`** shows leader→follower edges only — no R6 universe,
   no R8 strategy fingerprint, no R11 wallet microstructure signature.
4. **`BOT HEALTH`** shows R3-era ingestion health (CLOB WS + REST) —
   nothing about R6 on-chain CLOB, R6 crawler, R7 mempool, R11 L3 book,
   R12 social/cross-market daemons.
5. **No surface for R7** at all — the mempool watcher exists but the
   operator has no visibility into intent detection.
6. **No surface for R10** at all — the causal layer ships gated OFF and
   would be invisible even if flipped ON.
7. **No surface for R13** at all — calibration loss + auto-disabled
   models are critical operational signals and have no UI.

---

## 3. Gap analysis — R6→R13 features that need UI

### 3.1 R6 — The Spine (data sovereignty)

| Feature | UI need | Tab |
|---|---|---|
| `wallet_universe` table (2000+ wallets, depth_tier) | Universe browser with tier filter | WALLET LAB |
| On-chain CLOB events (`trades_observed.source='onchain'`) | Source mix gauge (REST vs WS vs onchain) | OPERATIONS / Health |
| Cold-tier Parquet exports | "Cold-tier" panel with last export ts + row counts | OPERATIONS / Health |
| Coverage reconciler (`coverage_reconciler.py`) | Coverage gauge per market (REST vs WS vs onchain disagreement) | OPERATIONS / Health |
| Daemon supervisor (`ingestion_daemon/`) | Per-daemon registry: status, last heartbeat, restart count | OPERATIONS / Health |
| RPC health (`rpc_health_history`) | Provider rotation panel (Quicknode / Alchemy / Infura) | OPERATIONS / Health |
| Falcon refresher | Event-driven refresh queue depth | OPERATIONS / Health |

### 3.2 R7 — The Front Door (mempool)

| Feature | UI need | Tab |
|---|---|---|
| `MempoolSubscription` live feed | Live intent stream (last 100 LeaderIntent) | MEMPOOL |
| `NonceTracker` replacement chains | Replacement-chain visualisation (gas wars) | MEMPOOL |
| `CLOBTxDecoder` decode outcomes | Decode hit-rate per selector (fill/match/cancel) + not_clob / decode_failed | MEMPOOL |
| `WatchedWalletIndex` bloom | Bloom membership FP rate over time | MEMPOOL |
| `PreSignedPool` inventory | Bucket fit table per market | MEMPOOL |
| `IntentRouter` decisions | Filterable decision log (killswitch_off / confidence_skip / size_cap / cooldown / shadow / pool_miss / filled / error) | MEMPOOL |
| Intent router latency p50/p99 | Trace timeline against R7 §6 gate (< 250ms p50) | MEMPOOL |
| `prefill_live_enabled` flag | Toggle in OPERATIONS, banner in MEMPOOL | both |

### 3.3 R8 — The Lens (strategy classifier)

| Feature | UI need | Tab |
|---|---|---|
| Per-leader strategy fingerprint | Strategy card on every wallet profile (9 classes + confidence + drift score) | WALLET LAB |
| `strategy_labels` table | Labelling workspace (Jupyter-like inline; pin to Top 100 wallets) | INTELLIGENCE |
| Cohen's κ on validation set | κ gauge + per-labeller agreement matrix | INTELLIGENCE |
| `UnsupervisedStrategyExplorer` clusters | Cluster scatter (K-means + DBSCAN); operator surfaces candidate new classes | INTELLIGENCE |
| `StrategyDriftDetector` | Drift heatmap (leader × time), z-score gauge | INTELLIGENCE |
| `leader_strategy_history` | History sparkline per wallet | WALLET LAB |
| `strategy_conditional_confidence_enabled` flag | Toggle + ON / OFF impact preview | OPERATIONS |

### 3.4 R9 — The Web (Hawkes + Kalman)

| Feature | UI need | Tab |
|---|---|---|
| `multivariate_hawkes_fits` α matrix | α-matrix heatmap per leader (4-pool default) | INTELLIGENCE |
| Per-pool BIC stat | BIC strip per (leader, pool) | INTELLIGENCE |
| `FollowerPoolKalman` state | 3-state vector card per (leader, pool) — pool_size, response%, decay | INTELLIGENCE |
| `FollowerVolumePredictor` output | Forecast graph (total + by_pool + CI + time CDF) | INTELLIGENCE |
| `HawkesCouplingDriftDetector` alerts | Drift event log + gate switch | INTELLIGENCE |
| `volume_anticipation_enabled` flag | Toggle + recent volume_anticipation decisions count | OPERATIONS |

### 3.5 R10 — The Truth Test (causal)

| Feature | UI need | Tab |
|---|---|---|
| `causal_estimates` per (leader, pool) | Causal vs Hawkes scatter (the keystone plot) | INTELLIGENCE / Causal |
| Wu-Hausman p distribution | Histogram + per-pair table | INTELLIGENCE / Causal |
| First-stage F gauge | Per-pair F value vs > 10 gate | INTELLIGENCE / Causal |
| `instrumental_events` timeline | Event ticker by type (news / oracle / outage / gas / funding) | PERIPHERY |
| `CounterfactualReplayer` | Interactive what-if launcher (period + policy override + go) | OPERATIONS / Research |
| `causal_gating_enabled` flag | Toggle + gate decisions count + methodology-audit banner | OPERATIONS |

### 3.6 R11 — The Microscope (L3 book)

| Feature | UI need | Tab |
|---|---|---|
| `CLOBBookObserver` event firehose | Live stream of book events (filterable by market/type) | MICROSCOPE |
| Backpressure / queue depth | Queue-depth gauge + drop counter | MICROSCOPE |
| `IcebergDetector` flags | Per-market iceberg ticker | MICROSCOPE |
| `SpoofDetector` flags | Per-market spoof ticker | MICROSCOPE |
| `OrderFlowImbalanceCalculator` | Per-market OFI sparkline (5s rolling) | MICROSCOPE |
| `microstructure_features` rollups | Per-market rollup table | MICROSCOPE |
| `wallet_microstructure_signature` | Inline on wallet profile (cancel/fill ratio, place-to-fill p50/p99) | WALLET LAB |
| Partition maintenance state | Active partitions + retention horizon | OPERATIONS / Health |

### 3.7 R12 — The Periphery (social + cross-market)

| Feature | UI need | Tab |
|---|---|---|
| `XFirehoseSubscriber` tweet stream | Social signal feed (X / TG / Discord) | PERIPHERY |
| `HeuristicTweetClassifier` output | Per-tweet intent + confidence | PERIPHERY |
| Tweet-to-trade lag | Distribution chart per leader | PERIPHERY |
| `KalshiClient` / `ManifoldClient` / `PredictItClient` | Per-venue status panel | PERIPHERY |
| `WalletResolver` | Pending-review queue (operator confirms auto-matches) | PERIPHERY |
| Cross-market operator resolution | Resolved operators table (polymarket ↔ kalshi ↔ manifold) | PERIPHERY |
| Cross-market positions snapshot | Multi-venue position table | PERIPHERY |
| Cross-venue correlation | Per-operator correlation matrix | PERIPHERY |

### 3.8 R13 — The Mirror (calibration)

| Feature | UI need | Tab |
|---|---|---|
| `decision_predictions` per decision | "Why this decision?" drill-down on every decision | EXECUTION |
| `calibration_loss_history` per model | Per-model loss trajectory chart (Brier / MAPE / log_loss) | OPERATIONS / Calibration |
| `ModelDriftMonitor` z-score | Drift z-score gauge per (model, strategy_class) | OPERATIONS / Calibration |
| `ModelAutoDisabler` state | List of disabled models + reason + re-enable button | OPERATIONS / Calibration |
| `follow_confidence` protection guard | Visual indicator that auto-disable refuses on this model | OPERATIONS / Calibration |
| Research notebooks launcher | 6 notebook tiles with run/last-run + open-in-Jupyter link | OPERATIONS / Research |

---

## 4. Proposed new IA

### 4.1 Recommendation: 8 tabs, repurposed

```
BEFORE (R0–R5 era)              AFTER (R6–R13 era)
─────────────────────           ────────────────────────────
1. ALPHA TERMINAL               1. OVERVIEW           (kept, refonded — exec summary)
2. ML PROGRESSION               2. INTELLIGENCE       (refonded — R8 + R9 + R10 + R13)
3. WALLET GRAPH                 3. WALLET LAB         (refonded — R6 universe + R8 fingerprint + R11 signature + graph)
4. LIVE PORTFOLIO       ──┐
5. DECISION ENGINE      ──┼──→  4. EXECUTION         (merged — Portfolio / Decisions / Inspector sub-tabs)
6. INSPECTOR            ──┘
7. RISK & CONFIG        ──┐
8. BOT HEALTH           ──┴──→  5. OPERATIONS        (merged — Risk / Health / Calibration / Research sub-tabs)
                                6. MEMPOOL            (NEW — R7)
                                7. MICROSCOPE         (NEW — R11)
                                8. PERIPHERY          (NEW — R12 + R10 instrument events)
```

8 → 8 tabs. Same top-level navigation depth. The three "merged" tabs
gain internal sub-tabs but stay within their original space.

### 4.2 Sub-tab structure

```
EXECUTION
├─ Portfolio        (existing LIVE PORTFOLIO)
├─ Decisions        (existing DECISION ENGINE + R7 intent decisions + R13 prediction drill-down)
└─ Inspector        (existing INSPECTOR + R10 causal gate decisions)

OPERATIONS
├─ Risk & Config    (existing RISK & CONFIG)
├─ Health           (existing BOT HEALTH + R6 daemons + R6 cold-tier)
├─ Calibration      (NEW — R13)
└─ Research         (NEW — R13 § 3.5 notebooks + R10 counterfactual replay)

INTELLIGENCE
├─ Maturity         (existing ML PROGRESSION — pre-R8 model phases)
├─ Lens             (NEW — R8 strategy classifier)
├─ Web              (NEW — R9 mvHawkes + Kalman + volume forecast)
└─ Causal           (NEW — R10 IV vs Hawkes scatter + Wu-Hausman + counterfactual launcher)

WALLET LAB
├─ Universe         (NEW — R6 wallet_universe browser with tier filter)
├─ Graph            (existing WALLET GRAPH)
├─ Scanner          (existing Wallet Scanner)
└─ Profile          (existing per-wallet drill-down + R8 + R11 augmentations)
```

### 4.3 Rationale

| Decision | Rationale |
|---|---|
| Keep 8 top-level tabs | Muscle memory. Operator has been using this layout. Sub-tabs absorb growth. |
| Merge Portfolio/Decisions/Inspector into Execution | All three answer "what is the bot deciding/doing right now?". Sub-tabs preserve granularity. |
| Merge Risk/Health/Calibration/Research into Operations | All four answer "how do I control / observe / extend the bot?". Operator-facing surface. |
| Refonde ML Progression → Intelligence | The brain is now R8+R9+R10+R13, not just the 3-phase progression. The old Maturity view stays as a sub-tab. |
| Refonde Wallet Graph → Wallet Lab | The wallet is now the primary unit of analysis (universe + fingerprint + signature + graph). |
| MEMPOOL / MICROSCOPE / PERIPHERY as top-level | Each represents a fundamentally new data layer. They warrant their own top-level. |
| Don't introduce a "Causal" top-level | R10 is methodologically heavy but operationally scoped (gate switch + scatter). Lives as Intelligence/Causal sub-tab. |
| Notebooks under Operations/Research | Operator-driven, not bot-driven. Tucked away but discoverable. |

---

## 5. Per-tab wireframes (new + refonded)

### 5.1 OVERVIEW (refonde of ALPHA TERMINAL)

Purpose: 30-second exec summary for the operator. "Is the bot OK
right now?"

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ OVERVIEW   wallet: 0x76aa…eff9   [RUNNING] [DRY RUN] latency 8ms  UTC … │
├─ KPI STRIP (8) ─────────────────────────────────────────────────────────┤
│ NET PNL │ WIN RATE │ POSITIONS │ DECISIONS 24h │ INTENT/HR │ BOT UPTIME │
│ +$0.00  │  0.0%    │   0/10    │     52        │    0      │ 10h43m    │
│ INGEST RATE │ AUTO-DISABLED                                              │
│  4 / 2765   │     0                                                      │
├─ THREE ROWS ────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─ BRAIN ─────────────┐  ┌─ EYES ──────────────┐  ┌─ HANDS ─────────┐  │
│  │ Phase: P1 (411) /   │  │ R6 onchain:    ✓    │  │ R7 mempool:  ✓  │  │
│  │        P2 (3) /     │  │ R6 cold tier:  ✓    │  │ Prefill pool:38 │  │
│  │        P3 (0)       │  │ R11 L3 book:   ✓    │  │ Last fire: —    │  │
│  │ Maturity: 0%        │  │ R12 social:    idle │  │ Shadow firing:0 │  │
│  │ Lens trained: ✗     │  │ R12 crossmkt:  idle │  │ Live firing: ✗  │  │
│  │ Causal gate: OFF    │  │ Coverage: 89.4%     │  │ Killswitch:  OK │  │
│  └─────────────────────┘  └─────────────────────┘  └─────────────────┘  │
│                                                                          │
│  ┌─ MIRROR (R13 calibration) ──────────────────────────────────────────┐ │
│  │ follow_confidence │ Brier 0.043 │ z 0.2  │ enabled  ←protected     │ │
│  │ volume_forecast   │ MAPE  —     │ z —    │ enabled  ←R9 + R10      │ │
│  │ causal_ate        │ —           │ z —    │ enabled  ←needs gate    │ │
│  │ strategy_class    │ log 0.51    │ z 1.1  │ enabled  ←R8            │ │
│  │ Auto-disabled: 0   Manual disabled: 0   Last batch: 04:30 UTC      │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
├─ FOOTER ────────────────────────────────────────────────────────────────┤
│  Recent operator actions (last 5)   |   Next nightly batch: 04:30 UTC    │
└─────────────────────────────────────────────────────────────────────────┘
```

**Headline pattern**: brain / eyes / hands / mirror — maps to the
four R6→R13 architectural layers (intelligence / data / execution /
self-observation).

---

### 5.2 INTELLIGENCE (refonde of ML PROGRESSION, sub-tabs)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ INTELLIGENCE   [Maturity] [Lens] [Web] [Causal]                          │
├─ KPI STRIP (varies per sub-tab) ────────────────────────────────────────┤
```

#### 5.2.1 Sub-tab "Maturity" — existing ML PROGRESSION

Kept identical. Profile phase distribution + learning trajectory +
Top 6 by velocity + close-method breakdown.

#### 5.2.2 Sub-tab "Lens" — R8 Strategy Classifier

```
KPI: PROFILES CLASSIFIED │ TRAINED CLASSES │ COHEN'S κ │ DRIFT ALERTS

┌─ CLASS DISTRIBUTION (last 30d) ─────────────────────────────────────┐
│ directional ████████████░░ 314                                       │
│ momentum    ████░░░░░░░░░░  62                                       │
│ contrarian  ██░░░░░░░░░░░░  28                                       │
│ arb_2way    ░░░░░░░░░░░░░░   3                                       │
│ market_maker█░░░░░░░░░░░░░   7                                       │
│ ...                                                                  │
└──────────────────────────────────────────────────────────────────────┘

┌─ DRIFT HEATMAP (leader × 7d) ─────────────────────┐ ┌─ CLUSTER SCATTER ─┐
│           Mon  Tue  Wed  Thu  Fri  Sat  Sun        │ │  K-means + DBSCAN │
│ 0x32b4…   ░░   ░░   ▒▒   ▒▒   ░░   ░░   ░░         │ │  (PCA-2D)         │
│ 0x1838…   ░░   ░░   ░░   ░░   ░░   ░░   ░░         │ │  • directional    │
│ 0xa87d…   ░░   ░░   ░░   ▓▓   ▓▓   ░░   ░░  ⚠      │ │  • momentum       │
│ ...                                                │ │  • candidate #1   │
└────────────────────────────────────────────────────┘ └───────────────────┘

┌─ LABELLING WORKSPACE (operator) ─────────────────────────────────────┐
│ Wallet         Suggested  Confidence  Your label   Rationale        │
│ 0x32b4…8b21    directional  0.84      [select…  ▾]  [textarea]      │
│ 0xaab9…a08d    directional  0.76      [select…  ▾]  [textarea]      │
│ ...                                                                  │
│ [SAVE LABEL]  [REQUEST 2ND LABELLER]                                 │
└──────────────────────────────────────────────────────────────────────┘

GATED: strategy_conditional_confidence_enabled = OFF
  → flip in OPERATIONS / Risk & Config to apply STRATEGY_WEIGHTS to FOLLOW/FADE
```

#### 5.2.3 Sub-tab "Web" — R9 Hawkes + Kalman

```
KPI: ACTIVE FITS │ ACCEPTED COUPLINGS │ KALMAN UPDATES 24h │ FORECASTS 24h

┌─ α-MATRIX HEATMAP (leader: 0x32b4…) ─────────────────────────────────┐
│            leader  directional momentum  contrarian  social_driven   │
│  leader     0.00       —          —          —             —          │
│  direct.    0.14    [0.08]        0           0             0         │
│  momentum   0.22       0       [0.04]         0             0         │
│  contr.     0.03       0          0        [0.02]           0         │
│  social     0.00       0          0           0          [0.01]       │
│  β shared: 0.0023  | BIC stat: 8945.2 vs threshold 35.2 (CONVERGED)  │
└──────────────────────────────────────────────────────────────────────┘

┌─ FOLLOWER POOL KALMAN (leader: 0x32b4…) ─────┐ ┌─ VOLUME FORECAST ───┐
│ pool         size_usdc  resp%  decay   n_obs │ │  total: $14,200      │
│ directional  $42,300    18.2%  0.034   142   │ │  95% CI: [$8.2k–$22k]│
│ momentum     $11,800     6.4%  0.061    52   │ │  by_pool:            │
│ contrarian   $3,200      3.1%  0.028     8   │ │   directional 58%    │
│ social       $480        2.0%  0.010     2   │ │   momentum    32%    │
└──────────────────────────────────────────────┘ │   contrarian   8%    │
                                                 │   social       2%    │
                                                 │  time CDF:           │
                                                 │   0-5min  40%        │
                                                 │   5-15min 35%        │
                                                 │   15-30   20%        │
                                                 │   30-60    5%        │
                                                 └──────────────────────┘

GATED: volume_anticipation_enabled = OFF
  → flip in OPERATIONS to allow new "volume_anticipation" decision policy
```

#### 5.2.4 Sub-tab "Causal" — R10 Truth Test

```
KPI: ESTIMATES 24h │ Wu-H p<0.05 RATE │ FIRST-STAGE F>10 │ DISAGREE/ALPHA

┌─ THE KEYSTONE PLOT: IV ATE vs Hawkes α/μ ───────────────────────────┐
│                                                                       │
│  causal_ate                                                           │
│    ↑                                                                  │
│  3 ┤              ◇  ◇  → identity (causal == correlation)            │
│  2 ┤        ◇  ◇  /                                                   │
│  1 ┤     ◇  /                                                         │
│  0 ┤◇  ◇                  ← high α, near-zero ATE = NEWS CONFOUNDING  │
│ -1 ┤  ◇                                                               │
│    └────────────────────────────────────────────────→ hawkes_alpha_mu │
│       0    1    2    3                                                │
│  Each point: (leader, pool_class). Hover for details.                 │
└───────────────────────────────────────────────────────────────────────┘

┌─ WU-HAUSMAN p DISTRIBUTION ─────────┐ ┌─ FIRST-STAGE F DISTRIBUTION ─┐
│   ▓▓▓                                │ │       ▓▓                     │
│   ▓▓▓ ▓▓                             │ │     ▓▓▓▓▓                    │
│   ▓▓▓▓▓▓ ▓                           │ │  ▓▓▓▓▓▓▓▓ ▓                  │
│   0   0.05  0.1  ...  1.0           │ │  0   10   100   1000          │
│   target: > 70% at p < 0.05          │ │  target: > 80% at F > 10     │
└──────────────────────────────────────┘ └──────────────────────────────┘

┌─ INSTRUMENT EVENT TIMELINE (last 24h) ───────────────────────────────┐
│ news_event       ●●  ●     ●●●         ●                              │
│ oracle_update    ●     ●●●        ●●                                  │
│ api_outage       ─────●─────────                                      │
│ gas_quirk        ●  ●  ●  ●  ●                                        │
│ funding          (reserved — future)                                  │
└──────────────────────────────────────────────────────────────────────┘

GATED + METHODOLOGY-AUDIT BANNER:
  causal_gating_enabled = OFF
  ⚠ Methodology audit pending (1 week external causal-inference expert)
  See docs/audit/phase3/round10_wave3_review.md § 11 for audit prep.
```

---

### 5.3 WALLET LAB (refonde of WALLET GRAPH, sub-tabs)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ WALLET LAB   [Universe] [Graph] [Scanner] [Profile]                      │
```

#### 5.3.1 Sub-tab "Universe" — R6

```
KPI: UNIVERSE SIZE │ TIER-0 (whales) │ TIER-1 (top) │ TIER-2 (depth) │ LAST CRAWL

┌─ FILTERS ────────────────────────────────────────────────────────────┐
│ Tier: [All ▾]  Strategy: [All 9 ▾]  Active 7d: [☑]  Falcon: [—]      │
└──────────────────────────────────────────────────────────────────────┘

┌─ WALLET TABLE (sortable, 2000+ rows, paginated) ─────────────────────┐
│ Wallet      Tier  Strategy   Conf.  Vol 30d   Trades  Last seen      │
│ 0x32b4…     0     direct.    0.84  $1.2M     412      11:13:55       │
│ 0xaab9…     0     momentum   0.76  $890K     308      11:08:12       │
│ 0xa87d…     1     contrar.   0.62  $234K     142      10:57:30       │
│ ...                                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

#### 5.3.2 Sub-tab "Profile" (per-wallet drill-down)

```
─── 0x32b4…8b21  ────────────────────────────────────────────────────────
KPI: TIER │ STRATEGY (R8) │ DRIFT z │ VOL 30d │ POSITIONS RESOLVED │ Win%

┌─ STRATEGY FINGERPRINT (R8) ──────────────────────────────────────────┐
│ directional ████████████████  84%                                      │
│ momentum    ████░░░░░░░░░░░░  12%                                      │
│ contrarian  █░░░░░░░░░░░░░░░   3%                                      │
│ ...                                                                   │
│ Last trained: 2026-05-10 23:30  |  History: ── stable for 30d         │
└──────────────────────────────────────────────────────────────────────┘

┌─ MICROSTRUCTURE SIGNATURE (R11) ──┐ ┌─ HAWKES COUPLING (R9) ───────┐
│ cancel_to_fill_30d    0.18         │ │ → directional pool   α=0.08  │
│ place_to_fill_p50_s   2.4          │ │ → momentum pool      α=0.04  │
│ place_to_fill_p99_s   42.1         │ │ → social pool        α=0.01  │
│ iceberg_score_30d     0.02         │ │ BIC accepted: 2 of 4 pools   │
│ spoof_score_30d       0.00         │ │ Coupling drift: stable        │
│ n_orders_30d          1,402        │ └──────────────────────────────┘
└────────────────────────────────────┘

┌─ CAUSAL ESTIMATES (R10) per pool ────────────────────────────────────┐
│ pool          hawkes α/μ   IV ATE   95% CI       Wu-H p   F-stat    │
│ directional   1.42        1.38     [0.92, 1.84]   0.012   142.3     │
│ momentum      0.84        0.21     [-0.18, 0.60]  0.681     8.2 ⚠   │
│ contrarian    0.32        —        —              —        —        │
└──────────────────────────────────────────────────────────────────────┘

┌─ SOCIAL SIGNAL (R12) ──────────────┐ ┌─ CROSS-MARKET (R12) ─────────┐
│ Tweets 30d: 142 (X)                │ │ Resolved venues:              │
│ Intent split: entry 22% / exit 8%  │ │  • polymarket 0x32b4…         │
│  / noise 70%                       │ │  • kalshi    @anon_42        │
│ Tweet→trade lag median: -14m        │ │  • manifold  conf 0.8 pending│
│ Concordance: 0.71                  │ │ Cross-venue correlation: 0.42 │
└────────────────────────────────────┘ └──────────────────────────────┘
```

---

### 5.4 MEMPOOL (NEW — R7 front door)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ MEMPOOL   [Live] [Prefill Pool] [Decisions]                              │
├─ KPI STRIP ─────────────────────────────────────────────────────────────┤
│ INTENT/MIN │ DECODE HIT% │ POOL SIZE │ POOL FRESH% │ INTENT→FIRE p50    │
│   12.4     │   94.2%     │   38/40   │    88%      │     147 ms        │
│ SHADOW FIRES│ LIVE FIRES  │ ROUTER ERR│ NONCE CHAINS                    │
│    4        │    0        │    0      │    1 active                     │
└─────────────────────────────────────────────────────────────────────────┘

┌─ LIVE INTENT FEED (last 100) ────────────────────────────────────────┐
│ T-1.2s  0x32b4… BUY 1000 USDC @ 0.87  Trump 2024  GTC  decoded ✓     │
│ T-3.4s  0xaab9… SEL  450 USDC @ 0.42  Trump CN    GTC  decoded ✓     │
│ T-8.1s  0xa87d… BUY 1800 USDC @ 0.62  Crypto ETF  GTC  not_clob      │
│ ...                                                                   │
└──────────────────────────────────────────────────────────────────────┘

┌─ NONCE REPLACEMENT CHAINS (live) ────────────────────────────────────┐
│ 0xaab9…  nonce 42  →  0xtx_a (50 gwei) → 0xtx_b (75 gwei) → MINED   │
│          intent replaces=0xtx_a in latest                            │
└──────────────────────────────────────────────────────────────────────┘

┌─ PREFILL POOL INVENTORY ─────────────────────────────────────────────┐
│ Market           Token  Side  Bucket    Age      Status              │
│ Trump 2024       YES    buy   $500       4.2s    ready                │
│ Trump 2024       YES    buy   $1000      8.1s    ready                │
│ Trump 2024       YES    buy   $2500     12.4s    ready                │
│ Trump 2024       NO     sell  $500       4.0s    ready                │
│ ...                                                                   │
│ Miss reasons last hour: no_bucket_fit 14 | all_expired 3 | …          │
└──────────────────────────────────────────────────────────────────────┘

┌─ INTENT ROUTER DECISIONS ───────────────────────────────────────────┐
│ T-12s   0x32b4…  Trump 2024  →  shadow_fired (cost=0.87, size=$500)  │
│ T-31s   0xaab9…  Trump CN    →  cooldown (within 300s)               │
│ T-1m4s  0xa87d…  Crypto      →  confidence_skip (c=0.12 < 0.30)      │
│ T-2m1s  0xdd68…  Politics    →  pool_miss (no_token_match)           │
│ ...                                                                   │
│ Filter: [All ▾]                                                       │
└──────────────────────────────────────────────────────────────────────┘

GATED:
  prefill_live_enabled = OFF  →  shadow mode only
  → flip in OPERATIONS to enable live firing (operator gates: 30d shadow soak,
    CLOBClientWrapper sign+submit split, p50 < 250ms verified)
```

---

### 5.5 MICROSCOPE (NEW — R11 L3 book)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ MICROSCOPE   [Firehose] [Microstructure] [Wallet Signatures]             │
├─ KPI STRIP ─────────────────────────────────────────────────────────────┤
│ EVENTS/SEC │ QUEUE DEPTH │ DROPPED/24h │ ACTIVE PARTITIONS │ STORAGE   │
│    832     │   12k/50k   │    0        │   24 hourly       │  324 GB   │
│ ICEBERG/HR │ SPOOF/HR    │ OFI MEAN    │ PLACE→FILL p50                │
│    18      │    2        │   +0.04     │     1.8s                       │
└─────────────────────────────────────────────────────────────────────────┘

┌─ FIREHOSE LIVE STREAM (filterable, last 200) ────────────────────────┐
│ T-0.2s  Trump 2024  YES  placed     0xord_1a  +500    0x32b4…       │
│ T-0.3s  Trump 2024  YES  cancelled  0xord_1a   -      0x32b4…       │
│ T-0.5s  Crypto ETF  NO   partial    0xord_2b  -120    0xaab9…       │
│ ...                                                                   │
│ Filter: market=Trump 2024  event=placed/cancelled                    │
└──────────────────────────────────────────────────────────────────────┘

┌─ MICROSTRUCTURE PER MARKET (top 20 by volume) ───────────────────────┐
│ Market         Iceberg  Spoof  OFI mean  OFI max  Cancel-to-fill    │
│ Trump 2024     14       0      +0.08     +0.42    1.8                │
│ Trump CN       8        1      -0.02     -0.18    2.4                │
│ Crypto ETF     2        0      +0.01     +0.04    1.2                │
│ ...                                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

---

### 5.6 PERIPHERY (NEW — R12 social + cross-market + R10 instruments)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ PERIPHERY   [Social Feed] [Cross-Market] [Resolution] [Instruments]      │
├─ KPI STRIP ─────────────────────────────────────────────────────────────┤
│ TWEETS 24h │ ENTRY/EXIT %│ X QUOTA % │ CROSS-MKT OPS │ PENDING REVIEW   │
│    234     │    18% / 6% │    72%    │      14       │       3          │
│ KALSHI │ MANIFOLD │ PREDICTIT  │ INSTRUMENT EVENTS 24h                  │
│  ✓     │   ✓      │   ✓        │    142                                 │
└─────────────────────────────────────────────────────────────────────────┘

┌─ SOCIAL FEED (X / Telegram / Discord) ────────────────────────────────┐
│ T-2m   X  @leader_42  "just entered YES on Trump 2024 🚀"             │
│         intent=entry  conf=0.87  market=Trump 2024  direction=yes      │
│ T-8m   X  @anon_x    "lol the dump tho"                                │
│         intent=noise  conf=0.94                                        │
│ T-14m  TG #signals   "TP hit, closing out"                             │
│         intent=exit   conf=0.71                                        │
│ ...                                                                    │
│ Filter: [All ▾] handle [—] market [—]                                  │
└──────────────────────────────────────────────────────────────────────┘

┌─ CROSS-MARKET OPERATORS ───────────────────────────────────────────────┐
│ Operator       Polymarket    Kalshi      Manifold    Confidence       │
│ Op#1   manual   0x32b4…       @anon_42    —           1.00            │
│ Op#5   profile  0xaab9…       @kal_88    @mani_42     0.94            │
│ Op#8   finger.  0xa87d…       —           —           0.62 pending    │
│ ...                                                                    │
│ [REVIEW PENDING (3)]                                                    │
└──────────────────────────────────────────────────────────────────────┘

┌─ INSTRUMENT EVENT TIMELINE (R10) ────────────────────────────────────┐
│ news_event       ●●  ●     ●●●         ●                              │
│ oracle_update    ●     ●●●        ●●                                  │
│ api_outage       ─────●─────────                                      │
│ gas_quirk        ●  ●  ●  ●  ●                                        │
└──────────────────────────────────────────────────────────────────────┘

REQUIRES (operator setup): X API key, Kalshi key, NLP-classifier model,
manual seed of ≥10 cross-market operators.
```

---

### 5.7 EXECUTION (merge of Portfolio + Decisions + Inspector)

Top-level: shared KPI strip + sub-tab nav.

```
KPI: OPEN │ FILLED 24h │ SHADOW 24h │ DECISIONS/HR │ ACTIONABLE │ NET PnL

  Sub-tabs: [Portfolio] [Decisions] [Inspector]
```

- **Portfolio**: existing LIVE PORTFOLIO unchanged.
- **Decisions**: existing DECISION ENGINE + R7 intent-router decisions
  (filter `source=intent_router` vs `source=confidence_engine`) + R13
  prediction drill-down on every decision (click → see all model
  predictions at decision time).
- **Inspector**: existing INSPECTOR + R10 causal gate decisions in the
  "Recent decisions" panel (filter `gate=causal`).

---

### 5.8 OPERATIONS (merge of Risk + Health + new Calibration + Research)

```
┌─ HEADER ────────────────────────────────────────────────────────────────┐
│ OPERATIONS   [Risk & Config] [Health] [Calibration] [Research]           │
```

#### 5.8.1 Risk & Config (existing — extended)

Add toggles for the four new gated flags:
- `strategy_conditional_confidence_enabled` (R8)
- `volume_anticipation_enabled` (R9)
- `causal_gating_enabled` (R10, with methodology-audit banner)
- `prefill_live_enabled` (R7, with operator-only gates banner)

#### 5.8.2 Health (existing BOT HEALTH + extensions)

Add panels for:
- R6 daemon registry (per-daemon last heartbeat, restart count)
- R6 cold-tier (last export ts, row counts, disk usage)
- R6 RPC providers (rotation status, quota remaining)
- R11 partition state (24 hourly active, retention horizon)
- R12 social/crossmarket daemon status

#### 5.8.3 Calibration (NEW — R13)

```
KPI: MODELS │ DISABLED │ DRIFT ALERTS 24h │ LAST BATCH │ BACKFILL %

┌─ PER-MODEL LOSS TRAJECTORY (30d) ───────────────────────────────────┐
│ follow_confidence    Brier  ▁▁▂▁▁▂▁▁▁▂▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁  0.043 │
│ volume_forecast      MAPE   ▁▁▁▂▂▂▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁  pending│
│ causal_ate           resid  pending — needs causal_gating_enabled    │
│ strategy_class       logL   ▂▂▂▂▂▂▂▂▂▂▂▂▂▃▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂  0.51  │
└─────────────────────────────────────────────────────────────────────┘

┌─ DRIFT GAUGES ─────────────────────────────────────────────────────┐
│ follow_confidence  z=  0.2  ░░░░|░░░░  threshold ±2.0  protected   │
│ volume_forecast    z=  —    not yet baseline (n<3)                  │
│ causal_ate         z=  —    gated OFF                              │
│ strategy_class     z= +1.1  ░░░░░░|▓░░  threshold ±2.0             │
└─────────────────────────────────────────────────────────────────────┘

┌─ AUTO-DISABLED MODELS ─────────────────────────────────────────────┐
│ (none currently)                                                    │
│                                                                     │
│ Last events:                                                        │
│   2026-04-22  causal_ate  auto-disabled  drift z=+2.8 for 3d        │
│              ↑ re-enabled by operator 2026-04-25                    │
└─────────────────────────────────────────────────────────────────────┘

⚠ DEFERRED HOOK (audit § 4.A): engine + position_tracker hooks not yet
   wired. The calibration daemon runs but receives 0 predictions.
   Apply pseudo-diff from round13_wave3_review.md to activate.
```

#### 5.8.4 Research (NEW — R13 § 3.5 notebooks + counterfactual)

```
┌─ NOTEBOOK TILES ────────────────────────────────────────────────────┐
│ ┌─ 00_data_loader ──────┐  ┌─ 01_strategy_validation ────┐         │
│ │ DuckDB views over     │  │ R8 vs hand labels disagree  │         │
│ │ cold tier             │  │ surface                     │         │
│ │ last run: 4h ago      │  │ last run: 2d ago            │         │
│ │ [run] [open jupyter]  │  │ [run] [open jupyter]        │         │
│ └───────────────────────┘  └─────────────────────────────┘         │
│ ┌─ 02_causal_analysis ──┐  ┌─ 03_counterfactual_replay ──┐         │
│ │ IV vs Hawkes diverge  │  │ INTERACTIVE: what if we'd   │         │
│ │ surface               │  │ disabled volume_anticipation │         │
│ │ last run: never       │  │ over April?                 │         │
│ │ [run] [open jupyter]  │  │ [launch UI →]               │         │
│ └───────────────────────┘  └─────────────────────────────┘         │
│ ┌─ 04_what_if_explorer ─┐  ┌─ 05_calibration_review ─────┐         │
│ │ Per-hypothesis sandbox │  │ Drift trajectories + auto-  │         │
│ │ last run: 1w ago       │  │ disable triage              │         │
│ │ [run] [open jupyter]  │  │ last run: 4h ago            │         │
│ └───────────────────────┘  └─────────────────────────────┘         │
└──────────────────────────────────────────────────────────────────────┘

┌─ COUNTERFACTUAL REPLAY LAUNCHER ────────────────────────────────────┐
│ Replay window: [2026-04-11 → 2026-05-11]                            │
│ Policy override: [Disable causal_gating ▾]                          │
│ Confidence threshold override: [0.30 ▾]                             │
│ [▶ RUN REPLAY]                                                       │
│                                                                      │
│ Last result: hypothetical PnL +$847.32 vs actual +$0.00              │
│              decisions changed: 14 | wall time: 3m 42s               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Shared components catalogue

| Component | Reuse points | Existing? |
|---|---|---|
| `KpiStrip` | every tab header | Yes — extract from existing tabs |
| `StatusPill` (variant: ok / degraded / off / gated) | global | Yes — extract |
| `BreadcrumbHeader` | every tab | Yes — extract |
| `AuditLogTable` | Risk, Calibration, Resolution | Yes — extract from Risk |
| `GateToggle` (with banner) | Operations, Mempool, Intelligence/Causal | NEW |
| `MissingHookBanner` | Operations/Calibration | NEW |
| `MethodologyAuditBanner` | Intelligence/Causal | NEW |
| `WalletCell` (sortable, deep-linkable) | Wallet Lab, Mempool, Inspector | Yes — extract |
| `MarketCell` | Wallet Lab/Profile, Microscope, Execution | Yes |
| `SparklineCell` | Maturity, Lens, Calibration | Yes |
| `HeatmapMatrix` (α-matrix, drift heatmap) | Intelligence/Web, Intelligence/Lens | NEW |
| `ScatterPlot` (IV vs Hawkes, cluster explorer) | Intelligence/Causal, Intelligence/Lens | NEW |
| `EventTimelineTrack` (instruments, social) | Periphery | NEW |
| `LiveStreamFeed` (intent, firehose, social) | Mempool, Microscope, Periphery | NEW |
| `NotebookTile` | Operations/Research | NEW |
| `CrossMarketOperatorCard` | Periphery | NEW |
| `StrategyFingerprintBar` | Wallet Lab/Profile, Intelligence/Lens | NEW |
| `LabellingForm` (operator input) | Intelligence/Lens | NEW |

**New components to write**: ~12. Most reuse the table / sparkline /
KPI patterns already present. None require a charting library beyond
what's already loaded (the existing dashboard uses Chart.js).

---

## 7. Implementation roadmap

### 7.1 MVP cut — Priority 1 (ships in one PR cycle)

Goal: visible R6→R13 surface, even if some panels are minimal.

| # | Tab / Sub-tab | Effort | Why P1 |
|---|---|---|---|
| 1 | OVERVIEW refonde (brain/eyes/hands/mirror layout) | M | Single biggest UX win — operator's home page |
| 2 | OPERATIONS / Calibration (R13 surface) | M | The auto-disable + drift surface is critical operationally |
| 3 | OPERATIONS / Risk extension (4 new gate toggles) | S | One-line addition per toggle |
| 4 | MEMPOOL tab (live feed + pool + decisions) | L | New top-level; spec required |
| 5 | INTELLIGENCE / Lens (R8 fingerprint + label workspace) | L | Spec acceptance gate |

### 7.2 Polish — Priority 2 (second cycle)

| # | Tab / Sub-tab | Effort |
|---|---|---|
| 6 | INTELLIGENCE / Web (R9 Hawkes heatmap + Kalman card + volume forecast) | L |
| 7 | INTELLIGENCE / Causal (the keystone IV vs Hawkes scatter) | M |
| 8 | WALLET LAB refonde (Universe + Profile augmentation) | L |
| 9 | MICROSCOPE tab (firehose + microstructure) | M |
| 10 | PERIPHERY tab (social feed + cross-market) | L |
| 11 | OPERATIONS / Health extensions (R6 daemons + cold tier) | S |

### 7.3 Research-tier — Priority 3 (third cycle, operator-driven)

| # | Tab / Sub-tab | Effort |
|---|---|---|
| 12 | OPERATIONS / Research (notebook launcher + counterfactual UI) | M |
| 13 | INTELLIGENCE / Lens — cluster explorer + drift heatmap | M |
| 14 | INTELLIGENCE / Causal — instrument timeline + Wu-H histogram | S |
| 15 | PERIPHERY — wallet resolver "pending review" workflow | M |

### 7.4 Effort legend

| Symbol | Meaning |
|---|---|
| S | < 1 day of frontend work |
| M | 1–3 days |
| L | 3–7 days |

Total estimated frontend effort, MVP through P3: **~6 weeks** of
focused single-dev work, or **~3 weeks** with 2 devs (operations +
intelligence can parallelise).

---

## 8. API gaps to close

The new UI requires new backend endpoints. Per-tab inventory:

| Endpoint | Round | Returns |
|---|---|---|
| `GET /api/mempool/live` | R7 | last N LeaderIntent + nonce chains |
| `GET /api/mempool/pool` | R7 | PreSignedPool inventory |
| `GET /api/mempool/decisions` | R7 | IntentRouter decision log (filterable) |
| `GET /api/intelligence/lens/distribution` | R8 | strategy class counts |
| `GET /api/intelligence/lens/drift` | R8 | per-wallet drift heatmap |
| `POST /api/intelligence/lens/label` | R8 | operator labels a wallet |
| `GET /api/intelligence/web/alpha/{leader}` | R9 | α-matrix for one leader |
| `GET /api/intelligence/web/forecast/{leader}` | R9 | volume forecast |
| `GET /api/intelligence/causal/scatter` | R10 | IV ATE vs Hawkes α/μ points |
| `GET /api/intelligence/causal/instruments` | R10 | instrumental_events timeline |
| `POST /api/research/counterfactual` | R10 | trigger CounterfactualReplayer |
| `GET /api/microscope/firehose` | R11 | streaming WS of book events (use existing WS bridge) |
| `GET /api/microscope/microstructure` | R11 | per-market rollup |
| `GET /api/wallet/{w}/microstructure` | R11 | per-wallet signature |
| `GET /api/periphery/social/feed` | R12 | tweet stream |
| `GET /api/periphery/crossmarket/operators` | R12 | resolved + pending operators |
| `POST /api/periphery/crossmarket/confirm/{op}` | R12 | operator confirms resolution |
| `GET /api/calibration/losses` | R13 | per-model loss history |
| `GET /api/calibration/drift` | R13 | drift z-scores |
| `GET /api/calibration/disabled` | R13 | currently-disabled models |
| `POST /api/calibration/disable/{model}` | R13 | manual disable |
| `POST /api/calibration/enable/{model}` | R13 | manual enable |

**Total new endpoints**: ~22. Most are thin SQL-to-JSON wrappers
over already-shipped tables. Streaming endpoints (mempool live,
microscope firehose, social feed) reuse the existing `ws_bridge.py`
infrastructure.

---

## 9. Migration strategy (preserve operator muscle memory)

1. **Keep the 8-tab visual layout** (left sidebar, same order, same
   icons). The operator's eye doesn't have to relearn.
2. **Rename labels gradually**: ship the new tabs side-by-side with the
   old ones for one release cycle (feature flag), then swap.
3. **Sub-tab introduction**: new sub-tabs default to the first one
   (which is the existing surface for EXECUTION and OPERATIONS).
   Operator can stay on familiar territory unless they click.
4. **In-app "what's new" badge** on tabs that gained new content.
   Auto-dismisses after first interaction.
5. **Documentation**: a one-page tour at `/docs/dashboard-tour.md`
   keyed by tab.

---

## 10. Open questions for the operator

| # | Question | Default if no answer |
|---|---|---|
| Q1 | Should EXECUTION be one tab with sub-tabs, or stay as three separate top-level tabs? | One tab with sub-tabs (cleaner) |
| Q2 | Should the Lens labelling workspace live in-app or stay Jupyter-only? | In-app (faster operator iteration) |
| Q3 | Counterfactual replay: in-app UI or notebook-only? | In-app UI for fast presets, notebook for full freedom |
| Q4 | Drift z-score gauges: show all 4 models always, or only the ones with non-trivial values? | Show all 4 (consistency) |
| Q5 | Cross-market operator resolution: auto-confirm above confidence threshold, or always pending-review? | Always pending-review (manual-in-the-loop per spec) |
| Q6 | Mempool live feed: paginated table or streaming auto-scroll? | Streaming with pause-on-hover |

---

## 11. The single sentence

> **The Phase-3 dashboard exposes the bot's intelligence layers
> (Lens / Web / Causal / Mirror), data layers (Universe / Mempool /
> Microscope / Periphery), execution surface, and operations cockpit
> as a coherent 8-tab navigation — preserving operator muscle memory
> while surfacing every R6→R13 capability behind a clear gated /
> protected / deferred semantics.**

---

## Appendix A — Tab → Component mapping

```jsx
// dashboard-app.jsx (proposed)
const TABS = [
  { id: 'overview',     label: 'OVERVIEW',     icon: '◈', component: Overview },
  { id: 'intelligence', label: 'INTELLIGENCE', icon: '◍', component: Intelligence,
    subTabs: ['maturity', 'lens', 'web', 'causal'] },
  { id: 'wallet',       label: 'WALLET LAB',   icon: '⬢', component: WalletLab,
    subTabs: ['universe', 'graph', 'scanner', 'profile'] },
  { id: 'mempool',      label: 'MEMPOOL',      icon: '⧉', component: Mempool,
    subTabs: ['live', 'pool', 'decisions'] },
  { id: 'microscope',   label: 'MICROSCOPE',   icon: '◯', component: Microscope,
    subTabs: ['firehose', 'microstructure', 'signatures'] },
  { id: 'periphery',    label: 'PERIPHERY',    icon: '⌖', component: Periphery,
    subTabs: ['social', 'crossmarket', 'resolution', 'instruments'] },
  { id: 'execution',    label: 'EXECUTION',    icon: '✦', component: Execution,
    subTabs: ['portfolio', 'decisions', 'inspector'] },
  { id: 'operations',   label: 'OPERATIONS',   icon: '◆', component: Operations,
    subTabs: ['risk', 'health', 'calibration', 'research'] },
];
```

Same length array. Same icon vocabulary. The component file list
roughly mirrors today's structure (component-per-tab + sub-component-
per-sub-tab) under `static/dashboard/tabs/`.

---

## 12. v2 — Post-skill audit (self-critique iteration)

This section was added after running the `ui-ux-pro-max` design-
intelligence skill against the v1 proposal above. The skill validated
80 % of the v1 design but surfaced **14 concrete gaps** that the v1
doc missed, plus **3 stance revisions** on previously open questions.

### 12.1 What the skill validated (confidence-building)

| v1 decision | Skill style match | Verdict |
|---|---|---|
| Dark terminal aesthetic | "Dark Mode (OLED)" — WCAG AAA, Excellent perf | ✓ keep |
| KPI strips + dense tables + 8 px grid gap | "Data-Dense Dashboard" — exact match incl. design tokens | ✓ keep + adopt skill's specific tokens |
| Real-time live feeds (Mempool, Microscope, Periphery) | "Real-Time Monitoring" style | ✓ keep |
| Drill-down on wallet profile | "Drill-Down Analytics" style | ✓ keep |
| α-matrix heatmap, IV-vs-Hawkes scatter, calibration line charts | Chart-type matches exactly (Heat Map / Scatter / Line) | ✓ keep |
| Volume forecast = "Line with Confidence Band" | Exact chart-type match | ✓ keep + adopt specific color guidance |

### 12.2 v1 gaps surfaced by the skill (14 items)

#### A. Accessibility gaps — CRITICAL severity

| # | Gap | Fix |
|---|---|---|
| A1 | **Color-only status indicators** ("Color Only — HIGH severity" UX rule) — v1 still uses ONLY green/red/yellow for RUNNING / STOPPED / DEGRADED. | Add icons/symbols: `●` running, `○` stopped, `⚠` degraded, `▲` rising, `▼` falling. Keep color but never as the sole signal. |
| A2 | **Heatmap pattern overlays for colorblind** — skill flagged heatmaps ⚠ colorblind-limited. v1 α-matrix + drift heatmap rely on color gradient alone. | Add subtle pattern overlay (diagonal hatching at 25/50/75 % bins). Provide a "show as table" toggle on every heatmap. |
| A3 | **Scatter data-table alternative** — IV-vs-Hawkes scatter is the keystone plot but unreadable for screen-reader users. | "show as table" toggle next to the scatter; opens a sortable list of (leader, pool, hawkes_α/μ, IV_ATE, CI_low, CI_high, Wu-Hausman p, F-stat). |
| A4 | **Pause button on streaming feeds** — "⚠ Flashing elements — provide pause button" was explicit. | Add a pause/play toggle to every live feed (Mempool live, Microscope firehose, Social feed). When paused, freeze the view and queue incoming events with a `+N new` button to release. |
| A5 | **ARIA live regions for dynamic content** — KPI counters update silently for screen readers. | Wrap every auto-updating KPI value in `<span aria-live="polite">`. The "intent feed" + "decision log" lists get `aria-live="polite"` on the container, NOT on each child. |
| A6 | **Reduced motion preference** — v1 didn't pin the `@media (prefers-reduced-motion: reduce)` query. | Single shared utility class `.motion-safe-only { transition: …; @media (prefers-reduced-motion: reduce) { transition: none; } }`. Apply to ALL transitions globally. |

#### B. Information design gaps

| # | Gap | Fix |
|---|---|---|
| B1 | **Skeleton screens for async data** — v1 mentions spinners but not skeletons. | Each tab declares its skeleton shape — KPI strip + 1–3 panel placeholders. Render skeletons within 100 ms of mount; never show a blank state. |
| B2 | **Loading state contract per data fetch** — `<KpiCard loading />` and `<DataTable loading />` props standardised. | Add `loading: boolean` prop to every component in the catalogue. Component renders its own skeleton when `loading={true}`. |
| B3 | **Bulk actions in labelling workspace** — v1 shows one-row-at-a-time labelling. | Multi-select checkbox column + "label N wallets as ___" action bar. Operator can label a cluster of similar wallets in one go. |
| B4 | **"What changed" timeline on Overview** — common operator-dashboard pattern (Sentry, Datadog) — surfaces recent state changes. | Top-of-Overview component: last 5 events of type {decision, intent, gate-toggle, model-disable, drift-alert}, with timestamp + click-to-tab deep link. |

#### C. Visual design tokens (missing in v1)

The skill returned exact Data-Dense Dashboard design-system variables.
v1 left these implicit. v2 pins them:

```css
:root {
  /* Layout (skill-recommended Data-Dense Dashboard variables) */
  --grid-gap: 8px;
  --card-padding: 12px;
  --section-padding: 16px;
  --tab-header-height: 56px;
  --kpi-strip-height: 88px;
  --sidebar-width: 240px;
  --table-row-height: 36px;

  /* Typography (Fira Code + Fira Sans — skill-recommended "Dashboard Data" pairing) */
  --font-mono: 'Fira Code', ui-monospace, 'SF Mono', Menlo, monospace;
  --font-sans: 'Fira Sans', system-ui, sans-serif;
  --font-size-xs: 11px;     /* KPI labels, timestamps */
  --font-size-sm: 12px;     /* Table cells, body */
  --font-size-md: 14px;     /* Sub-tab labels */
  --font-size-lg: 18px;     /* KPI values */
  --font-size-xl: 24px;     /* Tab headers */
  --line-height-body: 1.5;
  --line-height-tight: 1.2; /* KPI values, table cells */

  /* Color palette — skill-recommended Fintech/Crypto (Gold trust + purple tech) */
  --bg-page: #0F172A;            /* slate-900 — page background */
  --bg-panel: #131A2D;           /* slate-850 — card background */
  --bg-panel-elevated: #1B243D;  /* slate-800 — hover/active card */
  --border-subtle: #1E293B;      /* slate-800 borders */
  --border-strong: #334155;      /* slate-700 borders */
  --text-primary: #F8FAFC;       /* slate-50 — body text */
  --text-secondary: #94A3B8;     /* slate-400 — labels/captions */
  --text-tertiary: #64748B;      /* slate-500 — disabled/inactive */
  --accent-primary: #F59E0B;     /* amber-500 — warnings, attention */
  --accent-cta: #8B5CF6;         /* violet-500 — wallet purple, CTAs */
  --status-ok: #22C55E;          /* green-500 — RUNNING/HEALTHY */
  --status-warn: #F59E0B;        /* amber-500 — DEGRADED/PENDING */
  --status-err: #EF4444;         /* red-500 — STOPPED/FAILED/SELL */
  --status-info: #3B82F6;        /* blue-500 — BUY/INFO/LATENCY */
  --gate-off: #64748B;           /* slate-500 — gated/inactive */

  /* Motion (skill-recommended 150–300 ms for micro-interactions) */
  --duration-fast: 150ms;
  --duration-base: 200ms;
  --duration-slow: 300ms;
  --easing-enter: cubic-bezier(0.0, 0.0, 0.2, 1);  /* ease-out */
  --easing-exit:  cubic-bezier(0.4, 0.0, 1, 1);    /* ease-in */
  --easing-move:  cubic-bezier(0.4, 0.0, 0.2, 1);  /* ease-in-out */

  /* Elevation */
  --shadow-card: 0 1px 2px rgba(0,0,0,0.4);
  --shadow-panel: 0 4px 12px rgba(0,0,0,0.5);
  --shadow-modal: 0 16px 48px rgba(0,0,0,0.65);

  /* Z-index scale (skill-recommended) */
  --z-base: 1;
  --z-sticky: 10;
  --z-overlay: 20;
  --z-modal: 30;
  --z-tooltip: 50;
}
```

#### D. Chart-specific color guidance (v1 was vague)

| Chart | v1 said | v2 (skill-pinned) |
|---|---|---|
| R10 IV vs Hawkes scatter | "scatter" | Color axis gradient blue→red; opacity 0.6–0.8 to show density; provide data-table toggle |
| R8 drift heatmap | "heatmap" | Diverging palette for ±drift; pattern overlay 25/50/75 bins; numerical legend |
| R9 α-matrix | "heatmap" | Cool-to-hot gradient; legend with α scale ticks |
| R9 volume forecast | "forecast" | Actual solid `#0080FF`; forecast dashed `#FF9500`; CI band light shading; legend mandatory |
| Calibration loss trajectories | "line chart" | Single-color per model, distinct colors per model — colorblind-safe palette; pattern overlay on lines |
| Mempool live feed | "streaming" | Bright pulse `#22C55E` on incoming; fading opacity for historical entries (1.0 → 0.6 over 30 s) |

#### E. Specific anti-patterns to avoid (skill flagged)

- **No emoji icons** — v1 wireframes show characters like `◈ ◍ ⬢ ⧉` for sidebar icons. Replace with **Lucide** or **Heroicons** SVG icons (24×24 viewBox, `w-5 h-5` Tailwind sizing).
- **No `bg-white/10` glass cards** — won't read in our dark mode. Use `bg-[--bg-panel]` solid backgrounds with `border border-[--border-subtle]`.
- **No `scale-105` hover transforms** — they shift surrounding layout in dense tables. Use `bg-[--bg-panel-elevated]` on hover instead.
- **No `bg-bounce` / `animate-bounce`** — continuous animations on decorative elements are distracting per UX rule. Reserve `animate-pulse` for skeleton screens only; reserve `animate-spin` for loaders only.

### 12.3 Stance revisions on v1 open questions

| Q from v1 § 10 | v1 default | v2 stance (after skill) | Why changed |
|---|---|---|---|
| Q1: EXECUTION sub-tabs vs three top-level | sub-tabs | **sub-tabs** (confirmed) | Drill-Down Analytics style validates the breadcrumb + sub-tab pattern |
| Q2: Labelling workspace in-app vs Jupyter | in-app | **in-app** (confirmed) + **multi-select bulk action** | Bulk Actions UX rule explicitly applies; in-app iteration is faster |
| Q3: Counterfactual UI vs notebook | UI for presets, notebook for freedom | **same — both** (no change) | Skill validated drill-down + research-tier separation |
| Q4: Drift gauges — all 4 vs only non-trivial | all 4 | **all 4 always visible** (confirmed) + add icon legend (●/○/⚠) per A1 | Color-only rule reinforces |
| Q5: Cross-market resolution — auto vs pending-review | pending-review | **pending-review always** (confirmed) | Bulk action available for the operator to confirm batches |
| Q6: Mempool feed — paginated vs streaming | streaming with pause-on-hover | **streaming with explicit pause button** + `+N new` badge | A4 explicit pause requirement from skill |

### 12.4 OVERVIEW layout revision — adopt Bento Grid

The skill explicitly recommends the "Bento Grid Showcase" pattern for
modular dashboard layouts ("scannable value props, high information
density without clutter"). v1 showed the brain/eyes/hands/mirror as a
3-card row + 1 wide card. v2 adopts a 2×2 bento grid:

```
┌─ KPI STRIP (8 KPIs across the top) ────────────────────────────────────┐

┌─ Brain ─────────────────┐  ┌─ Eyes ─────────────────────┐
│ Phase + maturity + 4    │  │ R6 ingestion ✓             │
│ model status pills      │  │ R6 cold tier  ✓            │
│  (●protected / ○off /   │  │ R11 L3 book   ✓            │
│   ⚠pending)             │  │ R12 social    idle (◯)     │
│ [click → INTELLIGENCE]  │  │ Coverage  89.4 % ●         │
└─────────────────────────┘  └────────────────────────────┘

┌─ Hands ─────────────────┐  ┌─ Mirror ───────────────────┐
│ R7 mempool      ●       │  │ follow_conf  Brier 0.043   │
│ Prefill pool   38/40    │  │ vol_forecast pending —     │
│ Last fire      — (●OK)  │  │ causal_ate   gated (◯)     │
│ Shadow firing  0        │  │ strategy_cls log 0.51 ●    │
│ Live firing    ○ off    │  │ Auto-disabled 0 / Manual 0 │
│ Killswitch     ● armed  │  │ Next batch   04:30 UTC     │
│ [click → MEMPOOL]       │  │ [click → OPERATIONS/Calib] │
└─────────────────────────┘  └────────────────────────────┘

┌─ "What Changed" Timeline (last 5 events) ─────────────────┐
│ 11:14:03  ○→●  causal_gate enabled by operator            │
│ 11:08:54  4 intents detected (mempool)                    │
│ 10:42:17  0 R8 drift alerts in last hour                   │
│ 04:30:00  calibration batch — 14 predictions logged       │
│ 00:33:00  bot started (10h 43m uptime)                    │
└────────────────────────────────────────────────────────────┘
```

Each Bento card is **clickable** (cursor-pointer + `bg-panel-elevated`
on hover) and deep-links to its owning tab — operator's primary entry
point post-login.

### 12.5 Implementation prerequisites (NEW — was implicit in v1)

Before the first MVP PR ships, these need to land:

| # | Prerequisite | Owner | Effort |
|---|---|---|---|
| P1 | Inject Fira Code + Fira Sans via Google Fonts CSS in `templates/dashboard.html` | frontend | 15 min |
| P2 | Replace existing character icons (◈ ◍ ⬢ etc.) with Lucide SVG bundle (CDN or vendored) | frontend | 1 h |
| P3 | Add `:root { --* }` design tokens block to a new `static/dashboard/dashboard-tokens.css` | frontend | 30 min |
| P4 | Extract the v1 shared components (KpiStrip, StatusPill, BreadcrumbHeader, AuditLogTable, WalletCell, MarketCell, SparklineCell) into `dashboard-components.jsx` | frontend | 4 h |
| P5 | Add reduced-motion media query to `dashboard-tokens.css` | frontend | 5 min |
| P6 | Replace `transition: …` ad-hoc with the `.motion-safe-only` utility class | frontend | 1 h |
| P7 | Wire `aria-live="polite"` on all KPI value spans + the live-feed list containers | frontend | 1 h |
| P8 | Skeleton component library (one per data shape: KpiStripSkeleton, TableSkeleton, ChartSkeleton) | frontend | 2 h |

Total prerequisite effort: ~10 h. Ships in a single dedicated PR
before P1 (Overview refonde) begins.

### 12.6 Updated implementation roadmap (v2)

```
PR-0: PREREQUISITES (10 h) — fonts, icons, tokens, motion, skeletons
   ↓
PR-1: OVERVIEW refonde with bento grid + What-Changed timeline
   ↓
PR-2: OPERATIONS / Calibration (R13 surface)
   ↓
PR-3: OPERATIONS / Risk extension (4 gate toggles with banners)
   ↓
PR-4: MEMPOOL tab (with explicit pause button on live feed)
   ↓
PR-5: INTELLIGENCE / Lens (R8 + bulk-action labelling)
   ↓ (MVP cut shipped)
PR-6: INTELLIGENCE / Web (R9 — α-matrix with pattern overlay)
   ↓
PR-7: INTELLIGENCE / Causal (R10 — scatter with data-table toggle)
   ↓
PR-8: WALLET LAB refonde
   ↓
PR-9: MICROSCOPE tab
   ↓
PR-10: PERIPHERY tab
   ↓
PR-11: OPERATIONS / Health + Research notebooks
   ↓ (P2 cut shipped)
PR-12: Polish — drift heatmap, cluster explorer, instrument timeline
PR-13: Operator-onboarding — in-app tour + What's New badges
```

### 12.7 Pre-delivery accessibility checklist (NEW)

Every component PR must satisfy:

- [ ] **Color contrast ≥ 4.5:1** for text on backgrounds (tested via DevTools or `contrast.js`)
- [ ] **No color-only signals** — status always has icon + text label alongside color
- [ ] **Pattern overlay on all heatmaps + scatter color-axis** (for colorblind users)
- [ ] **Data-table toggle on every chart** (scatter, heatmap, line) — for screen-reader users
- [ ] **`aria-live="polite"`** on dynamic content containers
- [ ] **Visible focus rings** on every interactive element (`focus-visible:ring-2 ring-[--accent-cta]`)
- [ ] **Keyboard navigation** — tab order matches visual order, no keyboard traps
- [ ] **`prefers-reduced-motion`** honored — transitions disabled when set
- [ ] **Skeleton screens** show within 100 ms of mount, never blank
- [ ] **Pause button** on every streaming feed
- [ ] **Touch targets ≥ 44×44 px** (sidebar items, toggles, buttons)
- [ ] **Alt text** on all meaningful images (graphs SHOULD have aria-label describing their content)

### 12.8 The single sentence (revised)

> **The Phase-3 dashboard surfaces every R6→R13 capability as 8
> coherent tabs built on the skill-validated Data-Dense Dashboard +
> Dark-Mode-OLED + Real-Time Monitoring style triplet, with
> explicit design tokens (Fira Code/Sans, fintech gold + violet
> accents on slate-900), Bento Grid Overview, pause-able streaming
> feeds, pattern-overlaid heatmaps, data-table toggles on every
> chart, and a reduced-motion-respecting + ARIA-live-announcing +
> skeleton-first interaction model — preserving operator muscle
> memory while removing the 14 accessibility / information-design
> gaps the v1 proposal missed.**

---

## 13. Skill query log (audit trail)

These commands were run to validate / iterate the v1 proposal. Re-run
any of them on a future iteration:

```bash
# Design system (top-level)
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "fintech trading dashboard dark terminal real-time monitoring data-dense" \
  --design-system -p "Polymarket Bot Phase 3" -f markdown

# Style domain
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "dashboard data-dense terminal monospace operator" --domain style -n 5

# Chart domain
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "real-time time-series multi-panel dashboard heatmap scatter" --domain chart -n 6

# UX domain
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "dashboard accessibility data-density information-architecture animation" \
  --domain ux -n 8

# Typography
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "monospace technical dashboard analytics" --domain typography -n 4

# Color
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "fintech crypto trading dark professional" --domain color -n 4

# React stack
python3 ~/.claude/skills/ui-ux-pro-max/scripts/search.py \
  "dashboard live updates websocket polling memoization" --stack react -n 6
```

Findings synthesised into § 12 above.
