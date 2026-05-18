// KpiRow.jsx — 6 KPI tiles across the top of the Live Portfolio
// Registers on window.Portfolio.KpiRow.
//
// Props:
//   kpis: {
//     balance, balance_24h_series:[number],
//     peak, drawdown_pct,
//     daily_pnl, daily_wins,
//     win_rate, wins, losses,
//     open_count, open_size_usdc,
//     latency_p50_ms,
//   }
//   loading: bool

(function () {
  const Sparkline = window.Sparkline; // exposed by dashboard-components.jsx
  const fmtUsd = (v, d = 0) => v == null ? '—' : '$' + Number(v).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });
  const fmtSignedUsd = (v, d = 0) => {
    if (v == null) return '—';
    const sign = v >= 0 ? '+' : '−';
    return sign + '$' + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });
  };
  const fmtPct = (v, d = 1) => v == null ? '—' : (Number(v) * 100).toFixed(d) + '%';

  // Local Tile — mirrors theme.css `.kpi-tile`. Falls back to inline styles.
  const KpiTile = window.UI && window.UI.KpiTile
    ? window.UI.KpiTile
    : ({ label, value, secondary, accent, sign, sparklineData }) => {
        const valCls = ['kpi-tile__value'];
        if (sign === '+') valCls.push('kpi-tile__value--pos');
        else if (sign === '-') valCls.push('kpi-tile__value--neg');
        const rootCls = ['kpi-tile'];
        if (accent) rootCls.push('kpi-tile--accent-' + accent);
        return (
          React.createElement('div', { className: rootCls.join(' ') },
            React.createElement('div', { className: 'kpi-tile__label' }, label),
            React.createElement('div', { className: valCls.join(' ') }, value),
            secondary != null && React.createElement('div', { className: 'kpi-tile__secondary' }, secondary),
            sparklineData && sparklineData.length > 0 && Sparkline && React.createElement('div',
              { className: 'kpi-tile__sparkline' },
              React.createElement(Sparkline, { data: sparklineData, width: 160, height: 24,
                color: sign === '-' ? '#f87171' : '#4ade80', fluid: true })
            )
          )
        );
      };

  const KpiRow = ({ kpis = {}, loading = false }) => {
    const k = kpis || {};
    const dPnL = k.daily_pnl;
    const dd   = k.drawdown_pct;

    const tiles = [
      {
        label: 'BALANCE',
        value: loading ? '—' : fmtUsd(k.balance, 0),
        secondary: k.balance_change_24h != null
          ? (k.balance_change_24h >= 0 ? '+' : '−') + fmtUsd(Math.abs(k.balance_change_24h), 0) + ' 24h'
          : null,
        sparklineData: k.balance_24h_series,
        sign: k.balance_change_24h != null ? (k.balance_change_24h >= 0 ? '+' : '-') : undefined,
        accent: undefined,
      },
      {
        label: 'PEAK',
        value: loading ? '—' : fmtUsd(k.peak, 0),
        secondary: dd != null ? ('DD ' + (dd * 100).toFixed(2) + '%') : null,
        sign: dd != null && dd > 0 ? '-' : undefined,
        accent: dd != null && dd > 0.05 ? 'red' : undefined,
      },
      {
        label: 'DAILY P&L',
        value: loading ? '—' : fmtSignedUsd(dPnL, 0),
        secondary: k.daily_wins != null
          ? (k.daily_wins + 'W · ' + (k.daily_losses ?? 0) + 'L')
          : null,
        sign: dPnL == null ? undefined : (dPnL >= 0 ? '+' : '-'),
        accent: dPnL == null ? undefined : (dPnL >= 0 ? 'green' : 'red'),
      },
      {
        label: 'WIN RATE',
        value: loading ? '—' : fmtPct(k.win_rate, 1),
        secondary: k.wins != null
          ? (k.wins + 'W / ' + (k.losses ?? 0) + 'L')
          : null,
        accent: k.win_rate != null && k.win_rate >= 0.5 ? 'green' : undefined,
      },
      {
        label: 'OPEN POSITIONS',
        value: loading ? '—' : (k.open_count ?? 0),
        secondary: k.open_size_usdc != null
          ? (fmtUsd(k.open_size_usdc, 0) + ' sized')
          : null,
        accent: 'blue',
      },
      {
        label: 'LATENCY p50',
        value: loading ? '—' : (k.latency_p50_ms != null ? k.latency_p50_ms + 'ms' : '—'),
        secondary: k.latency_p95_ms != null ? ('p95 ' + k.latency_p95_ms + 'ms') : null,
        accent: k.latency_p50_ms != null && k.latency_p50_ms > 200 ? 'amber' : undefined,
      },
    ];

    return (
      React.createElement('div', {
        className: 'portfolio-kpi-row',
        style: {
          display: 'grid',
          gridTemplateColumns: 'repeat(6, minmax(0, 1fr))',
          gap: 8,
          padding: 8,
          flexShrink: 0,
          background: 'var(--bg-0)',
        },
      },
        tiles.map((t, i) => React.createElement(KpiTile, Object.assign({ key: i }, t)))
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.KpiRow = KpiRow;
})();
