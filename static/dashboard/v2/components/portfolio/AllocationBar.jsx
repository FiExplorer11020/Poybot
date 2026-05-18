// AllocationBar.jsx — bottom strip
// Two horizontal stacked bars: capital allocation by category and by leader.
// Click a segment → calls onFilter({category}) or onFilter({leader}) so the
// parent can filter the TradeList.
// Registers on window.Portfolio.AllocationBar.
//
// Props:
//   allocation: {
//     total_usdc: number,
//     by_category: [{key:'sports', size_usdc, share}],
//     by_leader:   [{wallet, label, size_usdc, share}]   // already truncated to top 5 + 'other'
//   }
//   onFilter: ({category?: string, leader?: string}|null) => void
//   activeFilter: {category?, leader?}|null
//   loading: bool

(function () {
  const { useMemo } = React;
  const Panel = (window.UI && window.UI.Panel) || (({ title, subtitle, status, children, style }) =>
    React.createElement('div', { style: Object.assign({ background: 'var(--bg-1)', border: '1px solid var(--bd-1)', borderRadius: 2, display: 'flex', flexDirection: 'column', minHeight: 0 }, style) },
      React.createElement('header', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderBottom: '1px solid var(--bd-1)' } },
        React.createElement('div', null,
          React.createElement('span', { className: 'panel__title' }, title),
          subtitle && React.createElement('span', { className: 'panel__subtitle', style: { marginLeft: 8 } }, subtitle)
        ),
        status
      ),
      React.createElement('div', { style: { padding: 8, flex: 1 } }, children)
    ));

  // Stable color assignment for categories (deterministic mapping).
  const CATEGORY_COLORS = {
    sports:   'var(--accent-green)',
    crypto:   'var(--accent-blue)',
    macro:    'var(--accent-violet)',
    politics: 'var(--accent-amber)',
    other:    'var(--fg-1)',
  };
  // Round-robin palette for leaders (top 5 + 'other').
  const LEADER_PALETTE = [
    'var(--accent-blue)',
    'var(--accent-violet)',
    'var(--accent-amber)',
    'var(--accent-green)',
    'var(--accent-red)',
    'var(--fg-1)',
  ];
  const colorForCategory = key => CATEGORY_COLORS[String(key || 'other').toLowerCase()] || 'var(--fg-1)';
  const short = w => w ? (w.length > 10 ? w.slice(0, 6) + '…' + w.slice(-4) : w) : '—';
  const fmtUsd = v => v == null ? '—' : '$' + Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 });

  const StackedBar = ({ segments, total, onClick, isActive, kind }) => {
    if (!segments || segments.length === 0 || !total || total <= 0) {
      return (
        React.createElement('div', { style: { height: 28, background: 'var(--bg-2)', border: '1px solid var(--bd-1)', borderRadius: 2,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--fg-2)', fontFamily: 'var(--font-mono)', fontSize: 10 } },
          'NO ALLOCATION DATA')
      );
    }
    return (
      React.createElement('div', { style: { height: 28, display: 'flex', borderRadius: 2, overflow: 'hidden', border: '1px solid var(--bd-1)' } },
        segments.map((s, i) => {
          const w = (Number(s.share) || (Number(s.size_usdc) / Number(total))) * 100;
          const c = kind === 'category' ? colorForCategory(s.key) : LEADER_PALETTE[i % LEADER_PALETTE.length];
          const key = kind === 'category' ? s.key : (s.wallet || s.label);
          const isThisActive = isActive && isActive(key);
          return (
            React.createElement('div', {
              key: key + ':' + i,
              onClick: () => onClick && onClick(key, s),
              title: (s.label || s.key || short(s.wallet)) + ' · ' + fmtUsd(s.size_usdc) + ' · ' + w.toFixed(1) + '%',
              style: {
                width: w + '%',
                background: c,
                borderRight: i < segments.length - 1 ? '1px solid var(--bg-1)' : 'none',
                opacity: isActive && !isThisActive ? 0.35 : 1,
                cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 600,
                color: 'rgba(0,0,0,0.75)',
                textTransform: 'uppercase',
                transition: 'opacity 100ms',
                overflow: 'hidden',
              },
            }, w > 6 ? (kind === 'category' ? String(key).slice(0, 6) : short(s.wallet || s.label)) : '')
          );
        })
      )
    );
  };

  const Legend = ({ segments, total, kind, onClick, isActive }) =>
    React.createElement('div', { style: { display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 6, fontFamily: 'var(--font-mono)', fontSize: 9 } },
      segments.map((s, i) => {
        const c = kind === 'category' ? colorForCategory(s.key) : LEADER_PALETTE[i % LEADER_PALETTE.length];
        const key = kind === 'category' ? s.key : (s.wallet || s.label);
        const pct = ((Number(s.size_usdc) / Number(total)) * 100).toFixed(1);
        const this_active = isActive && isActive(key);
        return (
          React.createElement('span', {
            key: key + ':' + i,
            onClick: () => onClick && onClick(key, s),
            style: { display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer',
              color: this_active ? 'var(--fg-0)' : 'var(--fg-1)',
              opacity: isActive && !this_active ? 0.5 : 1 },
          },
            React.createElement('span', { style: { display: 'inline-block', width: 8, height: 8, background: c, borderRadius: 1 } }),
            React.createElement('span', null, kind === 'category' ? key : (s.label || short(s.wallet))),
            React.createElement('span', { style: { color: 'var(--fg-2)' } }, pct + '%')
          )
        );
      })
    );

  const AllocationBar = ({ allocation = {}, onFilter, activeFilter, loading = false }) => {
    const total = Number(allocation.total_usdc || 0);
    const cats  = allocation.by_category || [];
    const leads = allocation.by_leader   || [];

    const activeCategoryFn = key => activeFilter && activeFilter.category === key;
    const activeLeaderFn   = key => activeFilter && activeFilter.leader === key;

    const handleCategory = key => {
      if (activeFilter && activeFilter.category === key) onFilter && onFilter(null);
      else onFilter && onFilter({ category: key });
    };
    const handleLeader = key => {
      if (activeFilter && activeFilter.leader === key) onFilter && onFilter(null);
      else onFilter && onFilter({ leader: key });
    };

    return (
      React.createElement(Panel, {
        title: 'ALLOCATION',
        subtitle: total > 0 ? (fmtUsd(total) + ' deployed') : (loading ? 'loading' : 'waiting for /api/portfolio/allocation'),
        status: activeFilter && (activeFilter.category || activeFilter.leader)
          ? React.createElement('button', {
              type: 'button',
              onClick: () => onFilter && onFilter(null),
              style: { background: 'var(--bg-2)', color: 'var(--accent-amber)', border: '1px solid var(--accent-amber)',
                borderRadius: 2, padding: '2px 6px', cursor: 'pointer', fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 600 } },
              'CLEAR FILTER')
          : null,
        style: { flexShrink: 0 },
      },
        React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '120px 1fr', gap: 12, alignItems: 'flex-start' } },
          React.createElement('div', { style: { color: 'var(--fg-2)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', paddingTop: 8 } }, 'BY CATEGORY'),
          React.createElement('div', null,
            React.createElement(StackedBar, { segments: cats, total, onClick: handleCategory, isActive: activeCategoryFn, kind: 'category' }),
            cats.length > 0 && React.createElement(Legend, { segments: cats, total, kind: 'category', onClick: handleCategory, isActive: activeCategoryFn })
          )
        ),
        React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '120px 1fr', gap: 12, alignItems: 'flex-start', marginTop: 8 } },
          React.createElement('div', { style: { color: 'var(--fg-2)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', paddingTop: 8 } }, 'BY LEADER'),
          React.createElement('div', null,
            React.createElement(StackedBar, { segments: leads, total, onClick: handleLeader, isActive: activeLeaderFn, kind: 'leader' }),
            leads.length > 0 && React.createElement(Legend, { segments: leads, total, kind: 'leader', onClick: handleLeader, isActive: activeLeaderFn })
          )
        )
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.AllocationBar = AllocationBar;
})();
