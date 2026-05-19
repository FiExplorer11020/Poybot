// static/dashboard/walletgraph_v3/SelectionPanel.jsx
// Polymarket V3 — Wallet selection panel (HellBorn-style overlay).
// Lazy-fetches drilldown profile + markets on mount with cancellation guard.

(function() {
  const PHASE_COLOR = { 1: '#3b82f6', 2: '#f59e0b', 3: '#10b981' };
  const FOLLOWER_COLOR = '#a78bfa';

  function formatRelTime(iso) {
    if (!iso) return '—';
    const ms = Date.now() - new Date(iso).getTime();
    if (isNaN(ms) || ms < 0) return '—';
    if (ms < 60000) return Math.round(ms / 1000) + 's ago';
    if (ms < 3600000) return Math.round(ms / 60000) + 'm ago';
    if (ms < 86400000) return Math.round(ms / 3600000) + 'h ago';
    return Math.round(ms / 86400000) + 'd ago';
  }

  function shortAddr(id) {
    if (!id || typeof id !== 'string') return '—';
    if (id.length <= 12) return id;
    return id.slice(0, 6) + '…' + id.slice(-4);
  }

  function CopyButton({ value }) {
    const [copied, setCopied] = React.useState(false);
    function copy() {
      try {
        navigator.clipboard.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      } catch (_) {}
    }
    return (
      <span
        onClick={copy}
        style={{ cursor: 'pointer', color: copied ? '#10b981' : '#94a8d6', fontSize: 11, userSelect: 'none' }}
        title="Copy address"
      >
        {copied ? '✓' : '⎘'}
      </span>
    );
  }

  function CategoryBar({ name, pct, color }) {
    const safePct = Math.max(0, Math.min(1, pct || 0));
    return (
      <div style={{ display: 'grid', gridTemplateColumns: '64px 1fr 34px', gap: 6, fontSize: 11, alignItems: 'center', marginBottom: 4 }}>
        <span style={{ color: '#94a8d6', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        <div style={{ height: 4, background: 'rgba(255,255,255,0.08)', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{ width: (Math.round(safePct * 100)) + '%', height: '100%', background: color, transition: 'width 200ms ease-out' }} />
        </div>
        <span style={{ color: '#fff', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{Math.round(safePct * 100)}%</span>
      </div>
    );
  }

  function SelectionPanel({ wallet, onClose }) {
    const [profile, setProfile] = React.useState(null);
    const [markets, setMarkets] = React.useState(null);
    const [mounted, setMounted] = React.useState(false);

    // Entry animation
    React.useEffect(() => {
      const t = setTimeout(() => setMounted(true), 10);
      return () => clearTimeout(t);
    }, []);

    // Lazy fetch drilldown with cancellation guard
    React.useEffect(() => {
      if (!wallet || !wallet.id) return;
      let cancelled = false;
      setProfile(null);
      setMarkets(null);
      fetch('/api/wallet/' + encodeURIComponent(wallet.id) + '/profile')
        .then(function(r) { return r.json(); })
        .then(function(d) { if (!cancelled) setProfile(d); })
        .catch(function() { if (!cancelled) setProfile({ _error: true }); });
      fetch('/api/wallet/' + encodeURIComponent(wallet.id) + '/markets')
        .then(function(r) { return r.json(); })
        .then(function(d) { if (!cancelled) setMarkets(d); })
        .catch(function() { if (!cancelled) setMarkets({ _error: true }); });
      return function() { cancelled = true; };
    }, [wallet && wallet.id]);

    if (!wallet) return null;

    const phaseColor = wallet.role === 'follower'
      ? FOLLOWER_COLOR
      : (PHASE_COLOR[wallet.phase] || PHASE_COLOR[1]);

    const labelStyle = { color: '#94a8d6', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 2 };
    const valueStyle = { color: '#fff', fontSize: 12, fontVariantNumeric: 'tabular-nums' };

    const cats = (wallet.top_categories && wallet.top_categories.length)
      ? wallet.top_categories
      : (markets && markets.category_breakdown ? markets.category_breakdown : []);

    return (
      <div
        style={{
          position: 'absolute',
          top: 16,
          left: 16,
          width: 280,
          maxHeight: 'calc(100% - 32px)',
          overflowY: 'auto',
          overflowX: 'hidden',
          background: 'rgba(10, 14, 26, 0.92)',
          backdropFilter: 'blur(8px)',
          WebkitBackdropFilter: 'blur(8px)',
          border: '1px solid rgba(255, 255, 255, 0.12)',
          borderLeft: '4px solid ' + phaseColor,
          borderRadius: 2,
          padding: 14,
          zIndex: 10,
          fontFamily: 'JetBrains Mono, monospace',
          // 0 0 24px phase-tinted glow gives the panel its own ambient halo.
          boxShadow: '0 8px 32px rgba(0, 0, 0, 0.4), 0 0 24px ' + phaseColor + '66',
          transform: mounted ? 'translateX(0)' : 'translateX(-20px)',
          opacity: mounted ? 1 : 0,
          transition: 'transform 300ms ease-out, opacity 200ms ease-out',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <div style={labelStyle}>Selected Wallet</div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <CopyButton value={wallet.id} />
            {onClose && (
              <span
                onClick={onClose}
                style={{ cursor: 'pointer', color: '#94a8d6', fontSize: 12, userSelect: 'none' }}
                title="Close"
              >
                ✕
              </span>
            )}
          </div>
        </div>
        <div style={{ ...valueStyle, fontSize: 13, marginBottom: 12, wordBreak: 'break-all' }}>
          {wallet.label || shortAddr(wallet.id)}
        </div>

        {/* Role/Phase + Falcon */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          <div>
            <div style={labelStyle}>Role · Phase</div>
            <div style={valueStyle}>{(wallet.role || '—') + ' · ' + (wallet.phase ? 'P' + wallet.phase : '—')}</div>
          </div>
          <div>
            <div style={labelStyle}>Falcon</div>
            <div style={valueStyle}>{wallet.falcon_score != null ? wallet.falcon_score.toFixed(2) : '—'}</div>
          </div>
        </div>

        {/* Trades / 24h / Resolved */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          <div>
            <div style={labelStyle}>Trades · 24h</div>
            <div style={valueStyle}>{(wallet.trades_observed != null ? wallet.trades_observed : 0) + ' · ' + (wallet.trades_24h != null ? wallet.trades_24h : 0)}</div>
          </div>
          <div>
            <div style={labelStyle}>Resolved</div>
            <div style={valueStyle}>{wallet.positions_resolved != null ? wallet.positions_resolved : 0}</div>
          </div>
        </div>

        {/* Win Rate / PnL */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          <div>
            <div style={labelStyle}>Win Rate</div>
            <div style={valueStyle}>{wallet.win_rate != null ? Math.round(wallet.win_rate * 100) + '%' : '—'}</div>
          </div>
          <div>
            <div style={labelStyle}>PnL</div>
            <div style={{ ...valueStyle, color: wallet.pnl_total > 0 ? '#10b981' : (wallet.pnl_total < 0 ? '#ef4444' : '#fff') }}>
              {wallet.pnl_total != null ? '$' + wallet.pnl_total.toFixed(2) : '—'}
            </div>
          </div>
        </div>

        {/* Categories */}
        {cats && cats.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ ...labelStyle, marginBottom: 6 }}>Categories</div>
            {cats.slice(0, 3).map(function(c) {
              return <CategoryBar key={c.name} name={c.name} pct={c.pct || 0} color={phaseColor} />;
            })}
          </div>
        )}

        {/* Last action */}
        {wallet.last_action && (
          <div style={{ marginBottom: 12, paddingTop: 8, borderTop: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 6 }}>
              <div>
                <div style={labelStyle}>Last action</div>
                <div style={valueStyle}>{wallet.last_action}</div>
              </div>
              <div>
                <div style={labelStyle}>Confidence</div>
                <div style={valueStyle}>{wallet.last_confidence != null ? wallet.last_confidence.toFixed(2) : '—'}</div>
              </div>
            </div>
            <div style={{ ...labelStyle, fontSize: 9, marginBottom: 0 }}>{formatRelTime(wallet.last_decision_iso)}</div>
          </div>
        )}

        {/* Drilldown status */}
        {profile === null && (
          <div style={{ ...labelStyle, fontSize: 9, color: '#475569', marginBottom: 6 }}>Loading drilldown…</div>
        )}
        {profile && profile._error && (
          <div style={{ ...labelStyle, fontSize: 9, color: '#ef4444', marginBottom: 6 }}>Drilldown failed</div>
        )}

        {/* Exclude reason */}
        {wallet.exclude_reason && (
          <div style={{ ...labelStyle, fontSize: 9, color: '#f59e0b', marginBottom: 6 }}>
            Excluded: {wallet.exclude_reason}
          </div>
        )}

        {/* SHOW TRANSFERS button */}
        <button
          type="button"
          style={{
            marginTop: 12,
            width: '100%',
            padding: '10px 16px',
            background: 'transparent',
            border: '1px solid ' + phaseColor,
            color: phaseColor,
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 11,
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            cursor: 'pointer',
            transition: 'background 150ms, box-shadow 150ms',
          }}
          onMouseEnter={function(e) {
            e.currentTarget.style.background = phaseColor + '20';
            e.currentTarget.style.boxShadow = '0 0 12px ' + phaseColor + '80';
          }}
          onMouseLeave={function(e) {
            e.currentTarget.style.background = 'transparent';
            e.currentTarget.style.boxShadow = 'none';
          }}
        >
          ▶ Inspect wallet
        </button>
      </div>
    );
  }

  if (typeof window !== 'undefined') {
    window.WG_V3 = window.WG_V3 || {};
    window.WG_V3.SelectionPanel = SelectionPanel;
  }
})();
