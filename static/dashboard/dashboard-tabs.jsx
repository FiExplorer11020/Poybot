// dashboard-tabs.jsx — 6 tab views wired to Poybot LiveSnapshot

const { useState: useStateT, useEffect: useEffectT, useMemo: useMemoT } = React;
const {
  C, S, useLiveStore, ConnBanner,
  Badge, MiniBar, ScoreBar, Dot, KpiStrip, TH, TD, SectionLabel,
  short, fmtAge, fmtPnl, fmtPct, fmtMs, fmtNum,
  pnlColor, sideColor, actionType,
} = window;

// ─── ALPHA TERMINAL ───────────────────────────────────────────────────────────
const AlphaTerminal = () => {
  const { snapshot, connectionState } = useLiveStore();
  const stats  = snapshot?.stats                    || {};
  const ana    = snapshot?.analytics?.summary       || {};
  const de     = snapshot?.decision_engine?.summary || {};
  const trades = snapshot?.recent_trades            || [];

  const kpis = [
    { label: 'Net PnL',           value: fmtPnl(stats.total_pnl),       color: pnlColor(stats.total_pnl) },
    { label: 'Win Rate',          value: stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—', color: C.amber },
    { label: 'Active Markets',    value: stats.active_markets    ?? '—', color: C.blue },
    { label: 'Open Positions',    value: stats.open_positions    ?? '—', color: C.text },
    { label: 'Portfolio',         value: stats.portfolio_total   != null ? `$${stats.portfolio_total.toFixed(0)}` : '—', color: C.white },
    { label: 'PnL %',             value: stats.pnl_percent       != null ? `${stats.pnl_percent >= 0 ? '+' : ''}${(stats.pnl_percent * 100).toFixed(2)}%` : '—', color: pnlColor(stats.pnl_percent) },
    { label: 'Arbs Today',        value: stats.detected_arbs_today ?? '—', color: C.green },
    { label: 'Capital in Trade',  value: stats.capital_in_trade  != null ? `$${stats.capital_in_trade.toFixed(0)}` : '—', color: C.text },
  ];

  const mlCells = [
    { l: 'Tracked Markets',  v: ana.tracked_markets     ?? '—',   c: C.text },
    { l: 'Opportunities',    v: ana.opportunity_count   ?? '—',   c: C.green },
    { l: 'Top Signal Score', v: ana.top_signal_score    != null ? ana.top_signal_score.toFixed(3) : '—', c: C.amber },
    { l: 'Top Edge',         v: ana.top_edge            != null ? `${(ana.top_edge * 100).toFixed(2)}%` : '—', c: C.green },
    { l: 'Avg Freshness',    v: fmtMs(ana.avg_freshness_ms),      c: C.blue },
    { l: 'Avg Volatility',   v: ana.avg_volatility      != null ? ana.avg_volatility.toFixed(4) : '—', c: C.dim2 },
  ];

  const deCells = [
    { l: 'Actionable',     v: de.actionable_count ?? '—', c: C.green },
    { l: 'Open',           v: de.open_count       ?? '—', c: C.blue  },
    { l: 'Close',          v: de.close_count      ?? '—', c: C.red   },
    { l: 'Reduce',         v: de.reduce_count     ?? '—', c: C.amber },
    { l: 'Rejected',       v: de.reject_count     ?? '—', c: C.dim2  },
    { l: 'Slots Left',     v: de.slots_remaining  ?? '—', c: C.text  },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={kpis} />
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: '1fr 300px', overflow: 'hidden' }}>

        {/* Left panel */}
        <div style={{ overflow: 'auto', borderRight: `1px solid ${C.border}` }}>
          <div style={{ padding: '12px 14px', borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>Analytics Engine Pulse</SectionLabel>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6,1fr)', gap: 1, background: C.border }}>
              {mlCells.map((x, i) => (
                <div key={i} style={{ background: C.panel2, padding: '8px 10px' }}>
                  <div style={S.label}>{x.l}</div>
                  <div style={{ fontSize: 17, fontWeight: 700, color: x.c, marginTop: 4 }}>{x.v}</div>
                </div>
              ))}
            </div>
          </div>
          <div style={{ padding: '12px 14px' }}>
            <SectionLabel>Decision Engine — Current Cycle</SectionLabel>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6,1fr)', gap: 1, background: C.border }}>
              {deCells.map((x, i) => (
                <div key={i} style={{ background: C.panel2, padding: '10px 12px' }}>
                  <div style={S.label}>{x.l}</div>
                  <div style={{ fontSize: 26, fontWeight: 700, color: x.c, marginTop: 4 }}>{x.v}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Right: recent trades stream */}
        <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            <Dot status={connectionState === 'connected' ? 'live' : 'warn'} />
            <span style={S.label}>Recent Trades</span>
          </div>
          {trades.length === 0 ? (
            <div style={{ padding: 20, color: C.dim2, fontSize: 11 }}>{snapshot ? 'No trades yet.' : 'Waiting for data…'}</div>
          ) : (
            <div style={{ flex: 1, overflow: 'auto' }}>
              {trades.map((t, idx) => (
                <div key={t.id || idx} style={{
                  padding: '5px 10px', borderBottom: `1px solid ${C.border}`,
                  background: idx === 0 ? 'rgba(232,160,32,0.04)' : 'transparent',
                  display: 'grid', gridTemplateColumns: '52px 32px 1fr 54px 60px',
                  gap: 6, alignItems: 'center', fontSize: 11,
                }}>
                  <span style={{ color: C.dim2, fontSize: 10 }}>{t.timestamp ? new Date(t.timestamp).toLocaleTimeString('en-GB') : '—'}</span>
                  <span style={{ color: sideColor(t.side), fontWeight: 700, fontSize: 10 }}>{t.side}</span>
                  <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.market_title}</span>
                  <span style={{ color: C.dim2, textAlign: 'right', fontFamily: 'monospace' }}>{fmtNum(t.price, 3)}</span>
                  <span style={{ color: pnlColor(t.pnl_abs), textAlign: 'right', fontWeight: 600 }}>{fmtPnl(t.pnl_abs)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

// ─── MARKET SCANNER ───────────────────────────────────────────────────────────
const MarketScanner = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [view, setView]     = useStateT('opps');
  const [search, setSearch] = useStateT('');
  const opportunities = snapshot?.analytics?.opportunities || [];
  const leaderboard   = snapshot?.analytics?.leaderboard   || [];
  const anaSum        = snapshot?.analytics?.summary       || {};

  const rows = view === 'opps' ? opportunities : leaderboard;
  const filtered = useMemoT(() =>
    search ? rows.filter(r => r.title?.toLowerCase().includes(search.toLowerCase())) : rows,
    [rows, search]
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Tracked Markets',  value: anaSum.tracked_markets    ?? '—', color: C.text },
        { label: 'Opportunities',    value: anaSum.opportunity_count  ?? '—', color: C.green },
        { label: 'Top Edge',         value: anaSum.top_edge != null ? `${(anaSum.top_edge * 100).toFixed(2)}%` : '—', color: C.green },
        { label: 'Top Signal',       value: anaSum.top_signal_score != null ? anaSum.top_signal_score.toFixed(3) : '—', color: C.amber },
        { label: 'Avg Freshness',    value: fmtMs(anaSum.avg_freshness_ms), color: C.blue },
        { label: 'Avg Volatility',   value: anaSum.avg_volatility != null ? anaSum.avg_volatility.toFixed(4) : '—', color: C.dim2 },
      ]} />

      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
        {[['opps', 'Opportunities'], ['lb', 'Leaderboard']].map(([v, label]) => (
          <button key={v} onClick={() => setView(v)} style={{
            background: view === v ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${view === v ? C.amber : C.border2}`,
            color: view === v ? C.amber : C.dim2,
            padding: '3px 12px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter by title…"
          style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '4px 10px', fontSize: 11, flex: 1, maxWidth: 280, outline: 'none' }} />
        <span style={{ fontSize: 10, color: C.dim2, marginLeft: 'auto' }}>{filtered.length} markets</span>
      </div>

      <div style={{ flex: 1, overflow: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
          <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
            <tr>{['Market','Dir','Mid','Spread','Edge','Threshold','Signal','Z-Score','Regime','Decision','Obs.','Detected'].map(h => <TH key={h}>{h}</TH>)}</tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr><td colSpan={12} style={{ padding: '24px', color: C.dim2, textAlign: 'center', fontSize: 11 }}>{snapshot ? 'No markets available.' : 'Waiting for data…'}</td></tr>
            )}
            {filtered.map((m, i) => (
              <tr key={m.market_id || i}>
                <TD style={{ maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{m.title}</div></TD>
                <TD><Badge type={m.direction === 'YES' ? 'green' : m.direction === 'NO' ? 'red' : 'default'}>{m.direction || '—'}</Badge></TD>
                <TD style={{ color: C.text, fontFamily: 'monospace' }}>{fmtNum(m.mid_price, 3)}</TD>
                <TD style={{ color: m.spread > 0.04 ? C.red : C.green, fontFamily: 'monospace' }}>{fmtNum(m.spread, 4)}</TD>
                <TD style={{ color: m.expected_edge > 0 ? C.green : C.dim2, fontWeight: 600 }}>{m.expected_edge != null ? `${(m.expected_edge * 100).toFixed(2)}%` : '—'}</TD>
                <TD style={{ color: C.dim2 }}>{m.entry_threshold != null ? `${(m.entry_threshold * 100).toFixed(2)}%` : '—'}</TD>
                <TD><ScoreBar value={m.signal_strength || 0} /></TD>
                <TD style={{ color: m.z_score != null && Math.abs(m.z_score) > 1.5 ? C.amber : C.dim2, fontFamily: 'monospace' }}>{fmtNum(m.z_score, 2)}</TD>
                <TD><Badge type="default">{m.regime || '—'}</Badge></TD>
                <TD><Badge type={actionType(m.decision_action)}>{m.decision_action || 'hold'}</Badge></TD>
                <TD style={{ color: C.dim2, textAlign: 'right' }}>{m.observations ?? '—'}</TD>
                <TD><Dot status={m.detected ? 'ok' : 'off'} /></TD>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// ─── LIVE PORTFOLIO ───────────────────────────────────────────────────────────
const LivePortfolio = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [view, setView] = useStateT('open');
  const positions = snapshot?.positions || {};
  const openItems = positions.items     || [];
  const trades    = snapshot?.recent_trades || [];
  const stats     = snapshot?.stats     || {};
  const openPnl   = openItems.reduce((a, p) => a + (p.unrealized_pnl_abs || 0), 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Open Positions',   value: positions.open_count ?? openItems.length, color: C.text },
        { label: 'Unrealized PnL',   value: fmtPnl(openPnl),              color: pnlColor(openPnl) },
        { label: 'Capital in Trade', value: positions.capital_in_trade != null ? `$${positions.capital_in_trade.toFixed(0)}` : '—', color: C.amber },
        { label: 'Exposure %',       value: positions.exposure_pct != null ? `${(positions.exposure_pct * 100).toFixed(1)}%` : '—', color: C.text },
        { label: 'Net PnL',          value: fmtPnl(stats.total_pnl),      color: pnlColor(stats.total_pnl) },
        { label: 'Win Rate',         value: stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—', color: C.amber },
      ]} />

      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 8, flexShrink: 0 }}>
        {[['open', 'Open Positions'], ['history', 'Trade History']].map(([v, label]) => (
          <button key={v} onClick={() => setView(v)} style={{
            background: view === v ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${view === v ? C.amber : C.border2}`,
            color: view === v ? C.amber : C.dim2,
            padding: '3px 12px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        <span style={{ marginLeft: 'auto', fontSize: 10, color: C.dim2 }}>{view === 'open' ? openItems.length : trades.length} records</span>
      </div>

      <div style={{ flex: 1, overflow: 'auto' }}>
        {view === 'open' ? (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
              <tr>{['Market','Side','Entry','Size','Notional','Unreal. PnL','Unreal. %','Decision','Summary'].map(h => <TH key={h}>{h}</TH>)}</tr>
            </thead>
            <tbody>
              {openItems.length === 0 && <tr><td colSpan={9} style={{ padding: '24px', color: C.dim2, textAlign: 'center' }}>{snapshot ? 'No open positions.' : 'Waiting for data…'}</td></tr>}
              {openItems.map((p, i) => (
                <tr key={p.trade_id || i}>
                  <TD style={{ maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{p.market_title}</div></TD>
                  <TD><Badge type={p.side === 'YES' ? 'green' : 'red'} size="xs">{p.side}</Badge></TD>
                  <TD style={{ color: C.dim2, fontFamily: 'monospace' }}>{fmtNum(p.entry_price, 3)}</TD>
                  <TD style={{ color: C.text }}>{fmtNum(p.size, 0)}</TD>
                  <TD style={{ color: C.text }}>${fmtNum(p.notional, 2)}</TD>
                  <TD style={{ color: pnlColor(p.unrealized_pnl_abs), fontWeight: 600 }}>{fmtPnl(p.unrealized_pnl_abs)}</TD>
                  <TD style={{ color: pnlColor(p.unrealized_pnl_pct) }}>{p.unrealized_pnl_pct != null ? `${p.unrealized_pnl_pct >= 0 ? '+' : ''}${(p.unrealized_pnl_pct * 100).toFixed(2)}%` : '—'}</TD>
                  <TD><Badge type={actionType(p.decision_action)}>{p.decision_action || '—'}</Badge></TD>
                  <TD style={{ color: C.dim2, fontSize: 10, maxWidth: 200 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.decision_summary || '—'}</div></TD>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
              <tr>{['Timestamp','Market','Side','Price','Notional','Fees','PnL','PnL %','Mode','Status'].map(h => <TH key={h}>{h}</TH>)}</tr>
            </thead>
            <tbody>
              {trades.length === 0 && <tr><td colSpan={10} style={{ padding: '24px', color: C.dim2, textAlign: 'center' }}>{snapshot ? 'No trades yet.' : 'Waiting for data…'}</td></tr>}
              {trades.map((t, i) => (
                <tr key={t.id || i}>
                  <TD style={{ color: C.dim2, fontSize: 10, whiteSpace: 'nowrap' }}>{t.timestamp ? new Date(t.timestamp).toLocaleString('en-GB', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</TD>
                  <TD style={{ maxWidth: 180 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{t.market_title}</div></TD>
                  <TD><Badge type={t.side === 'BUY' ? 'green' : 'red'} size="xs">{t.side}</Badge></TD>
                  <TD style={{ color: C.dim2, fontFamily: 'monospace' }}>{fmtNum(t.price, 3)}</TD>
                  <TD style={{ color: C.text }}>${fmtNum(t.notional, 2)}</TD>
                  <TD style={{ color: C.dim2 }}>${fmtNum(t.fees, 2)}</TD>
                  <TD style={{ color: pnlColor(t.pnl_abs), fontWeight: 600 }}>{fmtPnl(t.pnl_abs)}</TD>
                  <TD style={{ color: pnlColor(t.pnl_pct) }}>{t.pnl_pct != null ? `${t.pnl_pct >= 0 ? '+' : ''}${(t.pnl_pct * 100).toFixed(2)}%` : '—'}</TD>
                  <TD><Badge type={t.execution_mode === 'live' ? 'green' : 'default'} size="xs">{t.execution_mode || '—'}</Badge></TD>
                  <TD><Badge type={t.status === 'closed' ? 'green' : t.status === 'open' ? 'blue' : 'default'} size="xs">{t.status || '—'}</Badge></TD>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

// ─── DECISION ENGINE ──────────────────────────────────────────────────────────
const DecisionEngine = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [filter, setFilter]   = useStateT('ALL');
  const [expanded, setExpanded] = useStateT(new Set());
  const de      = snapshot?.decision_engine || {};
  const summary = de.summary || {};
  const ranked  = de.ranked  || [];

  const filtered = useMemoT(() => {
    if (filter === 'ALL')  return ranked;
    if (filter === 'exec') return ranked.filter(d => d.executable);
    return ranked.filter(d => d.action?.toLowerCase() === filter.toLowerCase());
  }, [ranked, filter]);

  const toggle = id => setExpanded(prev => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Actionable',       value: summary.actionable_count ?? '—', color: C.green },
        { label: 'Open Signals',     value: summary.open_count       ?? '—', color: C.blue  },
        { label: 'Close Signals',    value: summary.close_count      ?? '—', color: C.red   },
        { label: 'Reduce',           value: summary.reduce_count     ?? '—', color: C.amber },
        { label: 'Rejected',         value: summary.reject_count     ?? '—', color: C.dim2  },
        { label: 'Slots Remaining',  value: summary.slots_remaining  ?? '—', color: C.text  },
        { label: 'Exposure Left',    value: summary.exposure_remaining != null ? `${(summary.exposure_remaining * 100).toFixed(1)}%` : '—', color: C.text },
      ]} />

      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 6, flexShrink: 0 }}>
        {['ALL', 'open', 'close', 'reduce', 'skip', 'exec'].map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{
            background: filter === f ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${filter === f ? C.amber : C.border2}`,
            color: filter === f ? C.amber : C.dim2,
            padding: '2px 10px', fontSize: 10, cursor: 'pointer', textTransform: 'uppercase',
          }}>{f}</button>
        ))}
        <span style={{ marginLeft: 'auto', fontSize: 10, color: C.dim2 }}>{filtered.length} decisions</span>
      </div>

      <div style={{ flex: 1, overflow: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
          <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
            <tr>{['Market', 'Action', 'Side', 'Confidence', 'Executable', 'Cooldown', 'Summary', ''].map(h => <TH key={h}>{h}</TH>)}</tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr><td colSpan={8} style={{ padding: '24px', color: C.dim2, textAlign: 'center' }}>{snapshot ? 'No decisions in this cycle.' : 'Waiting for data…'}</td></tr>
            )}
            {filtered.map((d, i) => {
              const key = d.market_id || i;
              const isOpen = expanded.has(key);
              return (
                <React.Fragment key={key}>
                  <tr onClick={() => toggle(key)} style={{ cursor: 'pointer', background: isOpen ? 'rgba(232,160,32,0.03)' : 'transparent' }}
                    onMouseEnter={e => { if (!isOpen) e.currentTarget.style.background = 'rgba(255,255,255,0.02)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = isOpen ? 'rgba(232,160,32,0.03)' : 'transparent'; }}>
                    <TD style={{ maxWidth: 200 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{d.title}</div></TD>
                    <TD><Badge type={actionType(d.action)}>{d.action || '—'}</Badge></TD>
                    <TD><Badge type={d.side === 'YES' ? 'green' : d.side === 'NO' ? 'red' : 'default'} size="xs">{d.side || '—'}</Badge></TD>
                    <TD><ScoreBar value={d.confidence || 0} /></TD>
                    <TD><Badge type={d.executable ? 'green' : 'default'} size="xs">{d.executable ? 'YES' : 'NO'}</Badge></TD>
                    <TD style={{ color: C.dim2 }}>{d.cooldown_remaining_ms > 0 ? fmtMs(d.cooldown_remaining_ms) : '—'}</TD>
                    <TD style={{ color: C.dim2, fontSize: 10, maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.summary}</div></TD>
                    <TD style={{ color: C.dim2 }}>{isOpen ? '▲' : '▼'}</TD>
                  </tr>
                  {isOpen && (
                    <tr style={{ background: 'rgba(232,160,32,0.02)', borderBottom: `1px solid ${C.border}` }}>
                      <td colSpan={8} style={{ padding: '10px 14px 14px' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, fontSize: 11 }}>
                          <div>
                            <div style={{ ...S.label, marginBottom: 6 }}>Reasons</div>
                            {(d.reasons || []).map((r, ri) => <div key={ri} style={{ color: C.green, marginBottom: 3 }}>+ {r}</div>)}
                            {(!d.reasons || d.reasons.length === 0) && <div style={{ color: C.dim2 }}>No reasons provided.</div>}
                          </div>
                          <div>
                            <div style={{ ...S.label, marginBottom: 6 }}>Rejections</div>
                            {(d.rejections || []).map((r, ri) => <div key={ri} style={{ color: C.red, marginBottom: 3 }}>✗ {r}</div>)}
                            {(!d.rejections || d.rejections.length === 0) && <div style={{ color: C.dim2 }}>No rejections.</div>}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// ─── RISK & CONFIG ────────────────────────────────────────────────────────────
const RiskConfig = () => {
  const { snapshot, connectionState } = useLiveStore();
  const bot   = snapshot?.bot         || {};
  const rcfg  = snapshot?.risk_config || {};
  const stats = snapshot?.stats       || {};

  const [edits, setEdits]           = useStateT({});
  const [saving, setSaving]         = useStateT(false);
  const [saveMsg, setSaveMsg]       = useStateT('');
  const [killConfirm, setKillConfirm] = useStateT(false);
  const [cmdBusy, setCmdBusy]       = useStateT(false);

  const merged  = { ...rcfg, ...edits };
  const isDirty = Object.keys(edits).length > 0;
  const isReadOnly = rcfg.config_mutable === false || bot.config_mutable === false;
  const controlsAvailable = bot.control_available === true;

  const numField = (key, label, step = 0.01) => (
    <div key={key} style={{ background: C.panel2, padding: '8px 10px' }}>
      <div style={S.label}>{label}</div>
      <input
        type="number" step={step}
        value={merged[key] ?? ''}
        disabled={isReadOnly}
        onChange={e => setEdits(p => ({ ...p, [key]: parseFloat(e.target.value) }))}
        style={{
          background: 'transparent', border: 'none',
          borderBottom: `1px solid ${edits[key] != null ? C.amber : C.border2}`,
          color: isReadOnly ? C.dim2 : C.text, width: '100%', marginTop: 4,
          padding: '2px 0', fontSize: 14, fontWeight: 700, outline: 'none',
        }}
      />
    </div>
  );

  const saveConfig = async () => {
    if (!isDirty) return;
    setSaving(true); setSaveMsg('');
    try {
      await window.PoybotAPI.updateConfig(edits);
      setEdits({}); setSaveMsg('✓ Saved');
    } catch (e) { setSaveMsg('✗ ' + e.message); }
    setSaving(false);
    setTimeout(() => setSaveMsg(''), 3000);
  };

  const sendCmd = async cmd => {
    setCmdBusy(true); setKillConfirm(false);
    try { await window.PoybotAPI.botControl(cmd); }
    catch (e) { console.warn('[Poybot] botControl:', e.message); }
    setCmdBusy(false);
  };

  const isRunning = bot.status === 'running';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Bot Status',    value: (bot.status || '—').toUpperCase(), color: isRunning ? C.green : C.red },
        { label: 'Uptime',        value: fmtAge(bot.uptime_seconds),        color: C.text },
        { label: 'Latency',       value: fmtMs(bot.latency_ms),             color: C.blue },
        { label: 'Cycle Latency', value: fmtMs(bot.cycle_latency_ms),       color: C.blue },
        { label: 'Execution',     value: bot.execution_enabled ? 'ENABLED' : 'DRY RUN', color: bot.execution_enabled ? C.green : C.amber },
        { label: 'Net PnL',       value: fmtPnl(stats.total_pnl),          color: pnlColor(stats.total_pnl) },
      ]} />

      <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 200px', gap: 14, alignItems: 'start' }}>

          {/* Config editor */}
          <div>
            <SectionLabel>Risk Configuration {isDirty && <span style={{ color: C.amber, marginLeft: 8 }}>● unsaved changes</span>}</SectionLabel>
            {isReadOnly && (
              <div style={{ fontSize: 10, color: C.dim2, marginBottom: 10 }}>
                Display-only mapping from live backend safeguards. Commands stay disabled in this build.
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border, marginBottom: 10 }}>
              {numField('risk_per_trade_pct',      'Risk / Trade %',    0.001)}
              {numField('max_total_exposure_pct',  'Max Exposure %',    0.01)}
              {numField('kelly_fraction',          'Kelly Fraction',    0.01)}
              {numField('max_drawdown_stop_pct',   'Max Drawdown %',    0.01)}
              {numField('base_entry_threshold',    'Entry Threshold',   0.001)}
              {numField('spread_cap',              'Spread Cap',        0.005)}
              {numField('fee_bps',                 'Fee (bps)',         0.5)}
              {numField('min_signal_strength',     'Min Signal',        0.1)}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border }}>
              {numField('max_concurrent_positions','Max Positions',     1)}
              {numField('max_positions_per_tick',  'Per Tick',          1)}
              {numField('cooldown_seconds',        'Cooldown (s)',      1)}
              {numField('max_holding_seconds',     'Max Hold (s)',      10)}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
              <button onClick={saveConfig} disabled={isReadOnly || !isDirty || saving} style={{
                background: !isReadOnly && isDirty ? 'rgba(40,168,78,0.1)' : 'transparent',
                border: `1px solid ${!isReadOnly && isDirty ? C.green : C.border2}`,
                color: !isReadOnly && isDirty ? C.green : C.dim2,
                padding: '5px 16px', cursor: !isReadOnly && isDirty ? 'pointer' : 'default', fontSize: 11, fontWeight: 700,
              }}>{saving ? 'SAVING…' : 'SAVE CONFIG'}</button>
              {isDirty && !isReadOnly && (
                <button onClick={() => setEdits({})} style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, padding: '5px 12px', cursor: 'pointer', fontSize: 11 }}>DISCARD</button>
              )}
              {saveMsg && <span style={{ fontSize: 11, color: saveMsg.startsWith('✓') ? C.green : C.red }}>{saveMsg}</span>}
            </div>
          </div>

          {/* Bot control */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ padding: 14, border: `1px solid ${C.border}` }}>
              <div style={{ ...S.label, marginBottom: 10 }}>Bot Control</div>
              {[
                { cmd: 'start', label: '▶ START', active: controlsAvailable && !isRunning, type: 'green' },
                { cmd: 'stop',  label: '■ STOP',  active: controlsAvailable && isRunning,  type: 'red'   },
                { cmd: 'pause', label: '⏸ PAUSE', active: controlsAvailable,                type: 'default' },
              ].map(({ cmd, label, active, type }) => (
                <button key={cmd} onClick={() => sendCmd(cmd)} disabled={cmdBusy || !active} style={{
                  display: 'block', width: '100%', marginBottom: 6,
                  background: active ? `rgba(${type === 'green' ? '40,168,78' : type === 'red' ? '201,53,69' : '255,255,255'},0.1)` : 'transparent',
                  border: `1px solid ${active ? C[type === 'green' ? 'green' : type === 'red' ? 'red' : 'border2'] : C.border}`,
                  color: active ? C[type === 'green' ? 'green' : type === 'red' ? 'red' : 'dim2'] : C.dim,
                  padding: '7px', cursor: active && !cmdBusy ? 'pointer' : 'default', fontSize: 11, fontWeight: 700,
                }}>{label}</button>
              ))}
              {!controlsAvailable && (
                <div style={{ fontSize: 10, color: C.dim2, marginTop: 6 }}>
                  Control actions are not exposed by this backend build.
                </div>
              )}
            </div>

            <div style={{ padding: 14, border: `1px solid ${C.red}`, background: 'rgba(201,53,69,0.03)' }}>
              <div style={{ ...S.label, color: C.red, marginBottom: 8 }}>Emergency Kill</div>
              <div style={{ fontSize: 10, color: C.dim2, marginBottom: 10, lineHeight: 1.7 }}>Halts all bot activity immediately.</div>
              {!killConfirm
                ? <button onClick={() => setKillConfirm(true)} disabled={cmdBusy || !controlsAvailable} style={{ width: '100%', background: 'rgba(201,53,69,0.1)', border: `1px solid ${C.red}`, color: controlsAvailable ? C.red : C.dim2, padding: '7px 0', cursor: controlsAvailable ? 'pointer' : 'default', fontSize: 11, fontWeight: 700 }}>KILL SWITCH</button>
                : <div>
                    <div style={{ fontSize: 11, color: C.red, marginBottom: 8, fontWeight: 700 }}>CONFIRM HALT?</div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button onClick={() => sendCmd('halt')} style={{ flex: 1, background: C.red, border: 'none', color: '#fff', padding: '6px 0', cursor: 'pointer', fontSize: 11, fontWeight: 700 }}>CONFIRM</button>
                      <button onClick={() => setKillConfirm(false)} style={{ flex: 1, background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, padding: '6px 0', cursor: 'pointer', fontSize: 11 }}>CANCEL</button>
                    </div>
                  </div>
              }
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// ─── BOT HEALTH ───────────────────────────────────────────────────────────────
const BotHealth = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [logFilter, setLogFilter] = useStateT('ALL');
  const ingestion = snapshot?.ingestion || {};
  const bot       = snapshot?.bot       || {};
  const logs      = snapshot?.logs      || [];
  const sources   = ingestion.sources   || [];
  const mkts      = ingestion.markets   || [];

  const logColor = lvl => ({ ERROR: C.red, WARNING: C.amber, INFO: C.text, DEBUG: C.dim2 })[lvl] || C.dim2;
  const filteredLogs = logFilter === 'ALL' ? logs : logs.filter(l => l.level === logFilter);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Total Markets',  value: ingestion.total_markets       ?? '—', color: C.text },
        { label: 'Live Markets',   value: ingestion.live_markets        ?? '—', color: C.green },
        { label: 'Stale Markets',  value: ingestion.stale_market_count  ?? '—', color: (ingestion.stale_market_count || 0) > 0 ? C.amber : C.dim2 },
        { label: 'Updates / min',  value: ingestion.updates_last_minute ?? '—', color: C.blue },
        { label: 'Avg Freshness',  value: fmtMs(ingestion.avg_freshness_ms),    color: C.text },
        { label: 'Bot Uptime',     value: fmtAge(bot.uptime_seconds),           color: C.text },
        { label: 'Cycle Latency',  value: fmtMs(bot.cycle_latency_ms),          color: C.blue },
      ]} />

      <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>

        {/* Sources */}
        <div>
          <SectionLabel>Ingestion Sources</SectionLabel>
          {sources.length === 0
            ? <div style={{ color: C.dim2, fontSize: 11 }}>{snapshot ? 'No sources reported.' : 'Waiting for data…'}</div>
            : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(210px,1fr))', gap: 8 }}>
                {sources.map((s, i) => {
                  const ok = s.status === 'ok' || s.status === 'healthy';
                  return (
                    <div key={i} style={{ border: `1px solid ${ok ? C.border : C.amber}`, padding: '10px 12px', background: ok ? 'transparent' : 'rgba(232,160,32,0.04)' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
                        <Dot status={ok ? 'ok' : 'warn'} />
                        <span style={{ color: C.text, fontWeight: 700, fontSize: 11 }}>{s.name}</span>
                        <Badge type={ok ? 'green' : 'amber'} size="xs">{s.status}</Badge>
                      </div>
                      <div style={{ fontSize: 10, color: C.dim2 }}>Lag: <span style={{ color: C.text }}>{fmtMs(s.lag_ms)}</span></div>
                      <div style={{ fontSize: 10, color: C.dim2 }}>Msgs/min: <span style={{ color: C.text }}>{s.messages_last_minute ?? '—'}</span></div>
                      {s.note && <div style={{ fontSize: 10, color: C.amber, marginTop: 4 }}>{s.note}</div>}
                    </div>
                  );
                })}
              </div>
            )
          }
        </div>

        {/* Market ingestion table */}
        {mkts.length > 0 && (
          <div>
            <SectionLabel>Market Ingestion Health ({mkts.length})</SectionLabel>
            <div style={{ maxHeight: 180, overflow: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
                  <tr>{['Market', 'Source', 'Freshness', 'Delay', 'Obs.', 'Msgs/min', 'Status'].map(h => <TH key={h}>{h}</TH>)}</tr>
                </thead>
                <tbody>
                  {mkts.map((m, i) => {
                    const stale = (m.freshness_ms || 0) > 15000;
                    return (
                      <tr key={i}>
                        <TD style={{ maxWidth: 180 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{m.title}</div></TD>
                        <TD style={{ color: C.dim2 }}>{m.quote_source}</TD>
                        <TD style={{ color: stale ? C.amber : C.green }}>{fmtMs(m.freshness_ms)}</TD>
                        <TD style={{ color: C.dim2 }}>{fmtMs(m.source_delay_ms)}</TD>
                        <TD style={{ color: C.text, textAlign: 'right' }}>{m.observations ?? '—'}</TD>
                        <TD style={{ color: C.text, textAlign: 'right' }}>{m.messages_last_minute ?? '—'}</TD>
                        <TD><Badge type={stale ? 'amber' : 'green'} size="xs">{stale ? 'STALE' : 'LIVE'}</Badge></TD>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Bot timing */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border }}>
          {[
            { l: 'Bot Status',     v: (bot.status || '—').toUpperCase(), c: bot.status === 'running' ? C.green : C.red },
            { l: 'Started At',     v: bot.started_at ? new Date(bot.started_at).toLocaleString('en-GB', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—', c: C.dim2 },
            { l: 'Run Accumulated',v: fmtAge(bot.accumulated_run_seconds), c: C.text },
            { l: 'Last Command',   v: bot.last_command_at ? new Date(bot.last_command_at).toLocaleTimeString('en-GB') : '—', c: C.dim2 },
          ].map((x, i) => (
            <div key={i} style={{ background: C.panel2, padding: '8px 10px' }}>
              <div style={S.label}>{x.l}</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: x.c, marginTop: 4 }}>{x.v}</div>
            </div>
          ))}
        </div>

        {/* Logs */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 180 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
            <SectionLabel mb={0}>System Logs</SectionLabel>
            <div style={{ display: 'flex', gap: 4, marginLeft: 12 }}>
              {['ALL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'].map(l => (
                <button key={l} onClick={() => setLogFilter(l)} style={{
                  background: logFilter === l ? 'rgba(232,160,32,0.1)' : 'transparent',
                  border: `1px solid ${logFilter === l ? C.amber : C.border2}`,
                  color: logFilter === l ? C.amber : C.dim2,
                  padding: '1px 8px', fontSize: 10, cursor: 'pointer',
                }}>{l}</button>
              ))}
            </div>
            <span style={{ marginLeft: 'auto', fontSize: 10, color: C.dim2 }}>{filteredLogs.length} entries</span>
          </div>
          <div style={{ flex: 1, overflow: 'auto', border: `1px solid ${C.border}`, minHeight: 0 }}>
            {filteredLogs.length === 0
              ? <div style={{ padding: '20px', color: C.dim2, fontSize: 11 }}>{snapshot ? 'No log entries.' : 'Waiting for data…'}</div>
              : filteredLogs.map((l, i) => (
                  <div key={i} style={{ padding: '4px 10px', borderBottom: `1px solid ${C.border}`, display: 'grid', gridTemplateColumns: '54px 56px 72px 1fr', gap: 8, fontSize: 10 }}>
                    <span style={{ color: C.dim2 }}>{l.timestamp ? new Date(l.timestamp).toLocaleTimeString('en-GB') : '—'}</span>
                    <span style={{ color: logColor(l.level), fontWeight: 700 }}>{l.level}</span>
                    <span style={{ color: C.blue }}>{l.category}</span>
                    <span style={{ color: C.text }}>{l.message}</span>
                  </div>
                ))
            }
          </div>
        </div>
      </div>
    </div>
  );
};

Object.assign(window, { AlphaTerminal, MarketScanner, LivePortfolio, DecisionEngine, RiskConfig, BotHealth });
