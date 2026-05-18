# Wallet Graph V3 — Design System

> **Agent**: WG-A1 (designer of the system)
> **Date**: 2026-05-19
> **Scope**: Visual + technical spec for the new Wallet Graph "universe" view in V1.
> **Out of scope**: Implementation. This document is a contract for WG-A2/A3.
>
> **References**:
> - HellBorn (intense halos, deep cosmos blacks, signature node glow)
> - BubbleMaps (colored clusters by group, edge transparency)
> - Moonlight (subtle elegance, tabular precision, restrained palette)
>
> **Constraints**:
> - React 18.3.1 (already loaded via CDN in `templates/dashboard.html`)
> - `react-force-graph-2d` via CDN (UMD) — no NPM install
> - Native canvas API for ambient + node painters
> - Must NOT break existing CSS in `static/dashboard/*.css`
> - Must NOT modify the V1 cold-load path (V2 lab is gated separately)

---

## 0. Design philosophy (Mix HellBorn + BubbleMaps + Moonlight)

| Layer                  | Source         | What we take                                       |
|-----------------------|----------------|----------------------------------------------------|
| Background ambience   | HellBorn       | Deep cosmos gradient, star particles, faint nebula |
| Node glow / halos     | HellBorn       | Multi-layer radial gradient (outer halo + inner glow + core) |
| Color-by-cluster      | BubbleMaps     | Phase = color (P1 cyan, P2 amber, P3 emerald, Followers violet) |
| Edge restraint        | BubbleMaps     | Sub-1% white edges by default, intensify on hover  |
| Typography & panels   | Moonlight      | Mono-spaced numerics, tabular alignment, ghost panels |
| Interaction polish    | Moonlight      | 200-400ms transitions, no abrupt state changes     |

The visual outcome: a quiet, professional cosmos. The graph reads as
*intelligence map*, not *toy*. Halos do the heavy lifting; the
background recedes.

---

## 1. Palette

### 1.1 Background

| Token                  | Value                                              | Use                                  |
|-----------------------|----------------------------------------------------|--------------------------------------|
| `--wg-bg-base`        | `#06080f`                                          | Outermost background fill           |
| `--wg-bg-mid`         | `#0a0e1a`                                          | Mid radius of gradient              |
| `--wg-bg-nebula`      | `rgba(80, 50, 160, 0.04)`                          | Faint violet nebula (bottom-right)  |

```css
background:
  radial-gradient(
    ellipse 1600px 1200px at 30% 40%,
    var(--wg-bg-mid) 0%,
    var(--wg-bg-base) 60%
  ),
  /* faint violet nebula bottom-right */
  radial-gradient(
    ellipse 800px 600px at 85% 90%,
    var(--wg-bg-nebula) 0%,
    transparent 70%
  );
```

### 1.2 Stars (ambient layer)

| Token                  | Value                  | Use                                       |
|-----------------------|------------------------|-------------------------------------------|
| `--wg-star-hot`       | `#ffffff`              | Bright stars (rare, ~10%)                |
| `--wg-star-cool`      | `#94a8d6`              | Cool stars (~70%)                         |
| `--wg-star-warm`      | `#d6c594`              | Warm stars (~20%, near nebula)            |
| `--wg-star-opacity`   | `0.2` → `0.8`          | Per-star, twinkle-animated                |

### 1.3 Nodes by phase

Phase comes from `leader_profiles.error_model_phase` (1/2/3). Wallets
that are confirmed followers (i.e. they appear in `follower_edges.follower_wallet`
but not as a leader) get the violet treatment.

| Phase / role          | Core color   | Glow color (with alpha)             | Notes                          |
|----------------------|--------------|-------------------------------------|--------------------------------|
| **P1** (Beta-Binom)  | `#3b82f6`    | `0 0 12px rgba(59,130,246,0.50)`    | Cyan-blue neon                 |
| **P2** (BayesLogReg) | `#f59e0b`    | `0 0 12px rgba(245,158,11,0.50)`    | Amber neon                     |
| **P3** (LightGBM)    | `#10b981`    | `0 0 12px rgba(16,185,129,0.50)`    | Emerald neon                   |
| **Follower**         | `#a78bfa`    | `0 0 10px rgba(167,139,250,0.40)`   | Violet, lower opacity (0.7)    |
| **Excluded / bot**   | `#475569`    | none                                 | Dim slate, no glow             |

