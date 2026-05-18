// MarketPriceOverlay.jsx — third panel under PnLTicks
// Line chart of YES (and optional NO) price for the currently-hovered trade's market.
// Updates when user hovers a row in TradeList.
// Registers on window.Portfolio.MarketPriceOverlay.
//
// Props:
//   market: { question, market_id, yes_price_series:[{time,value}], no_price_series:[{time,value}] }
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
      bg:     (cs.getPropertyValue('--bg-1') || '#0c0e12').trim(),
      grid:   (cs.getPropertyValue('--bd-1') || '#1a1f2b').trim(),
      text:   (cs.getPropertyValue('--fg-1') || '#9aa1b3').trim(),
      blue:   (cs.getPropertyValue('--accent-blue')   || '#60a5fa').trim(),
      violet: (cs.getPropertyValue('--accent-violet') || '#a78bfa').trim(),
    };
  };

  const MarketPriceOverlay = ({ market, loading = false }) => {
    const containerRef = useRef(null);
    const chartRef = useRef(null);
    const yesRef = useRef(null);
    const noRef = useRef(null);
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
        rightPriceScale: { borderColor: T.grid, scaleMargins: { top: 0.1, bottom: 0.1 }, mode: 0 },
        timeScale: { borderColor: T.grid, timeVisible: true, secondsVisible: false },
      });

      const yes = chart.addLineSeries({
        color: T.blue,
        lineWidth: 2,
        priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
        priceLineVisible: false,
        title: 'YES',
      });
      const no = chart.addLineSeries({
        color: T.violet,
        lineWidth: 1,
        lineStyle: 2, // dashed
        priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
        priceLineVisible: false,
        title: 'NO',
      });
      // Fix price scale to 0-1 (binary market)
      chart.priceScale('right').applyOptions({ autoScale: false, mode: 0 });

      chartRef.current = chart;
      yesRef.current = yes;
      noRef.current = no;

      const ro = new ResizeObserver(entries => {
        const e = entries[0];
        if (!e) return;
        chart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height });
      });
      ro.observe(containerRef.current);

      // Crosshair sync
      const onMove = (param) => {
        if (!param || param.time == null) return;
        window.Portfolio.__sync.publish({ source: 'market', time: param.time });
      };
      chart.subscribeCrosshairMove(onMove);

      const unsub = (window.Portfolio.__sync || { subscribe: () => () => {} }).subscribe(evt => {
        if (!evt || evt.source === 'market' || evt.time == null) return;
        try { chart.setCrosshairPosition(NaN, evt.time, yes); } catch (e) { /* noop */ }
      });

      setReady(true);

      return () => {
        try { ro.disconnect(); } catch (e) {}
        try { chart.unsubscribeCrosshairMove(onMove); } catch (e) {}
        try { unsub(); } catch (e) {}
        try { chart.remove(); } catch (e) {}
      };
    }, []);

    // Feed YES / NO series
    useEffect(() => {
      if (!ready) return;
      const yesData = ((market && market.yes_price_series) || []).map(p => ({ time: p.time, value: Number(p.value) }));
      const noData  = ((market && market.no_price_series)  || []).map(p => ({ time: p.time, value: Number(p.value) }));
      try {
        yesRef.current && yesRef.current.setData(yesData);
        noRef.current  && noRef.current.setData(noData);
        if (chartRef.current && yesData.length > 0) chartRef.current.timeScale().fitContent();
      } catch (e) { console.warn('[MarketPriceOverlay] setData failed', e); }
    }, [market && market.market_id, market && market.yes_price_series, market && market.no_price_series, ready]);

    const lwcAvailable = !!LWC();
    const hasMarket = !!(market && market.market_id);
    const question = market && (market.question || market.market_id) || null;

    return (
      React.createElement(Panel, {
        title: 'MARKET PRICE',
        subtitle: question ? (question.length > 60 ? question.slice(0, 60) + '…' : question) : 'hover a trade',
        style: { height: '100%' },
      },
        React.createElement('div', { style: { position: 'relative', width: '100%', height: '100%', minHeight: 120 } },
          React.createElement('div', { ref: containerRef, style: { position: 'absolute', inset: 0 } }),
          !lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--accent-amber)', fontFamily: 'var(--font-mono)', fontSize: 11 }
          }, 'lightweight-charts CDN not loaded'),
          !hasMarket && lwcAvailable && React.createElement('div', {
            style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 11, textAlign: 'center', padding: 12 }
          }, loading ? 'LOADING…' : 'HOVER A TRADE IN THE LIST →')
        )
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.MarketPriceOverlay = MarketPriceOverlay;
})();
