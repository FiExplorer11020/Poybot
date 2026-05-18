// TradeList.jsx — right side panel
// Vertical list of recent ~50 trades, color-coded by status, click → highlight in
// EquityTimeline + populate MarketPriceOverlay, hover → popover with leader+market.
// Registers on window.Portfolio.TradeList.
//
// Props:
//   trades: [{
//     id, opened_at, closed_at, market_id, market_question, strategy,
//     entry_price, exit_price, size_usdc, pnl_usdc, pnl_pct, status,
//     leader_wallet, hold_secs, side
//   }]
//   filter: { category?: string, leader?: string }  — from AllocationBar clicks
//   onSelect: (trade) => void   — fires when user clicks a row
//   onHover:  (trade|null) => void — fires when user hovers a row
//   selectedId: number|null
//   loading: bool

(function () {
  const { useRef, useState, useMemo } = React;
  const Panel = (window.UI && window.UI.Panel) || (({ title, subtitle, status, children, style }) =>
    React.createElement('div', { style: Object.assign({ background: 'var(--bg-1)', border: '1px solid var(--bd-1)', borderRadius: 2, display: 'flex', flexDirection: 'column', minHeight: 0 }, style) },
      React.createElement('header', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderBottom: '1px solid var(--bd-1)' } },
        React.createElement('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8 } },
          React.createElement('span', { className: 'panel__title' }, title),
          subtitle && React.createElement('span', { className: 'panel__subtitle' }, subtitle)
        ),
        status
      ),
      React.createElement('div', { style: { flex: 1, minHeight: 0, overflow: 'auto' } }, children)
    ));

  const fmtPnl = v => v == null ? '—' : (v >= 0 ? '+' : '−') + '$' + Math.abs(Number(v)).toFixed(2);
  const fmtPct = v => v == null ? '—' : ((v >= 0 ? '+' : '') + (Number(v) * 100).toFixed(2) + '%');
  const fmtPrice = v => v == null ? '—' : Number(v).toFixed(3);
  const fmtSize = v => v == null ? '—' : '$' + Number(v).toFixed(0);
  const short = w => w ? (w.length > 10 ? w.slice(0, 6) + '…' + w.slice(-4) : w) : '—';

  const fmtHold = s => {
    if (s == null) return '—';
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return (s / 3600).toFixed(1) + 'h';
    return (s / 86400).toFixed(1) + 'd';
  };

  // Determine status color category from the trade row.
  const statusFor = t => {
    if (t.status === 'open') return 'open';
    if (t.pnl_usdc == null) return 'open';
    if (Math.abs(Number(t.pnl_usdc)) < 0.01) return 'breakeven';
    return t.pnl_usdc > 0 ? 'win' : 'loss';
  };

  const statusColor = {
    open:      { fg: 'var(--accent-amber)', bg: 'var(--accent-amber-bg)' },
    win:       { fg: 'var(--accent-green)', bg: 'var(--accent-green-bg)' },
    loss:      { fg: 'var(--accent-red)',   bg: 'var(--accent-red-bg)'   },
    breakeven: { fg: 'var(--fg-1)',         bg: 'var(--fg-2-bg)'         },
  };

  const TradeRow = ({ trade, isSelected, onSelect, onHover, onLeave }) => {
    const st = statusFor(trade);
    const col = statusColor[st];
    const strat = (trade.strategy || '').toUpperCase();
    const pnlVal = trade.pnl_usdc != null ? Number(trade.pnl_usdc) : null;

    return (
      React.createElement('div', {
        onClick: () => onSelect && onSelect(trade),
        onMouseEnter: e => { onHover && onHover(trade, e.currentTarget); },
        onMouseLeave: () => { onLeave && onLeave(); },
        style: {
          display: 'grid',
          gridTemplateColumns: '52px 50px 1fr 70px',
          gap: 6,
          padding: '6px 8px',
          borderBottom: '1px solid var(--bd-1)',
          cursor: 'pointer',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          alignItems: 'center',
          background: isSelected ? 'var(--bg-2)' : 'transparent',
          borderLeft: isSelected ? '2px solid ' + col.fg : '2px solid transparent',
          transition: 'background 80ms',
        },
      },
        // Col 1: id + strategy
        React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 1 } },
          React.createElement('span', { style: { color: 'var(--fg-2)' } }, '#' + (trade.id != null ? String(trade.id).slice(-4) : '—')),
          React.createElement('span', { style: { color: strat === 'FOLLOW' ? 'var(--accent-blue)' : (strat === 'FADE' ? 'var(--accent-violet)' : 'var(--fg-2)'), fontSize: 9, fontWeight: 600 } }, strat || '—')
        ),
        // Col 2: status pill
        React.createElement('div', { style: { textAlign: 'center' } },
          React.createElement('span', {
            style: {
              display: 'inline-block',
              padding: '1px 5px',
              border: '1px solid ' + col.fg,
              borderRadius: 2,
              color: col.fg,
              background: col.bg,
              fontWeight: 600,
              fontSize: 9,
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            },
          }, st)
        ),
        // Col 3: prices + hold
        React.createElement('div', { style: { minWidth: 0, overflow: 'hidden' } },
          React.createElement('div', { style: { color: 'var(--fg-0)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
            fmtPrice(trade.entry_price), ' → ', fmtPrice(trade.exit_price)
          ),
          React.createElement('div', { style: { color: 'var(--fg-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
            fmtSize(trade.size_usdc), ' · ', fmtHold(trade.hold_secs)
          )
        ),
        // Col 4: PnL + %
        React.createElement('div', { style: { textAlign: 'right' } },
          React.createElement('div', { style: { color: col.fg, fontWeight: 600 } }, fmtPnl(pnlVal)),
          React.createElement('div', { style: { color: col.fg, fontSize: 9 } }, fmtPct(trade.pnl_pct))
        )
      )
    );
  };

  const Popover = ({ trade, anchor }) => {
    if (!trade || !anchor) return null;
    const rect = anchor.getBoundingClientRect();
    const style = {
      position: 'fixed',
      top:  Math.min(rect.top, window.innerHeight - 80),
      left: rect.left - 320,
      width: 300,
      background: 'var(--bg-2)',
      border: '1px solid var(--bd-2)',
      borderRadius: 2,
      padding: 8,
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      color: 'var(--fg-0)',
      zIndex: 9999,
      boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
      pointerEvents: 'none',
    };
    return (
      React.createElement('div', { style },
        React.createElement('div', { style: { color: 'var(--fg-2)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 } }, 'LEADER'),
        React.createElement('div', { style: { color: 'var(--accent-violet)', wordBreak: 'break-all', marginBottom: 6 } }, trade.leader_wallet || '—'),
        React.createElement('div', { style: { color: 'var(--fg-2)', fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 } }, 'MARKET'),
        React.createElement('div', { style: { color: 'var(--fg-0)' } }, trade.market_question || trade.market_id || '—')
      )
    );
  };

  const TradeList = ({ trades = [], filter = {}, onSelect, onHover, selectedId, loading = false }) => {
    const [hover, setHover] = useState({ trade: null, anchor: null });

    // Apply filter from AllocationBar
    const filtered = useMemo(() => {
      let rows = trades;
      if (filter && filter.category) rows = rows.filter(t => (t.category || '').toLowerCase() === filter.category.toLowerCase());
      if (filter && filter.leader)   rows = rows.filter(t => (t.leader_wallet || '') === filter.leader);
      return rows.slice(0, 50);
    }, [trades, filter && filter.category, filter && filter.leader]);

    const handleHover = (trade, anchor) => {
      setHover({ trade, anchor });
      onHover && onHover(trade);
    };
    const handleLeave = () => {
      setHover({ trade: null, anchor: null });
      onHover && onHover(null);
    };

    const filterLabel = filter && (filter.category || filter.leader)
      ? React.createElement('span', { className: 'panel__subtitle', style: { color: 'var(--accent-amber)' } },
          'filtered: ' + (filter.category ? filter.category : short(filter.leader)))
      : null;

    return (
      React.createElement(React.Fragment, null,
        React.createElement(Panel, {
          title: 'TRADES',
          subtitle: filtered.length + ' / ' + trades.length,
          accent: 'blue',
          status: filterLabel,
          style: { height: '100%' },
        },
          loading && trades.length === 0
            ? React.createElement('div', { style: { padding: 16, color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 11, textAlign: 'center' } }, 'LOADING…')
            : filtered.length === 0
              ? React.createElement('div', { style: { padding: 16, color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 11, textAlign: 'center' } }, 'NO TRADES · waiting for /api/portfolio/trades')
              : filtered.map(t =>
                  React.createElement(TradeRow, {
                    key: t.id || (t.opened_at + ':' + t.market_id),
                    trade: t,
                    isSelected: selectedId != null && t.id === selectedId,
                    onSelect, onHover: handleHover, onLeave: handleLeave,
                  })
                )
        ),
        React.createElement(Popover, { trade: hover.trade, anchor: hover.anchor })
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.TradeList = TradeList;
})();
