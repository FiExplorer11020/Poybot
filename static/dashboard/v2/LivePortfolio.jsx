// LivePortfolio.jsx — v2 orchestrator (Mirrorfish-style terminal layout).
//
// Rebinds window.LivePortfolio so the existing dashboard-app.jsx shell picks it up
// without any nav rewrite. Replaces the legacy LivePortfolio defined in
// dashboard-tabs.jsx (loaded earlier, overwritten by us at the bottom of HTML loader).
//
// Composes:
//   PipelineStatus  (top thin bar)
//   KpiRow          (6 KPI tiles)
//   EquityTimeline  (main, 1-9)
//   PnLTicks        (under equity, 1-9)
//   MarketPriceOverlay (under PnL, 1-9)
//   TradeList       (side, 10-12)
//   AllocationBar   (full width footer)
//
// Backend contract (Agent D):
//   GET /api/portfolio/timeseries?timeframe={tf}
//     -> { equity:[{time,value}], drawdown:[{time,value}], pnl_ticks:[{time,value}] }
//   GET /api/portfolio/trades?limit=50
//     -> { trades: [...], market_overlays: {market_id: {yes_price_series, no_price_series}} }
//   GET /api/portfolio/allocation
//     -> { total_usdc, by_category, by_leader }
//   GET /api/portfolio/kpis
//     -> { balance, peak, drawdown_pct, daily_pnl, win_rate, open_count, latency_p50_ms, ... }
//   GET /api/portfolio/pipeline_status (optional, falls back to /api/v1/live-summary)
//
// All four primary endpoints are polled every 5 s. Each call uses an
// AbortController so timeframe changes cancel in-flight requests.

