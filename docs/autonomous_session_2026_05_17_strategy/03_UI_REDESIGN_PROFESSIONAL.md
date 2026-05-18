# UI Redesign — Cosmograph + Professional Live Portfolio

**Trigger**: operator wants (1) all wallet leaders + edges rendered fluidly (159k edges, 2.6k leaders), (2) a Live Portfolio page reimagined as a professional terminal-style trading dashboard with interactive multi-timeframe charts.

**Reference aesthetic**: Mirrorfish Backtest Engine screenshot — dark theme, monospace, dense info, multi-panel layout, big synchronized equity/PnL timeline, color-coded trades list, pipeline status bar, real-time stats footer.

---

## 1. Tech Stack Decisions (final)

### Graph viz
**Cosmograph** (`@cosmograph/cosmograph`) — WebGL2 compute shaders, GPU-accelerated force layout, 1M+ nodes at 60-120fps. Vanilla JS API works via CDN script tag (compatible with the project's React-via-Babel-CDN setup).

### Time-series charts
**TradingView lightweight-charts** (`lightweight-charts` v4) — purpose-built for financial timeline charts, 1ms render, supports candlesticks/areas/lines/bars, synchronized crosshair across charts, built-in timeframe controls, free MIT-style license. Vanilla JS API, CDN-compatible.

### Typography
**JetBrains Mono** (already free, similar terminal feel to Mirrorfish) for headings + numeric data. **Inter** for body text. Both via Google Fonts.

### Color palette (design tokens)
```css
--bg-0: #050608        /* near-black canvas */
--bg-1: #0c0e12        /* panel bg */
--bg-2: #131720        /* card bg */
--bd-1: #1a1f2b        /* border subtle */
--bd-2: #2a3142        /* border emphasized */
--fg-0: #e8eaf0        /* primary text */
--fg-1: #9aa1b3        /* muted text */
--fg-2: #5b6376        /* dim text */
--accent-green: #4ade80 /* equity positive */
--accent-red:   #f87171 /* loss / stop */
--accent-amber: #fbbf24 /* warning / take-profit pending */
--accent-blue:  #60a5fa /* info / phase 2 */
--accent-violet:#a78bfa /* phase 3 / follower */
--accent-pink:  #ec4899 /* alerts / killswitch */
```

### Layout system
CSS Grid + Flex. 12-column outer grid. Each panel is its own grid cell with `aspect-ratio: auto` so the dashboard tiles cleanly across 1280px / 1440px / 1920px / 4k.

---

## 2. Modules Affected

### A) Cosmograph integration (priority 1)
- `static/dashboard/v2/WalletGraph.jsx` — full rewrite with Cosmograph
- `static/dashboard/v2/components/DecisionEngineGraph.jsx` (if exists) — same renderer for the decision-flow network
- Backend `src/api/queries.py::wallet_graph` — bump caps to expose full graph (`max_leaders=3000`, `edge LIMIT=100000`)

### B) Live Portfolio (priority 1) — the new professional view
- `static/dashboard/v2/LivePortfolio.jsx` — full rewrite (multi-panel terminal layout)
- New sub-components:
  - `<EquityTimeline>` — main chart, equity curve + drawdown band, sync crosshair
  - `<PnLTicks>` — secondary chart aligned with equity, per-trade P/L points
  - `<MarketPriceOverlay>` — third chart, YES/NO price tracking when a position is open
  - `<TradeList>` — color-coded list of recent trades with status + PnL
  - `<AllocationBar>` — horizontal stacked bar (current capital allocation by category/leader/strategy)
  - `<KpiTiles>` — top metrics: balance, leverage equivalent, daily P&L, win streak, latency
  - `<PipelineStatus>` — header bar: bot status, WS health, ingestion lag, exec mode, killswitch
  - `<TimeframeSelector>` — 1m / 5m / 15m / 1h / 4h / 1d / 1w buttons with active state
- Backend: new endpoints
  - `GET /api/portfolio/timeseries?timeframe=1h&from=...&to=...` — equity OHLCV-style buckets
  - `GET /api/portfolio/trades?limit=200&order=opened_desc` — recent trades for the list
  - `GET /api/portfolio/allocation?as_of=now` — current open positions by category/leader
  - `GET /api/portfolio/kpis` — top-line numbers (capital, peak, drawdown, win rate, streak, daily P&L, latency p50)
  - `GET /api/portfolio/pipeline_status` — bot/WS/ingestion/exec state (or merge into existing snapshot)

### C) Design system (priority 2)
- `static/dashboard/v2/theme.css` (new) — design tokens, base typography, panel/card primitives, status pills
- `static/dashboard/v2/components/primitives/` — small reusable: `<Panel>`, `<Stat>`, `<Pill>`, `<Sparkline>`
- Apply theme to other tabs progressively (Alpha Terminal, ML Progression, Decision Engine, Inspector, Risk & Config, Bot Health)

---

## 3. Sub-agent Assignments

### Agent A — Frontend Audit + Design System
- **Read**: `static/dashboard/`, all .jsx, templates/dashboard.html, current CSS
- **Output**:
  - Inventory of every JSX file (lines, responsibility)
  - Current font/color/spacing usage (extract palette in use)
  - Document current data fetch pattern (REST vs WS, polling cadence, snapshot endpoint)
  - Build `static/dashboard/v2/theme.css` with full design-token system
  - Build `static/dashboard/v2/components/primitives/` with `Panel`, `Stat`, `Pill`, `Sparkline`, `KpiTile`, `StatusPill`
- **Files**: only theme.css + primitives/* (touches no existing components)
- **Tests**: visual snapshots not required at this stage

### Agent B — Cosmograph Integration
- **Read**: current `WalletGraph.jsx`, `src/api/queries.py::wallet_graph`, Cosmograph docs at cosmograph.app
- **Output**:
  - Inject Cosmograph CDN script in `templates/dashboard.html`
  - Rewrite `WalletGraph.jsx` with Cosmograph:
    - Loads full graph via single fetch
    - Node color by phase (1=violet, 2=blue, 3=amber for leaders; muted gray for followers)
    - Node size proportional to trades_24h
    - Edge thickness proportional to `co_occurrences`, opacity by `follow_probability`
    - Hover: highlight node + outgoing edges, dim everything else
    - Click: zoom + side panel with leader stats
    - Toolbar: filter sliders for `co_occurrences ≥ N`, phase filter buttons, search box
    - Live tooltip on edge with all metrics
  - Bump backend `max_leaders=3000`, `edge LIMIT=100000`, `co_occurrences ≥ 2`
- **Files**: `static/dashboard/v2/WalletGraph.jsx`, `templates/dashboard.html`, `src/api/queries.py`
- **Tests**: backend test that the new caps don't blow the snapshot query

### Agent C — Live Portfolio Redesign
- **Read**: current `LivePortfolio.jsx`, `paper_trades` + `portfolio_state` + `portfolio_equity` schemas, existing `/api/portfolio*` endpoints (if any)
- **Output**:
  - Inject TradingView `lightweight-charts` CDN script in `templates/dashboard.html`
  - Build new `LivePortfolio.jsx` matching the Mirrorfish reference:
    - Top status bar (pipeline state + timestamp + latency)
    - 6-column KPI tile row (balance, peak, drawdown, daily P&L, win rate, latency)
    - Big main chart panel: equity curve + drawdown band + per-trade markers
    - Side panel: trade list (top 20 recent, color-coded by status/PnL)
    - Bottom strip: allocation bar (stacked horizontal, by category) + stats footer (best/worst trade, longest streak, etc.)
    - Timeframe selector synchronized across all charts (1m/5m/15m/1h/4h/1d/1w)
    - Synchronized crosshair across all timeline panels
- **Files**: `static/dashboard/v2/LivePortfolio.jsx`, all sub-components in `static/dashboard/v2/components/portfolio/`
- **Tests**: cosmetic — backend driven, no jest

### Agent D — Backend Timeseries + Aggregation Endpoints
- **Read**: `src/api/main.py`, `src/api/queries.py`, `portfolio_state`/`paper_trades` schema, existing snapshot composition
- **Output**:
  - `GET /api/portfolio/timeseries` — OHLCV-style buckets of equity + drawdown for a timeframe
  - `GET /api/portfolio/trades` — recent trades joined with markets (for color-coded list)
  - `GET /api/portfolio/allocation` — current allocation by category & leader
  - `GET /api/portfolio/kpis` — top-line numbers (single payload)
  - SQL queries optimised — use `portfolio_equity` table (if exists) or derive from `paper_trades` closes
  - All endpoints respect existing auth/middleware
- **Files**: `src/api/main.py` (add endpoints), `src/api/queries.py` (add SQL builders), no migration (re-use existing tables if possible; only add a migration if existing schema lacks a column)
- **Tests**: 8+ unit tests in `tests/test_api/test_portfolio_endpoints.py` for shape, edge cases (empty range, 1m vs 1d aggregation)

---

## 4. Implementation Order

```
[parallel]
  Agent A (design system + primitives)
  Agent D (backend endpoints)
  
[parallel after A]
  Agent B (cosmograph rewrite)
  Agent C (live portfolio rewrite)
```

Agent B and C both depend on the design system primitives (Panel, Pill, KpiTile) but neither depends on the other. Agent C also depends on Agent D's endpoints to provide data.

---

## 5. Acceptance Criteria

### Cosmograph
- Loads 2,628 leaders + 28,000+ edges in <2s
- Pan/zoom at 60fps even with full graph
- Hover/click interaction feels instant (<16ms)
- Toolbar filter sliders update graph in real time without reload

### Live Portfolio
- All KPIs update via single REST poll (5s cadence) — no per-tile request
- Timeframe selector swaps all 3 timeline panels in <500ms
- Crosshair across charts is pixel-synchronized
- Trade list scrolls smoothly with 200+ rows
- Renders identically on 1280px / 1920px / 4k displays

### Design System
- Single source of truth for colors, fonts, spacing in `theme.css`
- Primitives (`<Panel>`, `<Stat>`, etc.) used by Cosmograph + Live Portfolio + 2 other pages

### Deploy
- All static JSX served via the existing FastAPI static mount
- No bundler — Babel-on-the-fly via CDN remains the build strategy
- TradingView + Cosmograph loaded via CDN script tags in `templates/dashboard.html`
- Hard-refresh required for client to pick up new JSX

---

## 6. Risks

- **CDN dependency**: if Cosmograph or TradingView CDN is down, the page won't render. Mitigation: pin to a specific version and add fallback "graph unavailable" message.
- **JSX size**: Babel-on-the-fly compilation slows for very large JSX files. Mitigation: split LivePortfolio.jsx into 6-8 small files.
- **Backend query cost**: full 100k edges query is ~50ms with current indexes. Acceptable for 5s polling. If usage grows: add materialised view.
- **Mobile/responsive**: out of scope for v1 — desktop terminal-style only.
