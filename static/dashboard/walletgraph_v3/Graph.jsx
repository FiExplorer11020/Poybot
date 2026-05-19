// static/dashboard/walletgraph_v3/Graph.jsx
// Polymarket V3 — Wallet Graph "univers" core renderer.
// Consumes snapshot.wallet_graph from window.useLiveStore().
// Depends on window.ForceGraph2D (loaded via UMD CDN by WG-A6).
// Composes window.WG_V3.BackgroundAmbient (WG-A3) when available.
// Click on node fires onSelect(nodeData) prop (wired by WG-A6 to SelectionPanel).

(function () {
  'use strict';

  const PHASE_COLORS = {
    1: { core: '#3b82f6', glow: 'rgba(59, 130, 246, ' },
    2: { core: '#f59e0b', glow: 'rgba(245, 158, 11, ' },
    3: { core: '#10b981', glow: 'rgba(16, 185, 129, ' },
  };
  const FOLLOWER_COLOR = { core: '#a78bfa', glow: 'rgba(167, 139, 250, ' };
  const EXCLUDED_COLOR = { core: '#475569', glow: 'rgba(71, 85, 105, ' };
  const DEFAULT_TOP_N = 1500;
  const SHOW_ALL_TOP_N = 3000;

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
  }

  // Edge painter: 3x more opaque than before, 2x thicker; ambre tint when incident on selection.
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
    let width = Math.max(1.0, Math.log(co + 1) * 0.8);
    if (incident) {
      alpha = 0.9;
      stroke = 'rgba(245, 158, 11, ' + alpha.toFixed(3) + ')';
      width *= 1.5;
    } else {
      alpha = 0.18 + p * 0.45;
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
    const [showAll, setShowAll] = React.useState(false);

    const wg = (snapshot && snapshot.wallet_graph) || {};
    const allNodes = Array.isArray(wg.nodes) ? wg.nodes : [];
    const allEdges = Array.isArray(wg.edges) ? wg.edges : [];

    // Step 1: pick top-N leaders by trades_24h (1500 default, 3000 on "show all").
    // Step 2: add connected followers (nodes linked to leaders by any edge) — even if
    // they didn't make the trades_24h cut. This is what makes clusters readable.
    const visibleNodes = React.useMemo(() => {
      if (!allNodes.length) return [];
      const cap = showAll ? SHOW_ALL_TOP_N : DEFAULT_TOP_N;
      const sortedByActivity = allNodes.slice().sort((a, b) => (b.trades_24h || 0) - (a.trades_24h || 0));
      const leaders = sortedByActivity.slice(0, cap);
      const leaderSet = new Set(leaders.map((n) => n.id));
      // Walk edges once: collect IDs of followers connected to any leader.
      const connectedFollowers = new Set();
      for (let i = 0; i < allEdges.length; i++) {
        const e = allEdges[i];
        const hasS = leaderSet.has(e.source);
        const hasT = leaderSet.has(e.target);
        if (hasS && !hasT) connectedFollowers.add(e.target);
        else if (hasT && !hasS) connectedFollowers.add(e.source);
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
    }, [allNodes, allEdges, showAll]);

    const visibleNodeIds = React.useMemo(() => {
      const s = new Set();
      for (let i = 0; i < visibleNodes.length; i++) s.add(visibleNodes[i].id);
      return s;
    }, [visibleNodes]);

    const visibleEdges = React.useMemo(() => {
      if (!allEdges.length) return [];
      return allEdges.filter((e) => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target));
    }, [allEdges, visibleNodeIds]);

    // ForceGraph2D mutates link.source/target into objects; spread to avoid touching upstream.
    const graphData = React.useMemo(() => ({
      nodes: visibleNodes,
      links: visibleEdges.map((e) => ({ ...e })),
    }), [visibleNodes, visibleEdges]);

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

    // Tune d3-force on data settle: strong repulsion + loose center + custom
    // phase-cluster force + (optional) collide = clearly separated BubbleMaps-style
    // zones instead of one dense blob in the middle.
    React.useEffect(() => {
      const fg = fgRef.current;
      if (!fg || !graphData.nodes.length) return;
      try {
        const chargeForce = fg.d3Force && fg.d3Force('charge');
        if (chargeForce) {
          if (typeof chargeForce.strength === 'function') chargeForce.strength(-350);
          if (typeof chargeForce.distanceMax === 'function') chargeForce.distanceMax(800);
        }
        const linkForce = fg.d3Force && fg.d3Force('link');
        if (linkForce) {
          if (typeof linkForce.distance === 'function') linkForce.distance(100);
          if (typeof linkForce.strength === 'function') linkForce.strength(0.35);
        }
        const centerForce = fg.d3Force && fg.d3Force('center');
        if (centerForce && typeof centerForce.strength === 'function') centerForce.strength(0.015);

        // Collide force — anti-overlap. d3-force is bundled inside react-force-graph-2d
        // but not always exposed on window. Try a few common surfaces; fall back silently.
        const d3 = window.d3
          || (window.ForceGraph2D && window.ForceGraph2D.d3)
          || (fg && fg.d3);
        if (d3 && typeof d3.forceCollide === 'function') {
          fg.d3Force(
            'collide',
            d3.forceCollide().radius((node) => getNodeRadius(node) * 2.5).strength(0.7)
          );
        } // else: TODO: collide force - requires d3-force on window.

        // Custom phase-cluster force: pull each node toward its quadrant target.
        // 4 zones — P1 top-left, P2 top-right, P3 bottom-left, followers bottom-right.
        const CLUSTER_TARGETS = {
          1: { x: -400, y: -300 },
          2: { x:  400, y: -300 },
          3: { x: -400, y:  300 },
          follower: { x: 400, y: 300 },
        };
        fg.d3Force('phaseCluster', (alpha) => {
          // d3 custom force: called every tick. Stops drifting once stabilized.
          if (alpha < 0.05) return;
          const k = 0.04 * alpha;
          const nodes = (fg.graphData && fg.graphData().nodes) || [];
          for (let i = 0; i < nodes.length; i++) {
            const n = nodes[i];
            const phaseKey = n.role === 'follower' ? 'follower' : (n.phase || 1);
            const target = CLUSTER_TARGETS[phaseKey] || CLUSTER_TARGETS[1];
            n.vx = (n.vx || 0) + (target.x - (n.x || 0)) * k;
            n.vy = (n.vy || 0) + (target.y - (n.y || 0)) * k;
          }
        });
      } catch (_e) { /* ignore — fg API may not be ready yet */ }
      // Longer settle time to let new forces stabilize before auto-fitting.
      const t = setTimeout(() => {
        try { fg.zoomToFit(400, 60); } catch (_e) { /* ignore */ }
      }, 3500);
      return () => clearTimeout(t);
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
    // drift off-screen. Placed left of the "Show top N" button.
    const recenterBtn = React.createElement(
      'button',
      {
        onClick: () => {
          try { fgRef.current && fgRef.current.zoomToFit(500, 60); } catch (_e) { /* ignore */ }
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

    const showAllBtn = !showAll && totalNodes > DEFAULT_TOP_N
      ? React.createElement(
          'button',
          {
            onClick: () => setShowAll(true),
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
          'Show top ' + Math.min(SHOW_ALL_TOP_N, totalNodes) + ' →'
        )
      : null;

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
        'nodes ' + visibleNodes.length + (showAll ? '' : '/' + totalNodes) +
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
        // Longer cooldown + slightly less friction = forces have time to spread clusters
        // into distinct zones before the simulation halts.
        cooldownTicks: 600,
        d3AlphaDecay: 0.0228,
        d3VelocityDecay: 0.5,
        nodeRelSize: 6,
        enableNodeDrag: false,
        warmupTicks: 20,
        onEngineStop: () => {
          // Auto-refit once the simulation stabilizes (BubbleMaps-style framing).
          try { fgRef.current && fgRef.current.zoomToFit(500, 60); } catch (_e) { /* ignore */ }
        },
      }),
      statsHud,
      recenterBtn,
      showAllBtn
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
