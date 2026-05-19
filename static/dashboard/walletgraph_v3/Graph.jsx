// static/dashboard/walletgraph_v3/Graph.jsx
// Polymarket V3 — Wallet Graph "univers" core renderer.
// Consumes snapshot.wallet_graph from window.useLiveStore().
// Depends on window.ForceGraph2D (loaded via UMD CDN by WG-A6).
// Composes window.WG_V3.BackgroundAmbient (WG-A3) when available.
// Click on node fires onSelect(nodeData) prop (wired by WG-A6 to SelectionPanel).
//
// WG-DISCOVERY: BubbleMaps-style 3-mode visualizer.
//   - discovery: leaders only (top-100 default), edges leader↔leader only,
//                p_follow ≥ 0.7. Tighter forces (no leaderPull needed).
//   - network:   top-200 leaders + top-3 followers each, edges p_follow ≥ 0.6.
//   - full:      top-N leaders + ALL connected followers, no edge filter.
// 6 supernodes pinned in a hexagon at radius 500; top-30 leaders labeled.

(function () {
  'use strict';

  const PHASE_COLORS = {
    1: { core: '#3b82f6', glow: 'rgba(59, 130, 246, ' },
    2: { core: '#f59e0b', glow: 'rgba(245, 158, 11, ' },
    3: { core: '#10b981', glow: 'rgba(16, 185, 129, ' },
  };
  const FOLLOWER_COLOR = { core: '#a78bfa', glow: 'rgba(167, 139, 250, ' };
  const EXCLUDED_COLOR = { core: '#475569', glow: 'rgba(71, 85, 105, ' };

  // WG-DISCOVERY: 3-mode toggle with per-mode density cycles.
  const VIEW_MODES = ['discovery', 'network', 'full'];
  const DEFAULT_MODE = 'discovery';
  const DENSITY_CYCLES = {
    discovery: [50, 100, 200],
    network:   [100, 200, 500],
    full:      [200, 500, 3000],
  };
  const DEFAULT_DENSITY_INDEX = {
    discovery: 1,  // 100
    network:   1,  // 200
    full:      1,  // 500
  };
  const MODE_BADGE = {
    discovery: { icon: '◐', label: 'Discovery', color: '#3b82f6' },
    network:   { icon: '◑', label: 'Network',   color: '#f59e0b' },
    full:      { icon: '◉', label: 'Full',      color: '#10b981' },
  };

  // Top-6 supernodes pinned in a hexagon at radius 500 (was 10 @ 350).
  const SUPER_COUNT = 6;
  const SUPER_RADIUS = 500;
  const LABEL_TOP_N = 30; // label the top-30 leaders, not just supernodes

  // Per-mode force tuning. Discovery has no followers so we can tighten
  // charge + link distance; network/full keep the leaderPull custom force.
  const FORCES_BY_MODE = {
    discovery: { charge: -400, linkDist: 120, linkStrength: 0.5,  useLeaderPull: false },
    network:   { charge: -250, linkDist: 70,  linkStrength: 0.55, useLeaderPull: true  },
    full:      { charge: -200, linkDist: 50,  linkStrength: 0.6,  useLeaderPull: true  },
  };

  // Edge filter thresholds per mode (p_follow floor).
  const MIN_PFOLLOW_BY_MODE = { discovery: 0.7, network: 0.6, full: 0 };

  function getNodeColor(node) {
    if (node.exclude_reason) return EXCLUDED_COLOR;
    if (node.role === 'follower') return FOLLOWER_COLOR;
    return PHASE_COLORS[node.phase] || PHASE_COLORS[1];
  }

  // Supernodes get 2× size for prominence (vs siblings on the hexagon).
  function getNodeRadius(node) {
    const t24 = (node && node.trades_24h) || 1;
    const base = Math.max(4, Math.min(24, Math.pow(t24, 0.55) * 2.0));
    return (node && node._isSupernode) ? base * 2.0 : base;
  }

  // 4-layer node painter: outer-glow + halo + inner-glow + core.
  // Supernodes also get a permanent white ring (r+2).
  function paintNode(node, ctx, globalScale, isSelected, isHovered) {
    if (typeof node.x !== 'number' || typeof node.y !== 'number') return;
    const { core, glow } = getNodeColor(node);
    const baseR = getNodeRadius(node);
    const r = isHovered ? baseR * 1.4 : baseR;
    const x = node.x;
    const y = node.y;

    // 1) Outer glow (very subtle, very wide — r*6).
    const outerR = r * 6;
    const outerGrad = ctx.createRadialGradient(x, y, 0, x, y, outerR);
    outerGrad.addColorStop(0, glow + '0.15)');
    outerGrad.addColorStop(1, glow + '0)');
    ctx.fillStyle = outerGrad;
    ctx.fillRect(x - outerR, y - outerR, outerR * 2, outerR * 2);

    // 2) Halo (medium — r*3).
    const haloGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 3);
    haloGrad.addColorStop(0, glow + '0.55)');
    haloGrad.addColorStop(1, glow + '0)');
    ctx.fillStyle = haloGrad;
    ctx.fillRect(x - r * 3, y - r * 3, r * 6, r * 6);

    // 3) Inner glow.
    const glowGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 1.6);
    glowGrad.addColorStop(0, glow + '0.85)');
    glowGrad.addColorStop(1, glow + '0.5)');
    ctx.fillStyle = glowGrad;
    ctx.beginPath();
    ctx.arc(x, y, r * 1.6, 0, Math.PI * 2);
    ctx.fill();

    // 4) Solid core + 1px white outline.
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // 4b) Supernode ring — permanent, sits just outside the core.
    if (node._isSupernode) {
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.65)';
      ctx.lineWidth = 2 / Math.max(globalScale, 0.001);
      ctx.beginPath();
      ctx.arc(x, y, r + 2 / Math.max(globalScale, 0.001), 0, Math.PI * 2);
      ctx.stroke();
    }

    // 5) Selected: big aura + thick white ring.
    if (isSelected) {
      const selR = r * 9;
      const selGrad = ctx.createRadialGradient(x, y, 0, x, y, selR);
      selGrad.addColorStop(0, glow + '0.4)');
      selGrad.addColorStop(1, glow + '0)');
      ctx.fillStyle = selGrad;
      ctx.fillRect(x - selR, y - selR, selR * 2, selR * 2);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
      ctx.lineWidth = Math.max(2.5, 3 / Math.max(globalScale, 0.001));
      ctx.beginPath();
      ctx.arc(x, y, r + 1.5, 0, Math.PI * 2);
      ctx.stroke();
      ctx.shadowBlur = 24;
      ctx.shadowColor = '#ffffff';
      ctx.stroke();
      ctx.shadowBlur = 0;
    } else if (isHovered) {
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.55)';
      ctx.lineWidth = Math.max(1.5, 1.5 / Math.max(globalScale, 0.001));
      ctx.beginPath();
      ctx.arc(x, y, r + 1, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 6) Label — supernodes always, top-30 leaders also when _labeled is set.
    if (node._isSupernode || node._labeled) {
      const label = node.label || (typeof node.id === 'string'
        ? (node.id.slice(0, 6) + '…' + node.id.slice(-4))
        : String(node.id));
      const fontPx = Math.max(9, 11 / Math.max(globalScale, 0.001));
      ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
      ctx.font = fontPx + 'px JetBrains Mono, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.shadowBlur = 6;
      ctx.shadowColor = 'rgba(0, 0, 0, 0.7)';
      ctx.fillText(label, x, y + r + 6 / Math.max(globalScale, 0.001));
      ctx.shadowBlur = 0;
    }
  }

  // Edge painter: amped opacity, amber on selection.
  function paintEdge(edge, ctx, selectedId) {
    const s = edge.source;
    const t = edge.target;
    if (!s || !t || typeof s.x !== 'number' || typeof t.x !== 'number') return;
    const p = typeof edge.p_follow === 'number' ? edge.p_follow : 0.5;
    const co = edge.co_occurrences || 1;
    const sId = (s && s.id !== undefined) ? s.id : s;
    const tId = (t && t.id !== undefined) ? t.id : t;
    const incident = selectedId && (sId === selectedId || tId === selectedId);
    let alpha;
    let stroke;
    let width = Math.max(1.2, Math.log(co + 1) * 0.9);
    if (incident) {
      alpha = 0.9;
      stroke = 'rgba(245, 158, 11, ' + alpha.toFixed(3) + ')';
      width *= 1.5;
    } else {
      alpha = 0.28 + p * 0.6;
      stroke = 'rgba(255, 255, 255, ' + alpha.toFixed(3) + ')';
    }
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.moveTo(s.x, s.y);
    ctx.lineTo(t.x, t.y);
    ctx.stroke();
  }

  function WalletGraphV3(props) {
    const onSelect = (props && props.onSelect) || null;
    const useLiveStore = window.useLiveStore;
    const live = typeof useLiveStore === 'function' ? useLiveStore() : { snapshot: null };
    const snapshot = (live && live.snapshot) || null;

    const fgRef = React.useRef(null);
    const containerRef = React.useRef(null);
    const [size, setSize] = React.useState({ w: 800, h: 600 });
    const [hoverNode, setHoverNode] = React.useState(null);
    const [selectedId, setSelectedId] = React.useState(null);

    // WG-DISCOVERY: viewing mode + per-mode density index.
    const [mode, setMode] = React.useState(DEFAULT_MODE);
    const [densityIndex, setDensityIndex] = React.useState(
      DEFAULT_DENSITY_INDEX[DEFAULT_MODE]
    );

    // When mode flips, reset density to the per-mode default.
    React.useEffect(() => {
      setDensityIndex(DEFAULT_DENSITY_INDEX[mode]);
    }, [mode]);

    const currentDensityCycle = DENSITY_CYCLES[mode];
    const densityN = currentDensityCycle[densityIndex % currentDensityCycle.length];

    const wg = (snapshot && snapshot.wallet_graph) || {};
    const allNodes = Array.isArray(wg.nodes) ? wg.nodes : [];
    const allEdges = Array.isArray(wg.edges) ? wg.edges : [];

    // Build an id → node lookup for O(1) role checks during edge filtering.
    const allNodesById = React.useMemo(() => {
      const m = new Map();
      for (let i = 0; i < allNodes.length; i++) m.set(allNodes[i].id, allNodes[i]);
      return m;
    }, [allNodes]);

    // Degree by leader id — used to rank supernodes and visible leaders.
    const degreeByLeader = React.useMemo(() => {
      const map = new Map();
      for (let i = 0; i < allEdges.length; i++) {
        const e = allEdges[i];
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        if (sid === undefined || sid === null) continue;
        map.set(sid, (map.get(sid) || 0) + 1);
      }
      return map;
    }, [allEdges]);

    // Pre-rank all leaders by degree (then trades_24h tiebreak).
    const rankedLeaders = React.useMemo(() => {
      if (!allNodes.length) return [];
      const leaderPool = allNodes.filter((n) => degreeByLeader.has(n.id));
      const pool = leaderPool.length ? leaderPool : allNodes.slice();
      return pool.sort((a, b) => {
        const da = degreeByLeader.get(a.id) || 0;
        const db = degreeByLeader.get(b.id) || 0;
        if (db !== da) return db - da;
        return (b.trades_24h || 0) - (a.trades_24h || 0);
      });
    }, [allNodes, degreeByLeader]);

    // WG-DISCOVERY: mode-aware visible node selection.
    //   discovery → top-N leaders, no followers
    //   network   → top-N leaders + each leader's top-3 followers (by p_follow)
    //   full      → top-N leaders + ALL connected followers
    const visibleScope = React.useMemo(() => {
      if (!rankedLeaders.length) return { ids: new Set(), leaders: [] };
      const leaders = rankedLeaders.slice(0, densityN);
      const leaderIds = new Set(leaders.map((n) => n.id));

      if (mode === 'discovery') {
        return { ids: leaderIds, leaders: leaders };
      }

      if (mode === 'network') {
        // Per leader, pick top-3 edges by p_follow → collect those follower ids.
        const followers = new Set();
        const edgesByLeader = new Map();
        for (let i = 0; i < allEdges.length; i++) {
          const e = allEdges[i];
          const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
          const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
          if (!leaderIds.has(sid)) continue;
          if (leaderIds.has(tid)) continue; // leader→leader handled in edge step
          let arr = edgesByLeader.get(sid);
          if (!arr) { arr = []; edgesByLeader.set(sid, arr); }
          arr.push({ tid: tid, p: e.p_follow || 0 });
        }
        edgesByLeader.forEach(function (arr) {
          arr.sort(function (a, b) { return b.p - a.p; });
          for (let j = 0; j < Math.min(3, arr.length); j++) {
            followers.add(arr[j].tid);
          }
        });
        const ids = new Set(leaderIds);
        followers.forEach(function (fid) { ids.add(fid); });
        return { ids: ids, leaders: leaders };
      }

      // full mode → all connected followers.
      const followers = new Set();
      for (let i = 0; i < allEdges.length; i++) {
        const e = allEdges[i];
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
        if (leaderIds.has(sid) && !leaderIds.has(tid)) followers.add(tid);
        else if (leaderIds.has(tid) && !leaderIds.has(sid)) followers.add(sid);
      }
      const ids = new Set(leaderIds);
      followers.forEach(function (fid) { ids.add(fid); });
      return { ids: ids, leaders: leaders };
    }, [rankedLeaders, allEdges, mode, densityN]);

    // Materialize the visible node list (leaders first, then followers).
    const visibleNodes = React.useMemo(() => {
      if (!visibleScope.ids.size) return [];
      const leaderIdSet = new Set(visibleScope.leaders.map((n) => n.id));
      const extras = [];
      for (let i = 0; i < allNodes.length; i++) {
        const n = allNodes[i];
        if (visibleScope.ids.has(n.id) && !leaderIdSet.has(n.id)) extras.push(n);
      }
      return visibleScope.leaders.concat(extras);
    }, [allNodes, visibleScope]);

    // Edge filter: mode-specific p_follow floor + leader↔leader-only in discovery.
    const visibleEdges = React.useMemo(() => {
      if (!allEdges.length) return [];
      const minP = MIN_PFOLLOW_BY_MODE[mode];
      return allEdges.filter((e) => {
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
        if (!visibleScope.ids.has(sid) || !visibleScope.ids.has(tid)) return false;
        if (mode === 'discovery') {
          // Only leader↔leader edges. Followers were excluded from scope.ids
          // already, but defensively check the role too.
          const sNode = allNodesById.get(sid);
          const tNode = allNodesById.get(tid);
          if ((sNode && sNode.role === 'follower') ||
              (tNode && tNode.role === 'follower')) return false;
        }
        return (e.p_follow || 0) >= minP;
      });
    }, [allEdges, visibleScope, mode, allNodesById]);

    // ForceGraph2D mutates link.source/target into objects; spread to avoid touching upstream.
    // We stamp the top-6 supernodes (hexagon @ radius 500) and mark the top-30
    // leaders for labeling. Non-supernodes get any stale fx/fy wiped.
    const graphData = React.useMemo(() => {
      const supernodeIds = new Set();
      const visibleByDegree = visibleNodes
        .filter((n) => degreeByLeader.has(n.id))
        .sort((a, b) => (degreeByLeader.get(b.id) || 0) - (degreeByLeader.get(a.id) || 0));
      const topSupers = visibleByDegree.slice(0, SUPER_COUNT);
      const topLabeled = visibleByDegree.slice(0, LABEL_TOP_N);
      const labeledIds = new Set(topLabeled.map((n) => n.id));

      const nodes = visibleNodes.map((n) => {
        const copy = Object.assign({}, n);
        if (copy.fx !== undefined) delete copy.fx;
        if (copy.fy !== undefined) delete copy.fy;
        copy._isSupernode = false;
        copy._labeled = labeledIds.has(n.id);
        return copy;
      });
      const byId = new Map(nodes.map((n) => [n.id, n]));
      topSupers.forEach((node, i) => {
        const target = byId.get(node.id);
        if (!target) return;
        supernodeIds.add(node.id);
        target._isSupernode = true;
        // Hexagon (6 vertices) at radius 500, starting at top (−π/2).
        const angle = (i / Math.max(topSupers.length, 1)) * 2 * Math.PI - Math.PI / 2;
        target.fx = SUPER_RADIUS * Math.cos(angle);
        target.fy = SUPER_RADIUS * Math.sin(angle);
        target.x = target.fx;
        target.y = target.fy;
      });

      return {
        nodes: nodes,
        links: visibleEdges.map((e) => ({ ...e })),
        _supernodeIds: supernodeIds,
      };
    }, [visibleNodes, visibleEdges, degreeByLeader]);

    // Track container size for the canvas.
    React.useEffect(() => {
      const el = containerRef.current;
      if (!el) return;
      const measure = () => {
        const rect = el.getBoundingClientRect();
        setSize({ w: Math.max(200, Math.floor(rect.width)), h: Math.max(200, Math.floor(rect.height)) });
      };
      measure();
      let ro = null;
      if (typeof ResizeObserver !== 'undefined') {
        ro = new ResizeObserver(measure);
        ro.observe(el);
      } else {
        window.addEventListener('resize', measure);
      }
      return () => {
        if (ro) ro.disconnect();
        else window.removeEventListener('resize', measure);
      };
    }, []);

    // WG-DISCOVERY: forces re-tuned per mode.
    //   discovery → tight (charge -400, link 120, no leaderPull)
    //   network   → medium (charge -250, link 70, leaderPull active)
    //   full      → loose (charge -200, link 50, leaderPull active)
    React.useEffect(() => {
      const fg = fgRef.current;
      if (!fg || !graphData.nodes.length) return;
      const f = FORCES_BY_MODE[mode];

      // Precompute leader → [follower ids] for the custom leaderPull force.
      const followersOfLeader = new Map();
      for (let i = 0; i < graphData.links.length; i++) {
        const e = graphData.links[i];
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
        if (sid === undefined || tid === undefined) continue;
        let arr = followersOfLeader.get(sid);
        if (!arr) { arr = []; followersOfLeader.set(sid, arr); }
        arr.push(tid);
      }

      try {
        const chargeForce = fg.d3Force && fg.d3Force('charge');
        if (chargeForce) {
          if (typeof chargeForce.strength === 'function') chargeForce.strength(f.charge);
          if (typeof chargeForce.distanceMax === 'function') chargeForce.distanceMax(800);
        }
        const linkForce = fg.d3Force && fg.d3Force('link');
        if (linkForce) {
          if (typeof linkForce.distance === 'function') linkForce.distance(f.linkDist);
          if (typeof linkForce.strength === 'function') linkForce.strength(f.linkStrength);
        }
        const centerForce = fg.d3Force && fg.d3Force('center');
        if (centerForce && typeof centerForce.strength === 'function') centerForce.strength(0.005);

        // Always clear legacy forces.
        try { fg.d3Force('collide', null); } catch (_) { /* ignore */ }
        try { fg.d3Force('phaseCluster', null); } catch (_) { /* ignore */ }

        if (f.useLeaderPull) {
          fg.d3Force('leaderPull', function (alpha) {
            if (alpha < 0.05) return;
            const k = 0.18 * alpha;
            const nodes = (fg.graphData && fg.graphData().nodes) || [];
            const byId = new Map();
            for (let i = 0; i < nodes.length; i++) byId.set(nodes[i].id, nodes[i]);
            followersOfLeader.forEach(function (followerIds, leaderId) {
              const leader = byId.get(leaderId);
              if (!leader || typeof leader.x !== 'number') return;
              for (let j = 0; j < followerIds.length; j++) {
                const ff = byId.get(followerIds[j]);
                if (!ff || typeof ff.x !== 'number') continue;
                if (ff.fx !== undefined && ff.fx !== null) continue;
                ff.vx = (ff.vx || 0) + (leader.x - ff.x) * k;
                ff.vy = (ff.vy || 0) + (leader.y - ff.y) * k;
              }
            });
          });
        } else {
          // Discovery has no followers to pull — disable the custom force.
          try { fg.d3Force('leaderPull', null); } catch (_) { /* ignore */ }
        }
      } catch (_e) { /* fg API may not be ready yet */ }

      // Auto-fit after the simulation has had a chance to settle.
      const t = setTimeout(function () {
        try { fg.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
      }, 1800);
      return function () { clearTimeout(t); };
    }, [graphData, mode]);

    const handleNodeClick = React.useCallback((node) => {
      if (!node) return;
      setSelectedId(node.id);
      if (onSelect) onSelect(node);
    }, [onSelect]);

    const handleNodeHover = React.useCallback((node) => {
      setHoverNode(node || null);
      const el = containerRef.current;
      if (el) el.style.cursor = node ? 'pointer' : 'default';
    }, []);

    const ForceGraph2D = window.ForceGraph2D;
    const BackgroundAmbient = (window.WG_V3 || {}).BackgroundAmbient;

    const containerStyle = {
      position: 'relative',
      width: '100%',
      height: '100%',
      minHeight: 480,
      overflow: 'hidden',
      background: 'transparent',
    };

    if (!ForceGraph2D) {
      return React.createElement(
        'div',
        { ref: containerRef, style: containerStyle },
        BackgroundAmbient ? React.createElement(BackgroundAmbient, null) : null,
        React.createElement(
          'div',
          {
            style: {
              position: 'absolute', inset: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: '#94a8d6', fontFamily: 'JetBrains Mono, monospace', fontSize: 12,
            },
          },
          'Loading WalletGraph renderer… (waiting for ForceGraph2D CDN)'
        )
      );
    }

    const totalNodes = allNodes.length;
    const badge = MODE_BADGE[mode];

    // Manual recenter — left of the Top N button (unchanged behavior).
    const recenterBtn = React.createElement(
      'button',
      {
        onClick: () => {
          try { fgRef.current && fgRef.current.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
        },
        style: {
          position: 'absolute', bottom: 16, right: 340, zIndex: 5,
          padding: '8px 14px',
          background: 'rgba(10,14,26,0.85)',
          border: '1px solid rgba(255,255,255,0.12)',
          color: '#94a8d6',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
          letterSpacing: 0.4,
          cursor: 'pointer',
          backdropFilter: 'blur(8px)',
          WebkitBackdropFilter: 'blur(8px)',
          borderRadius: 4,
        },
      },
      '◯ Recenter'
    );

    // WG-DISCOVERY: Mode toggle — cycles discovery → network → full → discovery.
    const modeBtn = React.createElement(
      'button',
      {
        onClick: () => {
          setMode((m) => VIEW_MODES[(VIEW_MODES.indexOf(m) + 1) % VIEW_MODES.length]);
        },
        style: {
          position: 'absolute', bottom: 16, right: 170, zIndex: 5,
          padding: '8px 16px',
          background: 'rgba(10,14,26,0.85)',
          border: '1px solid ' + badge.color + '55',
          color: badge.color,
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
          letterSpacing: 0.4,
          cursor: 'pointer',
          backdropFilter: 'blur(8px)',
          WebkitBackdropFilter: 'blur(8px)',
          borderRadius: 4,
          fontWeight: 600,
        },
      },
      badge.icon + ' ' + badge.label
    );

    // Density cycle button — values depend on the active mode.
    const topNBtn = React.createElement(
      'button',
      {
        onClick: () => setDensityIndex((idx) => (idx + 1) % currentDensityCycle.length),
        style: {
          position: 'absolute', bottom: 16, right: 16, zIndex: 5,
          padding: '8px 16px',
          background: 'rgba(10,14,26,0.85)',
          border: '1px solid rgba(255,255,255,0.12)',
          color: '#ffffff',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
          letterSpacing: 0.4,
          cursor: 'pointer',
          backdropFilter: 'blur(8px)',
          WebkitBackdropFilter: 'blur(8px)',
          borderRadius: 4,
        },
      },
      '▣ Top ' + densityN
    );

    // Legend swatch.
    const swatch = (color, label) => React.createElement(
      'span',
      { style: { display: 'inline-flex', alignItems: 'center', gap: 4, marginRight: 8 } },
      React.createElement('span', {
        style: {
          width: 7, height: 7, borderRadius: '50%',
          background: color, boxShadow: '0 0 6px ' + color,
        },
      }),
      React.createElement('span', { style: { color: '#c4ccd8' } }, label)
    );

    // HUD now leads with the active mode badge in its accent color.
    const statsHud = React.createElement(
      'div',
      {
        style: {
          position: 'absolute', top: 12, left: 12, zIndex: 5,
          padding: '6px 10px',
          background: 'rgba(10,14,26,0.7)',
          border: '1px solid rgba(255,255,255,0.08)',
          color: '#94a8d6',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 10,
          letterSpacing: 0.3,
          pointerEvents: 'none',
          backdropFilter: 'blur(6px)',
          WebkitBackdropFilter: 'blur(6px)',
          borderRadius: 3,
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        },
      },
      React.createElement(
        'div',
        { style: { display: 'flex', alignItems: 'center', gap: 6 } },
        React.createElement(
          'span',
          { style: { color: badge.color, fontWeight: 600 } },
          badge.icon + ' ' + badge.label + ' mode'
        ),
        React.createElement('span', null,
          ' · nodes ' + visibleNodes.length + '/' + totalNodes +
          ' · edges ' + visibleEdges.length
        )
      ),
      React.createElement('div', { style: { display: 'flex', flexWrap: 'wrap' } },
        swatch('#3b82f6', 'P1'),
        swatch('#f59e0b', 'P2'),
        swatch('#10b981', 'P3'),
        swatch('#a78bfa', 'Follower')
      )
    );

    return React.createElement(
      'div',
      { ref: containerRef, style: containerStyle },
      BackgroundAmbient ? React.createElement(BackgroundAmbient, null) : null,
      React.createElement(ForceGraph2D, {
        ref: fgRef,
        graphData: graphData,
        width: size.w,
        height: size.h,
        nodeCanvasObject: (node, ctx, globalScale) =>
          paintNode(
            node,
            ctx,
            globalScale,
            node.id === selectedId,
            hoverNode && node.id === hoverNode.id
          ),
        nodeCanvasObjectMode: () => 'replace',
        linkCanvasObject: (edge, ctx) => paintEdge(edge, ctx, selectedId),
        linkCanvasObjectMode: () => 'replace',
        nodePointerAreaPaint: (node, color, ctx) => {
          const r = getNodeRadius(node) + 5;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
          ctx.fill();
        },
        onNodeClick: handleNodeClick,
        onNodeHover: handleNodeHover,
        backgroundColor: 'rgba(0,0,0,0)',
        cooldownTicks: 400,
        d3AlphaDecay: 0.0228,
        d3VelocityDecay: 0.4,
        nodeRelSize: 6,
        enableNodeDrag: false,
        warmupTicks: 20,
        onEngineStop: () => {
          try { fgRef.current && fgRef.current.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
        },
      }),
      statsHud,
      recenterBtn,
      modeBtn,
      topNBtn
    );
  }

  if (typeof window !== 'undefined') {
    window.WG_V3 = window.WG_V3 || {};
    window.WG_V3.WalletGraphV3 = WalletGraphV3;
    window.WG_V3._paintNode = paintNode;
    window.WG_V3._paintEdge = paintEdge;
    window.WG_V3._getNodeRadius = getNodeRadius;
    window.WG_V3._getNodeColor = getNodeColor;
  }
})();
