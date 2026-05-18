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
  const DEFAULT_TOP_N = 500;

  function getNodeColor(node) {
    if (node.exclude_reason) return EXCLUDED_COLOR;
    if (node.role === 'follower') return FOLLOWER_COLOR;
    return PHASE_COLORS[node.phase] || PHASE_COLORS[1];
  }

  function getNodeRadius(node) {
    const t24 = (node && node.trades_24h) || 1;
    return Math.max(3, Math.min(15, Math.sqrt(t24) * 2));
  }

  // Node painter: outer halo + inner glow + core + optional selected ring/halo.
  function paintNode(node, ctx, globalScale, isSelected, isHovered) {
    if (typeof node.x !== 'number' || typeof node.y !== 'number') return;
    const { core, glow } = getNodeColor(node);
    const baseR = getNodeRadius(node);
    const r = isHovered ? baseR * 1.3 : baseR;
    const x = node.x;
    const y = node.y;

    // 1) Outer halo (rectangular gradient fill — covers a 6r square).
    const haloGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 3);
    haloGrad.addColorStop(0, glow + '0.4)');
    haloGrad.addColorStop(1, glow + '0)');
    ctx.fillStyle = haloGrad;
    ctx.fillRect(x - r * 3, y - r * 3, r * 6, r * 6);

    // 2) Inner glow (soft circular).
    const glowGrad = ctx.createRadialGradient(x, y, 0, x, y, r * 1.5);
    glowGrad.addColorStop(0, glow + '0.8)');
    glowGrad.addColorStop(1, glow + '0.4)');
    ctx.fillStyle = glowGrad;
    ctx.beginPath();
    ctx.arc(x, y, r * 1.5, 0, Math.PI * 2);
    ctx.fill();

    // 3) Solid core.
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();

    // 4) Selected: bright ring + glow.
    if (isSelected) {
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.lineWidth = Math.max(1.5, 2 / Math.max(globalScale, 0.001));
      ctx.beginPath();
      ctx.arc(x, y, r + 1, 0, Math.PI * 2);
      ctx.stroke();
      ctx.shadowBlur = 20;
      ctx.shadowColor = '#ffffff';
      ctx.stroke();
      ctx.shadowBlur = 0;
    } else if (isHovered) {
      // Subtle hover ring.
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
      ctx.lineWidth = Math.max(1, 1 / Math.max(globalScale, 0.001));
      ctx.beginPath();
      ctx.arc(x, y, r + 0.5, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  // Edge painter: opacity ~ p_follow, width ~ log(co_occurrences).
  function paintEdge(edge, ctx) {
    const s = edge.source;
    const t = edge.target;
    if (!s || !t || typeof s.x !== 'number' || typeof t.x !== 'number') return;
    const p = typeof edge.p_follow === 'number' ? edge.p_follow : 0.5;
    const alpha = 0.05 + p * 0.15;
    const co = edge.co_occurrences || 1;
    const width = Math.max(0.5, Math.log(co + 1) * 0.4);
    ctx.strokeStyle = 'rgba(255, 255, 255, ' + alpha.toFixed(3) + ')';
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

    // Top-N nodes by trades_24h desc (default 500), or all if showAll.
    const visibleNodes = React.useMemo(() => {
      if (showAll) return allNodes;
      if (allNodes.length <= DEFAULT_TOP_N) return allNodes;
      const sorted = allNodes.slice().sort((a, b) => (b.trades_24h || 0) - (a.trades_24h || 0));
      return sorted.slice(0, DEFAULT_TOP_N);
    }, [allNodes, showAll]);

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

    // Fit view once data has settled.
    React.useEffect(() => {
      const fg = fgRef.current;
      if (!fg || !graphData.nodes.length) return;
      const t = setTimeout(() => {
        try { fg.zoomToFit(400, 80); } catch (_e) { /* ignore */ }
      }, 800);
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
          'Show all ' + totalNodes + ' nodes →'
        )
      : null;

    const statsHud = React.createElement(
      'div',
      {
        style: {
          position: 'absolute', top: 12, left: 12, zIndex: 5,
          padding: '6px 10px',
          background: 'rgba(10,14,26,0.6)',
          border: '1px solid rgba(255,255,255,0.08)',
          color: '#94a8d6',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 10,
          letterSpacing: 0.3,
          pointerEvents: 'none',
          backdropFilter: 'blur(6px)',
          WebkitBackdropFilter: 'blur(6px)',
          borderRadius: 3,
        },
      },
      'nodes ' + visibleNodes.length + (showAll ? '' : '/' + totalNodes) + ' · edges ' + visibleEdges.length
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
        linkCanvasObject: (edge, ctx) => paintEdge(edge, ctx),
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
        cooldownTicks: 100,
        d3AlphaDecay: 0.02,
        d3VelocityDecay: 0.3,
        enableNodeDrag: false,
        warmupTicks: 20,
      }),
      statsHud,
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
