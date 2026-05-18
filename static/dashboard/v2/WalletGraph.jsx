// static/dashboard/v2/WalletGraph.jsx
// =====================================================================
// Cosmograph-powered Wallet Graph (v2.0)
// ---------------------------------------------------------------------
// Replaces the legacy SVG renderer (dashboard-tabs.jsx::WalletGraph) with
// a WebGL2 force-directed view capable of fluidly rendering the full
// 2.6k leader + ~28k edge graph. Cosmograph is loaded via CDN in
// templates/dashboard.html (window.cosmograph).
//
// Wiring contract:
//   - Reassigns window.WalletGraph BEFORE dashboard-app.jsx destructures
//     it (see template load order). Keeps the legacy implementation
//     reachable via window.WalletGraphLegacy in case the operator wants
//     to A/B compare during the soak.
//   - Pulls its data from the global LiveStore snapshot.wallet_graph
//     (same as v1) — no new backend endpoint required. Backend caps were
//     bumped in queries.py to expose the full graph.
//   - Uses design-system primitives from window.UI.* when present,
//     falling back to inline styles so this file works in isolation
//     (Agent A's theme.css may not be deployed yet).
// =====================================================================

(function () {
  const { useState, useEffect, useMemo, useRef, useCallback } = React;
  const C = window.C || {
    bg: '#070809', panel: '#0c0e12', panel2: '#131720',
    border: '#1a1f2b', border2: '#2a3142',
    text: '#e8eaf0', dim: '#9aa1b3', dim2: '#5b6376',
    green: '#4ade80', red: '#f87171', amber: '#fbbf24',
    blue: '#60a5fa', purple: '#a78bfa',
  };
  const Badge   = window.Badge   || (({ type, children }) => <span style={{ padding: '1px 6px', fontSize: 9, border: `1px solid ${C.border2}`, color: C.text }}>{children}</span>);
  const Panel   = window.UI?.Panel || (({ children, style }) => <div style={{ background: C.panel, border: `1px solid ${C.border}`, ...(style || {}) }}>{children}</div>);
  const Pill    = window.UI?.Pill  || (({ active, onClick, color, children }) => (
    <button onClick={onClick} style={{
      background: active ? `${color || C.amber}14` : 'transparent',
      border: `1px solid ${active ? (color || C.amber) : C.border2}`,
      color: active ? (color || C.amber) : C.dim,
      padding: '3px 10px', fontSize: 10, cursor: 'pointer',
      fontFamily: "'JetBrains Mono', monospace", letterSpacing: '0.04em',
    }}>{children}</button>
  ));

  // ── Phase → color map ────────────────────────────────────────────────
  // Phase reflects the error-model maturity tier (see CLAUDE.md §7).
  // Followers stay muted-gray so leader phases dominate the visual.
  const phaseColor = (n) => {
    if (n.role !== 'leader') return '#5b6376';
    const p = n.phase || 1;
    if (p >= 3) return C.amber;   // LGBM, most mature
    if (p === 2) return C.blue;   // BayesianRidge
    return C.purple;              // Beta-Binomial bootstrap
  };

  // sqrt scaling on 24h activity keeps the dynamic range visually
  // useful (most leaders sit at 0-10 trades, a few at 200+).
  const nodeSize = (n) => 2 + Math.sqrt(Math.max(0, n.trades_24h || 0)) * 2;

  // Edge opacity uses follow_probability (0..1); width uses log(co_occurrences).
  const linkColor = (e) => `rgba(74,222,128,${0.10 + Math.min(1, e.p_follow || 0) * 0.50})`;
  const linkWidth = (e) => 0.5 + Math.log1p(e.co_occurrences || 1) * 0.5;

  const shortAddr = (a) => a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—';

  // ── Side panel: LeaderInspector ──────────────────────────────────────
  // Pure presentational component — receives the selected node + an
  // optional /api/wallet/<addr>/profile payload fetched on demand.
  const LeaderInspector = ({ node, profile, profileLoading, onClose, onZoom }) => {
    if (!node) return null;
    const isLeader = node.role === 'leader';
    const ph = node.phase || 1;
    const phLabel = ph >= 3 ? 'LGBM' : ph === 2 ? 'LOGREG' : 'BETA';
    const winColor = node.win_rate != null && node.win_rate >= 0.5 ? C.green : C.red;
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', height: '100%',
        background: C.panel, borderLeft: `1px solid ${C.border2}`,
        overflow: 'hidden',
      }}>
        <div style={{
          padding: '10px 14px', borderBottom: `1px solid ${C.border}`,
          display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0,
        }}>
          <span style={{ color: isLeader ? phaseColor(node) : C.dim, fontFamily: 'monospace', fontSize: 13, fontWeight: 700 }}>
            {shortAddr(node.id)}
          </span>
          <Badge type={isLeader ? (ph >= 3 ? 'green' : ph === 2 ? 'amber' : 'blue') : 'default'}>
            {isLeader ? `P${ph} ${phLabel}` : 'FOLLOWER'}
          </Badge>
          <button onClick={onZoom} title="Re-center camera on this node"
            style={{ marginLeft: 'auto', background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim, fontSize: 10, padding: '2px 8px', cursor: 'pointer', fontFamily: 'monospace' }}>⊕ zoom</button>
          <button onClick={onClose} title="Close (Esc)"
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim, fontSize: 11, padding: '2px 8px', cursor: 'pointer' }}>✕</button>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Top KPIs */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
            <KpiCell label="Falcon" value={(node.falcon_score || 0).toFixed(2)} color={C.amber} />
            <KpiCell label="Maturity" value={(node.maturity || 0).toFixed(2)} color={C.purple} />
            <KpiCell label="Trades 24h" value={node.trades_24h || 0} color={(node.trades_24h || 0) > 0 ? C.green : C.dim2} />
            <KpiCell label="Trades obs." value={(node.trades_observed || 0).toLocaleString()} color={C.text} />
            <KpiCell label="Resolved" value={node.positions_resolved || 0} color={C.green} />
            <KpiCell label="Win rate"
              value={node.win_rate != null ? `${(node.win_rate * 100).toFixed(0)}%` : '—'}
              color={node.win_rate != null ? winColor : C.dim2} />
          </div>

          {/* PnL + last action */}
          {isLeader && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              <KpiCell label="PnL (paper)"
                value={node.pnl_total != null ? `${node.pnl_total >= 0 ? '+' : '-'}$${Math.abs(node.pnl_total).toFixed(2)}` : '—'}
                color={node.pnl_total != null ? (node.pnl_total >= 0 ? C.green : C.red) : C.dim2} />
              <KpiCell label="Last action"
                value={node.last_action ? node.last_action.toUpperCase() : '—'}
                color={node.last_action === 'follow' ? C.green : node.last_action === 'fade' ? C.amber : C.dim2} />
            </div>
          )}

          {/* Top categories */}
          {Array.isArray(node.top_categories) && node.top_categories.length > 0 && (
            <div>
              <div style={{ fontSize: 9, color: C.dim2, letterSpacing: '0.08em', marginBottom: 6, textTransform: 'uppercase' }}>Top categories (30d)</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                {node.top_categories.slice(0, 5).map((c) => (
                  <div key={c.category} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}>
                    <span style={{ color: C.blue, minWidth: 90, fontFamily: 'monospace' }}>{c.category}</span>
                    <div style={{ flex: 1, height: 4, background: C.panel2 }}>
                      <div style={{ width: `${Math.round((c.pct || 0) * 100)}%`, height: '100%', background: C.amber }} />
                    </div>
                    <span style={{ color: C.text, fontFamily: 'monospace', minWidth: 38, textAlign: 'right' }}>
                      {Math.round((c.pct || 0) * 100)}% · {c.trades}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Profile drilldown (lazy fetch) */}
          {isLeader && (
            <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
              <div style={{ fontSize: 9, color: C.dim2, letterSpacing: '0.08em', marginBottom: 6, textTransform: 'uppercase' }}>Profile drill-down</div>
              {profileLoading
                ? <div style={{ color: C.dim2, fontSize: 11 }}>Loading…</div>
                : profile?._error
                  ? <div style={{ color: C.red, fontSize: 11 }}>Failed to fetch /api/wallet/{shortAddr(node.id)}/profile</div>
                  : profile
                    ? <ProfileSummary profile={profile} />
                    : <div style={{ color: C.dim2, fontSize: 11 }}>—</div>}
            </div>
          )}

          {/* Cross-tab nav */}
          <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10, display: 'flex', gap: 6 }}>
            <button onClick={() => window.PoybotNav?.selectWallet?.(node.id, { tabHint: 'graph', view: 'list' })}
              style={btnStyle()}>
              Open in scanner table
            </button>
          </div>
        </div>
      </div>
    );
  };

  const ProfileSummary = ({ profile }) => {
    const sizing = profile.sizing || {};
    const accuracy = profile.accuracy || {};
    const followerImpact = profile.follower_impact || {};
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 10 }}>
        {sizing.avg_size != null && (
          <Row label="Avg size" value={`$${Number(sizing.avg_size).toFixed(0)}`} />
        )}
        {sizing.ewma_size != null && (
          <Row label="EWMA size" value={`$${Number(sizing.ewma_size).toFixed(0)}`} />
        )}
        {accuracy.overall != null && (
          <Row label="Accuracy (overall)" value={`${(accuracy.overall * 100).toFixed(0)}%`} color={accuracy.overall >= 0.5 ? C.green : C.red} />
        )}
        {followerImpact.followers_activated != null && (
          <Row label="Followers activated" value={followerImpact.followers_activated} />
        )}
      </div>
    );
  };

  const Row = ({ label, value, color }) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '2px 0', borderBottom: `1px dotted ${C.border}` }}>
      <span style={{ color: C.dim2 }}>{label}</span>
      <span style={{ color: color || C.text, fontFamily: 'monospace' }}>{value}</span>
    </div>
  );

  const KpiCell = ({ label, value, color }) => (
    <div style={{ background: C.panel2, padding: '8px 10px', border: `1px solid ${C.border}` }}>
      <div style={{ fontSize: 9, color: C.dim2, letterSpacing: '0.08em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: color || C.text, fontFamily: 'monospace', marginTop: 2 }}>{value}</div>
    </div>
  );

  const btnStyle = () => ({
    background: 'transparent', border: `1px solid ${C.border2}`,
    color: C.dim, padding: '4px 10px', fontSize: 10,
    cursor: 'pointer', fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: '0.04em', flex: 1,
  });

  // ── Loading splash ───────────────────────────────────────────────────
  const LoadingSplash = ({ message }) => (
    <div style={{
      position: 'absolute', inset: 0, display: 'flex',
      alignItems: 'center', justifyContent: 'center',
      flexDirection: 'column', gap: 10, pointerEvents: 'none',
      background: 'rgba(7,8,9,0.6)', zIndex: 4,
    }}>
      <div style={{ width: 38, height: 38, border: `2px solid ${C.border2}`, borderTopColor: C.amber, borderRadius: '50%', animation: 'spin 0.8s linear infinite' }} />
      <div style={{ color: C.dim, fontSize: 11, letterSpacing: '0.08em' }}>{message || 'Loading…'}</div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );

  // ── Main component ──────────────────────────────────────────────────
  const WalletGraph = () => {
    const liveStore = (window.useLiveStore || (() => ({ snapshot: null, connectionState: 'connecting' })))();
    const { snapshot, connectionState } = liveStore;
    const wg = snapshot?.wallet_graph || { nodes: [], edges: [], stats: {} };

    // ── Filter state ───────────────────────────────────────────────────
    const usePersisted = window.usePersistedState || ((k, v) => useState(v));
    const [search, setSearch] = useState('');
    const [activePhases, setActivePhases] = usePersisted('wgv2.phases', [1, 2, 3]);
    const [minCoocc, setMinCoocc] = usePersisted('wgv2.minCoocc', 5);
    const [minProb, setMinProb]   = usePersisted('wgv2.minProb', 0.30);
    const [showFollowers, setShowFollowers] = usePersisted('wgv2.showFollowers', true);

    // ── Canvas + cosmograph instance ───────────────────────────────────
    const canvasRef = useRef(null);
    const cosmoRef = useRef(null);
    const [selected, setSelected] = useState(null);   // selected node object
    const [hovered, setHovered] = useState(null);     // {node, x, y} for tooltip
    const [walletProfile, setWalletProfile] = useState(null);
    const [walletProfileLoading, setWalletProfileLoading] = useState(false);

    // Compute the filtered dataset that we actually push to Cosmograph.
    const filtered = useMemo(() => {
      if (!wg.nodes.length) return { nodes: [], edges: [] };
      const phaseSet = new Set(activePhases);
      const matchSearch = (n) => !search || (n.id || '').toLowerCase().includes(search.toLowerCase());

      // Pass 1 — filter edges, then derive the set of node ids we need.
      const eOk = wg.edges.filter((e) =>
        (e.co_occurrences || 0) >= minCoocc &&
        (e.p_follow || 0) >= minProb
      );
      const keepIds = new Set();
      eOk.forEach((e) => { keepIds.add(e.source); keepIds.add(e.target); });

      // Pass 2 — keep nodes that satisfy phase + role + (optionally) search.
      // We always keep selected node so it stays visible during slider tweaks.
      const nOk = wg.nodes.filter((n) => {
        if (selected && n.id === selected.id) return true;
        if (n.role === 'leader' && !phaseSet.has(n.phase || 1)) return false;
        if (n.role === 'follower' && !showFollowers) return false;
        if (!keepIds.has(n.id)) return false;
        if (!matchSearch(n)) return false;
        return true;
      });
      // Drop edges whose endpoints didn't survive.
      const surviving = new Set(nOk.map((n) => n.id));
      const eOk2 = eOk.filter((e) => surviving.has(e.source) && surviving.has(e.target));
      return { nodes: nOk, edges: eOk2 };
    }, [wg.nodes, wg.edges, activePhases, minCoocc, minProb, showFollowers, search, selected]);

    // Bootstrap Cosmograph instance once the canvas + window.cosmograph
    // global are both ready. The library script is loaded via CDN in
    // templates/dashboard.html and may race the React mount on a cold
    // cache, so we poll for a few ticks before giving up.
    const [cosmoReady, setCosmoReady] = useState(!!window.cosmograph);
    useEffect(() => {
      if (cosmoReady) return;
      let cancelled = false;
      let tries = 0;
      const tick = () => {
        if (cancelled) return;
        if (window.cosmograph) { setCosmoReady(true); return; }
        if (++tries > 50) return;     // ~5s budget
        setTimeout(tick, 100);
      };
      tick();
      return () => { cancelled = true; };
    }, [cosmoReady]);

    useEffect(() => {
      if (!cosmoReady || !canvasRef.current) return;
      // Cosmograph exposes both `new cosmograph.Cosmograph(canvas, cfg)` and a
      // top-level `Cosmograph` constructor depending on the build. Probe both.
      const Ctor =
        (window.cosmograph && (window.cosmograph.Cosmograph || window.cosmograph.default || window.cosmograph)) ||
        window.Cosmograph;
      if (!Ctor || typeof Ctor !== 'function') {
        console.warn('[WalletGraph v2] cosmograph global not callable; got', Ctor);
        return;
      }
      let instance;
      try {
        instance = new Ctor(canvasRef.current, {
          backgroundColor: C.bg,
          nodeColor: phaseColor,
          nodeSize,
          nodeLabelAccessor: (n) => shortAddr(n.id),
          linkColor,
          linkWidth,
          linkArrows: false,
          // Force-layout knobs tuned for ~3k nodes / ~30k edges. Decay is in
          // milliseconds — we want the layout to settle within ~1.5s.
          simulationFriction:   0.85,
          simulationRepulsion:  0.5,
          simulationGravity:    0.05,
          simulationDecay:      1500,
          simulationLinkDistance: 8,
          simulationLinkSpring:   0.3,
          // Hover/click dimming so the selected ego-network pops.
          nodeGreyoutOpacity:   0.10,
          linkGreyoutOpacity:   0.05,
          showFPSMonitor:       false,
          hoveredNodeRingColor: C.amber,
          focusedNodeRingColor: C.amber,
          renderHoveredNodeRing: true,
          renderLinks:          true,
          onClick: (clicked) => {
            if (!clicked) {
              setSelected(null);
              try { instance.unselectNodes?.(); } catch (_) {}
              return;
            }
            setSelected(clicked);
            try {
              const neighbors = (instance.getAdjacentNodes?.(clicked.id) || []);
              instance.selectNodes?.([clicked, ...neighbors]);
              instance.zoomToNode?.(clicked);
            } catch (e) { /* defensive — older builds lack these helpers */ }
          },
          onNodeMouseOver: (node, _i, _link, evt) => {
            if (!node) { setHovered(null); return; }
            setHovered({
              node,
              x: evt?.clientX ?? evt?.offsetX ?? 0,
              y: evt?.clientY ?? evt?.offsetY ?? 0,
            });
            try {
              const neighbors = (instance.getAdjacentNodes?.(node.id) || []);
              instance.selectNodes?.([node, ...neighbors]);
            } catch (_) {}
          },
          onNodeMouseOut: () => {
            setHovered(null);
            try { instance.unselectNodes?.(); } catch (_) {}
          },
        });
      } catch (e) {
        console.error('[WalletGraph v2] cosmograph init failed:', e);
        return;
      }
      cosmoRef.current = instance;
      return () => {
        try { instance.destroy?.(); } catch (_) {}
        cosmoRef.current = null;
      };
    }, [cosmoReady]);

    // Push filtered data into the renderer whenever the filter changes
    // or a new snapshot lands. Cosmograph's setData is incremental — it
    // diffs the dataset internally, so this is cheap.
    useEffect(() => {
      const inst = cosmoRef.current;
      if (!inst || !filtered.nodes.length) return;
      let fallback;
      try {
        // The API has shifted across minor versions; try both signatures.
        if (typeof inst.setData === 'function') {
          inst.setData(filtered.nodes, filtered.edges, true);
        } else if (typeof inst.setNodes === 'function') {
          inst.setNodes(filtered.nodes);
          inst.setLinks?.(filtered.edges);
        }
        // Auto-fit after setData. We use a double rAF so the WebGL renderer
        // has time to (a) lay out its initial frame and (b) compute node
        // bounds — a single rAF fires before bounds are stable on a 4889-edge
        // dataset, which is why fitView() previously left the graph clipped
        // outside the viewport. A 500ms setTimeout fallback covers the heavy-
        // load case where two rAFs aren't enough.
        const fit = () => { try { cosmoRef.current?.fitView?.(); } catch (_) {} };
        requestAnimationFrame(() => requestAnimationFrame(fit));
        fallback = setTimeout(fit, 500);
      } catch (e) {
        console.warn('[WalletGraph v2] setData failed:', e.message);
      }
      return () => { if (fallback) clearTimeout(fallback); };
    }, [filtered, cosmoReady]);

    // Re-fit when the container is resized (window resize, side panel
    // toggle, tab switch reflow). ResizeObserver fires once layout settles.
    useEffect(() => {
      if (!cosmoReady || !canvasRef.current) return;
      const target = canvasRef.current.parentElement || canvasRef.current;
      if (typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(() => {
        try { cosmoRef.current?.fitView?.(); } catch (_) {}
      });
      observer.observe(target);
      return () => observer.disconnect();
    }, [cosmoReady]);

    // Lazy profile fetch when selection changes.
    useEffect(() => {
      if (!selected || selected.role !== 'leader') { setWalletProfile(null); return; }
      let cancelled = false;
      setWalletProfileLoading(true);
      const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
      fetch(`${base}/api/wallet/${selected.id}/profile`)
        .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
        .then((d) => { if (!cancelled) setWalletProfile(d); })
        .catch((e) => { if (!cancelled) { console.warn('[WalletGraph v2] profile fetch failed', e); setWalletProfile({ _error: true }); } })
        .finally(() => { if (!cancelled) setWalletProfileLoading(false); });
      return () => { cancelled = true; };
    }, [selected]);

    // Cross-tab nav: another tab can ask the graph to focus on a wallet.
    useEffect(() => {
      const handler = (e) => {
        const w = e.detail?.wallet;
        if (!w) return;
        const node = (wg.nodes || []).find((n) => n.id === w);
        if (node) {
          setSelected(node);
          try {
            const inst = cosmoRef.current;
            inst?.selectNodes?.([node]);
            inst?.zoomToNode?.(node);
          } catch (_) {}
        }
      };
      window.addEventListener('pmi:select-wallet', handler);
      return () => window.removeEventListener('pmi:select-wallet', handler);
    }, [wg.nodes]);

    // Topbar context publish (matches v1 behavior).
    useEffect(() => {
      if (!selected) { window.PoybotNav?.clearContext?.(); return; }
      window.PoybotNav?.setContext?.({
        type: 'wallet', id: selected.id,
        label: shortAddr(selected.id),
      });
    }, [selected]);

    const togglePhase = (p) => setActivePhases((prev) =>
      prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p].sort()
    );

    // Header status counts (visible after filtering).
    const visibleNodes = filtered.nodes.length;
    const visibleEdges = filtered.edges.length;
    const totalNodes   = wg.nodes.length;
    const totalEdges   = wg.edges.length;

    // Connection chip color.
    const connColor = { connected: C.green, reconnecting: C.amber, connecting: C.amber, disconnected: C.red }[connectionState] || C.dim2;

    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden', position: 'relative' }}>

        {/* ── Panel header ───────────────────────────────────────────── */}
        <div style={{
          padding: '8px 14px', borderBottom: `1px solid ${C.border}`,
          display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0,
          background: C.panel,
        }}>
          <span style={{ color: C.amber, fontWeight: 700, fontSize: 11, letterSpacing: '0.08em' }}>WALLET GRAPH</span>
          <span style={{
            background: 'rgba(167,139,250,0.10)', color: C.purple,
            border: `1px solid ${C.purple}40`,
            padding: '1px 6px', fontSize: 9, fontWeight: 700, letterSpacing: '0.06em',
            fontFamily: 'monospace',
          }} title="Cosmograph WebGL v2.0 — hard-refresh if you see the old SVG renderer">
            v2.0 · COSMOGRAPH
          </span>
          <span style={{ color: C.border2 }}>│</span>
          <span style={{ color: C.dim, fontSize: 10, fontFamily: 'monospace' }}>
            <span style={{ color: C.text, fontWeight: 700 }}>{visibleNodes.toLocaleString()}</span>
            <span style={{ color: C.dim2 }}> / {totalNodes.toLocaleString()} nodes</span>
            <span style={{ color: C.border2, margin: '0 8px' }}>·</span>
            <span style={{ color: C.text, fontWeight: 700 }}>{visibleEdges.toLocaleString()}</span>
            <span style={{ color: C.dim2 }}> / {totalEdges.toLocaleString()} edges</span>
          </span>
          <span style={{ marginLeft: 'auto', color: connColor, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em' }}>
            {connectionState === 'connected' ? '● LIVE' : connectionState === 'disconnected' ? '○ OFFLINE' : '◐ ' + connectionState.toUpperCase()}
          </span>
        </div>

        {/* ── Toolbar (search + phase pills + sliders) ──────────────── */}
        <div style={{
          padding: '8px 14px', borderBottom: `1px solid ${C.border}`,
          display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0,
          background: C.panel, flexWrap: 'wrap',
        }}>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search wallet…"
            style={{
              background: C.panel2, border: `1px solid ${C.border2}`, color: C.text,
              padding: '4px 10px', fontSize: 11, outline: 'none',
              width: 200, fontFamily: 'monospace',
            }} />

          <div style={{ display: 'flex', gap: 4 }}>
            <Pill active={activePhases.length === 3} color={C.text}
              onClick={() => setActivePhases([1, 2, 3])}>All</Pill>
            <Pill active={activePhases.includes(1)} color={C.purple} onClick={() => togglePhase(1)}>P1</Pill>
            <Pill active={activePhases.includes(2)} color={C.blue}   onClick={() => togglePhase(2)}>P2</Pill>
            <Pill active={activePhases.includes(3)} color={C.amber}  onClick={() => togglePhase(3)}>P3</Pill>
          </div>

          <SliderControl label="co-occ ≥" value={minCoocc} min={1} max={50} step={1} onChange={setMinCoocc} />
          <SliderControl label="P(follow) ≥" value={minProb} min={0} max={1} step={0.05} onChange={setMinProb} valueFmt={(v) => v.toFixed(2)} />

          <Pill active={showFollowers} color={C.dim} onClick={() => setShowFollowers(!showFollowers)}>
            {showFollowers ? '● followers' : '○ followers'}
          </Pill>

          <button onClick={() => {
            try { cosmoRef.current?.fitView?.(); } catch (_) {}
          }} style={{
            background: 'transparent', border: `1px solid ${C.border2}`,
            color: C.dim, padding: '3px 10px', fontSize: 10, cursor: 'pointer',
            fontFamily: 'monospace', marginLeft: 'auto',
          }} title="Fit view">⊡ fit</button>
        </div>

        {/* ── Main area: canvas + optional side panel ───────────────── */}
        <div style={{
          flex: 1, display: 'grid',
          gridTemplateColumns: selected ? 'minmax(0, 1fr) minmax(320px, 380px)' : '1fr',
          overflow: 'hidden', minHeight: 0,
        }}>
          <div style={{ position: 'relative', overflow: 'hidden' }}>
            <canvas ref={canvasRef} style={{
              display: 'block', width: '100%', height: '100%',
              background: C.bg,
            }} />

            {/* Loading splash while we wait for the first snapshot */}
            {(!wg.nodes.length) && (
              <LoadingSplash message={connectionState === 'connected'
                ? `Loading ${totalEdges > 0 ? totalEdges.toLocaleString() + '+' : '28,000+'} edges…`
                : 'Connecting to backend…'} />
            )}
            {(wg.nodes.length > 0 && !cosmoReady) && (
              <LoadingSplash message="Loading Cosmograph WebGL renderer…" />
            )}

            {/* Hover tooltip */}
            {hovered?.node && (
              <div style={{
                position: 'fixed', left: hovered.x + 14, top: hovered.y + 14,
                background: C.panel, border: `1px solid ${C.border2}`,
                padding: '8px 10px', fontSize: 10, fontFamily: 'monospace',
                color: C.text, pointerEvents: 'none', zIndex: 100,
                minWidth: 200, boxShadow: '0 4px 14px rgba(0,0,0,0.6)',
              }}>
                <div style={{ color: phaseColor(hovered.node), fontWeight: 700, fontSize: 11, marginBottom: 4 }}>
                  {shortAddr(hovered.node.id)}
                </div>
                <TooltipRow label="role" value={hovered.node.role} color={hovered.node.role === 'leader' ? C.purple : C.dim} />
                {hovered.node.role === 'leader' && <TooltipRow label="phase" value={`P${hovered.node.phase || 1}`} color={C.amber} />}
                <TooltipRow label="falcon" value={(hovered.node.falcon_score || 0).toFixed(2)} color={C.amber} />
                <TooltipRow label="trades 24h" value={hovered.node.trades_24h || 0} color={(hovered.node.trades_24h || 0) > 0 ? C.green : C.dim2} />
                <TooltipRow label="win rate"
                  value={hovered.node.win_rate != null ? `${(hovered.node.win_rate * 100).toFixed(0)}%` : '—'}
                  color={hovered.node.win_rate != null && hovered.node.win_rate >= 0.5 ? C.green : C.red} />
                <TooltipRow label="last action" value={hovered.node.last_action || '—'} color={C.text} />
                <div style={{ color: C.dim2, fontSize: 9, marginTop: 4, fontStyle: 'italic' }}>click to inspect</div>
              </div>
            )}

            {/* Legend (bottom-left) */}
            <div style={{
              position: 'absolute', left: 10, bottom: 10,
              background: 'rgba(12,14,18,0.85)', border: `1px solid ${C.border}`,
              padding: '6px 10px', fontSize: 9, color: C.dim, fontFamily: 'monospace',
              display: 'flex', flexDirection: 'column', gap: 3, pointerEvents: 'none',
            }}>
              <div style={{ color: C.amber, fontWeight: 700, marginBottom: 2 }}>LEGEND</div>
              <LegendDot color={C.purple} label="phase 1 (beta)" />
              <LegendDot color={C.blue}   label="phase 2 (logreg)" />
              <LegendDot color={C.amber}  label="phase 3 (lgbm)" />
              <LegendDot color="#5b6376"  label="follower" />
              <div style={{ marginTop: 4, color: C.dim2 }}>node size ∝ √(trades 24h)</div>
              <div style={{ color: C.dim2 }}>edge α ∝ p(follow); width ∝ log(co-occ)</div>
            </div>
          </div>

          {selected && (
            <LeaderInspector
              node={selected}
              profile={walletProfile}
              profileLoading={walletProfileLoading}
              onClose={() => { setSelected(null); try { cosmoRef.current?.unselectNodes?.(); } catch (_) {} }}
              onZoom={() => { try { cosmoRef.current?.zoomToNode?.(selected); } catch (_) {} }}
            />
          )}
        </div>
      </div>
    );
  };

  const SliderControl = ({ label, value, min, max, step, onChange, valueFmt }) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ color: C.dim2, fontSize: 10, fontFamily: 'monospace' }}>{label}</span>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: 90, accentColor: C.amber, cursor: 'pointer' }} />
      <span style={{ color: C.amber, fontSize: 10, fontFamily: 'monospace', minWidth: 30, textAlign: 'right' }}>
        {valueFmt ? valueFmt(value) : value}
      </span>
    </div>
  );

  const TooltipRow = ({ label, value, color }) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
      <span style={{ color: C.dim2 }}>{label}</span>
      <span style={{ color: color || C.text }}>{String(value)}</span>
    </div>
  );

  const LegendDot = ({ color, label }) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: color, display: 'inline-block' }} />
      <span>{label}</span>
    </div>
  );

  // Keep the v1 SVG renderer reachable for side-by-side debugging.
  if (window.WalletGraph && !window.WalletGraphLegacy) {
    window.WalletGraphLegacy = window.WalletGraph;
  }
  window.WalletGraph = WalletGraph;
})();
