# ADR-PMK-014 — UI Terminal Mission Alignment

**Status:** Accepted — 2026-05-18
**Author:** UI audit team
**Supersedes:** none
**Amends:** CLAUDE.md §15 (Current Implementation Status), §17 (Recent changes)
**Implements:** PLAN-UIA-001

---

## Context

The May 17, 2026 ground-truth audit (see `docs/AUDIT_PAPER_TRADING_2026_05_17.md`
and memory `project_paper_trading_truth.md`) found the operator dashboard
displaying paper PnL of **+$39 784** while Gamma's actual settlement value
was **−$2 062** — a $42k lie. Two failure modes drove the gap:

1. **Phantom BTC trades** where the close audit recorded prices that never
   actually executed.
2. **Premature pre-UMA closes** where positions were closed before market
   resolution, but the inscribed PnL used a stale book quote.

The audit also confirmed that the V2 dashboard, supposed to be
*"lab gated OFF, ne pas migrer"* per memory `project_v1_vs_v2_terminal.md`,
was loaded unconditionally by `templates/dashboard.html` and rebound
`window.LivePortfolio` + `window.WalletGraph` on cold load. V2 reads from a
parallel `/api/portfolio/*` compute path — two paths to the same number =
two ways to drift from ground truth.

A subsequent UI audit (the call that produced this ADR) enumerated 18
mission-alignment gaps: V2 not gated, no reconciliation surface, dishonest
controls, dead code, missing 5-pillars gauge, no category risk flags, PnL
ranked above the actual mission KPI (win rate, 28% → 70%+).

---

## Decision

We adopt **ten sub-decisions**, documented in turn. The aggregate effect
is summarised at the end.

### ADR-PMK-014.1 — V2 stays in tree, gated by `localStorage.poybot_v2_lab`

V2 represents real engineering work (~3 100 LOC of TradingView + Cosmograph
integrations). Deleting it discards recovery potential. But the memory anchor
"lab gated OFF" must be true by default.

**Decision.** `templates/dashboard.html` only fetches the V2 file list
when `localStorage.poybot_v2_lab === '1'`. The LAB tab provides a
`V2LabToggle` card that flips the flag (operator confirms reload).

**Alternatives considered.** (a) Delete V2 entirely — discards work, hard
to recover; (b) Runtime feature flag (Redis) — operator can't toggle
without backend; (c) Path-based routing (`?v2=1`) — survives reload but
loses on URL share.

**Consequences.** Positive: clean V1 baseline by default; recoverable lab;
CDN deps (TradingView + Cosmograph) only loaded when ON. Negative: two
render paths to maintain.

### ADR-PMK-014.2 — Reconciliation = pull + cron pre-warm

The recon endpoint joins `paper_close_divergences` × `paper_trades` and
returns a small JSON payload (verdict + sparkline). Pulled by the dashboard
when the operator opens Inspector; pre-warmed via the existing 5-min cron
that runs `scripts/reconciliation.py`.

**Alternative considered.** Push via WS bridge in the snapshot — would
inflate every snapshot payload for a value the operator only consults
when something looks off.

### ADR-PMK-014.3 — Three-state mode chip with reconciliation suffix

Sidebar replaces the binary `PAPER | LIVE` badge with a `ModeChip` that
renders `PAPER · OK | PAPER · WARN | PAPER · DRIFT | LIVE | DUAL`.
Suffix derives from `snapshot.reconciliation.verdict`. Verdict thresholds
are centralised in `src/api/reconciliation_queries.py`:

| Verdict | Delta | Color |
|---------|-------|-------|
| `ok` | <$25 | green |
| `warn` | $25–$250 | amber |
| `critical` | ≥$250 | red |
| `unknown` | no recon yet | dim |

Operator never sees an ambiguous PAPER chip without a truth signal.

### ADR-PMK-014.4 — EMERGENCY HALT = new endpoint, not killswitch relabel

Killswitch only gates NEW trades. Halt must ALSO close open positions.
Different op = different endpoint. `POST /api/control/halt` flips the
killswitch synchronously, then publishes Redis `control:halt` →
`PaperTrader._on_halt_message` → `force_close_all_positions(reason)`.

**Why not relabel?** The old UI had a dishonest "EMERGENCY KILL" button
that just flipped the killswitch — same effect as STOP/PAUSE buttons.
Operator panic + dishonest control = the worst possible combination.

### ADR-PMK-014.5 — Category risk badges centralised in dashboard-components.jsx

A single `categoryRisk(category, feeRatePct)` helper + `CategoryRiskBadge`
React component. Applied at 4 sites (Recent Trades, Decision Engine,
LivePortfolio open + history). Crypto markets → red `⚠ CRYPTO` badge.
Markets with fee > 0.5% → amber `⚠ FEE x.xx%` badge.

**Rationale.** The audit memory found phantom BTC trades. CLAUDE.md §2
notes crypto fees peak at 1.56%. Visually flagging dangerous markets
prevents the next phantom from sliding past unnoticed.

### ADR-PMK-014.6 — KPI hierarchy: Win Rate FIRST, PnL second

Mission is 28% → 70%+ win rate (memory `project_polymarket_bot.md`).
PnL is currently unreliable (memory `project_paper_trading_truth.md`).
Win rate is the truer signal during paper-trading bootstrap.

Sidebar puts Win Rate first with a target marker `/ 70%`. PnL second,
with a `⚠` glyph when reconciliation verdict is critical.

### ADR-PMK-014.7 — esbuild over Vite/webpack for JSX precompile

100× faster cold-start than webpack, zero config, single binary, no
plugin churn. Vite would be overkill for a non-SPA dashboard. The bundle
ships at `static/dashboard/dist/dashboard.bundle.js` (~160 KB minified);
build runs in ~20ms locally.

