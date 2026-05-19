// static/dashboard/walletgraph_v3/Graph.jsx
// Polymarket V3 — Wallet Graph "univers" core renderer.
// Consumes snapshot.wallet_graph from window.useLiveStore().
// Depends on window.ForceGraph2D (loaded via UMD CDN by WG-A6).
// Composes window.WG_V3.BackgroundAmbient (WG-A3) when available.
// Click on node fires onSelect(nodeData) prop (wired by WG-A6 to SelectionPanel).
//
// WG-BUBBLE: BubbleMaps-style layout. Top supernodes pinned in a circle around
// the viewport center, followers pulled toward their leader via a custom force,
// no quadrant clustering (the previous phaseCluster + tight collide combo collapsed
// everything into a concentric disc). Density gated by a 4-step "Top N" cycle.

(function () {
  'use strict';

  const PHASE_COLORS = {
    1: { core: '#3b82f6', glow: 'rgba(59, 130, 246, ' },
    2: { core: '#f59e0b', glow: 'rgba(245, 158, 11, ' },
    3: { core: '#10b981', glow: 'rgba(16, 185, 129, ' },
  };
  const FOLLOWER_COLOR = { core: '#a78bfa', glow: 'rgba(167, 139, 250, ' };
  const EXCLUDED_COLOR = { core: '#475569', glow: 'rgba(71, 85, 105, ' };

  // 4-step density cycle: 50 → 200 → 500 → 3000 → 50 …
  const TOP_N_CYCLE = [50, 200, 500, 3000];
  const DEFAULT_TOP_N_INDEX = 1; // start at 200 leaders by default
  const SUPER_COUNT = 10;        // top-K supernodes pinned in a circle
  const SUPER_RADIUS = 350;      // px radius for the pinned circle

  function getNodeColor(node) {
    if (node.exclude_reason) return EXCLUDED_COLOR;
    if (node.role === 'follower') return FOLLOWER_COLOR;
    return PHASE_COLORS[node.phase] || PHASE_COLORS[1];
  }

  // Power 0.55 (vs sqrt) + larger range [4, 24] amplifies whale vs small visual gap.
  function getNodeRadius(node) {
    const t24 = (node && node.trades_24h) || 1;
    return Math.max(4, Math.min(24, Math.pow(t24, 0.55) * 2.0));
  }

  // 4-layer node painter: outer-glow (r*6) + halo (r*3) + inner-glow (r*1.6) + core w/ stroke.
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

    // 2) Halo (medium — r*3, much more intense than before).
    const haloGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 3);
    haloGrad.addColorStop(0, glow + '0.55)');
    haloGrad.addColorStop(1, glow + '0)');
    ctx.fillStyle = haloGrad;
    ctx.fillRect(x - r * 3, y - r * 3, r * 6, r * 6);

    // 3) Inner glow (tight, near-opaque core bleed).
    const glowGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 1.6);
    glowGrad.addColorStop(0, glow + '0.85)');
    glowGrad.addColorStop(1, glow + '0.5)');
    ctx.fillStyle = glowGrad;
    ctx.beginPath();
    ctx.arc(x, y, r * 1.6, 0, Math.PI * 2);
    ctx.fill();

    // 4) Solid core + subtle 1px white outline (gives nodes physical edge).
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // 5) Selected: big aura (r*9) + thick white ring.
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
      // Hover ring — slightly brighter (visible against intense halo).
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.55)';
      ctx.lineWidth = Math.max(1.5, 1.5 / Math.max(globalScale, 0.001));
      ctx.beginPath();
      ctx.arc(x, y, r + 1, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 6) Supernode label (top-10 by degree only — keeps the canvas readable).
    if (node._isSupernode) {
      const label = node.label || (typeof node.id === 'string'
        ? (node.id.slice(0, 6) + '…' + node.id.slice(-4))
        : String(node.id));
      const fontPx = Math.max(9, 11 / Math.max(globalScale, 0.001));
      ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
      ctx.font = fontPx + 'px JetBrains Mono, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      // Subtle shadow so labels read on dark + bright glow.
      ctx.shadowBlur = 6;
      ctx.shadowColor = 'rgba(0, 0, 0, 0.7)';
      ctx.fillText(label, x, y + r + 6 / Math.max(globalScale, 0.001));
      ctx.shadowBlur = 0;
    }
  }

  // Edge painter: amped opacity (we display far fewer edges now), ambre on selection.
  function paintEdge(edge, ctx, selectedId) {
    const s = edge.source;
    const t = edge.target;
    if (!s || !t || typeof s.x !== 'number' || typeof t.x !== 'number') return;
    const p = typeof edge.p_follow === 'number' ? edge.p_follow : 0.5;
    const co = edge.co_occurrences || 1;
    // Incident-on-selection edges get amber tint + much higher opacity + 1.5x width.
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
      // WG-BUBBLE: bumped to 0.28..0.88 — fewer edges visible means each one must read.
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
    // Density cycle index (0..3 → 50, 200, 500, 3000 leaders). Defaults to 200.
    const [topNIndex, setTopNIndex] = React.useState(DEFAULT_TOP_N_INDEX);
    const currentTopN = TOP_N_CYCLE[topNIndex];

    const wg = (snapshot && snapshot.wallet_graph) || {};
    const allNodes = Array.isArray(wg.nodes) ? wg.nodes : [];
    const allEdges = Array.isArray(wg.edges) ? wg.edges : [];

    // Degree = number of edges where the node is the SOURCE (leader → follower
    // edges in this graph). Used to rank leaders + pick supernodes.
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

    // Visible scope: top-N leaders by degree (followers count) + their connected
    // followers. Followers without a visible leader are dropped.
    const visibleNodes = React.useMemo(() => {
      if (!allNodes.length) return [];
      // Leaders = nodes that appear as a source in any edge.
      const leaderPool = allNodes.filter((n) => degreeByLeader.has(n.id));
      // Fallback: if degree map is empty (edges don't reference ids yet),
      // fall back to all nodes ranked by trades_24h to avoid a black canvas.
      const ranked = (leaderPool.length ? leaderPool : allNodes.slice()).sort((a, b) => {
        const da = degreeByLeader.get(a.id) || 0;
        const db = degreeByLeader.get(b.id) || 0;
        if (db !== da) return db - da;
        return (b.trades_24h || 0) - (a.trades_24h || 0);
      });
      const leaders = ranked.slice(0, currentTopN);
      const leaderSet = new Set(leaders.map((n) => n.id));

      // Add followers connected to any visible leader.
      const connectedFollowers = new Set();
      for (let i = 0; i < allEdges.length; i++) {
        const e = allEdges[i];
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
        if (leaderSet.has(sid) && !leaderSet.has(tid)) connectedFollowers.add(tid);
        else if (leaderSet.has(tid) && !leaderSet.has(sid)) connectedFollowers.add(sid);
      }
      if (!connectedFollowers.size) return leaders;
      const visibleSet = new Set(leaderSet);
      const extras = [];
      for (let i = 0; i < allNodes.length; i++) {
        const n = allNodes[i];
        if (!visibleSet.has(n.id) && connectedFollowers.has(n.id)) {
          extras.push(n);
          visibleSet.add(n.id);
        }
      }
      return leaders.concat(extras);
    }, [allNodes, allEdges, degreeByLeader, currentTopN]);

    const visibleNodeIds = React.useMemo(() => {
      const s = new Set();
      for (let i = 0; i < visibleNodes.length; i++) s.add(visibleNodes[i].id);
      return s;
    }, [visibleNodes]);

    const visibleEdges = React.useMemo(() => {
      if (!allEdges.length) return [];
      return allEdges.filter((e) => {
        const sid = (e.source && e.source.id !== undefined) ? e.source.id : e.source;
        const tid = (e.target && e.target.id !== undefined) ? e.target.id : e.target;
        return visibleNodeIds.has(sid) && visibleNodeIds.has(tid);
      });
    }, [allEdges, visibleNodeIds]);

    // ForceGraph2D mutates link.source/target into objects; spread to avoid touching upstream.
    // We also stamp the top supernodes here so the painter can label them, and we
    // pre-pin their fx/fy on the circle so the simulation respects the bubble layout.
    const graphData = React.useMemo(() => {
      const supernodeIds = new Set();
      // Identify the top supernodes (highest degree first) among the visible
      // leaders. We rebuild a ranking from the visible set so the choice is
      // stable across density toggles.
      const visibleByDegree = visibleNodes
        .filter((n) => degreeByLeader.has(n.id))
        .sort((a, b) => (degreeByLeader.get(b.id) || 0) - (degreeByLeader.get(a.id) || 0));
      const topK = visibleByDegree.slice(0, SUPER_COUNT);

      // Clone nodes (don't mutate snapshot data) and tag/pin supernodes.
      const nodes = visibleNodes.map((n) => {
        const copy = Object.assign({}, n);
        // Wipe any stale pin so non-supernodes are free in subsequent renders.
        if (copy.fx !== undefined) delete copy.fx;
        if (copy.fy !== undefined) delete copy.fy;
        copy._isSupernode = false;
        return copy;
      });
      const byId = new Map(nodes.map((n) => [n.id, n]));
      topK.forEach((node, i) => {
        const target = byId.get(node.id);
        if (!target) return;
        supernodeIds.add(node.id);
        target._isSupernode = true;
        const angle = (i / Math.max(topK.length, 1)) * 2 * Math.PI - Math.PI / 2;
        target.fx = SUPER_RADIUS * Math.cos(angle);
        target.fy = SUPER_RADIUS * Math.sin(angle);
        // Seed initial x/y too so the first paint already shows the ring.
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

    // WG-BUBBLE: forces tuned for "supernodes-pinned + followers attracted to
    // their leader". The previous setup (phaseCluster + strong collide) was
    // collapsing every node into a concentric ring because the 4 cluster
    // targets are aligned and the collide radius forced uniform spacing.
    //
    // New recipe:
    //   - charge -200 (was -350): a bit less global repulsion since pins anchor the layout
    //   - link distance 50, strength 0.6: short + tight so followers stick to their leader
    //   - center 0.005: near-zero (pins do the work)
    //   - collide: removed entirely (let the simulation breathe)
    //   - phaseCluster: removed (it caused the disc)
    //   - leaderPull: NEW custom force, every follower yanked toward its leader
    React.useEffect(() => {
      const fg = fgRef.current;
      if (!fg || !graphData.nodes.length) return;

      // Precompute leader → [follower ids] for the custom force. We use raw ids
      // (not node refs) so the map stays valid even if d3 re-mutates link objects.
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
          if (typeof chargeForce.strength === 'function') chargeForce.strength(-200);
          if (typeof chargeForce.distanceMax === 'function') chargeForce.distanceMax(800);
        }
        const linkForce = fg.d3Force && fg.d3Force('link');
        if (linkForce) {
          if (typeof linkForce.distance === 'function') linkForce.distance(50);
          if (typeof linkForce.strength === 'function') linkForce.strength(0.6);
        }
        const centerForce = fg.d3Force && fg.d3Force('center');
        if (centerForce && typeof centerForce.strength === 'function') centerForce.strength(0.005);

        // Kill the legacy forces from the previous polish pass.
        try { fg.d3Force('collide', null); } catch (_) { /* ignore */ }
        try { fg.d3Force('phaseCluster', null); } catch (_) { /* ignore */ }

        // Custom force: pull each follower toward its leader's current position.
        // Strong-ish k (0.18 * alpha) because we removed collide & cluster — this
        // is now the main shape driver alongside the link force.
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
              const f = byId.get(followerIds[j]);
              if (!f || typeof f.x !== 'number') continue;
              // Skip pinned supernodes — fx/fy already lock them in place.
              if (f.fx !== undefined && f.fx !== null) continue;
              f.vx = (f.vx || 0) + (leader.x - f.x) * k;
              f.vy = (f.vy || 0) + (leader.y - f.y) * k;
            }
          });
        });
      } catch (_e) { /* fg API may not be ready yet */ }

      // Quicker auto-fit: supernodes are pinned so the layout settles fast.
      const t = setTimeout(function () {
        try { fg.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
      }, 1800);
      return function () { clearTimeout(t); };
    }, [graphData]);

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

    // Manual recenter — useful after the user pans/zooms or when clusters
    // drift off-screen. Placed left of the "Top N" cycle button.
    const recenterBtn = React.createElement(
      'button',
      {
        onClick: () => {
          try { fgRef.current && fgRef.current.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
        },
        style: {
          position: 'absolute', bottom: 16, right: 200, zIndex: 5,
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

    // Density cycle button: 50 → 200 → 500 → 3000 → 50 …
    // Always rendered (even if totalNodes < currentTopN) so the operator can
    // dial densities up and down freely.
    const topNBtn = React.createElement(
      'button',
      {
        onClick: () => setTopNIndex((idx) => (idx + 1) % TOP_N_CYCLE.length),
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
      '▣ Top ' + currentTopN
    );

    // Legend swatch — small color dot + label.
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
      React.createElement('div', null,
        'nodes ' + visibleNodes.length + '/' + totalNodes +
        ' · edges ' + visibleEdges.length
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
        // Shorter cooldown: supernodes are pinned so the simulation converges fast.
        cooldownTicks: 400,
        d3AlphaDecay: 0.0228,
        d3VelocityDecay: 0.4,
        nodeRelSize: 6,
        enableNodeDrag: false,
        warmupTicks: 20,
        onEngineStop: () => {
          // Auto-refit once the simulation stabilizes (BubbleMaps-style framing).
          try { fgRef.current && fgRef.current.zoomToFit(500, 80); } catch (_e) { /* ignore */ }
        },
      }),
      statsHud,
      recenterBtn,
      topNBtn
    );
  }

  // Public export on window.WG_V3 namespace.
  if (typeof window !== 'undefined') {
    window.WG_V3 = window.WG_V3 || {};
    window.WG_V3.WalletGraphV3 = WalletGraphV3;
    window.WG_V3._paintNode = paintNode;
    window.WG_V3._paintEdge = paintEdge;
    window.WG_V3._getNodeRadius = getNodeRadius;
    window.WG_V3._getNodeColor = getNodeColor;
  }
})();