```css
--wg-node-p1: #3b82f6;
--wg-node-p1-glow: rgba(59, 130, 246, 0.50);
--wg-node-p2: #f59e0b;
--wg-node-p2-glow: rgba(245, 158, 11, 0.50);
--wg-node-p3: #10b981;
--wg-node-p3-glow: rgba(16, 185, 129, 0.50);
--wg-node-follower: #a78bfa;
--wg-node-follower-glow: rgba(167, 139, 250, 0.40);
--wg-node-excluded: #475569;
```

### 1.4 Edges

| State                 | Stroke                                | Notes                              |
|----------------------|---------------------------------------|------------------------------------|
| Default              | `rgba(255, 255, 255, 0.08)`           | Width: `0.5 + follow_prob * 0.8`   |
| Hover (incident)     | `rgba(255, 200, 100, 0.50)`           | Amber tint when source/target hovered |
| Selected (incident)  | `rgba(255, 255, 255, 0.85)`           | White when source/target selected  |
| High co-occurrence   | `rgba(255, 200, 100, 0.20)`           | Adds a 20% amber when `co_occurrences > 50` |

```css
--wg-edge-base: rgba(255, 255, 255, 0.08);
--wg-edge-hover: rgba(255, 200, 100, 0.50);
--wg-edge-selected: rgba(255, 255, 255, 0.85);
--wg-edge-strong: rgba(255, 200, 100, 0.20);
```

### 1.5 Selection halo

```css
--wg-select-ring: #ffffff;
--wg-select-ring-width: 2;        /* px, drawn on canvas */
--wg-select-halo: 0 0 24px rgba(255, 255, 255, 0.50);
```

### 1.6 Panel

```css
--wg-panel-bg: rgba(10, 14, 26, 0.85);
--wg-panel-border: rgba(255, 255, 255, 0.12);
--wg-panel-blur: 8px;
--wg-panel-text: #c4ccd8;     /* matches existing dashboard body color */
--wg-panel-muted: #6b7a94;    /* matches existing placeholder color */
--wg-panel-accent: #e8a020;   /* matches existing brand accent */
```

### 1.7 Compatibility — do NOT redefine existing variables

The existing dashboard uses:
- `body { background: #070809; color: #c4ccd8; }`
- Accent `#e8a020` for the brand line, `#6b7a94` for muted text,
  `#c93545` for errors.

All new tokens live under the `--wg-*` namespace. They are scoped to
`.wg-root` so they cannot leak into the rest of the dashboard.

```css
.wg-root {
  --wg-bg-base: #06080f;
  /* ... all wg-* tokens defined here ... */
}
```

---

## 2. Typography

The dashboard already loads JetBrains Mono via Google Fonts in
`templates/dashboard.html` (line 9). We reuse it. Do NOT add a new
font request — the cosmos look comes from spacing and weight, not from
a new family.

| Use case            | Family           | Size  | Weight | Letter-spacing | Other        |
|---------------------|------------------|-------|--------|----------------|--------------|
| Panel title         | JetBrains Mono   | 14px  | 600    | 0.08em         | uppercase    |
| Panel section label | JetBrains Mono   | 10px  | 500    | 0.10em         | uppercase, muted color |
| Body text           | JetBrains Mono   | 11px  | 400    | 0              | normal       |
| Numeric             | JetBrains Mono   | 11px  | 500    | 0              | `font-variant-numeric: tabular-nums` |
| KPI strip number    | JetBrains Mono   | 18px  | 600    | 0              | tabular-nums, `text-shadow: 0 0 8px currentColor` |
| Pill / filter       | JetBrains Mono   | 10px  | 500    | 0.06em         | uppercase    |
| Wallet hash         | JetBrains Mono   | 11px  | 400    | 0              | tabular-nums |

---

## 3. Components

### 3.1 Background ambient (`<WgAmbientCanvas />`)