V2 lab files are NOT in the bundle — they stay runtime-fetched + Babel-
transformed so the lab-only contract is enforced at the asset layer too.

### ADR-PMK-014.8 — MarketScanner is deleted, not deprecated

Removed from nav 2026-05-17 (per CLAUDE.md §17). Six months dead =
delete. 167 LOC purged from `static/dashboard/dashboard-tabs.jsx` +
removed from `Object.assign(window, …)` export. The wallet-centric
Wallet Scanner sub-tab inside Wallet Graph is the live entry point.

### ADR-PMK-014.9 — Inspector hosts the Reconciliation panel

Inspector tab was already the "pipeline observability" surface (raw
trades, source mix, pipeline health). Adding "Paper Truth Reconciliation"
there matches the operator's mental model: when something looks off,
open Inspector.

The panel surfaces hero delta + verdict badge + phantom/premature counts
+ 5-run sparkline + "↻ Run now" button + "View N drift trades" drill-down
modal hitting `/api/inspector/reconciliation/trades?classification=…`.

### ADR-PMK-014.10 — Sidebar RECON chip ALWAYS rendered (never hidden)

Silence implies trustable. The chip's UNKNOWN state teaches the operator
that the question exists and trains them to look for it. Once recon runs
once, the chip stays in ok/warn/critical state forever (the underlying
table is append-only).

---

## Aggregate consequences

### Positive
- Operator can answer "is the displayed PnL real?" in <1 second on every tab.
- V2 returns to its intended lab-only role per memory.
- 5-pillars gauge in Bot Health surfaces oracle/recon/backfill/spread/audit
  health. Degradation is detected before it becomes a $42k surprise.
- Honest controls eliminate operator panic about "what does HALT actually do?".
- JSX cold-start TTI drops from multi-second to <1s (162.6 KB bundle, 19ms build).
- Mission KPI (win rate) is the first thing the operator reads.

### Negative
- Two render paths (V1 default, V2 lab) — maintenance overhead.
- Snapshot composition now does two more async fetches in parallel (recon
  summary + pillars status) — both cached 30s; budget cost is small.
- New cron-triggered Redis key (`recon:trigger:queued`) adds a moving part
  the scheduler must consume.
- ~35 new pytest cases added to CI runtime (+0.4s).

### Neutral
- Sidebar layout changes — operator retraining cost is a single screenshot.

---

## Rollback

Each phase is a single-commit revert. The new tables (049/050/051) already
existed; this work only adds query helpers + endpoints + UI surfaces.
Reverting any phase doesn't require schema changes.

- Phase 1 (backend endpoints): `git revert <sha>` — endpoints disappear,
  tables remain. Dashboard falls back to "UNKNOWN" verdict.
- Phase 2 (V2 gating): revert restores V2 always-on behavior. No data migration.
- Phase 3–7 (UI): pure JSX commits; revert and rebuild bundle.
- Phase 8 (esbuild): revert restores Babel-on-the-fly via the dev-fallback
  path in `templates/dashboard.html` (the fallback is preserved on purpose).

Browser-side recovery for V2 lab mode: `localStorage.removeItem('poybot_v2_lab'); location.reload();`.

---

## Files touched

### Backend (Python)
- `src/api/reconciliation_queries.py` — new (260 LOC)
- `src/api/pillars_queries.py` — new (240 LOC)
- `src/api/main.py` — +120 LOC (4 endpoints + TTL entries + snapshot wiring)
- `src/api/terminal_snapshot.py` — +35 LOC (execution_mode + recon + pillars fields)
- `src/engine/paper_trader.py` — +135 LOC (force_close_all_positions + _on_halt_message + Redis subscription)

### Frontend (JSX)
- `templates/dashboard.html` — rewritten (V2 gating, bundle loader with dev fallback)
- `static/dashboard/dashboard-app.jsx` — sidebar overhaul (RECON chip, ModeChip,
  KPI reorder, topbar WS lag, `g l` shortcut, MarketScanner removed from
  destructure, Tweaks panel deleted, ModeChip component)
- `static/dashboard/dashboard-tabs.jsx` — ReconciliationPanel + drift modal,
  PillarsGauge, V2LabToggle, honest controls, category badges at 4 sites,
  LivePortfolio reconciliation stamp, MarketScanner deleted (167 LOC)
- `static/dashboard/dashboard-components.jsx` — categoryRisk + CategoryRiskBadge
- `static/dashboard/api-client.js` — honest enable/disable/halt verbs

### Build pipeline (new)
- `package.json`
- `scripts/build_dashboard.mjs`
- `scripts/build_dashboard.sh`
- `static/dashboard/_entry.jsx`
- `.gitignore` (additions)

### Tests
- `tests/test_api/test_reconciliation_endpoints.py` — 17 cases
- `tests/test_api/test_pillars_endpoint.py` — 18 cases

### Docs
- This ADR
- `docs/RUNBOOK-RECONCILIATION.md`
- CLAUDE.md §15 + §17 updates

### Memory (auto-memory files)
- `project_v1_vs_v2_terminal.md` — updated to reflect gating mechanism
- `project_paper_trading_pillars.md` — updated to mark all 5 pillars IMPLEMENTED
- `project_ui_v1_truth_surface.md` — new

---

## References
- Memory: `project_paper_trading_truth.md`, `project_paper_trading_pillars.md`,
  `project_v1_vs_v2_terminal.md`, `project_polymarket_bot.md`
- Migrations: 049 (paper_trades invariants), 050 (close_audit_log),
  051 (paper_close_divergences)
- Existing modules: `src/control/price_oracle.py`, `scripts/reconciliation.py`
- Runbook: `docs/RUNBOOK-RECONCILIATION.md`