(function () {
  const { useEffect, useState, useRef, useCallback } = React;

  const fetchJson = async (path, signal) => {
    const r = await fetch(path, { signal });
    if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + path);
    return r.json();
  };

  const safe = (p) => p.catch(e => { if (e.name !== 'AbortError') console.warn('[LivePortfolio]', e.message); return null; });

  const LivePortfolio = () => {
    const [tf, setTf]               = useState('1h');
    const [kpis, setKpis]           = useState({});
    const [series, setSeries]       = useState({ equity: [], drawdown: [], pnl_ticks: [] });
    const [trades, setTrades]       = useState([]);
    const [overlays, setOverlays]   = useState({}); // market_id → {yes_price_series, no_price_series, question}
    const [allocation, setAllocation] = useState({});
    const [pipeline, setPipeline]   = useState({});
    const [loading, setLoading]     = useState({ kpis: true, series: true, trades: true, allocation: true, pipeline: true });

    const [selectedTrade, setSelectedTrade]     = useState(null);
    const [hoveredMarket, setHoveredMarket]     = useState(null); // market overlay object
    const [filter, setFilter]                   = useState(null);  // {category} | {leader}

    // ── Fetch loop (mount + 5s poll + abort on timeframe change) ─────────────
    const reqIdRef = useRef(0);
    useEffect(() => {
      let cancelled = false;
      const ac = new AbortController();
      const id = ++reqIdRef.current;

      const loadOnce = async () => {
        const results = await Promise.all([
          safe(fetchJson('/api/portfolio/timeseries?timeframe=' + encodeURIComponent(tf), ac.signal)),
          safe(fetchJson('/api/portfolio/trades?limit=50', ac.signal)),
          safe(fetchJson('/api/portfolio/allocation', ac.signal)),
          safe(fetchJson('/api/portfolio/kpis', ac.signal)),
          safe(fetchJson('/api/portfolio/pipeline_status', ac.signal)),
        ]);
        if (cancelled || id !== reqIdRef.current) return;
        const [tsRes, trRes, alRes, kpRes, plRes] = results;

        if (tsRes) {
          setSeries({
            equity:    tsRes.equity    || [],
            drawdown:  tsRes.drawdown  || [],
            pnl_ticks: tsRes.pnl_ticks || [],
          });
          setLoading(s => Object.assign({}, s, { series: false }));
        }
        if (trRes) {
          setTrades(trRes.trades || []);
          if (trRes.market_overlays) setOverlays(trRes.market_overlays);
          setLoading(s => Object.assign({}, s, { trades: false }));
        }
        if (alRes) { setAllocation(alRes); setLoading(s => Object.assign({}, s, { allocation: false })); }
        if (kpRes) { setKpis(kpRes); setLoading(s => Object.assign({}, s, { kpis: false })); }

        // pipeline_status is OPTIONAL — fall back to live-summary if 404 / null
        if (plRes) { setPipeline(plRes); setLoading(s => Object.assign({}, s, { pipeline: false })); }
        else {
          // Soft fallback: read snapshot stored in window.LiveStore
          const snap = (window.LiveStore && window.LiveStore.snapshot) || null;
          if (snap) {
            setPipeline({
              bot:          snap.bot?.status || 'unknown',
              ws:           (window.LiveStore?.connectionState === 'connected') ? 'live' : 'down',
              ingest_live:  snap.ingestion?.live_markets || 0,
              ingest_total: snap.ingestion?.total_markets || 0,
              exec_mode:    snap.bot?.execution_enabled ? 'LIVE' : 'PAPER',
              killswitch:   snap.bot?.killswitch_on ? 'on' : 'off',
              latency_ms:   snap.meta?.poll_latency_ms,
            });
            setLoading(s => Object.assign({}, s, { pipeline: false }));
          }
        }
      };

      loadOnce();
      const interval = setInterval(loadOnce, 5000);
      return () => { cancelled = true; ac.abort(); clearInterval(interval); };
    }, [tf]);

    // ── Handlers ──────────────────────────────────────────────────────────────
    const handleTradeSelect = useCallback((trade) => {
      setSelectedTrade(prev => (prev && prev.id === trade.id ? null : trade));
    }, []);

    const handleTradeHover = useCallback((trade) => {
      if (!trade) { setHoveredMarket(null); return; }
      const ov = overlays[trade.market_id];
      setHoveredMarket(ov
        ? Object.assign({ market_id: trade.market_id, question: trade.market_question }, ov)
        : { market_id: trade.market_id, question: trade.market_question, yes_price_series: [], no_price_series: [] });
    }, [overlays]);

    // ── Layout (12-column CSS grid) ───────────────────────────────────────────
    const { PipelineStatus, KpiRow, EquityTimeline, PnLTicks, MarketPriceOverlay, TradeList, AllocationBar } = window.Portfolio || {};
    if (!PipelineStatus || !KpiRow || !EquityTimeline || !PnLTicks || !MarketPriceOverlay || !TradeList || !AllocationBar) {
      return React.createElement('div', {
        className: 'ui-root',
        style: { padding: 24, color: 'var(--accent-amber)', fontFamily: 'var(--font-mono)', fontSize: 11 } },
        'LIVE PORTFOLIO v2: portfolio components not yet loaded.\nExpected window.Portfolio.{PipelineStatus,KpiRow,EquityTimeline,PnLTicks,MarketPriceOverlay,TradeList,AllocationBar}.\nDid /static/dashboard/v2/components/portfolio/*.jsx fail to load?');
    }

    return (
      React.createElement('div', {
        className: 'ui-root',
        style: {
          display: 'grid',
          gridTemplateRows: 'auto auto 1fr auto',
          height: '100%',
          width: '100%',
          background: 'var(--bg-0)',
          color: 'var(--fg-0)',
          overflow: 'hidden',
        },
      },
        // Row 1: pipeline status (thin)
        React.createElement(PipelineStatus, { pipeline, loading: loading.pipeline }),

        // Row 2: KPI tiles
        React.createElement(KpiRow, { kpis, loading: loading.kpis }),

        // Row 3: main grid (12 cols, chart stack + trade list)
        React.createElement('div', {
          style: {
            display: 'grid',
            gridTemplateColumns: 'repeat(12, minmax(0, 1fr))',
            gridTemplateRows: 'minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr)',
            gap: 8,
            padding: '0 8px 8px',
            minHeight: 0,
            overflow: 'hidden',
          },
        },
          React.createElement('div', { style: { gridColumn: '1 / span 9', gridRow: '1 / span 1', minHeight: 0 } },
            React.createElement(EquityTimeline, {
              series,
              timeframe: tf,
              onTimeframeChange: setTf,
              highlightTime: selectedTrade && selectedTrade.closed_at
                ? Math.floor(new Date(selectedTrade.closed_at).getTime() / 1000)
                : null,
              loading: loading.series,
            })
          ),
          React.createElement('div', { style: { gridColumn: '1 / span 9', gridRow: '2 / span 1', minHeight: 0 } },
            React.createElement(PnLTicks, { ticks: series.pnl_ticks, loading: loading.series })
          ),
          React.createElement('div', { style: { gridColumn: '1 / span 9', gridRow: '3 / span 1', minHeight: 0 } },
            React.createElement(MarketPriceOverlay, { market: hoveredMarket, loading: false })
          ),
          // Trade list spans all 3 rows on right side
          React.createElement('div', { style: { gridColumn: '10 / span 3', gridRow: '1 / span 3', minHeight: 0 } },
            React.createElement(TradeList, {
              trades, filter,
              onSelect: handleTradeSelect,
              onHover:  handleTradeHover,
              selectedId: selectedTrade ? selectedTrade.id : null,
              loading: loading.trades,
            })
          )
        ),

        // Row 4: allocation bar (full width)
        React.createElement('div', { style: { padding: '0 8px 8px' } },
          React.createElement(AllocationBar, {
            allocation,
            onFilter: setFilter,
            activeFilter: filter,
            loading: loading.allocation,
          })
        )
      )
    );
  };

  // Rebind window.LivePortfolio so dashboard-app.jsx picks up the v2 version.
  window.LivePortfolio = LivePortfolio;
})();