A canvas pinned `position: absolute; inset: 0; z-index: 0;` *behind*
the force-graph canvas. Renders ~150 stars with twinkle + 4-8 larger
"planets" with radial gradients.

```jsx
// Pseudocode — WG-A2/A3 will produce the JSX/canvas implementation.
function WgAmbientCanvas({ width, height }) {
  const canvasRef = useRef(null);
  const starsRef = useRef([]);

  useEffect(() => {
    // Seed once on mount. Avoid regenerating on resize — adjust positions instead.
    starsRef.current = Array.from({ length: 150 }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      r: Math.random() < 0.1 ? 1.4 : 0.6,           // 10% bright
      color: pickStarColor(),                       // see palette 1.2
      baseOpacity: 0.2 + Math.random() * 0.6,
      twinkleSpeed: 0.5 + Math.random() * 1.5,      // Hz
      twinklePhase: Math.random() * Math.PI * 2,
    }));
    // Add 4-8 larger "planets" with radial gradient
    starsRef.current.push(...planetSeeds(4 + Math.floor(Math.random() * 5), width, height));
  }, []);

  useEffect(() => {
    const ctx = canvasRef.current.getContext('2d');
    let raf;
    const start = performance.now();
    const tick = (t) => {
      const elapsed = (t - start) / 1000;
      ctx.clearRect(0, 0, width, height);
      for (const s of starsRef.current) {
        const op = s.baseOpacity * (0.7 + 0.3 * Math.sin(elapsed * s.twinkleSpeed + s.twinklePhase));
        if (s.isPlanet) {
          const g = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, s.r * 3);
          g.addColorStop(0,   `${s.color}cc`);
          g.addColorStop(0.4, `${s.color}55`);
          g.addColorStop(1,   `${s.color}00`);
          ctx.fillStyle = g;
          ctx.beginPath(); ctx.arc(s.x, s.y, s.r * 3, 0, 2 * Math.PI); ctx.fill();
        } else {
          ctx.globalAlpha = op;
          ctx.fillStyle = s.color;
          ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 2 * Math.PI); ctx.fill();
          ctx.globalAlpha = 1;
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [width, height]);

  return <canvas ref={canvasRef} width={width} height={height}
                 style={{ position: 'absolute', inset: 0, zIndex: 0, pointerEvents: 'none' }} />;
}
```

Performance notes:
- A single `requestAnimationFrame` loop drives both canvases.
- `clearRect` is fine at 60fps for 150 + ~6 entities on any modern GPU.
- Twinkle math runs at ~9μs per star — negligible.
- On `prefers-reduced-motion: reduce`, set `baseOpacity` directly (skip the sine wave).

### 3.2 Node painter (canvas custom in `react-force-graph-2d`)

`react-force-graph-2d` exposes the `nodeCanvasObject(node, ctx, globalScale)`
prop. We override it to paint our cosmos nodes.

```jsx
function paintNode(node, ctx, globalScale) {
  const baseRadius = clamp(Math.sqrt((node.trades_24h ?? 1)) * 2, 3, 15);
  const isHover = node === hoveredNode;
  const isSelected = node.id === selectedId;
  const radius = baseRadius * (isHover ? 1.3 : 1.0);
  const { core, glow } = colorForNode(node);   // see palette 1.3

  // 1. Outer halo (3x radius radial gradient, alpha 0.4 → 0)
  const haloR = radius * 3;
  const g1 = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, haloR);
  g1.addColorStop(0,   glow);                                    // e.g. rgba(59,130,246,0.50)
  g1.addColorStop(0.5, replaceAlpha(glow, 0.18));
  g1.addColorStop(1,   replaceAlpha(glow, 0.00));
  ctx.fillStyle = g1;
  ctx.beginPath(); ctx.arc(node.x, node.y, haloR, 0, 2 * Math.PI); ctx.fill();

  // 2. Inner glow (1.5x radius, alpha 0.8 → 0.4)
  const innerR = radius * 1.5;
  const g2 = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, innerR);
  g2.addColorStop(0, replaceAlpha(core, 0.85));
  g2.addColorStop(1, replaceAlpha(core, 0.30));
  ctx.fillStyle = g2;
  ctx.beginPath(); ctx.arc(node.x, node.y, innerR, 0, 2 * Math.PI); ctx.fill();

  // 3. Core (solid)
  ctx.fillStyle = core;
  ctx.beginPath(); ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI); ctx.fill();

  // 4. Border (only when active)
  if (isHover || isSelected) {
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.lineWidth = 1 / globalScale;
    ctx.stroke();
  }

  // 5. Selection ring + extra halo
  if (isSelected) {
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2 / globalScale;
    ctx.beginPath(); ctx.arc(node.x, node.y, radius + 4, 0, 2 * Math.PI); ctx.stroke();

    // Extra outer halo for selection
    const g3 = ctx.createRadialGradient(node.x, node.y, radius, node.x, node.y, radius * 5);
    g3.addColorStop(0, 'rgba(255,255,255,0.5)');
    g3.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g3;
    ctx.beginPath(); ctx.arc(node.x, node.y, radius * 5, 0, 2 * Math.PI); ctx.fill();
  }
}
```

