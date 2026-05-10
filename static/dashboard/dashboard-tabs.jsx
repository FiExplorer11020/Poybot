// dashboard-tabs.jsx — 6 tab views wired to Poybot LiveSnapshot

const { useState: useStateT, useEffect: useEffectT, useMemo: useMemoT } = React;
const {
  C, S, useLiveStore, ConnBanner,
  Badge, MiniBar, ScoreBar, Dot, KpiStrip, TH, TD, SectionLabel, Sparkline, ProgressBar,
  short, fmtAge, fmtPnl, fmtPct, fmtMs, fmtNum,
  pnlColor, sideColor, actionType,
} = window;

// ─── ALPHA TERMINAL ───────────────────────────────────────────────────────────
const AlphaTerminal = () => {
  const { snapshot, connectionState } = useLiveStore();
  const stats   = snapshot?.stats                    || {};
  const ana     = snapshot?.analytics?.summary       || {};
  const de      = snapshot?.decision_engine?.summary || {};
  const trades  = snapshot?.recent_trades            || [];
  const extras  = snapshot?.alpha_extras             || {};
  const timeline = extras.timeline || [];
  const followReady = extras.follow_ready || [];
  const totals  = extras.totals || {};

  // Sparkline data (from 24h timeline buckets)
  const tradesSpark    = timeline.map(b => b.trades || 0);
  const leaderSpark    = timeline.map(b => b.leader_trades || 0);
  const positionsSpark = timeline.map(b => b.positions_resolved || 0);
  const edgesSpark     = timeline.map(b => b.edges_active || 0);

  // 24h cumulative (latest bucket sums)
  const trades24h    = tradesSpark.reduce((a, b) => a + b, 0);
  const leader24h    = leaderSpark.reduce((a, b) => a + b, 0);
  const positions24h = positionsSpark.reduce((a, b) => a + b, 0);

  // Hero KPIs with sparklines
  const kpis = [
    {
      label: 'Net PnL',
      value: fmtPnl(stats.total_pnl),
      color: pnlColor(stats.total_pnl),
      spark: <Sparkline data={timeline.map(_ => stats.total_pnl || 0)} color={pnlColor(stats.total_pnl)} />,
    },
    {
      label: 'Portfolio',
      value: stats.portfolio_total != null ? `$${stats.portfolio_total.toFixed(0)}` : '—',
      color: C.white,
      sub: stats.pnl_percent != null ? `${stats.pnl_percent >= 0 ? '+' : ''}${(stats.pnl_percent * 100).toFixed(2)}%` : '',
    },
    {
      label: 'Open Positions',
      value: stats.open_positions ?? '—',
      color: C.text,
      sub: stats.capital_in_trade != null ? `$${stats.capital_in_trade.toFixed(0)} in trade` : '',
    },
    {
      label: 'Win Rate',
      value: stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—',
      color: C.amber,
    },
    {
      label: 'Trades 24h',
      value: trades24h ? trades24h.toLocaleString() : '0',
      color: C.blue,
      spark: <Sparkline data={tradesSpark} color={C.blue} />,
    },
    {
      label: 'Leader Trades 24h',
      value: leader24h ? leader24h.toLocaleString() : '0',
      color: C.amber,
      spark: <Sparkline data={leaderSpark} color={C.amber} />,
    },
    {
      label: 'Active Markets',
      value: stats.active_markets ?? '—',
      color: C.green,
    },
    {
      label: 'Slots Left',
      value: de.slots_remaining ?? '—',
      color: C.text,
      sub: `${de.reject_count ?? 0} skipped`,
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={kpis} />

      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'minmax(0, 2fr) minmax(280px, 1fr)', overflow: 'hidden' }}>

        {/* Left — analytical panels */}
        <div style={{ overflow: 'auto', borderRight: `1px solid ${C.border}` }}>

          {/* Learning Trajectory */}
          <div style={{ padding: '12px 14px', borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>Learning Trajectory · 24h</SectionLabel>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 1, background: C.border }}>
              <TrajectoryCard label="Trades observed"     total={totals.trades_total ?? trades24h} delta={trades24h} spark={tradesSpark} color={C.blue} />
              <TrajectoryCard label="Positions resolved"  total={totals.positions_resolved_total ?? 0} delta={positions24h} spark={positionsSpark} color={C.green} />
              <TrajectoryCard label="Follower edges"      total={totals.edges_total ?? 0} subtotal={totals.edges_confirmed ?? 0} subtotalLabel="confirmed" spark={edgesSpark} color={C.amber} />
              <TrajectoryCard label="Avg profile maturity" total={(totals.avg_maturity ?? 0).toFixed(3)} subtotal={totals.profiles_total ?? 0} subtotalLabel="profiles" color={C.purple} />
            </div>
          </div>

          {/* ML Pipeline phases */}
          <div style={{ padding: '12px 14px', borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>ML Training Pipeline</SectionLabel>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
              <PhaseCard phase={1} count={totals.phase1 ?? 0} target={100} label="Beta-Binomial" desc="0–99 resolved" color={C.blue} />
              <PhaseCard phase={2} count={totals.phase2 ?? 0} target={500} label="Bayesian LogReg" desc="100–499 resolved" color={C.amber} />
              <PhaseCard phase={3} count={totals.phase3 ?? 0} target={null} label="LightGBM + Platt" desc="500+ resolved" color={C.green} />
            </div>
          </div>

          {/* Next Signal ETA */}
          <div style={{ padding: '12px 14px', borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>Closest to FOLLOW Readiness</SectionLabel>
            {followReady.length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11, padding: '10px 0' }}>
                {snapshot ? 'No leaders profiled yet.' : 'Waiting for data…'}
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 1, background: C.border }}>
                <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr 1fr 1fr 80px 70px', gap: 8, padding: '6px 10px', background: C.panel2, ...S.label }}>
                  <span>Wallet</span>
                  <span>Trades</span>
                  <span>Resolved</span>
                  <span>Followers</span>
                  <span style={{ textAlign: 'right' }}>Phase</span>
                  <span style={{ textAlign: 'right' }}>ETA</span>
                </div>
                {followReady.map((r, i) => (
                  <FollowReadyRow key={i} row={r} />
                ))}
              </div>
            )}
          </div>

          {/* Analytics + Decision compacted */}
          <div style={{ padding: '12px 14px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <SectionLabel>Analytics Pulse</SectionLabel>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 1, background: C.border }}>
                <SmallCell l="Tracked Markets" v={ana.tracked_markets ?? '—'} c={C.text} />
                <SmallCell l="Opportunities" v={ana.opportunity_count ?? '—'} c={C.green} />
                <SmallCell l="Top Signal" v={ana.top_signal_score != null ? ana.top_signal_score.toFixed(3) : '—'} c={C.amber} />
                <SmallCell l="Top Edge" v={ana.top_edge != null ? `${(ana.top_edge * 100).toFixed(2)}%` : '—'} c={C.green} />
                <SmallCell l="Avg Freshness" v={fmtMs(ana.avg_freshness_ms)} c={C.blue} />
                <SmallCell l="Avg Volatility" v={ana.avg_volatility != null ? ana.avg_volatility.toFixed(4) : '—'} c={C.dim2} />
              </div>
            </div>
            <div>
              <SectionLabel>Decision Cycle</SectionLabel>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 1, background: C.border }}>
                <SmallCell l="Actionable" v={de.actionable_count ?? '—'} c={C.green} />
                <SmallCell l="Open" v={de.open_count ?? '—'} c={C.blue} />
                <SmallCell l="Close" v={de.close_count ?? '—'} c={C.red} />
                <SmallCell l="Reduce" v={de.reduce_count ?? '—'} c={C.amber} />
                <SmallCell l="Rejected" v={de.reject_count ?? '—'} c={C.dim2} />
                <SmallCell l="Slots Left" v={de.slots_remaining ?? '—'} c={C.text} />
              </div>
            </div>
          </div>
        </div>

        {/* Right: recent trades stream */}
        <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            <Dot status={connectionState === 'connected' ? 'live' : 'warn'} />
            <span style={S.label}>Recent Trades</span>
            <span style={{ marginLeft: 'auto', fontSize: 9, color: C.dim2 }}>{trades.length} events</span>
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

