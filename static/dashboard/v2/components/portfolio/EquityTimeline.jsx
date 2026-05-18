// EquityTimeline.jsx — main chart panel
// Area series of equity_usdc + drawdown band + per-trade close markers.
// Synchronized crosshair (publishes to window.Portfolio.__sync).
// Registers on window.Portfolio.EquityTimeline.
//
// Props:
//   series: { equity:[{time, value}], drawdown:[{time, value}], trades:[{time, side:'win'|'loss', pnl}] }
//   timeframe: '1h'  (display only — fetching done by parent)
//   onTimeframeChange: (tf) => void
//   highlightTime:  number|null  (unix seconds) — when set, draws a vertical line marker
//   loading: bool
//
// Synchronized crosshair contract:
//   - On crosshair move, publish { source: 'equity', time, price } via Portfolio.__sync.publish(...)
//   - Subscribe to other-source crosshair events to redraw the local crosshair on chart

(function () {
  const { useEffect, useRef, useState } = React;
  const Panel = (window.UI && window.UI.Panel) || (({ title, subtitle, status, children, style }) =>
    React.createElement('div', { style: Object.assign({ background: 'var(--bg-1)', border: '1px solid var(--bd-1)', borderRadius: 2, display: 'flex', flexDirection: 'column', minHeight: 0 }, style) },
      React.createElement('header', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderBottom: '1px solid var(--bd-1)' } },
        React.createElement('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8 } },
          React.createElement('span', { className: 'panel__title' }, title),
          subtitle && React.createElement('span', { className: 'panel__subtitle' }, subtitle)
        ),
        status
      ),
      React.createElement('div', { style: { flex: 1, minHeight: 0 } }, children)
    ));

  // Lightweight-charts is loaded via CDN script tag. Guard for early-render.
  const LWC = () => (typeof window !== 'undefined' ? window.LightweightCharts : null);

  // ── Sync hub (singleton) ───────────────────────────────────────────────────
  // Lets EquityTimeline / PnLTicks / MarketPriceOverlay share crosshair state.
  window.Portfolio = window.Portfolio || {};
  if (!window.Portfolio.__sync) {
    const listeners = new Set();
    window.Portfolio.__sync = {
      publish(evt) { listeners.forEach(fn => { try { fn(evt); } catch (e) { /* ignore */ } }); },
      subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    };
  }

  // ── Timeframe selector (inline; mirrors theme.css `.tf-selector`) ─────────
  const TF_OPTIONS = ['1m', '5m', '15m', '1h', '4h', '1d', '1w'];
  const TimeframeSelector = window.UI && window.UI.TimeframeSelector
    ? window.UI.TimeframeSelector
    : ({ value, onChange, options = TF_OPTIONS }) =>
        React.createElement('div', { className: 'tf-selector' },
          options.map(opt =>
            React.createElement('button', {
              key: opt,
              type: 'button',
              className: 'tf-selector__btn' + (opt === value ? ' tf-selector__btn--active' : ''),
              onClick: () => onChange && onChange(opt),
            }, opt)
          )
        );

  // ── Dark theme colors (read from theme.css custom properties) ─────────────
  const theme = () => {
    const cs = getComputedStyle(document.documentElement);
    return {
      bg:     (cs.getPropertyValue('--bg-1') || '#0c0e12').trim(),
      grid:   (cs.getPropertyValue('--bd-1') || '#1a1f2b').trim(),
      border: (cs.getPropertyValue('--bd-2') || '#2a3142').trim(),
      text:   (cs.getPropertyValue('--fg-1') || '#9aa1b3').trim(),
      green:  (cs.getPropertyValue('--accent-green') || '#4ade80').trim(),
      red:    (cs.getPropertyValue('--accent-red')   || '#f87171').trim(),
      amber:  (cs.getPropertyValue('--accent-amber') || '#fbbf24').trim(),
    };
  };

  const EquityTimeline = ({ series = {}, timeframe = '1h', onTimeframeChange, highlightTime, loading = false }) => {
    const containerRef = useRef(null);
    const chartRef = useRef(null);
    const equitySeriesRef = useRef(null);
    const ddSeriesRef = useRef(null);
    const lastTimeRef = useRef(null);
    const [chartReady, setChartReady] = useState(false);

    // Create chart once
    useEffect(() => {
      const lwc = LWC();
      if (!lwc || !containerRef.current) return;
      const T = theme();

      const chart = lwc.createChart(containerRef.current, {
        layout: { background: { color: T.bg }, textColor: T.text, fontFamily: 'JetBrains Mono, monospace', fontSize: 10 },
        grid:   { vertLines: { color: T.grid }, horzLines: { color: T.grid } },
        crosshair: {
          mode: 0, // Normal — follows mouse on both axes
          vertLine: { color: T.border, width: 1, style: 0, labelBackgroundColor: T.bg },
          horzLine: { color: T.border, width: 1, style: 0, labelBackgroundColor: T.bg },
        },
        rightPriceScale: { borderColor: T.grid },
        timeScale:       { borderColor: T.grid, timeVisible: true, secondsVisible: false },
        handleScroll: true,
        handleScale:  true,
      });

      const equityArea = chart.addAreaSeries({
        lineColor: T.green,
        topColor: 'rgba(74, 222, 128, 0.35)',
        bottomColor: 'rgba(74, 222, 128, 0.02)',
        lineWidth: 2,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: false,
      });

      const ddArea = chart.addAreaSeries({
        lineColor: T.red,
        topColor: 'rgba(248, 113, 113, 0.02)',
        bottomColor: 'rgba(248, 113, 113, 0.25)',
        lineWidth: 1,
        priceScaleId: 'left',
        priceFormat: { type: 'percent', precision: 2, minMove: 0.01 },
        priceLineVisible: false,
      });
      chart.priceScale('left').applyOptions({ borderColor: T.grid, visible: true, scaleMargins: { top: 0.7, bottom: 0 } });

      chartRef.current = chart;
      equitySeriesRef.current = equityArea;
      ddSeriesRef.current = ddArea;

      // Resize observer
      const ro = new ResizeObserver(entries => {
        const e = entries[0];
        if (!e) return;
        chart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height });
      });
      ro.observe(containerRef.current);

      // Publish crosshair moves to sync hub
      const onMove = (param) => {
        if (!param || param.time == null) return;
        lastTimeRef.current = param.time;
        const price = param.seriesData ? param.seriesData.get(equityArea) : null;
        window.Portfolio.__sync.publish({ source: 'equity', time: param.time, price: price?.value });
      };
      chart.subscribeCrosshairMove(onMove);

      // Subscribe to peer crosshair moves → set our crosshair to that time
      const unsub = window.Portfolio.__sync.subscribe(evt => {
        if (!evt || evt.source === 'equity' || evt.time == null) return;
        try {
          chart.setCrosshairPosition(NaN, evt.time, equityArea);
        } catch (e) { /* lightweight-charts < 4.1 may lack this */ }
      });

      setChartReady(true);

      return () => {
        try { ro.disconnect(); } catch (e) {}
        try { chart.unsubscribeCrosshairMove(onMove); } catch (e) {}
        try { unsub(); } catch (e) {}
        try { chart.remove(); } catch (e) {}
        chartRef.current = null;
      };
    }, []);

    // Feed data → equity + drawdown
    useEffect(() => {
      if (!chartReady) return;
      const eq = (series.equity || []).map(p => ({ time: p.time, value: Number(p.value) }));
      const dd = (series.drawdown || []).map(p => ({ time: p.time, value: -Math.abs(Number(p.value)) }));
      try {
        equitySeriesRef.current && equitySeriesRef.current.setData(eq);
        ddSeriesRef.current && ddSeriesRef.current.setData(dd);
      } catch (e) { console.warn('[EquityTimeline] setData failed:', e); }
    }, [series.equity, series.drawdown, chartReady]);

    // Trade markers (per-trade close)
    useEffect(() => {
      if (!chartReady || !equitySeriesRef.current) return;
      const T = theme();
      const trades = series.trades || [];
      const markers = trades.map(t => ({
        time: t.time,
        position: t.side === 'win' ? 'aboveBar' : 'belowBar',
        color: t.side === 'win' ? T.green : T.red,
        shape: t.side === 'win' ? 'arrowUp' : 'arrowDown',
        text: (t.side === 'win' ? '+' : '') + (t.pnl != null ? Number(t.pnl).toFixed(0) : ''),
      }));
      try { equitySeriesRef.current.setMarkers(markers); } catch (e) { /* noop */ }
    }, [series.trades, chartReady]);

    // Highlight time (from TradeList click) — set crosshair to that timestamp
    useEffect(() => {
      if (!chartReady || !chartRef.current || highlightTime == null) return;
      try {
        chartRef.current.timeScale().scrollToPosition(0, false);
        equitySeriesRef.current && chartRef.current.setCrosshairPosition(NaN, highlightTime, equitySeriesRef.current);
      } catch (e) { /* noop */ }
    }, [highlightTime, chartReady]);

    const lwcAvailable = !!LWC();
    const empty = !loading && (!series.equity || series.equity.length === 0);

    return (
      React.createElement(Panel, {
        title: 'EQUITY TIMELINE',
        subtitle: timeframe.toUpperCase() + (loading ? ' · loading' : ''),
        accent: 'green',
        style: { height: '100%' },
        status: React.createElement(TimeframeSelector, {
          value: timeframe,
          onChange: onTimeframeChange,
        }),
      },
        React.createElement('div', { style: { position: 'relative', width: '100%', height: '100%', minHeight: 220 } },
          React.createElement('div', { ref: containerRef, style: { position: 'absolute', inset: 0 } }),
          !lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--accent-amber)', fontFamily: 'var(--font-mono)', fontSize: 11 }
          }, 'lightweight-charts CDN not loaded — hard refresh required'),
          empty && lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 11 }
          }, loading ? 'LOADING…' : 'NO EQUITY DATA · waiting for /api/portfolio/timeseries'),
          loading && lwcAvailable && !empty && React.createElement('div', {
            style: { position: 'absolute', top: 6, right: 8, color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 10 }
          }, '⟳ syncing…')
        )
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.EquityTimeline = EquityTimeline;
})();