`colorForNode(node)` returns `{ core, glow }` based on:
1. If `node.excluded === true` → excluded (slate, no glow).
2. Else if `node.role === 'follower'` and `node.is_leader === false` → follower violet.
3. Else map `node.phase` (1/2/3) → cyan / amber / emerald.

Helper `replaceAlpha("rgba(r,g,b,a)", newAlpha)` is a small util to
re-stamp the alpha channel.

### 3.3 Edge painter (canvas custom)

```jsx
function paintEdge(link, ctx, globalScale) {
  const sourceHot = hoveredId === link.source.id || selectedId === link.source.id;
  const targetHot = hoveredId === link.target.id || selectedId === link.target.id;
  const hot       = sourceHot || targetHot;
  const isSel     = selectedId === link.source.id || selectedId === link.target.id;
  const strong    = (link.co_occurrences ?? 0) > 50;

  let stroke;
  if (isSel)        stroke = 'rgba(255,255,255,0.85)';
  else if (hot)     stroke = 'rgba(255,200,100,0.50)';
  else if (strong)  stroke = 'rgba(255,200,100,0.20)';
  else              stroke = 'rgba(255,255,255,0.08)';

  ctx.strokeStyle = stroke;
  ctx.lineWidth   = (0.5 + (link.follow_probability ?? 0) * 0.8) / globalScale;
  ctx.beginPath();
  ctx.moveTo(link.source.x, link.source.y);
  ctx.lineTo(link.target.x, link.target.y);
  ctx.stroke();
}
```

Arrowheads: **off by default**. The graph has ~5000 edges; arrowheads
become visual noise. Render an arrowhead only on selection of one
endpoint (in the same painter, draw a small triangle at the target if
`isSel`).

### 3.4 Selection Panel (`<WgSelectionPanel />`)

Floating HTML overlay (NOT canvas). Positioned `absolute; top: 16px;
left: 16px;` at the top-left of the graph container, 280px wide.

```text
┌────────────────────────────────────────┐
│ SELECTED WALLET    [eye]  [copy]       │   ← title bar (14px / 600 / uppercase)
│ 0xf6c0...3cb0                          │   ← wallet hash (11px / 400 / muted)
├────────────────────────────────────────┤
│ Phase          P1     │ Falcon   46.94 │   ← grid 2 cols, label muted / value bright
│ Trades         58     │ Resolved   0   │
│ 24h Trades     19     │ Followers  3   │
│ Categories                              │
│   unknown 63% · politics 25%            │
│ Strategy       directional              │
│ Horizon        swing                    │
├────────────────────────────────────────┤
│         [  SHOW TRANSFERS  ]            │   ← primary button, neon style
└────────────────────────────────────────┘
```

#### CSS skeleton