// ── ALPHA TERMINAL helper components ──────────────────────────────────────────
const TrajectoryCard = ({ label, total, subtotal, subtotalLabel, delta, spark, color }) => (
  <div style={{ background: C.panel2, padding: '10px 12px' }}>
    <div style={S.label}>{label}</div>
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
      <span style={{ fontSize: 22, fontWeight: 700, color, letterSpacing: '-0.02em' }}>{total ?? '—'}</span>
      {subtotal != null && (
        <span style={{ fontSize: 10, color: C.dim2 }}>{subtotal} {subtotalLabel}</span>
      )}
      {delta != null && delta > 0 && (
        <span style={{ fontSize: 10, color: C.green, marginLeft: 'auto' }}>+{delta} 24h</span>
      )}
    </div>
    {spark && <div style={{ marginTop: 6, height: 18 }}><Sparkline data={spark} color={color} width={160} /></div>}
  </div>
);

const PhaseCard = ({ phase, count, target, label, desc, color }) => {
  const pct = target ? Math.min(100, (count / target) * 100) : (count > 0 ? 100 : 0);
  return (
    <div style={{ background: C.panel2, padding: '10px 12px', border: `1px solid ${C.border}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        <Badge type={phase === 1 ? 'blue' : phase === 2 ? 'amber' : 'green'} size="xs">P{phase}</Badge>
        <span style={{ fontSize: 11, color: C.text, fontWeight: 600 }}>{label}</span>
      </div>
      <div style={{ fontSize: 24, fontWeight: 700, color, marginTop: 6, letterSpacing: '-0.02em' }}>{count}</div>
      <div style={{ fontSize: 9, color: C.dim2, letterSpacing: '0.05em', marginBottom: 8 }}>{desc}</div>
      <ProgressBar value={count} max={target || (count + 1)} color={color} height={4} />
      {target && (
        <div style={{ fontSize: 9, color: C.dim2, marginTop: 4 }}>
          {count >= target ? `${count - target} above threshold` : `${target - count} to next phase`}
        </div>
      )}
    </div>
  );
};

const FollowReadyRow = ({ row }) => {
  const tradesPct    = (row.trades / row.trades_target) * 100;
  const resolvedPct  = (row.resolved / row.resolved_target) * 100;
  const followersPct = (row.followers / row.followers_target) * 100;
  const eta = row.ready ? 'READY' : (row.eta_h != null ? (row.eta_h < 1 ? '<1h' : row.eta_h < 24 ? `${row.eta_h.toFixed(0)}h` : `${(row.eta_h / 24).toFixed(1)}d`) : '—');
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr 1fr 1fr 80px 70px', gap: 8, padding: '6px 10px', background: C.panel, alignItems: 'center', fontSize: 10 }}>
      <span style={{ color: C.text, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{short(row.wallet_address)}</span>
      <ProgressBar value={row.trades} max={row.trades_target} color={tradesPct >= 100 ? C.green : C.blue} height={5} sublabel={`${row.trades}/${row.trades_target}`} />
      <ProgressBar value={row.resolved} max={row.resolved_target} color={resolvedPct >= 100 ? C.green : C.amber} height={5} sublabel={`${row.resolved}/${row.resolved_target}`} />
      <ProgressBar value={row.followers} max={row.followers_target} color={followersPct >= 100 ? C.green : C.purple} height={5} sublabel={`${row.followers}/${row.followers_target}`} />
      <span style={{ textAlign: 'right' }}><Badge type={row.phase === 1 ? 'blue' : row.phase === 2 ? 'amber' : 'green'} size="xs">P{row.phase}</Badge></span>
      <span style={{ textAlign: 'right', color: row.ready ? C.green : C.amber, fontWeight: 700 }}>{eta}</span>
    </div>
  );
};

const SmallCell = ({ l, v, c }) => (
  <div style={{ background: C.panel2, padding: '8px 10px' }}>
    <div style={S.label}>{l}</div>
    <div style={{ fontSize: 17, fontWeight: 700, color: c, marginTop: 4 }}>{v}</div>
  </div>
);

// ─── MARKET SCANNER ───────────────────────────────────────────────────────────
// ─── WALLET SCANNER (was MarketScanner) ──────────────────────────────────────
// Renamed and refocused: instead of scanning markets (the bot's edge is
// wallet-centric, not market-centric), this view ranks the leaders we
// profile, sortable by 24h activity / win-rate / readiness / latest action.
// The old market-based view is preserved under "Markets (legacy)" for
// debugging until it can be safely removed.
const MarketScanner = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [view, setView]     = useStateT('wallets');
  const [search, setSearch] = useStateT('');
  const [sortKey, setSortKey] = useStateT('readiness');
  const [sortDir, setSortDir] = useStateT('desc');

  const opportunities = snapshot?.analytics?.opportunities || [];
  const leaderboard   = snapshot?.analytics?.leaderboard   || [];
  const anaSum        = snapshot?.analytics?.summary       || {};
  const wallets       = (snapshot?.wallet_graph?.nodes || []).filter(n => n.role === 'leader');
  const adaptive      = snapshot?.adaptive_thresholds?.values || {};
  const followMin     = adaptive.FOLLOW_MIN_TRADES || 25;
  const fadeMin       = adaptive.FADE_MIN_RESOLVED || 25;

  // Compute readiness as a composite of the gates: trades / resolved /
  // followers progress towards the FOLLOW gate. 0 = nothing, 1 = passes all.
  const enrichedWallets = useMemoT(() => wallets.map(w => {
    const tradesProgress = Math.min(1, (w.trades_observed || 0) / followMin);
    const resolvedProgress = Math.min(1, (w.positions_resolved || 0) / fadeMin);
    const maturity = Math.max(0, Math.min(1, w.maturity || 0));
    const readiness = (tradesProgress * 0.4 + resolvedProgress * 0.4 + maturity * 0.2);
    return { ...w, readiness };
  }), [wallets, followMin, fadeMin]);

  const sortedWallets = useMemoT(() => {
    const arr = [...enrichedWallets];
    const dir = sortDir === 'asc' ? 1 : -1;
    arr.sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      if (av === bv) return 0;
      return av < bv ? -dir : dir;
    });
    return arr;
  }, [enrichedWallets, sortKey, sortDir]);

  const filteredWallets = useMemoT(() =>
    search ? sortedWallets.filter(w => (w.id || '').toLowerCase().includes(search.toLowerCase())) : sortedWallets,
    [sortedWallets, search]
  );

  const rows = view === 'opps' ? opportunities : leaderboard;
  const filtered = useMemoT(() =>
    search ? rows.filter(r => r.title?.toLowerCase().includes(search.toLowerCase())) : rows,
    [rows, search]
  );

  const setSort = (k) => {
    if (sortKey === k) setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    else { setSortKey(k); setSortDir('desc'); }
  };
  const sortIndicator = (k) => sortKey === k ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Wallets Tracked',  value: enrichedWallets.length, color: C.text },
        { label: 'Active 24h',       value: enrichedWallets.filter(w => (w.trades_24h || 0) > 0).length, color: C.green },
        { label: 'Phase 2+',         value: enrichedWallets.filter(w => (w.phase || 1) >= 2).length, color: C.amber },
        { label: 'Phase 3',          value: enrichedWallets.filter(w => (w.phase || 1) >= 3).length, color: C.green },
        { label: 'Avg Maturity',     value: enrichedWallets.length ? (enrichedWallets.reduce((a, w) => a + (w.maturity || 0), 0) / enrichedWallets.length).toFixed(3) : '—', color: C.purple },
        { label: 'Top Readiness',    value: enrichedWallets.length ? `${Math.round(Math.max(...enrichedWallets.map(w => w.readiness || 0)) * 100)}%` : '—', color: C.green },
      ]} />

      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
        {[['wallets', 'Wallets'], ['opps', 'Markets (legacy)']].map(([v, label]) => (
          <button key={v} onClick={() => setView(v)} style={{
            background: view === v ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${view === v ? C.amber : C.border2}`,
            color: view === v ? C.amber : C.dim2,
            padding: '3px 12px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder={view === 'wallets' ? 'Filter by wallet…' : 'Filter by title…'}
          style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '4px 10px', fontSize: 11, flex: 1, maxWidth: 280, outline: 'none' }} />
        <span style={{ fontSize: 10, color: C.dim2, marginLeft: 'auto' }}>
          {view === 'wallets' ? `${filteredWallets.length} wallets` : `${filtered.length} markets`}
        </span>
      </div>

      {view === 'wallets' ? (
        <div style={{ flex: 1, overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
              <tr>
                <TH>Wallet</TH>
                <TH>Phase</TH>
                <TH>Strategy</TH>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('falcon_score')}>Falcon{sortIndicator('falcon_score')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_24h')}>24h{sortIndicator('trades_24h')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_observed')}>Trades{sortIndicator('trades_observed')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('positions_resolved')}>Resolved{sortIndicator('positions_resolved')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('win_rate')}>Win%{sortIndicator('win_rate')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('pnl_total')}>PnL{sortIndicator('pnl_total')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('readiness')}>Readiness{sortIndicator('readiness')}</th>
                <TH>Last Action</TH>
              </tr>
            </thead>
            <tbody>
              {filteredWallets.length === 0 && (
                <tr><td colSpan={11} style={{ padding: '24px', color: C.dim2, textAlign: 'center', fontSize: 11 }}>{snapshot ? 'No leader wallets profiled yet.' : 'Waiting for data…'}</td></tr>
              )}
              {filteredWallets.map((w) => (
                <tr key={w.id}>
                  <TD style={{ color: C.purple, fontFamily: 'monospace' }}>{w.label}</TD>
                  <TD><Badge type={w.phase >= 3 ? 'green' : w.phase === 2 ? 'amber' : 'blue'}>P{w.phase}</Badge></TD>
                  <TD style={{ color: C.dim2 }}>{w.classification || '—'}</TD>
                  <TD style={{ color: C.amber, fontFamily: 'monospace' }}>{(w.falcon_score || 0).toFixed(2)}</TD>
                  <TD style={{ color: w.trades_24h > 0 ? C.green : C.dim2, fontWeight: 600 }}>{w.trades_24h || 0}</TD>
                  <TD style={{ color: C.text }}>{w.trades_observed || 0}</TD>
                  <TD style={{ color: C.text }}>{w.positions_resolved || 0}</TD>
                  <TD style={{ color: w.win_rate != null ? (w.win_rate >= 0.5 ? C.green : C.red) : C.dim2 }}>
                    {w.win_rate != null ? `${(w.win_rate * 100).toFixed(0)}%` : '—'}
                  </TD>
                  <TD style={{ color: pnlColor(w.pnl_total), fontWeight: 600 }}>{fmtPnl(w.pnl_total)}</TD>
                  <TD style={{ minWidth: 120 }}><ScoreBar value={w.readiness || 0} /></TD>
                  <TD>{w.last_action ? <Badge type={actionType(w.last_action)}>{w.last_action}</Badge> : <span style={{ color: C.dim2 }}>—</span>}</TD>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div style={{ flex: 1, overflow: 'auto' }}>
          <div style={{ padding: '8px 14px', fontSize: 10, color: C.dim2, borderBottom: `1px solid ${C.border}` }}>
            ⚠ Legacy market view — kept for debugging. The bot's edge is wallet-centric, not market-centric. Switch to "Wallets" above.
          </div>
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
      )}
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
  const eq = snapshot?.equity_curve || { series: [], by_leader: [], by_strategy: [] };
  const equitySeries = (eq.series || []).map(s => s.equity);
  const realizedSeries = (eq.series || []).map(s => s.realized_pnl_cum);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Open Positions',   value: positions.open_count ?? openItems.length, color: C.text },
        { label: 'Unrealized PnL',   value: fmtPnl(openPnl),              color: pnlColor(openPnl) },
        { label: 'Capital in Trade', value: positions.capital_in_trade != null ? `$${positions.capital_in_trade.toFixed(0)}` : '—', color: C.amber },
        { label: 'Exposure %',       value: positions.exposure_pct != null ? `${(positions.exposure_pct * 100).toFixed(1)}%` : '—', color: C.text },
        { label: 'Net PnL',          value: fmtPnl(stats.total_pnl),      color: pnlColor(stats.total_pnl), spark: <Sparkline data={realizedSeries} color={pnlColor(stats.total_pnl)} /> },
        { label: 'Equity',           value: stats.portfolio_total != null ? `$${stats.portfolio_total.toFixed(0)}` : '—', color: C.white, spark: <Sparkline data={equitySeries} color={C.white} /> },
        { label: 'Win Rate',         value: stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—', color: C.amber },
      ]} />

      {/* Equity & PnL breakdown panels */}
      {(eq.by_leader.length > 0 || eq.by_strategy.length > 0) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 1, background: C.border, padding: 0, flexShrink: 0 }}>
          <div style={{ background: C.panel2, padding: 12 }}>
            <SectionLabel>Top Leaders by PnL · 30d</SectionLabel>
            {eq.by_leader.length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>No closed paper trades yet.</div>
            ) : (
              <div style={{ display: 'grid', gap: 4, fontSize: 10 }}>
                {eq.by_leader.slice(0, 10).map((r, i) => (
                  <div key={i} style={{ display: 'grid', gridTemplateColumns: '120px 60px 60px 1fr', gap: 6, alignItems: 'center', padding: '3px 0' }}>
                    <span style={{ color: C.purple, fontFamily: 'monospace' }}>{short(r.wallet) || '—'}</span>
                    <span style={{ color: C.dim2 }}>{r.trades} trades</span>
                    <span style={{ color: C.green, fontSize: 10 }}>{r.trades > 0 ? `${Math.round(r.wins / r.trades * 100)}% W` : '—'}</span>
                    <span style={{ color: pnlColor(r.pnl), textAlign: 'right', fontWeight: 700 }}>{fmtPnl(r.pnl)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div style={{ background: C.panel2, padding: 12 }}>
            <SectionLabel>By Strategy</SectionLabel>
            {eq.by_strategy.length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>No data.</div>
            ) : (
              <div style={{ display: 'grid', gap: 6, fontSize: 11 }}>
                {eq.by_strategy.map((s, i) => (
                  <div key={i} style={{ display: 'grid', gridTemplateColumns: '80px 60px 1fr 80px', gap: 8, alignItems: 'center' }}>
                    <Badge type={s.strategy === 'follow' ? 'blue' : s.strategy === 'fade' ? 'amber' : 'default'}>{s.strategy?.toUpperCase()}</Badge>
                    <span style={{ color: C.dim2 }}>{s.trades} trades</span>
                    <ProgressBar value={s.trades > 0 ? (s.wins / s.trades * 100) : 0} max={100} color={C.green} height={5} sublabel={`${s.trades > 0 ? Math.round(s.wins / s.trades * 100) : 0}% win`} />
                    <span style={{ color: pnlColor(s.pnl), textAlign: 'right', fontWeight: 700 }}>{fmtPnl(s.pnl)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

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
  const [filter, setFilter]     = useStateT('ALL');
  const [groupBy, setGroupBy]   = useStateT('leader'); // 'leader' | 'flat'
  const [expanded, setExpanded] = useStateT(new Set());
  const de      = snapshot?.decision_engine || {};
  const summary = de.summary || {};
  const ranked  = de.ranked  || [];

  const filtered = useMemoT(() => {
    if (filter === 'ALL')  return ranked;
    if (filter === 'exec') return ranked.filter(d => d.executable);
    return ranked.filter(d => d.action?.toLowerCase() === filter.toLowerCase());
  }, [ranked, filter]);

  // Group decisions by leader_wallet (the strategic angle: bot follows
  // wallets, not markets). Shows count per leader + actions distribution.
  const byLeader = useMemoT(() => {
    const map = new Map();
    for (const d of filtered) {
      const key = d.leader_wallet || '__unknown__';
      if (!map.has(key)) {
        map.set(key, { wallet: d.leader_wallet, decisions: [], actions: {} });
      }
      const entry = map.get(key);
      entry.decisions.push(d);
      entry.actions[d.action] = (entry.actions[d.action] || 0) + 1;
    }
    return Array.from(map.values()).sort((a, b) => b.decisions.length - a.decisions.length);
  }, [filtered]);

  const toggle = id => setExpanded(prev => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Actionable',       value: summary.actionable_count ?? '—', color: C.green },
        { label: 'Open Signals',     value: summary.open_count       ?? '—', color: C.blue  },
        { label: 'Close Signals',    value: summary.close_count      ?? '—', color: C.red   },
        { label: 'Reduce',           value: summary.reduce_count     ?? '—', color: C.amber },
        { label: 'Rejected',         value: summary.reject_count     ?? '—', color: C.dim2  },
        { label: 'Unique Leaders',   value: byLeader.length, color: C.purple },
        { label: 'Slots Remaining',  value: summary.slots_remaining  ?? '—', color: C.text  },
        { label: 'Exposure Left',    value: summary.exposure_remaining != null ? `${(summary.exposure_remaining * 100).toFixed(1)}%` : '—', color: C.text },
      ]} />

      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 6, flexShrink: 0, alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ ...S.label, marginRight: 4 }}>group:</span>
        {['leader', 'flat'].map(g => (
          <button key={g} onClick={() => setGroupBy(g)} style={{
            background: groupBy === g ? 'rgba(120,85,192,0.12)' : 'transparent',
            border: `1px solid ${groupBy === g ? C.purple : C.border2}`,
            color: groupBy === g ? C.purple : C.dim2,
            padding: '2px 10px', fontSize: 10, cursor: 'pointer', textTransform: 'uppercase',
          }}>{g === 'leader' ? '↳ by leader' : '⋮ flat list'}</button>
        ))}
        <span style={{ width: 12 }} />
        <span style={{ ...S.label, marginRight: 4 }}>filter:</span>
        {['ALL', 'open', 'close', 'reduce', 'skip', 'exec'].map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{
            background: filter === f ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${filter === f ? C.amber : C.border2}`,
            color: filter === f ? C.amber : C.dim2,
            padding: '2px 10px', fontSize: 10, cursor: 'pointer', textTransform: 'uppercase',
          }}>{f}</button>
        ))}
        <span style={{ marginLeft: 'auto', fontSize: 10, color: C.dim2 }}>{filtered.length} decisions · {byLeader.length} leaders</span>
      </div>

      {groupBy === 'leader' ? (
        <DecisionsByLeader byLeader={byLeader} expanded={expanded} toggle={toggle} snapshot={snapshot} />
      ) : (
        <DecisionsFlat filtered={filtered} expanded={expanded} toggle={toggle} snapshot={snapshot} />
      )}
    </div>
  );
};

const DecisionsByLeader = ({ byLeader, expanded, toggle, snapshot }) => (
  <div style={{ flex: 1, overflow: 'auto' }}>
    {byLeader.length === 0 && (
      <div style={{ padding: '24px', color: C.dim2, textAlign: 'center', fontSize: 11 }}>
        {snapshot ? 'No decisions in this cycle.' : 'Waiting for data…'}
      </div>
    )}
    {byLeader.map((g, i) => {
      const key = `leader_${g.wallet || 'unk'}_${i}`;
      const isOpen = expanded.has(key);
      const a = g.actions;
      return (
        <div key={key} style={{ borderBottom: `1px solid ${C.border}` }}>
          <div onClick={() => toggle(key)} style={{
            cursor: 'pointer',
            display: 'grid', gridTemplateColumns: '20px 200px 1fr 80px 80px 80px 80px 60px',
            gap: 12, padding: '10px 14px', alignItems: 'center',
            background: isOpen ? 'rgba(120,85,192,0.04)' : 'transparent',
          }}
            onMouseEnter={e => { if (!isOpen) e.currentTarget.style.background = 'rgba(255,255,255,0.02)'; }}
            onMouseLeave={e => { e.currentTarget.style.background = isOpen ? 'rgba(120,85,192,0.04)' : 'transparent'; }}
          >
            <span style={{ color: C.dim2, fontSize: 11 }}>{isOpen ? '▾' : '▸'}</span>
            <span style={{ color: C.purple, fontFamily: 'monospace', fontSize: 11, fontWeight: 600 }}>{short(g.wallet) || '— unknown —'}</span>
            <span style={{ color: C.dim2, fontSize: 11 }}>{g.decisions.length} markets watched</span>
            <Badge type="green" size="xs">{a.open || 0} open</Badge>
            <Badge type="amber" size="xs">{a.reduce || 0} reduce</Badge>
            <Badge type="red" size="xs">{a.close || 0} close</Badge>
            <Badge type="default" size="xs">{a.skip || 0} skip</Badge>
            <span />
          </div>
          {isOpen && (
            <div style={{ background: 'rgba(120,85,192,0.02)', padding: '0 14px 10px' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                <thead>
                  <tr>{['Market', 'Action', 'Side', 'Confidence', 'Thompson F/F', 'Kelly', 'Exec', 'Summary'].map(h => <TH key={h}>{h}</TH>)}</tr>
                </thead>
                <tbody>
                  {g.decisions.map((d, di) => (
                    <tr key={di}>
                      <TD style={{ maxWidth: 250 }}>
                        <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{d.title}</div>
                      </TD>
                      <TD><Badge type={actionType(d.action)} size="xs">{d.action || '—'}</Badge></TD>
                      <TD><Badge type={d.side === 'YES' ? 'green' : d.side === 'NO' ? 'red' : 'default'} size="xs">{d.side || '—'}</Badge></TD>
                      <TD><ScoreBar value={d.confidence || 0} /></TD>
                      <TD style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 10 }}>
                        {(d.thompson_follow || 0).toFixed(2)} / {(d.thompson_fade || 0).toFixed(2)}
                      </TD>
                      <TD style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 10 }}>{(d.kelly_fraction || 0).toFixed(3)}</TD>
                      <TD><Badge type={d.executable ? 'green' : 'default'} size="xs">{d.executable ? 'YES' : 'NO'}</Badge></TD>
                      <TD style={{ color: C.dim2, fontSize: 10, maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.summary}</div></TD>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      );
    })}
  </div>
);

const DecisionsFlat = ({ filtered, expanded, toggle, snapshot }) => (
  <div style={{ flex: 1, overflow: 'auto' }}>
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
      <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
        <tr>{['Leader', 'Market', 'Action', 'Side', 'Confidence', 'Thompson F/F', 'Exec', 'Summary', ''].map(h => <TH key={h}>{h}</TH>)}</tr>
      </thead>
      <tbody>
        {filtered.length === 0 && (
          <tr><td colSpan={9} style={{ padding: '24px', color: C.dim2, textAlign: 'center' }}>{snapshot ? 'No decisions in this cycle.' : 'Waiting for data…'}</td></tr>
        )}
        {filtered.map((d, i) => {
          const key = `${d.leader_wallet || 'unk'}_${d.market_id || i}`;
          const isOpen = expanded.has(key);
          return (
            <React.Fragment key={key}>
              <tr onClick={() => toggle(key)} style={{ cursor: 'pointer', background: isOpen ? 'rgba(232,160,32,0.03)' : 'transparent' }}>
                <TD style={{ maxWidth: 130 }}>
                  <span style={{ color: C.purple, fontFamily: 'monospace', fontWeight: 600 }}>{short(d.leader_wallet) || '—'}</span>
                </TD>
                <TD style={{ maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{d.title}</div></TD>
                <TD><Badge type={actionType(d.action)}>{d.action || '—'}</Badge></TD>
                <TD><Badge type={d.side === 'YES' ? 'green' : d.side === 'NO' ? 'red' : 'default'} size="xs">{d.side || '—'}</Badge></TD>
                <TD><ScoreBar value={d.confidence || 0} /></TD>
                <TD style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 10 }}>
                  {(d.thompson_follow || 0).toFixed(2)} / {(d.thompson_fade || 0).toFixed(2)}
                </TD>
                <TD><Badge type={d.executable ? 'green' : 'default'} size="xs">{d.executable ? 'YES' : 'NO'}</Badge></TD>
                <TD style={{ color: C.dim2, fontSize: 10, maxWidth: 220 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.summary}</div></TD>
                <TD style={{ color: C.dim2 }}>{isOpen ? '▲' : '▼'}</TD>
              </tr>
              {isOpen && (
                <tr style={{ background: 'rgba(232,160,32,0.02)', borderBottom: `1px solid ${C.border}` }}>
                  <td colSpan={9} style={{ padding: '10px 14px 14px' }}>
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
);

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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
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
            {isReadOnly ? (
              <div style={{ fontSize: 10, color: C.dim2, marginBottom: 10 }}>
                Display-only — the live backend hasn't enabled config writes (set runtime.config_mutable to true).
              </div>
            ) : (
              <div style={{ fontSize: 10, color: C.dim2, marginBottom: 10 }}>
                Live cockpit — edits are validated server-side, persisted to Redis, and propagate to RiskManager / ConfidenceEngine within 30 s.
              </div>
            )}
            {/* Sizing & Kelly knobs */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border, marginBottom: 10 }}>
              {numField('risk_per_trade_pct',      'Risk / Trade %',    0.001)}
              {numField('max_total_exposure_pct',  'Max Exposure %',    0.01)}
              {numField('kelly_fraction',          'Kelly Fraction',    0.01)}
              {numField('fade_size_ratio',         'Fade Size Ratio',   0.05)}
            </div>
            {/* Circuit breakers */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border, marginBottom: 10 }}>
              {numField('max_drawdown_stop_pct',         'Max Drawdown %',         0.01)}
              {numField('max_consecutive_losses',        'Max Cons. Losses',       1)}
              {numField('max_recent_losses_per_market',  'Max Mkt Losses 24h',     1)}
              {numField('min_signal_strength',           'Min Signal',             0.05)}
            </div>
            {/* Position management */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 1, background: C.border }}>
              {numField('max_concurrent_positions','Max Positions',     1)}
              {numField('cooldown_seconds',        'Cooldown (s)',      1)}
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

  // Data Quality drill-down: surface the actual blocking gates, not just
  // a "N issues" badge. Each rule mirrors queries.data_quality().
  const dqFull = snapshot?.data_quality_full || {};
  const dqMarkets = dqFull.markets || {};
  const dqLeaders = dqFull.leaders || {};
  const dqProfiles = dqFull.profiles || {};
  const dqFeed = dqFull.feed || {};

  const dqIssues = [];
  if ((dqMarkets.unmapped_tokens || 0) > 0) {
    dqIssues.push({
      key: 'unmapped_tokens',
      severity: 'warn',
      title: 'Markets without token mapping',
      detail: `${dqMarkets.unmapped_tokens} of ${dqMarkets.total} markets are missing token_yes / token_no.`,
      hint: 'Registry sync_markets re-enriches these on its next cycle (every 30 min).',
    });
  }
  if ((dqMarkets.expired_still_active || 0) > 0) {
    dqIssues.push({
      key: 'expired_still_active',
      severity: 'warn',
      title: 'Expired markets marked as active',
      detail: `${dqMarkets.expired_still_active} markets have an end_date in the past but active=TRUE.`,
      hint: 'A registry pass should mark them inactive — check sync_markets logs.',
    });
  }
  if ((dqMarkets.orphan_market_ids_7d || 0) > 0) {
    dqIssues.push({
      key: 'orphan_market_ids',
      severity: 'warn',
      title: 'Orphan trades (no market metadata)',
      detail: `${dqMarkets.orphan_market_ids_7d} unique market_ids in trades_observed (last 7d) have no corresponding row in markets.`,
      hint: 'Observer auto-stubs new market rows; verify _handle_trade is committing successfully.',
    });
  }
  if ((dqLeaders.stale_refresh || 0) > 0) {
    dqIssues.push({
      key: 'stale_leaders',
      severity: 'warn',
      title: 'Leaders with stale Falcon refresh',
      detail: `${dqLeaders.stale_refresh} of ${dqLeaders.active} active leaders have last_refresh older than ${dqLeaders.stale_threshold_s}s.`,
      hint: 'enrich_leaders runs every 30min — wallets Falcon doesn\'t know are now stamped no_data.',
    });
  }
  if ((dqProfiles.stale_over_24h || 0) > 0) {
    dqIssues.push({
      key: 'stale_profiles',
      severity: 'warn',
      title: 'Profiles not updated in 24h',
      detail: `${dqProfiles.stale_over_24h} of ${dqProfiles.total} profiles last_updated > 24h ago.`,
      hint: 'Likely leaders that stopped trading; consider excluding them from the watchlist.',
    });
  }
  if (dqFeed.ws_healthy === false) {
    dqIssues.push({
      key: 'ws_dead',
      severity: 'err',
      title: 'WebSocket feed unhealthy',
      detail: `Last WS message received ${dqFeed.ws_last_message_age_s != null ? Math.round(dqFeed.ws_last_message_age_s) + 's' : 'never'} ago.`,
      hint: 'Check observer logs for reconnect attempts; verify Polymarket WS is reachable.',
    });
  }

  const logColor = lvl => ({ ERROR: C.red, WARNING: C.amber, INFO: C.text, DEBUG: C.dim2 })[lvl] || C.dim2;
  const filteredLogs = logFilter === 'ALL' ? logs : logs.filter(l => l.level === logFilter);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
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

        {/* Data Quality issues drill-down */}
        {dqIssues.length > 0 && (
          <div>
            <SectionLabel>Data Quality Issues · {dqIssues.length} active</SectionLabel>
            <div style={{ display: 'grid', gap: 8 }}>
              {dqIssues.map((iss, i) => (
                <div key={i} style={{
                  border: `1px solid ${iss.severity === 'err' ? C.red : C.amber}`,
                  background: iss.severity === 'err' ? 'rgba(201,53,69,0.05)' : 'rgba(232,160,32,0.04)',
                  padding: '10px 12px',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                    <Dot status={iss.severity === 'err' ? 'err' : 'warn'} />
                    <span style={{ color: iss.severity === 'err' ? C.red : C.amber, fontWeight: 700, fontSize: 11 }}>{iss.title}</span>
                    <Badge type={iss.severity === 'err' ? 'red' : 'amber'} size="xs">{iss.key}</Badge>
                  </div>
                  <div style={{ fontSize: 11, color: C.text, marginBottom: 4 }}>{iss.detail}</div>
                  <div style={{ fontSize: 10, color: C.dim2, fontStyle: 'italic' }}>↳ {iss.hint}</div>
                </div>
              ))}
            </div>
          </div>
        )}

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

// ─── ML PROGRESSION ───────────────────────────────────────────────────────────
const MLProgression = () => {
  const { snapshot, connectionState } = useLiveStore();
  const extras   = snapshot?.alpha_extras || {};
  const totals   = extras.totals || {};
  const followReady = extras.follow_ready || [];
  const rejections = snapshot?.rejections || { breakdown: [], total: 0 };
  const timeline = extras.timeline || [];
  const adaptive = snapshot?.adaptive_thresholds || { maturity: 0, values: {}, ranges: {} };

  const totalProfiles = (totals.phase1 || 0) + (totals.phase2 || 0) + (totals.phase3 || 0);
  const tradesSpark    = timeline.map(b => b.trades || 0);
  const positionsSpark = timeline.map(b => b.positions_resolved || 0);
  const edgesSpark     = timeline.map(b => b.edges_active || 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Total Profiles', value: totalProfiles, color: C.text },
        { label: 'System Maturity', value: `${(adaptive.maturity * 100).toFixed(1)}%`, color: C.purple },
        { label: 'Avg Maturity',   value: (totals.avg_maturity ?? 0).toFixed(3), color: C.purple },
        { label: 'Phase 1',        value: totals.phase1 ?? 0, color: C.blue },
        { label: 'Phase 2',        value: totals.phase2 ?? 0, color: C.amber },
        { label: 'Phase 3',        value: totals.phase3 ?? 0, color: C.green },
        { label: 'Edges Total',    value: totals.edges_total ?? 0, color: C.text },
        { label: 'Edges Confirmed',value: totals.edges_confirmed ?? 0, color: C.green },
        { label: 'Follow Ready',   value: followReady.filter(r => r.ready).length, color: C.amber },
      ]} />

      <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'grid', gap: 14, gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))', width: '100%' }}>

        {/* Training pipeline */}
        <Panel title="Training Pipeline">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
            <PhaseCard phase={1} count={totals.phase1 ?? 0} target={100} label="Beta-Binomial" desc="0–99 resolved" color={C.blue} />
            <PhaseCard phase={2} count={totals.phase2 ?? 0} target={500} label="Bayesian LogReg" desc="100–499 resolved" color={C.amber} />
            <PhaseCard phase={3} count={totals.phase3 ?? 0} target={null} label="LightGBM + Platt" desc="500+ resolved" color={C.green} />
          </div>
          <div style={{ marginTop: 12, fontSize: 10, color: C.dim2 }}>
            Phase auto-promotes when leader hits next threshold. Fits run nightly (LogReg) / weekly (LightGBM).
          </div>
        </Panel>

        {/* Learning trajectory 24h */}
        <Panel title="Learning Trajectory · 24h">
          <div style={{ display: 'grid', gap: 8, fontSize: 11 }}>
            <SparkRow label="Trades observed"    color={C.blue}  data={tradesSpark}    total={totals.trades_total ?? 0} />
            <SparkRow label="Positions resolved" color={C.green} data={positionsSpark} total={totals.positions_resolved_total ?? 0} />
            <SparkRow label="Active edges"       color={C.amber} data={edgesSpark}     total={totals.edges_total ?? 0} />
          </div>
        </Panel>

        {/* Decision rejections breakdown */}
        <Panel title={`Rejections Last Hour · ${rejections.total} total`}>
          {rejections.breakdown.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No SKIP decisions logged this window.</div>
          ) : (
            <div style={{ display: 'grid', gap: 6 }}>
              {rejections.breakdown.map((r, i) => (
                <div key={i} style={{ display: 'grid', gridTemplateColumns: '180px 1fr 60px 80px', gap: 8, alignItems: 'center', fontSize: 11 }}>
                  <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.reason}</span>
                  <ProgressBar value={r.pct} max={100} color={r.pct > 60 ? C.red : r.pct > 30 ? C.amber : C.blue} height={6} />
                  <span style={{ color: C.dim2, fontSize: 10, textAlign: 'right' }}>{r.pct}%</span>
                  <span style={{ color: C.text, fontFamily: 'monospace', fontSize: 10, textAlign: 'right' }}>{r.count} ({r.uniq_leaders}L)</span>
                </div>
              ))}
            </div>
          )}
        </Panel>

        {/* Closest to FOLLOW */}
        <Panel title={`Closest to FOLLOW Readiness · top ${Math.min(6, followReady.length)}`}>
          {followReady.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No leaders profiled yet.</div>
          ) : (
            <div style={{ display: 'grid', gap: 1, background: C.border }}>
              {followReady.map((r, i) => (
                <FollowReadyRow key={i} row={r} />
              ))}
            </div>
          )}
          <div style={{ marginTop: 8, fontSize: 10, color: C.dim2 }}>
            Adaptive gates · effective right now (system maturity {(adaptive.maturity * 100).toFixed(1)}%): {' '}
            {(adaptive.values.FOLLOW_MIN_TRADES ?? 25).toFixed(0)} trades · {' '}
            {(adaptive.values.FOLLOW_MIN_FOLLOWERS ?? 3).toFixed(0)} confirmed followers
          </div>
        </Panel>

        {/* Adaptive thresholds drill-down */}
        <Panel title={`Adaptive Thresholds · maturity ${(adaptive.maturity * 100).toFixed(1)}%`}>
          <div style={{ fontSize: 10, color: C.dim2, marginBottom: 8 }}>
            Each gate interpolates between cold-start (more permissive) and mature (stricter) values
            based on accumulated profiles + resolutions + confirmed edges.
          </div>
          {Object.keys(adaptive.values).length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No adaptive thresholds reported yet.</div>
          ) : (
            <div style={{ display: 'grid', gap: 6, fontSize: 10 }}>
              {Object.entries(adaptive.values).map(([name, val]) => {
                const range = adaptive.ranges?.[name] || { cold: val, mature: val };
                const span = (range.mature - range.cold) || 1;
                const pct = ((val - range.cold) / span) * 100;
                return (
                  <div key={name} style={{ display: 'grid', gridTemplateColumns: '230px 50px 1fr 50px', gap: 8, alignItems: 'center' }}>
                    <span style={{ color: C.text, fontFamily: 'monospace' }}>{name}</span>
                    <span style={{ color: C.blue, textAlign: 'right' }}>{Number(range.cold).toFixed(2)}</span>
                    <ProgressBar value={pct} max={100} color={C.purple} height={5} sublabel={Number(val).toFixed(2)} />
                    <span style={{ color: C.green, textAlign: 'right' }}>{Number(range.mature).toFixed(2)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
};

const Panel = ({ title, children }) => (
  <div style={{ background: C.panel2, border: `1px solid ${C.border}`, padding: 12, minWidth: 0 }}>
    <SectionLabel>{title}</SectionLabel>
    {children}
  </div>
);

const SparkRow = ({ label, color, data, total }) => (
  <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr 70px', gap: 8, alignItems: 'center' }}>
    <span style={{ color: C.dim2, fontSize: 11 }}>{label}</span>
    <Sparkline data={data} color={color} width={200} height={20} />
    <span style={{ color: color, textAlign: 'right', fontWeight: 700, fontSize: 13 }}>{total.toLocaleString()}</span>
  </div>
);

// ─── WALLET GRAPH (now hosts the Wallet Scanner table view too) ─────────────
const WalletGraph = () => {
  const { snapshot, connectionState } = useLiveStore();
  const wg     = snapshot?.wallet_graph || { nodes: [], edges: [], stats: {} };
  const stats  = wg.stats || {};
  const [selected, setSelected] = useStateT(null);
  const [view, setView] = useStateT(() => localStorage.getItem('pmi_wg_view') || 'graph');
  useEffectT(() => localStorage.setItem('pmi_wg_view', view), [view]);

  const [sortKey, setSortKey] = useStateT('readiness');
  const [sortDir, setSortDir] = useStateT('desc');
  const [search,  setSearch]  = useStateT('');
  const adaptive  = snapshot?.adaptive_thresholds?.values || {};
  const followMin = adaptive.FOLLOW_MIN_TRADES || 25;
  const fadeMin   = adaptive.FADE_MIN_RESOLVED || 25;

  const wallets = wg.nodes.filter(n => n.role === 'leader');
  const enrichedWallets = useMemoT(() => wallets.map(w => {
    const tradesProgress = Math.min(1, (w.trades_observed || 0) / followMin);
    const resolvedProgress = Math.min(1, (w.positions_resolved || 0) / fadeMin);
    const maturity = Math.max(0, Math.min(1, w.maturity || 0));
    const readiness = (tradesProgress * 0.4 + resolvedProgress * 0.4 + maturity * 0.2);
    return { ...w, readiness };
  }), [wallets, followMin, fadeMin]);

  const sortedWallets = useMemoT(() => {
    const arr = [...enrichedWallets];
    const dir = sortDir === 'asc' ? 1 : -1;
    arr.sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      if (av === bv) return 0;
      return av < bv ? -dir : dir;
    });
    return arr;
  }, [enrichedWallets, sortKey, sortDir]);

  const filteredWallets = useMemoT(() =>
    search ? sortedWallets.filter(w => (w.id || '').toLowerCase().includes(search.toLowerCase())) : sortedWallets,
    [sortedWallets, search]
  );

  const setSort = (k) => {
    if (sortKey === k) setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    else { setSortKey(k); setSortDir('desc'); }
  };
  const sortIndicator = (k) => sortKey === k ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '';

  // Layout — circular for leaders (outer ring), spiral for followers (inner).
  // Deterministic so position doesn't jump between updates.
  const layout = useMemoT(() => {
    const W = 720, H = 520, cx = W / 2, cy = H / 2;
    const positions = {};
    const leaders   = wg.nodes.filter(n => n.role === 'leader');
    const followers = wg.nodes.filter(n => n.role === 'follower');
    leaders.forEach((n, i) => {
      const a = (i / Math.max(1, leaders.length)) * Math.PI * 2 - Math.PI / 2;
      const r = 220;
      positions[n.id] = { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r };
    });
    followers.forEach((n, i) => {
      const a = (i / Math.max(1, followers.length)) * Math.PI * 2 + Math.PI / 4;
      const r = 90 + (i % 4) * 18;
      positions[n.id] = { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r };
    });
    return { positions, W, H };
  }, [wg.nodes]);

  const sel = selected ? wg.nodes.find(n => n.id === selected) : null;
  const selEdges = selected ? wg.edges.filter(e => e.source === selected || e.target === selected) : [];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Leaders',         value: stats.leaders ?? 0,         color: C.amber },
        { label: 'Followers',       value: stats.followers ?? 0,       color: C.purple },
        { label: 'Edges Total',     value: stats.edges_total ?? 0,     color: C.text },
        { label: 'Edges Confirmed', value: stats.edges_confirmed ?? 0, color: C.green },
        { label: 'Active 24h',      value: enrichedWallets.filter(w => (w.trades_24h || 0) > 0).length, color: C.green },
        { label: 'Phase 2+',        value: enrichedWallets.filter(w => (w.phase || 1) >= 2).length, color: C.amber },
      ]} />

      {/* View toggle */}
      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
        {[['graph', '⬢ Graph View'], ['list', '☰ Wallet Scanner']].map(([v, label]) => (
          <button key={v} onClick={() => setView(v)} style={{
            background: view === v ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${view === v ? C.amber : C.border2}`,
            color: view === v ? C.amber : C.dim2,
            padding: '3px 12px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        {view === 'list' && (
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter by wallet…"
            style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '4px 10px', fontSize: 11, flex: 1, maxWidth: 280, outline: 'none', marginLeft: 8 }} />
        )}
        <span style={{ fontSize: 10, color: C.dim2, marginLeft: 'auto' }}>
          {view === 'list' ? `${filteredWallets.length} wallets` : `${stats.leaders ?? 0} leaders · ${stats.edges_total ?? 0} edges`}
        </span>
      </div>

      {view === 'list' ? (
        <div style={{ flex: 1, overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
              <tr>
                <TH>Wallet</TH>
                <TH>Phase</TH>
                <TH>Strategy</TH>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('falcon_score')}>Falcon{sortIndicator('falcon_score')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_24h')}>24h{sortIndicator('trades_24h')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_observed')}>Trades{sortIndicator('trades_observed')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('positions_resolved')}>Resolved{sortIndicator('positions_resolved')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('win_rate')}>Win%{sortIndicator('win_rate')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('pnl_total')}>PnL{sortIndicator('pnl_total')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('readiness')}>Readiness{sortIndicator('readiness')}</th>
                <TH>Last Action</TH>
              </tr>
            </thead>
            <tbody>
              {filteredWallets.length === 0 && (
                <tr><td colSpan={11} style={{ padding: '24px', color: C.dim2, textAlign: 'center', fontSize: 11 }}>{snapshot ? 'No leader wallets profiled yet.' : 'Waiting for data…'}</td></tr>
              )}
              {filteredWallets.map((w) => (
                <tr key={w.id} style={{ cursor: 'pointer' }} onClick={() => { setSelected(w.id); setView('graph'); }}>
                  <TD style={{ color: C.purple, fontFamily: 'monospace' }}>{w.label}</TD>
                  <TD><Badge type={w.phase >= 3 ? 'green' : w.phase === 2 ? 'amber' : 'blue'}>P{w.phase}</Badge></TD>
                  <TD style={{ color: C.dim2 }}>{w.classification || '—'}</TD>
                  <TD style={{ color: C.amber, fontFamily: 'monospace' }}>{(w.falcon_score || 0).toFixed(2)}</TD>
                  <TD style={{ color: w.trades_24h > 0 ? C.green : C.dim2, fontWeight: 600 }}>{w.trades_24h || 0}</TD>
                  <TD style={{ color: C.text }}>{w.trades_observed || 0}</TD>
                  <TD style={{ color: C.text }}>{w.positions_resolved || 0}</TD>
                  <TD style={{ color: w.win_rate != null ? (w.win_rate >= 0.5 ? C.green : C.red) : C.dim2 }}>
                    {w.win_rate != null ? `${(w.win_rate * 100).toFixed(0)}%` : '—'}
                  </TD>
                  <TD style={{ color: pnlColor(w.pnl_total), fontWeight: 600 }}>{fmtPnl(w.pnl_total)}</TD>
                  <TD style={{ minWidth: 120 }}><ScoreBar value={w.readiness || 0} /></TD>
                  <TD>{w.last_action ? <Badge type={actionType(w.last_action)}>{w.last_action}</Badge> : <span style={{ color: C.dim2 }}>—</span>}</TD>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'minmax(0, 2fr) minmax(280px, 1fr)', overflow: 'hidden' }}>

        {/* Force-directed graph (circular layout SVG) */}
        <div style={{ overflow: 'auto', borderRight: `1px solid ${C.border}`, padding: 14 }}>
          {wg.nodes.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11, padding: 40, textAlign: 'center' }}>
              {snapshot ? 'No leaders profiled yet — graph will populate as leaders accumulate trades.' : 'Waiting for data…'}
            </div>
          ) : (
            <svg viewBox={`0 0 ${layout.W} ${layout.H}`} width="100%" style={{ display: 'block', maxHeight: '85vh' }}>
              {/* Edges */}
              {wg.edges.map((e, i) => {
                const a = layout.positions[e.source];
                const b = layout.positions[e.target];
                if (!a || !b) return null;
                const stroke = e.confirmed ? C.green : 'rgba(120,120,160,0.25)';
                const opacity = e.confirmed ? 0.7 : 0.3;
                return (
                  <line
                    key={i}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={stroke} strokeWidth={e.confirmed ? 1.5 : 0.6}
                    opacity={opacity}
                    strokeDasharray={e.confirmed ? '' : '2 2'}
                  />
                );
              })}
              {/* Nodes */}
              {wg.nodes.map(n => {
                const p = layout.positions[n.id];
                if (!p) return null;
                const r = n.role === 'leader' ? 8 + Math.min(8, (n.maturity || 0) * 12) : 4;
                const phaseColor = n.phase === 3 ? C.green : n.phase === 2 ? C.amber : C.blue;
                const fill = n.role === 'leader' ? phaseColor : C.purple;
                const isSel = selected === n.id;
                return (
                  <g key={n.id} style={{ cursor: 'pointer' }} onClick={() => setSelected(isSel ? null : n.id)}>
                    <circle cx={p.x} cy={p.y} r={r + (isSel ? 4 : 0)} fill={fill}
                      stroke={isSel ? C.white : 'transparent'} strokeWidth={2} opacity={0.9} />
                    {n.role === 'leader' && (
                      <text x={p.x} y={p.y - r - 4} fill={C.dim2} fontSize="9"
                        textAnchor="middle" fontFamily="monospace">{n.label}</text>
                    )}
                  </g>
                );
              })}
            </svg>
          )}

          <div style={{ marginTop: 16, fontSize: 10, color: C.dim2, display: 'flex', gap: 16 }}>
            <span><span style={{ color: C.blue }}>●</span> phase 1</span>
            <span><span style={{ color: C.amber }}>●</span> phase 2</span>
            <span><span style={{ color: C.green }}>●</span> phase 3</span>
            <span><span style={{ color: C.purple }}>●</span> follower</span>
            <span style={{ marginLeft: 'auto' }}>━ confirmed edge   ┄ pending</span>
          </div>
        </div>

        {/* Inspector */}
        <div style={{ overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {!sel ? (
            <div style={{ color: C.dim2, fontSize: 11, textAlign: 'center', paddingTop: 40 }}>
              Click a node to inspect.
            </div>
          ) : (
            <>
              <SectionLabel>{sel.role === 'leader' ? 'Leader' : 'Follower'} · {sel.label}</SectionLabel>
              <div style={{ fontFamily: 'monospace', fontSize: 10, color: C.dim2, wordBreak: 'break-all' }}>{sel.id}</div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginTop: 8 }}>
                <Stat l="Falcon Score" v={(sel.falcon_score || 0).toFixed(3)} c={C.amber} />
                <Stat l="Phase" v={`P${sel.phase}`} c={sel.phase === 3 ? C.green : sel.phase === 2 ? C.amber : C.blue} />
                <Stat l="Maturity" v={(sel.maturity || 0).toFixed(3)} c={C.purple} />
                <Stat l="Trades" v={(sel.trades_observed || 0).toLocaleString()} c={C.text} />
                <Stat l="Resolved" v={(sel.positions_resolved || 0)} c={C.green} />
                <Stat l="Strategy" v={sel.classification || '—'} c={C.blue} />
              </div>

              <SectionLabel mb={6}>Edges ({selEdges.length})</SectionLabel>
              {selEdges.length === 0 ? (
                <div style={{ color: C.dim2, fontSize: 11 }}>No edges.</div>
              ) : (
                <div style={{ display: 'grid', gap: 4, fontSize: 10 }}>
                  {selEdges.map((e, i) => (
                    <div key={i} style={{ background: C.panel, padding: '6px 8px', borderLeft: `2px solid ${e.confirmed ? C.green : C.dim}` }}>
                      <div style={{ color: C.text, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {short(e.source === sel.id ? e.target : e.source)}
                      </div>
                      <div style={{ display: 'flex', gap: 8, marginTop: 2, color: C.dim2 }}>
                        <span>p={e.p_follow?.toFixed(2)}</span>
                        <span>α/μ={e.hawkes_alpha_mu != null ? e.hawkes_alpha_mu.toFixed(2) : '—'}</span>
                        <span>{e.delay_s != null ? `${Math.round(e.delay_s)}s` : '—'}</span>
                        <span style={{ marginLeft: 'auto' }}>{e.co_occurrences} obs</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      )}
    </div>
  );
};

const Stat = ({ l, v, c }) => (
  <div style={{ background: C.panel, padding: '6px 8px' }}>
    <div style={{ ...S.label, fontSize: 9 }}>{l}</div>
    <div style={{ fontSize: 14, fontWeight: 700, color: c, marginTop: 2 }}>{v}</div>
  </div>
);

// ─── INSPECTOR ────────────────────────────────────────────────────────────────
// Pipeline observability tab — surfaces what the server is actually
// receiving and what it's deciding, so operators can debug attribution
// + latency + decision-pipeline issues without SSH.
const Inspector = () => {
  const { connectionState } = useLiveStore();
  const [snap, setSnap] = useStateT(null);
  const [filter, setFilter] = useStateT('all');     // all | leader | non-leader
  const [sourceFilter, setSourceFilter] = useStateT('all');
  const [autoRefresh, setAutoRefresh] = useStateT(true);
  const [lastFetched, setLastFetched] = useStateT(null);

  const refresh = async () => {
    try {
      const r = await fetch(`${window.PoybotAPI?.getSettings?.()?.API_BASE || ''}/api/inspector/snapshot?limit=120`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setSnap(data);
      setLastFetched(Date.now());
    } catch (e) { console.warn('[Inspector] fetch failed', e.message); }
  };

  useEffectT(() => { refresh(); }, []);
  useEffectT(() => {
    if (!autoRefresh) return;
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [autoRefresh]);

  const trades    = snap?.raw_trades   || [];
  const decisions = snap?.decisions    || [];
  const sourceMix = snap?.source_mix   || [];
  const counters  = snap?.counters     || {};
  const pipeline  = snap?.pipeline     || {};

  const filteredTrades = trades.filter(t => {
    if (filter === 'leader' && !t.is_leader) return false;
    if (filter === 'non-leader' && t.is_leader) return false;
    if (sourceFilter !== 'all' && t.source !== sourceFilter) return false;
    return true;
  });

  const allSources = Array.from(new Set(trades.map(t => t.source).filter(Boolean)));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Trades 1h',         value: counters.trades_1h ?? '—',           color: C.blue },
        { label: 'Leader Trades 1h',  value: counters.leader_trades_1h ?? '—',    color: C.amber },
        { label: 'Decisions 1h',      value: counters.decisions_1h ?? '—',        color: C.purple },
        { label: 'Actionable 1h',     value: counters.actionable_1h ?? '—',       color: C.green },
        { label: 'Closes 1h',         value: counters.closes_1h ?? '—',           color: C.text },
        { label: 'WS Lag',            value: pipeline.ws_last_message_age_s != null ? `${pipeline.ws_last_message_age_s.toFixed(1)}s` : '—', color: pipeline.ws_last_message_age_s > 30 ? C.red : C.green },
      ]} />

      {/* Toolbar */}
      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${C.border}`, display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0, fontSize: 11 }}>
        <span style={{ color: C.dim2 }}>Wallet:</span>
        {[['all', 'All'], ['leader', 'Leaders only'], ['non-leader', 'Non-leaders']].map(([v, label]) => (
          <button key={v} onClick={() => setFilter(v)} style={{
            background: filter === v ? 'rgba(232,160,32,0.1)' : 'transparent',
            border: `1px solid ${filter === v ? C.amber : C.border2}`,
            color: filter === v ? C.amber : C.dim2,
            padding: '2px 10px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        <span style={{ color: C.dim2, marginLeft: 12 }}>Source:</span>
        {[['all', 'All'], ...allSources.map(s => [s, s])].map(([v, label]) => (
          <button key={v} onClick={() => setSourceFilter(v)} style={{
            background: sourceFilter === v ? 'rgba(61,125,200,0.15)' : 'transparent',
            border: `1px solid ${sourceFilter === v ? C.blue : C.border2}`,
            color: sourceFilter === v ? C.blue : C.dim2,
            padding: '2px 10px', fontSize: 11, cursor: 'pointer',
          }}>{label}</button>
        ))}
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <label style={{ color: C.dim2, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4 }}>
            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
            auto-refresh 3s
          </label>
          <button onClick={refresh} style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, padding: '2px 10px', fontSize: 11, cursor: 'pointer' }}>↻ refresh</button>
          {lastFetched && <span style={{ color: C.dim2, fontSize: 10 }}>last: {new Date(lastFetched).toLocaleTimeString('en-GB')}</span>}
        </span>
      </div>

      {/* Body — 2 cols: raw trades + (source mix / decisions / pipeline) */}
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'minmax(0, 2fr) minmax(320px, 1fr)', overflow: 'hidden' }}>
        {/* Raw trades stream */}
        <div style={{ overflow: 'auto', borderRight: `1px solid ${C.border}` }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 8, position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
            <Dot status={autoRefresh ? 'live' : 'off'} />
            <SectionLabel mb={0}>Raw Trades · {filteredTrades.length}/{trades.length}</SectionLabel>
          </div>
          {filteredTrades.length === 0 ? (
            <div style={{ padding: 20, color: C.dim2, fontSize: 11 }}>No trades match the current filter.</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10, fontFamily: 'monospace' }}>
              <thead style={{ position: 'sticky', top: 35, background: C.panel, zIndex: 1 }}>
                <tr>
                  {['Time','Wallet','L?','Side','Price','Size $','Source','Market'].map(h => <TH key={h}>{h}</TH>)}
                </tr>
              </thead>
              <tbody>
                {filteredTrades.map((t) => (
                  <tr key={t.id}>
                    <TD style={{ color: C.dim2 }}>{t.time ? new Date(t.time).toLocaleTimeString('en-GB') : '—'}</TD>
                    <TD style={{ color: t.is_leader ? C.amber : C.dim2 }}>{short(t.wallet_address)}</TD>
                    <TD>{t.is_leader ? <Badge type="amber" size="xs">L</Badge> : <span style={{ color: C.dim }}>·</span>}</TD>
                    <TD style={{ color: sideColor(t.side), fontWeight: 700 }}>{t.side}</TD>
                    <TD style={{ color: C.text }}>{t.price?.toFixed(3)}</TD>
                    <TD style={{ color: C.text }}>{t.size_usdc?.toFixed(0)}</TD>
                    <TD><Badge type={t.source === 'websocket' ? 'blue' : t.source === 'api_market' ? 'green' : 'default'} size="xs">{t.source || '—'}</Badge></TD>
                    <TD style={{ maxWidth: 250 }}><div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{t.market_question || t.market_id?.slice(0, 30)}</div></TD>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Right rail: source mix + pipeline + decisions */}
        <div style={{ overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
          <div style={{ padding: 12, borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>Source Mix · last 5 min</SectionLabel>
            {sourceMix.length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>No trades in the last 5 min.</div>
            ) : (
              <div style={{ display: 'grid', gap: 4 }}>
                {sourceMix.map((s) => {
                  const totalAll = sourceMix.reduce((a, x) => a + x.total, 0) || 1;
                  return (
                    <div key={s.source}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 2 }}>
                        <span style={{ color: C.text }}>{s.source}</span>
                        <span style={{ color: C.dim2 }}>{s.total} ({s.leader_count} leaders)</span>
                      </div>
                      <ProgressBar value={s.total / totalAll * 100} max={100} color={s.source === 'websocket' ? C.blue : s.source === 'api_market' ? C.green : C.purple} height={5} />
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div style={{ padding: 12, borderBottom: `1px solid ${C.border}` }}>
            <SectionLabel>Pipeline Health</SectionLabel>
            <div style={{ display: 'grid', gap: 6, fontSize: 11 }}>
              <PipeRow label="Redis reachable" value={pipeline.redis_reachable ? 'OK' : 'DOWN'} color={pipeline.redis_reachable ? C.green : C.red} />
              <PipeRow label="WS last msg" value={pipeline.ws_last_message_age_s != null ? `${pipeline.ws_last_message_age_s.toFixed(1)}s ago` : '—'} color={pipeline.ws_last_message_age_s > 30 ? C.red : pipeline.ws_last_message_age_s > 10 ? C.amber : C.green} />
              <PipeRow label="WS msgs/min" value={pipeline.ws_msgs_per_min != null ? `${Math.round(pipeline.ws_msgs_per_min)}` : '—'} color={C.text} />
              <PipeRow label="trades:observed subscribers" value={pipeline.trades_pubsub_subscribers ?? '—'} color={C.purple} />
            </div>
          </div>

          <div style={{ padding: 12 }}>
            <SectionLabel>Recent Decisions · last {decisions.length}</SectionLabel>
            {decisions.length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>No decisions yet.</div>
            ) : (
              <div style={{ display: 'grid', gap: 4, fontSize: 10 }}>
                {decisions.slice(0, 20).map((d, i) => (
                  <div key={i} style={{ background: C.panel, padding: '4px 6px', borderLeft: `2px solid ${d.action === 'follow' ? C.green : d.action === 'fade' ? C.amber : C.dim}` }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 2 }}>
                      <span style={{ color: C.dim2 }}>{d.time ? new Date(d.time).toLocaleTimeString('en-GB') : '—'}</span>
                      <Badge type={d.action === 'follow' ? 'green' : d.action === 'fade' ? 'amber' : 'default'} size="xs">{d.action}</Badge>
                      <span style={{ color: C.purple, fontFamily: 'monospace' }}>{short(d.leader_wallet)}</span>
                      {d.confidence != null && <span style={{ color: C.amber, marginLeft: 'auto' }}>c={d.confidence.toFixed(2)}</span>}
                    </div>
                    {d.reason && <div style={{ color: C.dim2, fontSize: 9, lineHeight: 1.4 }}>{d.reason}</div>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

const PipeRow = ({ label, value, color }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
    <span style={{ color: C.dim2 }}>{label}</span>
    <span style={{ color, fontWeight: 700, fontFamily: 'monospace' }}>{value}</span>
  </div>
);

Object.assign(window, { AlphaTerminal, MarketScanner, LivePortfolio, DecisionEngine, RiskConfig, BotHealth, WalletGraph, MLProgression, Inspector });
