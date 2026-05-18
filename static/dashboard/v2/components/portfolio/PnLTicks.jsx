// PnLTicks.jsx — secondary panel under the equity chart
// Histogram of per-trade PnL (green if >0, red if <0), x-axis synced to EquityTimeline.
// Registers on window.Portfolio.PnLTicks.
//
// Props:
//   ticks: [{time, value}]  — one bar per closed trade
//   loading: bool

(function () {
  const { useEffect, useRef, useState } = React;
  const Panel = (window.UI && window.UI.Panel) || (({ title, subtitle, status, children, style }) =>
    React.createElement('div', { style: Object.assign({ background: 'var(--bg-1)', border: '1px solid var(--bd-1)', borderRadius: 2, display: 'flex', flexDirection: 'column', minHeight: 0 }, style) },
      React.createElement('header', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderBottom: '1px solid var(--bd-1)' } },
        React.createElement('div', null,
          React.createElement('span', { className: 'panel__title' }, title),
          subtitle && React.createElement('span', { className: 'panel__subtitle', style: { marginLeft: 8 } }, subtitle)
        ),
        status
      ),
      React.createElement('div', { style: { flex: 1, minHeight: 0 } }, children)
    ));

  const LWC = () => (typeof window !== 'undefined' ? window.LightweightCharts : null);
  const theme = () => {
    const cs = getComputedStyle(document.documentElement);
    return {
      bg:    (cs.getPropertyValue('--bg-1') || '#0c0e12').trim(),
      grid:  (cs.getPropertyValue('--bd-1') || '#1a1f2b').trim(),
      text:  (cs.getPropertyValue('--fg-1') || '#9aa1b3').trim(),
      green: (cs.getPropertyValue('--accent-green') || '#4ade80').trim(),
      red:   (cs.getPropertyValue('--accent-red')   || '#f87171').trim(),
    };
  };

  const PnLTicks = ({ ticks = [], loading = false }) => {
    const containerRef = useRef(null);
    const chartRef = useRef(null);
    const histRef  = useRef(null);
    const [ready, setReady] = useState(false);

    useEffect(() => {
      const lwc = LWC();
      if (!lwc || !containerRef.current) return;
      const T = theme();

      const chart = lwc.createChart(containerRef.current, {
        layout: { background: { color: T.bg }, textColor: T.text, fontFamily: 'JetBrains Mono, monospace', fontSize: 10 },
        grid:   { vertLines: { color: T.grid }, horzLines: { color: T.grid } },
        crosshair: {
          mode: 0,
          vertLine: { color: T.text, width: 1, style: 3, labelVisible: false },
          horzLine: { color: T.text, width: 1, style: 3, labelVisible: false },
        },
        rightPriceScale: { borderColor: T.grid },
        timeScale: { borderColor: T.grid, timeVisible: true, secondsVisible: false },
      });

      const hist = chart.addHistogramSeries({
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: false,
        base: 0,
      });

      chartRef.current = chart;
      histRef.current = hist;

      const ro = new ResizeObserver(entries => {
        const e = entries[0];
        if (!e) return;
        chart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height });
      });
      ro.observe(containerRef.current);

      // Crosshair sync — publish + subscribe
      const onMove = (param) => {
        if (!param || param.time == null) return;
        window.Portfolio.__sync.publish({ source: 'pnl', time: param.time });
      };
      chart.subscribeCrosshairMove(onMove);

      const unsub = (window.Portfolio.__sync || { subscribe: () => () => {} }).subscribe(evt => {
        if (!evt || evt.source === 'pnl' || evt.time == null) return;
        try { chart.setCrosshairPosition(NaN, evt.time, hist); } catch (e) { /* noop */ }
      });

      setReady(true);

      return () => {
        try { ro.disconnect(); } catch (e) {}
        try { chart.unsubscribeCrosshairMove(onMove); } catch (e) {}
        try { unsub(); } catch (e) {}
        try { chart.remove(); } catch (e) {}
      };
    }, []);

    useEffect(() => {
      if (!ready || !histRef.current) return;
      const T = theme();
      const data = (ticks || []).map(p => ({
        time: p.time,
        value: Number(p.value),
        color: Number(p.value) >= 0 ? T.green : T.red,
      }));
      try { histRef.current.setData(data); } catch (e) { console.warn('[PnLTicks] setData failed', e); }
    }, [ticks, ready]);

    const lwcAvailable = !!LWC();
    const empty = !loading && (!ticks || ticks.length === 0);

    return (
      React.createElement(Panel, { title: 'PNL TICKS', subtitle: 'per-trade · synced', style: { height: '100%' } },
        React.createElement('div', { style: { position: 'relative', width: '100%', height: '100%', minHeight: 120 } },
          React.createElement('div', { ref: containerRef, style: { position: 'absolute', inset: 0 } }),
          !lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--accent-amber)', fontFamily: 'var(--font-mono)', fontSize: 11 }
          }, 'lightweight-charts CDN not loaded'),
          empty && lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 11 }
          }, loading ? 'LOADING…' : 'NO CLOSED TRADES YET')
        )
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.PnLTicks = PnLTicks;
})();