```css
.wg-panel {
  position: absolute;
  top: 16px;
  left: 16px;
  width: 280px;
  padding: 14px;
  background: var(--wg-panel-bg);
  border: 1px solid var(--wg-panel-border);
  border-radius: 6px;
  backdrop-filter: blur(var(--wg-panel-blur));
  -webkit-backdrop-filter: blur(var(--wg-panel-blur));
  color: var(--wg-panel-text);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  line-height: 1.4;
  opacity: 0;
  transform: translateX(-8px);
  transition: opacity 300ms ease-out, transform 300ms ease-out;
  z-index: 10;
}
.wg-panel.is-visible { opacity: 1; transform: translateX(0); }

.wg-panel__title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--wg-panel-muted);
  display: flex; justify-content: space-between; align-items: center;
}
.wg-panel__hash {
  margin-top: 4px;
  font-size: 13px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--wg-panel-text);
}
.wg-panel__grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  row-gap: 6px;
  column-gap: 12px;
}
.wg-panel__field-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--wg-panel-muted);
}
.wg-panel__field-value {
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.wg-panel__cta {
  margin-top: 14px;
  width: 100%;
  padding: 8px 0;
  background: transparent;
  border: 1px solid var(--wg-panel-accent);
  color: var(--wg-panel-accent);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  cursor: pointer;
  transition: all 200ms ease-out;
}
.wg-panel__cta:hover {
  background: rgba(232, 160, 32, 0.10);
  box-shadow: 0 0 16px rgba(232, 160, 32, 0.30);
}
```

Field source (snapshot data already exposed by the API):
- `phase`     ← `leader_profiles.error_model_phase`
- `falcon`    ← `leaders.falcon_score`
- `trades`    ← total trades observed (already in snapshot)
- `resolved`  ← `leader_profiles.positions_resolved`
- `24h trades` ← derived from `trades_observed` in last 24h
- `followers` ← `count(follower_edges where leader_wallet = wallet)`
- `categories` ← top 2 from `profile_json.preferred_categories`
- `strategy`  ← `classification_json.strategy`
- `horizon`   ← `classification_json.horizon`

The eye icon toggles a "hide-this-wallet-locally" preference (mirrors a
common BubbleMaps gesture). The copy icon copies the full wallet
address to clipboard.

### 3.5 KPI strip (top of the graph view)

Existing strip in the Wallet Graph tab (LEADERS / FOLLOWERS / EDGES /
…). We re-skin, we do not rebuild.

```css
.wg-kpi-strip {
  display: flex;
  gap: 24px;
  padding: 10px 16px;
  background: linear-gradient(180deg, rgba(255,255,255,0.02) 0%, transparent 100%);
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
.wg-kpi-tile { display: flex; flex-direction: column; gap: 2px; }
.wg-kpi-tile__label {
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--wg-panel-muted);
}
.wg-kpi-tile__value {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-shadow: 0 0 8px currentColor;
}
.wg-kpi-tile--p1 .wg-kpi-tile__value { color: var(--wg-node-p1); }
.wg-kpi-tile--p2 .wg-kpi-tile__value { color: var(--wg-node-p2); }
.wg-kpi-tile--p3 .wg-kpi-tile__value { color: var(--wg-node-p3); }
.wg-kpi-tile--follower .wg-kpi-tile__value { color: var(--wg-node-follower); }
```

### 3.6 Search + filter bar (under KPI strip)

```text
┌────────────────────────────────────────────────────────────────┐
│ [ search wallet ... ]   All  P1  P2  P3  Followers   co≥5 P≥.5│
└────────────────────────────────────────────────────────────────┘
```

```css
.wg-search-input {
  flex: 1 1 240px;
  height: 28px;
  padding: 0 10px;
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 4px;
  color: var(--wg-panel-text);
  font: 11px/1 'JetBrains Mono', monospace;
}
.wg-search-input:focus {
  outline: none;
  border-color: rgba(255,255,255,0.20);
  box-shadow: 0 0 8px rgba(255,255,255,0.08);
}
.wg-pill {
  height: 24px;
  padding: 0 10px;
  background: transparent;
  border: 1px solid rgba(255,255,255,0.10);
  border-radius: 12px;
  font: 10px/22px 'JetBrains Mono', monospace;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--wg-panel-muted);
  cursor: pointer;
  transition: all 150ms ease-out;
}
.wg-pill.is-active {
  border-color: currentColor;
  color: var(--wg-panel-text);
  box-shadow: 0 0 8px currentColor;
}
.wg-pill--p1.is-active { color: var(--wg-node-p1); }
.wg-pill--p2.is-active { color: var(--wg-node-p2); }
.wg-pill--p3.is-active { color: var(--wg-node-p3); }
.wg-pill--follower.is-active { color: var(--wg-node-follower); }
```

The two existing sliders (`co_occurrences ≥ N`, `P(follow) ≥ X`) are
restyled with a thin track and a small handle. They keep their current
state binding.

---

## 4. Animations

| Trigger                  | Property               | Duration   | Easing            |
|--------------------------|------------------------|------------|-------------------|
| Graph mount              | opacity 0 → 1          | 400ms      | `ease-out`        |
| Panel open               | opacity + translateX   | 300ms      | `ease-out`        |
| Panel close              | opacity                | 200ms      | `ease-in`         |
| Node hover               | halo alpha + radius    | 150ms      | `ease-out` (canvas-driven via lerp) |
| Click cluster (fit to)   | zoom + pan             | 400ms      | `ease-in-out` (use `forceGraphRef.current.zoomToFit(400, 60)`) |
| Star twinkle             | opacity sine wave      | continuous | n/a               |
| Pill activation          | border + color + shadow| 150ms      | `ease-out`        |

Reduced-motion: when `window.matchMedia('(prefers-reduced-motion: reduce)').matches`,
disable twinkle, set all transitions to 0ms, set zoomToFit duration to 0.

---

## 5. Performance constraints

- **Default visible cap**: top-500 wallets by `trades_24h DESC`. The
  KPI strip shows e.g. `Leaders 1837 · Visible 500 [show all]`.
- **"Show all" button**: appends remaining nodes lazily on next frame.
  Up to ~2000 we expect 30+ fps on a 2020 MBP.
- **Edge filtering**: only render edges where both endpoints are in the
  visible set. With 5000 edges and 500 visible nodes, expect ~1500-2500
  edges drawn — comfortable.
- **WebGL fallback**: `react-force-graph-2d` is canvas-only by design;
  if a future agent wants WebGL acceleration, switch to
  `react-force-graph-3d` or `cosmograph`. The painters in §3.2 / §3.3
  must be re-expressed in shaders.
- **Twinkle loop**: bail out when the wallet graph tab is not active
  (use `useIsTabVisible('walletgraph')` or `document.visibilityState`).
- **Resize**: throttle to 60Hz via `requestAnimationFrame`; recompute
  star positions only on width/height change > 5%.

---

## 6. Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  [KPI strip — leaders / followers / edges / 24h]                │
├─────────────────────────────────────────────────────────────────┤
│  [search]  [All P1 P2 P3 Followers]  [co≥N]  [P≥X]   [show all]│
├─────────────────────────────────────────────────────────────────┤
│ ┌──────────┐                                                    │
│ │ Selected │     ⭐         ●○                ✦                  │
│ │  Wallet  │          ╱   ●●●●  ╲                              │
│ │  panel   │       ●●●○  ●●●●  ●●●                             │
│ │          │      ●●     ●●●●     ●●                           │
│ │  280px   │       ╲    ●●●●   ╱                               │
│ │          │          ✦   ●○      ⭐                            │
│ │   only   │                                                    │
│ │  shown   │                                                    │
│ │   when   │                                                    │
│ │ a node   │                                                    │
│ │  picked  │                                                    │
│ └──────────┘                                                    │
│                       [confirmed edge ─── pending - - -]        │
└─────────────────────────────────────────────────────────────────┘
```

- Wrapper: `position: relative; height: 100%;`
- Ambient canvas: `position: absolute; inset: 0; z-index: 0;`
- Force graph canvas: `position: absolute; inset: 0; z-index: 1;`
- Panel: `position: absolute; top: 16px; left: 16px; z-index: 10;`
- KPI strip and search bar live ABOVE the canvas wrapper (separate flex container).

The Wallet Scanner toggle (existing in the current tab) is preserved —
it lives next to the search input.

---

## 7. Matching the three references

| Reference  | Where it shows up in the spec                                                                                |
|-----------|--------------------------------------------------------------------------------------------------------------|
| **HellBorn**   | §3.1 ambient canvas with twinkling stars + faint nebula; §3.2 three-layer node halo (outer / inner / core); §1.5 white selection halo |
| **BubbleMaps** | §1.3 colored clusters by phase; §3.3 mostly-invisible edges that intensify on hover; §3.4 corner overlay panel |
| **Moonlight**  | §2 tabular-nums numerics with restrained sizing; §3.5 KPI strip with `text-shadow` instead of solid blocks; §4 measured 150-400ms transitions; §1.7 panel uses 85% black + 8px blur (subtle, not opaque) |

The cosmos aesthetic is achieved by composition, not by any single
heavy effect. No bloom shaders, no particle systems beyond the simple
twinkle. The visual weight of the graph comes from many small
correctly-tuned details.

---

## 8. Open questions for WG-A2 (graph runtime)

1. Should `nodeRadius` use `trades_24h` or a composite of
   `trades_24h * (1 + 0.5 * has_followers)`? The composite would make
   leaders stand out more.
2. The "Show transfers" CTA — does it open a side panel listing recent
   trades, or does it overlay edges showing transfer volume to/from the
   wallet? Spec leaves this to the data engineer; visually both fit.
3. On-graph cluster labels (e.g. "Politics · 23 wallets"): out of scope
   for V3.0 but easy to add later — reserve a `wg-cluster-label` class.

---

## 9. CSS tokens — full block to paste into the wallet graph stylesheet

```css
.wg-root {
  /* Background */
  --wg-bg-base: #06080f;
  --wg-bg-mid:  #0a0e1a;
  --wg-bg-nebula: rgba(80, 50, 160, 0.04);

  /* Stars */
  --wg-star-hot:  #ffffff;
  --wg-star-cool: #94a8d6;
  --wg-star-warm: #d6c594;

  /* Node colors */
  --wg-node-p1:        #3b82f6;
  --wg-node-p1-glow:   rgba(59, 130, 246, 0.50);
  --wg-node-p2:        #f59e0b;
  --wg-node-p2-glow:   rgba(245, 158, 11, 0.50);
  --wg-node-p3:        #10b981;
  --wg-node-p3-glow:   rgba(16, 185, 129, 0.50);
  --wg-node-follower:      #a78bfa;
  --wg-node-follower-glow: rgba(167, 139, 250, 0.40);
  --wg-node-excluded:  #475569;

  /* Edges */
  --wg-edge-base:     rgba(255, 255, 255, 0.08);
  --wg-edge-strong:   rgba(255, 200, 100, 0.20);
  --wg-edge-hover:    rgba(255, 200, 100, 0.50);
  --wg-edge-selected: rgba(255, 255, 255, 0.85);

  /* Selection */
  --wg-select-ring:        #ffffff;
  --wg-select-halo-color:  rgba(255, 255, 255, 0.50);

  /* Panel */
  --wg-panel-bg:     rgba(10, 14, 26, 0.85);
  --wg-panel-border: rgba(255, 255, 255, 0.12);
  --wg-panel-blur:   8px;
  --wg-panel-text:   #c4ccd8;
  --wg-panel-muted:  #6b7a94;
  --wg-panel-accent: #e8a020;
}
```

These tokens are scoped to `.wg-root`. Wrap the wallet graph React
sub-tree in `<div className="wg-root">…</div>` and nothing outside it
will see these variables.

---

## 10. Deliverable summary (for the planner)

This document is the contract for two follow-up agents:

- **WG-A2** (graph runtime): implements §3.1 (ambient), §3.2 (node painter),
  §3.3 (edge painter), §5 (perf cap) — JSX/canvas only, no styling.
- **WG-A3** (chrome): implements §3.4 (panel), §3.5 (KPI strip), §3.6
  (search + filters), §4 (animations), §9 (CSS tokens) — pure HTML/CSS.

Both agents must respect §1.7 (no override of existing CSS variables)
and §0 (the visual mix of HellBorn / BubbleMaps / Moonlight).
