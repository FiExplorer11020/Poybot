// dashboard-tabs.jsx — 6 tab views wired to Poybot LiveSnapshot

const { useState: useStateT, useEffect: useEffectT, useMemo: useMemoT } = React;
const {
  C, S, useLiveStore, usePersistedState, ConnBanner,
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
              {trades.map((t, idx) => {
                const clickable = !!t.wallet_address;
                return (
                  <div key={t.id || idx}
                    onClick={clickable ? () => window.PoybotNav?.selectWallet(t.wallet_address) : undefined}
                    title={clickable ? `Open ${short(t.wallet_address)} in Wallet Graph` : undefined}
                    style={{
                      padding: '5px 10px', borderBottom: `1px solid ${C.border}`,
                      background: idx === 0 ? 'rgba(232,160,32,0.04)' : 'transparent',
                      display: 'grid', gridTemplateColumns: '52px 32px 1fr 54px 60px',
                      gap: 6, alignItems: 'center', fontSize: 11,
                      cursor: clickable ? 'pointer' : 'default',
                      transition: 'background 120ms',
                    }}
                    onMouseEnter={clickable ? e => e.currentTarget.style.background = 'rgba(120,85,192,0.08)' : undefined}
                    onMouseLeave={clickable ? e => e.currentTarget.style.background = idx === 0 ? 'rgba(232,160,32,0.04)' : 'transparent' : undefined}
                  >
                    <span style={{ color: C.dim2, fontSize: 10 }}>{t.timestamp ? new Date(t.timestamp).toLocaleTimeString('en-GB') : '—'}</span>
                    <span style={{ color: sideColor(t.side), fontWeight: 700, fontSize: 10 }}>{t.side}</span>
                    <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.market_title}</span>
                    <span style={{ color: C.dim2, textAlign: 'right', fontFamily: 'monospace' }}>{fmtNum(t.price, 3)}</span>
                    <span style={{ color: pnlColor(t.pnl_abs), textAlign: 'right', fontWeight: 600 }}>{fmtPnl(t.pnl_abs)}</span>
                  </div>
                );
              })}
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
  const tradesPct    = Math.min(100, (row.trades / row.trades_target) * 100);
  const resolvedPct  = Math.min(100, (row.resolved / row.resolved_target) * 100);
  const followersPct = Math.min(100, (row.followers / row.followers_target) * 100);
  const eta = row.ready ? 'READY' : (row.eta_h != null ? (row.eta_h < 1 ? '<1h' : row.eta_h < 24 ? `${row.eta_h.toFixed(0)}h` : `${(row.eta_h / 24).toFixed(1)}d`) : '—');

  // 2-line layout: header (wallet · phase · eta) + 3 thin progress dots-bars.
  // Works in narrow columns (≥ 240 px) without truncating numbers.
  const tooltip = `Trades ${row.trades}/${row.trades_target} · Resolved ${row.resolved}/${row.resolved_target} · Followers ${row.followers}/${row.followers_target}\n\nClick to open in Wallet Graph`;
  const onClick = row.wallet_address ? () => window.PoybotNav?.selectWallet(row.wallet_address) : undefined;
  return (
    <div title={tooltip} onClick={onClick}
      style={{ padding: '6px 10px', background: C.panel, fontSize: 10, display: 'flex', flexDirection: 'column', gap: 5,
        cursor: onClick ? 'pointer' : 'default', transition: 'background 120ms' }}
      onMouseEnter={onClick ? e => e.currentTarget.style.background = 'rgba(120,85,192,0.06)' : undefined}
      onMouseLeave={onClick ? e => e.currentTarget.style.background = C.panel : undefined}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color: C.text, fontFamily: 'monospace', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {short(row.wallet_address)}
        </span>
        <Badge type={row.phase === 1 ? 'blue' : row.phase === 2 ? 'amber' : 'green'} size="xs">P{row.phase}</Badge>
        <span style={{ color: row.ready ? C.green : C.amber, fontWeight: 700, fontFamily: 'monospace', minWidth: 36, textAlign: 'right' }}>{eta}</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6, alignItems: 'center' }}>
        <MicroBar pct={tradesPct}    color={tradesPct >= 100 ? C.green : C.blue}   label={`T ${row.trades}/${row.trades_target}`} />
        <MicroBar pct={resolvedPct}  color={resolvedPct >= 100 ? C.green : C.amber} label={`R ${row.resolved}/${row.resolved_target}`} />
        <MicroBar pct={followersPct} color={followersPct >= 100 ? C.green : C.purple} label={`F ${row.followers}/${row.followers_target}`} />
      </div>
    </div>
  );
};

// Tiny inline stat: label + thin bar. Replaces the ProgressBar with sublabel
// pattern in narrow grids where vertical stacking wastes too much space.
const MicroBar = ({ pct, color, label }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
    <span style={{ color: C.dim2, fontSize: 9, fontFamily: 'monospace', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</span>
    <div style={{ height: 3, background: 'rgba(255,255,255,0.06)' }}>
      <div style={{ width: `${Math.max(0, Math.min(100, pct))}%`, height: '100%', background: color, transition: 'width 0.4s' }} />
    </div>
  </div>
);

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
  const [view, setView] = usePersistedState('lp.view', 'open');
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
              {openItems.map((p, i) => {
                const wallet = p.leader_wallet || p.wallet_address;
                const onClick = wallet ? () => window.PoybotNav?.selectWallet(wallet) : undefined;
                return (
                <tr key={p.trade_id || i} onClick={onClick}
                    title={wallet ? `Open ${short(wallet)} in Wallet Graph` : undefined}
                    style={{ cursor: wallet ? 'pointer' : 'default', transition: 'background 120ms' }}
                    onMouseEnter={wallet ? e => e.currentTarget.style.background = 'rgba(120,85,192,0.06)' : undefined}
                    onMouseLeave={wallet ? e => e.currentTarget.style.background = 'transparent' : undefined}>
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
                );
              })}
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
  const [filter, setFilter]     = usePersistedState('de.filter', 'ALL');
  const [groupBy, setGroupBy]   = usePersistedState('de.groupBy', 'leader'); // 'leader' | 'flat'
  const [expanded, setExpanded] = useStateT(new Set());  // ephemeral — reset on remount
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
            <span
              onClick={g.wallet ? (e) => { e.stopPropagation(); window.PoybotNav?.selectWallet(g.wallet); } : undefined}
              title={g.wallet ? `Open ${short(g.wallet)} in Wallet Graph` : undefined}
              style={{
                color: C.purple, fontFamily: 'monospace', fontSize: 11, fontWeight: 600,
                cursor: g.wallet ? 'pointer' : 'default',
                textDecoration: g.wallet ? 'underline dotted rgba(120,85,192,0.3)' : 'none',
                textUnderlineOffset: 3,
              }}>{short(g.wallet) || '— unknown —'}</span>
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
                  {g.decisions.map((d, di) => {
                    const onOpen = d.id ? (e) => { e.stopPropagation(); window.PoybotNav?.selectDecision(d); } : undefined;
                    return (
                    <tr key={di} onClick={onOpen} title={d.id ? 'Click for full reasoning' : undefined}
                      style={{ cursor: onOpen ? 'pointer' : 'default', transition: 'background 100ms' }}
                      onMouseEnter={onOpen ? e => e.currentTarget.style.background = 'rgba(232,160,32,0.04)' : undefined}
                      onMouseLeave={onOpen ? e => e.currentTarget.style.background = 'transparent' : undefined}>
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
                    );
                  })}
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
                  <span
                    onClick={d.leader_wallet ? (e) => { e.stopPropagation(); window.PoybotNav?.selectWallet(d.leader_wallet); } : undefined}
                    title={d.leader_wallet ? `Open ${short(d.leader_wallet)} in Wallet Graph` : undefined}
                    style={{
                      color: C.purple, fontFamily: 'monospace', fontWeight: 600,
                      cursor: d.leader_wallet ? 'pointer' : 'default',
                      textDecoration: d.leader_wallet ? 'underline dotted rgba(120,85,192,0.3)' : 'none',
                      textUnderlineOffset: 3,
                    }}>{short(d.leader_wallet) || '—'}</span>
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
      refreshAuditLog();
    } catch (e) { setSaveMsg('✗ ' + e.message); }
    setSaving(false);
    setTimeout(() => setSaveMsg(''), 3000);
  };

  // Audit log of recent risk config changes — fetched on mount, refreshed
  // after each save so the operator sees their change land immediately.
  const [auditLog, setAuditLog] = useStateT(null);
  const refreshAuditLog = () => {
    const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
    fetch(`${base}/api/risk/history?limit=30`)
      .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
      .then(d => setAuditLog(d))
      .catch(e => { console.warn('[RiskConfig] audit fetch failed', e); setAuditLog({ items: [], _error: true }); });
  };
  useEffectT(() => { refreshAuditLog(); }, []);

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

      {/* Audit log — recent runtime config changes (last 30) */}
      <div style={{ padding: '14px 14px 18px', borderTop: `1px solid ${C.border}`, flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <SectionLabel mb={0}>Audit log · risk config changes</SectionLabel>
          <span style={{ color: C.dim2, fontSize: 9, fontFamily: 'monospace', marginLeft: 'auto' }}>
            {auditLog ? `${(auditLog.items || []).length} of ${auditLog.total ?? '—'}` : 'loading…'}
          </span>
          <button onClick={refreshAuditLog} title="Refresh"
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 10, padding: '2px 8px', cursor: 'pointer' }}>
            ↻
          </button>
        </div>
        {!auditLog ? (
          <div style={{ color: C.dim2, fontSize: 11 }}>Loading…</div>
        ) : (auditLog.items || []).length === 0 ? (
          <div style={{ color: C.dim2, fontSize: 11 }}>No config changes recorded yet.</div>
        ) : (
          <div style={{ maxHeight: 280, overflow: 'auto', border: `1px solid ${C.border}` }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
              <thead style={{ position: 'sticky', top: 0, background: C.panel2, zIndex: 1 }}>
                <tr>
                  <TH>When</TH>
                  <TH>Key</TH>
                  <TH>Old → New</TH>
                  <TH>Actor</TH>
                  <TH>Source</TH>
                </tr>
              </thead>
              <tbody>
                {(auditLog.items || []).map(h => (
                  <tr key={h.id}>
                    <TD style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 10, whiteSpace: 'nowrap' }}>
                      {h.changed_at_iso ? new Date(h.changed_at_iso).toLocaleString('en-GB', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'}
                    </TD>
                    <TD style={{ color: C.amber, fontFamily: 'monospace' }}>{h.key}</TD>
                    <TD style={{ fontFamily: 'monospace', fontSize: 10 }}>
                      <span style={{ color: C.dim2, textDecoration: 'line-through' }}>{h.old_value ?? '—'}</span>
                      <span style={{ color: C.dim2, margin: '0 6px' }}>→</span>
                      <span style={{ color: C.green, fontWeight: 600 }}>{h.new_value ?? '—'}</span>
                    </TD>
                    <TD style={{ color: C.purple, fontFamily: 'monospace' }}>{h.actor || '—'}</TD>
                    <TD><Badge type="default" size="xs">{h.source || '—'}</Badge></TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

// ─── BOT HEALTH ───────────────────────────────────────────────────────────────
const BotHealth = () => {
  const { snapshot, connectionState } = useLiveStore();
  const [logFilter, setLogFilter] = usePersistedState('bh.logFilter', 'ALL');
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
              {dqIssues.map((iss, i) => {
                // Issues that map to a drill-down endpoint can be clicked to
                // open the modal listing the affected items.
                const drillable = ['unmapped_tokens', 'expired_still_active', 'orphan_market_ids', 'stale_leaders', 'stale_profiles'].includes(iss.key);
                const onClick = drillable ? () => window.PoybotNav?.showDataQualityIssue(iss.key) : undefined;
                return (
                <div key={i} onClick={onClick}
                  title={drillable ? `Click to see affected items` : undefined}
                  style={{
                    border: `1px solid ${iss.severity === 'err' ? C.red : C.amber}`,
                    background: iss.severity === 'err' ? 'rgba(201,53,69,0.05)' : 'rgba(232,160,32,0.04)',
                    padding: '10px 12px',
                    cursor: drillable ? 'pointer' : 'default',
                    transition: 'background 120ms, border-color 120ms',
                  }}
                  onMouseEnter={drillable ? e => { e.currentTarget.style.background = iss.severity === 'err' ? 'rgba(201,53,69,0.1)' : 'rgba(232,160,32,0.1)'; } : undefined}
                  onMouseLeave={drillable ? e => { e.currentTarget.style.background = iss.severity === 'err' ? 'rgba(201,53,69,0.05)' : 'rgba(232,160,32,0.04)'; } : undefined}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                    <Dot status={iss.severity === 'err' ? 'err' : 'warn'} />
                    <span style={{ color: iss.severity === 'err' ? C.red : C.amber, fontWeight: 700, fontSize: 11 }}>{iss.title}</span>
                    <Badge type={iss.severity === 'err' ? 'red' : 'amber'} size="xs">{iss.key}</Badge>
                    {drillable && <span style={{ marginLeft: 'auto', color: C.dim2, fontSize: 10 }}>view affected ›</span>}
                  </div>
                  <div style={{ fontSize: 11, color: C.text, marginBottom: 4 }}>{iss.detail}</div>
                  <div style={{ fontSize: 10, color: C.dim2, fontStyle: 'italic' }}>↳ {iss.hint}</div>
                </div>
                );
              })}
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

  // Lazy-fetched diagnostics endpoint — refreshes every 60 s.
  const [diag, setDiag] = useStateT(null);
  useEffectT(() => {
    let cancelled = false;
    const fetchDiag = () => {
      const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
      fetch(`${base}/api/ml/diagnostics`)
        .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
        .then(d => { if (!cancelled) setDiag(d); })
        .catch(e => console.warn('[MLProgression] diagnostics fetch failed', e));
    };
    fetchDiag();
    const id = setInterval(fetchDiag, 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const totalProfiles = (totals.phase1 || 0) + (totals.phase2 || 0) + (totals.phase3 || 0);
  const tradesSpark    = timeline.map(b => b.trades || 0);
  const positionsSpark = timeline.map(b => b.positions_resolved || 0);
  const edgesSpark     = timeline.map(b => b.edges_active || 0);

  // Format helpers
  const fmtSec = (s) => {
    if (!s) return '—';
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
    return `${(s / 86400).toFixed(1)}d`;
  };
  const closeMethods = diag?.close_methods || [];
  const sampleEff = diag?.sample_efficiency || {};
  const holdingByPhase = diag?.holding_by_phase || [];
  const catCoverage = diag?.category_coverage || [];
  const decisions24h = diag?.decisions_24h || { total: 0, by_action: [] };
  const enrichLag = diag?.falcon_enrichment_lag || {};
  const phaseEta = diag?.phase_eta_top || [];

  // Latest category coverage % for the top KPI strip.
  const latestCovPct = catCoverage.length ? catCoverage[catCoverage.length - 1].pct : null;

  // Hero status — the one question that matters most: are we trading yet?
  const followReadyCount = followReady.filter(r => r.ready).length;
  const isReady = followReadyCount > 0;
  // Top blocker = the rejection reason with the highest share, or fallback.
  const topBlocker = rejections.breakdown[0] || null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', overflow: 'hidden' }}>
      <ConnBanner state={connectionState} />
      <KpiStrip items={[
        { label: 'Total Profiles', value: totalProfiles, color: C.text },
        { label: 'System Maturity', value: `${(adaptive.maturity * 100).toFixed(1)}%`, color: C.purple },
        { label: 'Phase 1 / 2 / 3', value: `${totals.phase1 ?? 0}/${totals.phase2 ?? 0}/${totals.phase3 ?? 0}`, color: C.blue },
        { label: 'Sample Eff.', value: sampleEff.ratio != null ? `${(sampleEff.ratio * 100).toFixed(1)}%` : '—', color: C.amber, sub: sampleEff.positions_resolved_total ? `${sampleEff.positions_resolved_total} / ${sampleEff.trades_observed_total}` : '' },
        { label: 'Cat. Coverage', value: latestCovPct != null ? `${(latestCovPct * 100).toFixed(0)}%` : '—', color: latestCovPct >= 0.8 ? C.green : latestCovPct >= 0.5 ? C.amber : C.red },
        { label: 'Decisions 24h', value: decisions24h.total ?? 0, color: C.text },
        { label: 'Edges Conf.', value: totals.edges_confirmed ?? 0, color: C.green, sub: `${totals.edges_total ?? 0} total` },
        { label: 'Follow Ready', value: followReadyCount, color: isReady ? C.green : C.amber },
      ]} />

      <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'grid', gap: 14, gridTemplateColumns: 'repeat(12, 1fr)', gridAutoRows: 'min-content', width: '100%', alignContent: 'start' }}>

        {/* ── HERO — readiness banner spans full width ───────────────────── */}
        <div style={{
          gridColumn: 'span 12',
          background: isReady ? 'rgba(40,168,78,0.06)' : 'rgba(232,160,32,0.05)',
          border: `1px solid ${isReady ? 'rgba(40,168,78,0.25)' : 'rgba(232,160,32,0.2)'}`,
          padding: '14px 18px',
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.3fr) minmax(0, 1.4fr) minmax(0, 1fr)',
          gap: 28,
          alignItems: 'center',
        }}>
          {/* Status (left) */}
          <div style={{ minWidth: 0 }}>
            <div style={{ ...S.label, marginBottom: 4 }}>Trading readiness</div>
            <div style={{
              fontSize: 18, fontWeight: 700,
              color: isReady ? C.green : C.amber,
              letterSpacing: '-0.01em', lineHeight: 1.2,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>
              {isReady ? `${followReadyCount} leader${followReadyCount > 1 ? 's' : ''} ready to FOLLOW` : 'Bootstrapping — not trading'}
            </div>
            <div style={{ fontSize: 10, color: C.dim2, marginTop: 4, fontFamily: 'monospace' }}>
              {totalProfiles} profiles · {totals.positions_resolved_total ?? 0} resolved
            </div>
          </div>

          {/* Top blocker (middle) */}
          <div style={{ minWidth: 0 }}>
            <div style={{ ...S.label, marginBottom: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              Top blocker{topBlocker ? ` · ${topBlocker.reason}` : ''}
            </div>
            {topBlocker ? (
              <>
                <div style={{ fontSize: 13, color: C.text, fontWeight: 600, fontFamily: 'monospace', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {topBlocker.pct}% of skips · {topBlocker.count} dec ({topBlocker.uniq_leaders}L)
                </div>
                <div style={{ marginTop: 6, height: 4, background: 'rgba(255,255,255,0.05)' }}>
                  <div style={{ width: `${topBlocker.pct}%`, height: '100%', background: topBlocker.pct > 60 ? C.red : C.amber }} />
                </div>
              </>
            ) : (
              <div style={{ fontSize: 12, color: C.dim2 }}>No SKIP decisions in the last hour.</div>
            )}
          </div>

          {/* Velocity (right) — now in their own labeled grid that won't overflow */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 14, minWidth: 0 }}>
            {[
              { label: 'Trades 24h', value: totals.trades_total ?? 0, color: C.blue },
              { label: 'Resolved 24h', value: totals.positions_resolved_total ?? 0, color: C.green },
              { label: 'Decisions 24h', value: decisions24h.total ?? 0, color: C.text },
            ].map(s => (
              <div key={s.label} style={{ minWidth: 0, textAlign: 'right' }}>
                <div style={{ ...S.label, fontSize: 9 }}>{s.label}</div>
                <div style={{ color: s.color, fontSize: 16, fontWeight: 700, marginTop: 2, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>
                  {s.value.toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ── ROW 1 — phase pipeline (4) · trajectory (4) · closest to follow (4) ── */}
        <Panel title="Training pipeline" span={4}
          info="Each leader auto-promotes when their resolved-position count crosses the next threshold. Phase 2 fits run nightly, Phase 3 weekly.">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
            <PhaseCard phase={1} count={totals.phase1 ?? 0} target={100} label="Beta-Binomial" desc="0–99 resolved" color={C.blue} />
            <PhaseCard phase={2} count={totals.phase2 ?? 0} target={500} label="Bayesian LogReg" desc="100–499 resolved" color={C.amber} />
            <PhaseCard phase={3} count={totals.phase3 ?? 0} target={null} label="LightGBM + Platt" desc="500+ resolved" color={C.green} />
          </div>
          {phaseEta.length > 0 && (
            <>
              <div style={{ ...S.label, marginTop: 14, marginBottom: 6, fontSize: 9 }}>Phase progression ETA · top {phaseEta.length} by velocity</div>
              <div style={{ display: 'grid', gap: 5, fontSize: 10 }}>
                {phaseEta.map(p => {
                  const pct = p.target ? Math.min(100, (p.resolved / p.target) * 100) : 100;
                  return (
                    <div key={p.wallet} style={{ display: 'grid', gridTemplateColumns: '90px 56px minmax(0, 1fr) 60px 50px', gap: 6, alignItems: 'center', minWidth: 0 }}>
                      <span style={{ color: C.purple, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.label}</span>
                      <Badge type={p.current_phase === 1 ? 'blue' : 'amber'} size="xs">P{p.current_phase}→P{p.current_phase + 1}</Badge>
                      <ProgressBar value={pct} max={100} height={4} color={C.purple} />
                      <span style={{ color: C.dim2, textAlign: 'right', fontFamily: 'monospace' }}>{p.resolved}/{p.target ?? '—'}</span>
                      <span style={{ color: p.eta_days != null && p.eta_days < 30 ? C.green : C.dim2, fontWeight: 600, textAlign: 'right' }}>
                        {p.eta_days != null ? `${p.eta_days}d` : '∞'}
                      </span>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </Panel>

        <Panel title="Learning trajectory · 24h" span={4}
          info="Hourly buckets of trade ingestion, position closure, and active follower edges.">
          <div style={{ display: 'grid', gap: 10, fontSize: 11 }}>
            <SparkRow label="Trades observed"    color={C.blue}  data={tradesSpark}    total={totals.trades_total ?? 0} />
            <SparkRow label="Positions resolved" color={C.green} data={positionsSpark} total={totals.positions_resolved_total ?? 0} />
            <SparkRow label="Active edges"       color={C.amber} data={edgesSpark}     total={totals.edges_total ?? 0} />
          </div>
          <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.border}` }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 4 }}>
              <span style={S.label}>Decisions split (24h)</span>
              <span style={{ fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>{decisions24h.total ?? 0} total</span>
            </div>
            {decisions24h.total > 0 ? (
              <>
                <div style={{ display: 'flex', height: 8, background: 'rgba(255,255,255,0.04)', overflow: 'hidden' }}>
                  {decisions24h.by_action.map(a => (
                    <div key={a.action}
                      title={`${a.action}: ${a.count} (${(a.pct * 100).toFixed(0)}%)`}
                      style={{
                        width: `${a.pct * 100}%`,
                        background: a.action === 'follow' ? C.green : a.action === 'fade' ? C.amber : C.dim2,
                      }}
                    />
                  ))}
                </div>
                <div style={{ display: 'flex', gap: 14, marginTop: 6, fontSize: 10 }}>
                  {decisions24h.by_action.map(a => (
                    <span key={a.action} style={{ color: a.action === 'follow' ? C.green : a.action === 'fade' ? C.amber : C.dim2 }}>
                      ● {a.action} <span style={{ fontFamily: 'monospace' }}>{(a.pct * 100).toFixed(0)}%</span>
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <div style={{ fontSize: 11, color: C.dim2 }}>No decisions logged yet.</div>
            )}
          </div>
        </Panel>

        <Panel title={`Closest to FOLLOW · top ${Math.min(6, followReady.length)}`} span={4}
          info={`Adaptive gates @ system maturity ${(adaptive.maturity * 100).toFixed(1)}% require ${(adaptive.values.FOLLOW_MIN_TRADES ?? 25).toFixed(0)} trades · ${(adaptive.values.FOLLOW_MIN_FOLLOWERS ?? 3).toFixed(0)} confirmed followers.`}>
          {followReady.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No leaders profiled yet.</div>
          ) : (
            <div style={{ display: 'grid', gap: 1, background: C.border }}>
              {followReady.map((r, i) => (
                <FollowReadyRow key={i} row={r} />
              ))}
            </div>
          )}
        </Panel>

        {/* ── ROW 2 — close mix (4) · holding (4) · category coverage (4) ── */}
        <Panel title="Position close methods · 30d" span={4}
          info="Sell vs merge vs resolution distribution. Merge detection validates the bot tracks complementary-token exits — see CLAUDE.md §14.">
          {closeMethods.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No resolved positions yet.</div>
          ) : (
            <div style={{ display: 'grid', gap: 8, fontSize: 11 }}>
              {closeMethods.map(cm => {
                const color = cm.method === 'sell' ? C.green : cm.method === 'merge' ? C.purple : cm.method === 'resolution' ? C.blue : C.dim2;
                return (
                  <div key={cm.method}>
                    <div style={{ display: 'grid', gridTemplateColumns: 'auto auto 1fr auto', gap: 8, alignItems: 'center', marginBottom: 3 }}>
                      <span style={{ color, textTransform: 'uppercase', fontWeight: 700, fontSize: 10, letterSpacing: '0.06em' }}>{cm.method}</span>
                      <span style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 10 }}>{cm.count}</span>
                      <span /> {/* spacer */}
                      <span style={{ color: C.dim2, fontSize: 10, fontFamily: 'monospace' }}>{(cm.pct * 100).toFixed(1)}% · med {fmtSec(cm.median_holding_s)}</span>
                    </div>
                    <ProgressBar value={cm.pct * 100} max={100} color={color} height={5} />
                  </div>
                );
              })}
            </div>
          )}
        </Panel>

        <Panel title="Holding period · by phase" span={4}
          info="Median + p90 of the position holding period per phase. Confirms strategy classifications: scalper < 1h, swing 1d–2w, holder > 2w.">
          {holdingByPhase.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No data.</div>
          ) : (
            <div style={{ display: 'grid', gap: 10, fontSize: 11 }}>
              {holdingByPhase.map(hp => {
                const color = hp.phase === 3 ? C.green : hp.phase === 2 ? C.amber : C.blue;
                const maxP90 = Math.max(...holdingByPhase.map(x => x.p90_s), 1);
                return (
                  <div key={hp.phase}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                      <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <Badge type={hp.phase === 3 ? 'green' : hp.phase === 2 ? 'amber' : 'blue'} size="xs">P{hp.phase}</Badge>
                        <span style={{ color: C.dim2, fontSize: 10 }}>{hp.count} resolved</span>
                      </span>
                      <span style={{ color, fontFamily: 'monospace', fontSize: 10 }}>med <b>{fmtSec(hp.median_s)}</b> · p90 {fmtSec(hp.p90_s)}</span>
                    </div>
                    {/* Stacked bar: median + p90 */}
                    <div style={{ position: 'relative', height: 6, background: 'rgba(255,255,255,0.04)' }}>
                      <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${(hp.p90_s / maxP90) * 100}%`, background: color, opacity: 0.25 }} />
                      <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${(hp.median_s / maxP90) * 100}%`, background: color }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>

        <Panel title="Category coverage · 14d" span={4}
          info="Stacked bars: full bar = total trades volume that day, colored portion = trades with a known category, grey = still 'unknown'. Re-categorizer runs every registry cycle (≈30 min).">
          {catCoverage.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No data.</div>
          ) : (() => {
              const maxVol = Math.max(...catCoverage.map(d => d.total), 1);
              const totalKnown = catCoverage.reduce((s, d) => s + (d.known || 0), 0);
              const totalAll = catCoverage.reduce((s, d) => s + (d.total || 0), 0);
              const periodPct = totalAll > 0 ? totalKnown / totalAll : 0;
              return (
                <>
                  {/* Stacked bars: encode both volume (height) and coverage (color split). */}
                  <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 90, marginBottom: 8 }}>
                    {catCoverage.map(d => {
                      const heightPct = Math.max(3, (d.total / maxVol) * 100);
                      const knownPct = d.total > 0 ? (d.known / d.total) * 100 : 0;
                      const knownColor = d.pct >= 0.8 ? C.green : d.pct >= 0.5 ? C.amber : C.red;
                      return (
                        <div key={d.day}
                          title={`${d.day}\n${d.total} trades · ${d.known} known (${(d.pct * 100).toFixed(1)}%) · ${d.total - d.known} unknown`}
                          style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', minWidth: 0, height: '100%', cursor: 'help' }}>
                          {/* Stacked: known (bottom, colored) + unknown (top, grey) */}
                          <div style={{ width: '100%', height: `${heightPct}%`, display: 'flex', flexDirection: 'column-reverse', minHeight: 3 }}>
                            <div style={{ height: `${knownPct}%`, background: knownColor, transition: 'height 0.3s' }} />
                            <div style={{ height: `${100 - knownPct}%`, background: 'rgba(120,120,150,0.35)', transition: 'height 0.3s' }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  {/* Footer: x-axis dates + period summary */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: C.dim2, fontFamily: 'monospace', marginBottom: 8 }}>
                    <span>{catCoverage[0]?.day}</span>
                    <span>{catCoverage[catCoverage.length - 1]?.day}</span>
                  </div>
                  {/* Numeric summary */}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6, fontSize: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
                    <div>
                      <div style={{ ...S.label, fontSize: 9 }}>Today</div>
                      <div style={{ color: latestCovPct >= 0.8 ? C.green : latestCovPct >= 0.5 ? C.amber : C.red, fontFamily: 'monospace', fontSize: 13, fontWeight: 700, marginTop: 2 }}>
                        {latestCovPct != null ? `${(latestCovPct * 100).toFixed(0)}%` : '—'}
                      </div>
                      <div style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 9 }}>
                        {catCoverage[catCoverage.length - 1]?.known || 0} / {catCoverage[catCoverage.length - 1]?.total || 0}
                      </div>
                    </div>
                    <div>
                      <div style={{ ...S.label, fontSize: 9 }}>14d avg</div>
                      <div style={{ color: periodPct >= 0.8 ? C.green : periodPct >= 0.5 ? C.amber : C.red, fontFamily: 'monospace', fontSize: 13, fontWeight: 700, marginTop: 2 }}>
                        {`${(periodPct * 100).toFixed(0)}%`}
                      </div>
                      <div style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 9 }}>
                        {totalKnown.toLocaleString()} / {totalAll.toLocaleString()}
                      </div>
                    </div>
                    <div>
                      <div style={{ ...S.label, fontSize: 9 }}>Peak vol</div>
                      <div style={{ color: C.text, fontFamily: 'monospace', fontSize: 13, fontWeight: 700, marginTop: 2 }}>{maxVol.toLocaleString()}</div>
                      <div style={{ color: C.dim2, fontFamily: 'monospace', fontSize: 9 }}>trades / day</div>
                    </div>
                  </div>
                </>
              );
            })()}
        </Panel>

        {/* ── ROW 3 — rejections (6) · falcon (3) · adaptive cold/mature (3) compact ── */}
        <Panel title={`Rejections last hour · ${rejections.total} total`} span={6}
          info="Why the decision engine declined to trade. Heavy 'insufficient_data' is normal during bootstrap; persistent 'low_confidence' once leaders mature signals model conservatism.">
          {rejections.breakdown.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No SKIP decisions logged this window.</div>
          ) : (
            <div style={{ display: 'grid', gap: 6 }}>
              {rejections.breakdown.map((r, i) => (
                <div key={i} style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 200px) minmax(0, 1fr) 50px 70px', gap: 8, alignItems: 'center', fontSize: 11, minWidth: 0 }}>
                  <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.reason}</span>
                  <ProgressBar value={r.pct} max={100} color={r.pct > 60 ? C.red : r.pct > 30 ? C.amber : C.blue} height={6} />
                  <span style={{ color: C.dim2, fontSize: 10, textAlign: 'right' }}>{r.pct}%</span>
                  <span style={{ color: C.text, fontFamily: 'monospace', fontSize: 10, textAlign: 'right' }}>{r.count} ({r.uniq_leaders}L)</span>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Falcon enrichment" span={3}
          info="Time from leader.first_seen → wallet360 populated. P90 spikes typically mean Falcon hasn't indexed the wallet yet — repeated misses get stamped 'falcon_no_data'.">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, fontSize: 11 }}>
            <Stat l="Median lag" v={fmtSec(enrichLag.median_s)} c={C.blue} />
            <Stat l="P90 lag" v={fmtSec(enrichLag.p90_s)} c={C.amber} />
            <Stat l="Enriched" v={enrichLag.enriched ?? 0} c={C.green} />
            <Stat l="Pending" v={enrichLag.pending ?? 0} c={enrichLag.pending > 0 ? C.amber : C.dim2} />
          </div>
        </Panel>

        <Panel title="Sample efficiency" span={3}
          info="positions_resolved / trades_observed across all profiles. Low ratio = lots of trade activity but few reconstructable cycles (often means partial position tracking or merge exits the bot misses).">
          <div style={{ fontSize: 28, fontWeight: 700, color: sampleEff.ratio >= 0.1 ? C.green : sampleEff.ratio >= 0.03 ? C.amber : C.red, letterSpacing: '-0.02em', lineHeight: 1 }}>
            {sampleEff.ratio != null ? `${(sampleEff.ratio * 100).toFixed(1)}%` : '—'}
          </div>
          <div style={{ marginTop: 6, fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>
            {(sampleEff.positions_resolved_total ?? 0).toLocaleString()} resolved / {(sampleEff.trades_observed_total ?? 0).toLocaleString()} trades
          </div>
          <div style={{ marginTop: 8, height: 6, background: 'rgba(255,255,255,0.04)' }}>
            <div style={{ width: `${Math.min(100, (sampleEff.ratio ?? 0) * 1000)}%`, height: '100%', background: sampleEff.ratio >= 0.1 ? C.green : sampleEff.ratio >= 0.03 ? C.amber : C.red, transition: 'width 0.4s' }} />
          </div>
          <div style={{ marginTop: 6, fontSize: 9, color: C.dim2 }}>{sampleEff.active_profiles ?? 0} active profiles</div>
        </Panel>

        {/* ── ROW 4 — adaptive thresholds full width ────────────────────────── */}
        <Panel title={`Adaptive thresholds · maturity ${(adaptive.maturity * 100).toFixed(1)}%`} span={12}
          info="Each gate interpolates between cold-start (more permissive) and mature (stricter) values based on accumulated profiles + resolutions + confirmed edges.">
          {Object.keys(adaptive.values).length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>No adaptive thresholds reported yet.</div>
          ) : (
            <div style={{ display: 'grid', gap: 5, gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', columnGap: 24, fontSize: 10 }}>
              {Object.entries(adaptive.values).map(([name, val]) => {
                const range = adaptive.ranges?.[name] || { cold: val, mature: val };
                const span2 = (range.mature - range.cold) || 1;
                const pct = ((val - range.cold) / span2) * 100;
                return (
                  <div key={name} style={{ display: 'grid', gridTemplateColumns: '210px 50px minmax(0, 1fr) 50px', gap: 8, alignItems: 'center', minWidth: 0 }}>
                    <span style={{ color: C.text, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
                    <span style={{ color: C.blue, textAlign: 'right', fontFamily: 'monospace' }}>{Number(range.cold).toFixed(2)}</span>
                    <ProgressBar value={pct} max={100} color={C.purple} height={5} sublabel={Number(val).toFixed(2)} />
                    <span style={{ color: C.green, textAlign: 'right', fontFamily: 'monospace' }}>{Number(range.mature).toFixed(2)}</span>
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

// Panel: minWidth:0 lets long content wrap inside grid cells; overflow:hidden
// prevents stray descriptions from leaking past the border. The optional
// `info` prop attaches a "(?)" hover tooltip — keeps the panel visually
// quiet while still surfacing context on demand.
const Panel = ({ title, info, children, span }) => (
  <div style={{
    background: C.panel2, border: `1px solid ${C.border}`, padding: 12,
    minWidth: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    gridColumn: span ? `span ${span}` : undefined,
  }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10, paddingBottom: 6, borderBottom: `1px solid ${C.border}` }}>
      <span style={{ ...S.label, flex: 1 }}>{title}</span>
      {info && (
        <span title={info} style={{
          color: C.dim2, fontSize: 9, cursor: 'help',
          border: `1px solid ${C.border2}`, borderRadius: '50%',
          width: 13, height: 13, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', lineHeight: 1,
        }}>?</span>
      )}
    </div>
    <div style={{ minWidth: 0, overflow: 'hidden' }}>{children}</div>
  </div>
);

// Fluid sparkline row — the sparkline expands with the container and the total
// stays right-aligned without ever getting clipped.
const SparkRow = ({ label, color, data, total }) => (
  <div style={{ display: 'grid', gridTemplateColumns: '120px minmax(0, 1fr) auto', gap: 8, alignItems: 'center', minWidth: 0 }}>
    <span style={{ color: C.dim2, fontSize: 11, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</span>
    <Sparkline data={data} color={color} width={200} height={20} fluid />
    <span style={{ color: color, textAlign: 'right', fontWeight: 700, fontSize: 13, whiteSpace: 'nowrap', fontFamily: 'monospace' }}>
      {total.toLocaleString()}
    </span>
  </div>
);

// ─── WALLET GRAPH (now hosts the Wallet Scanner table view too) ─────────────
const WalletGraph = () => {
  const { snapshot, connectionState } = useLiveStore();
  const wg     = snapshot?.wallet_graph || { nodes: [], edges: [], stats: {} };
  const stats  = wg.stats || {};
  const [selected, setSelected] = useStateT(null);
  // All persisted via usePersistedState so reload restores the user's filter
  // and sort context. Only short-lived UI state (search box, hover, zoom/pan)
  // stays in-memory.
  const [view, setView]               = usePersistedState('wg.view', 'graph');
  const [sortKey, setSortKey]         = usePersistedState('wg.sortKey', 'readiness');
  const [sortDir, setSortDir]         = usePersistedState('wg.sortDir', 'desc');
  const [search,  setSearch]          = useStateT('');
  const [activeOnly, setActiveOnly]   = usePersistedState('wg.activeOnly', true);

  // ── Graph interactivity state ────────────────────────────────────────────
  // Zoom/pan/hover stay in-memory (intentionally non-persistent so reload
  // resets the camera). Filter intent (phaseMin, confirmedOnly) IS persisted.
  const [zoom, setZoom] = useStateT(1);
  const [pan, setPan] = useStateT({ x: 0, y: 0 });
  const [hover, setHover] = useStateT(null);          // {node, x, y} or null
  const [graphSearch, setGraphSearch] = useStateT(''); // search inside graph view
  const [phaseMin, setPhaseMin] = usePersistedState('wg.phaseMin', 1);
  const [confirmedOnly, setConfirmedOnly] = usePersistedState('wg.confirmedOnly', false);
  const isPanning = React.useRef(false);
  const panStart = React.useRef(null);
  const svgRef = React.useRef(null);
  const resetView = () => { setZoom(1); setPan({ x: 0, y: 0 }); };
  const clampZoom = (z) => Math.max(0.3, Math.min(4, z));
  const adaptive  = snapshot?.adaptive_thresholds?.values || {};
  const followMin = adaptive.FOLLOW_MIN_TRADES || 25;
  const fadeMin   = adaptive.FADE_MIN_RESOLVED || 25;

  const wallets = wg.nodes.filter(n => n.role === 'leader');
  const enrichedWallets = useMemoT(() => wallets.map(w => {
    const tradesProgress = Math.min(1, (w.trades_observed || 0) / followMin);
    const resolvedProgress = Math.min(1, (w.positions_resolved || 0) / fadeMin);
    const maturity = Math.max(0, Math.min(1, w.maturity || 0));
    const readiness = (tradesProgress * 0.4 + resolvedProgress * 0.4 + maturity * 0.2);
    return {
      ...w, readiness,
      _rb: {
        trades:   tradesProgress * 0.4,
        resolved: resolvedProgress * 0.4,
        maturity: maturity * 0.2,
      },
    };
  }), [wallets, followMin, fadeMin]);

  // Drop the Strategy column when every leader has the same classification.
  const allSameStrategy = useMemoT(() => {
    const set = new Set(enrichedWallets.map(w => w.classification).filter(Boolean));
    return set.size <= 1;
  }, [enrichedWallets]);

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

  const filteredWallets = useMemoT(() => {
    let arr = sortedWallets;
    if (activeOnly) arr = arr.filter(w => (w.trades_24h || 0) > 0 || (w.positions_resolved || 0) > 0);
    if (search) arr = arr.filter(w => (w.id || '').toLowerCase().includes(search.toLowerCase()));
    return arr;
  }, [sortedWallets, search, activeOnly]);

  const setSort = (k) => {
    if (sortKey === k) setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    else { setSortKey(k); setSortDir('desc'); }
  };
  const sortIndicator = (k) => sortKey === k ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '';

  // Layout: leaders on outer ring, followers placed near the angular barycenter
  // of the leaders they're connected to (so siblings cluster around their leader).
  const layout = useMemoT(() => {
    const W = 820, H = 560, cx = W / 2, cy = H / 2;
    const positions = {};
    const leaderAngle = {};
    const leaders   = wg.nodes.filter(n => n.role === 'leader');
    const followers = wg.nodes.filter(n => n.role === 'follower');

    leaders.forEach((n, i) => {
      const a = (i / Math.max(1, leaders.length)) * Math.PI * 2 - Math.PI / 2;
      leaderAngle[n.id] = a;
      const r = 230;
      positions[n.id] = { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r };
    });

    followers.forEach((n, i) => {
      const connected = wg.edges
        .filter(e => e.source === n.id || e.target === n.id)
        .map(e => e.source === n.id ? e.target : e.source)
        .filter(id => leaderAngle[id] !== undefined);
      let a;
      if (connected.length > 0) {
        // Angular barycenter via vector sum (handles the −π/π wraparound correctly).
        const sx = connected.reduce((s, id) => s + Math.cos(leaderAngle[id]), 0);
        const sy = connected.reduce((s, id) => s + Math.sin(leaderAngle[id]), 0);
        a = Math.atan2(sy, sx);
      } else {
        a = (i / Math.max(1, followers.length)) * Math.PI * 2;
      }
      const jitter = ((i % 9) - 4) * 0.05;
      const r = 95 + (i % 5) * 20;
      positions[n.id] = { x: cx + Math.cos(a + jitter) * r, y: cy + Math.sin(a + jitter) * r };
    });
    return { positions, W, H, leaderAngle };
  }, [wg.nodes, wg.edges]);

  const sel = selected ? wg.nodes.find(n => n.id === selected) : null;
  const selEdges = selected ? wg.edges.filter(e => e.source === selected || e.target === selected) : [];

  // Cross-module nav listener: another tab can ask the graph to focus on a
  // specific wallet via window.PoybotNav.selectWallet(addr). When that fires,
  // switch to the graph view and select the node.
  useEffectT(() => {
    const handler = (e) => {
      const w = e.detail?.wallet;
      if (!w) return;
      setSelected(w);
      if (e.detail?.view === 'list') setView('list');
      else setView('graph');
    };
    window.addEventListener('pmi:select-wallet', handler);
    return () => window.removeEventListener('pmi:select-wallet', handler);
  }, []);

  // Publish current selection to the global nav context so the topbar
  // breadcrumb can surface "wallet: 0xabc…" without prop drilling.
  useEffectT(() => {
    if (!selected) { window.PoybotNav?.clearContext?.(); return; }
    window.PoybotNav?.setContext?.({
      type: 'wallet',
      id: selected,
      label: selected.slice(0, 6) + '…' + selected.slice(-4),
    });
  }, [selected]);

  // Per-wallet market drilldown — fetched lazily whenever selection changes.
  // Cancellation guard via a ref-style closure: if the user clicks another
  // node before the request lands, we drop the late response.
  const [walletMarkets, setWalletMarkets] = useStateT(null);
  const [walletMarketsLoading, setWalletMarketsLoading] = useStateT(false);
  // Profile drill-down — separate fetch, separate state, fired in parallel.
  const [walletProfile, setWalletProfile] = useStateT(null);
  const [walletProfileLoading, setWalletProfileLoading] = useStateT(false);
  useEffectT(() => {
    if (!selected) { setWalletMarkets(null); setWalletProfile(null); return; }
    let cancelled = false;
    setWalletMarketsLoading(true);
    setWalletProfileLoading(true);
    const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
    fetch(`${base}/api/wallet/${selected}/markets?window_days=30&limit=15`)
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(d => { if (!cancelled) setWalletMarkets(d); })
      .catch(e => { if (!cancelled) { console.warn('[WalletGraph] markets fetch failed', e.message); setWalletMarkets({ markets: [], category_breakdown: [], total_trades: 0, distinct_markets: 0, _error: true }); } })
      .finally(() => { if (!cancelled) setWalletMarketsLoading(false); });
    fetch(`${base}/api/wallet/${selected}/profile`)
      .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
      .then(d => { if (!cancelled) setWalletProfile(d); })
      .catch(e => { if (!cancelled) { console.warn('[WalletGraph] profile fetch failed', e); setWalletProfile({ _error: true }); } })
      .finally(() => { if (!cancelled) setWalletProfileLoading(false); });
    return () => { cancelled = true; };
  }, [selected]);

  // Category filter (toolbar) — multi-select chips backed by the
  // top_categories present in current leader nodes. Persisted so a deep
  // analysis session survives reload.
  const [categoryFilter, setCategoryFilter] = usePersistedState('wg.categoryFilter', []);
  const availableCategories = useMemoT(() => {
    const set = new Set();
    wg.nodes.forEach(n => (n.top_categories || []).forEach(c => c.category && set.add(c.category)));
    return Array.from(set).sort();
  }, [wg.nodes]);
  const toggleCategory = (c) => setCategoryFilter(prev =>
    prev.includes(c) ? prev.filter(x => x !== c) : [...prev, c]
  );

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
          <>
            <button onClick={() => setActiveOnly(!activeOnly)} title="Show only wallets with 24h activity or resolved positions" style={{
              background: activeOnly ? 'rgba(40,168,78,0.12)' : 'transparent',
              border: `1px solid ${activeOnly ? C.green : C.border2}`,
              color: activeOnly ? C.green : C.dim2,
              padding: '3px 10px', fontSize: 10, cursor: 'pointer', marginLeft: 4, whiteSpace: 'nowrap',
            }}>{activeOnly ? '● Active only' : '○ Show all'}</button>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter by wallet…"
              style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '4px 10px', fontSize: 11, flex: 1, maxWidth: 280, outline: 'none', marginLeft: 8 }} />
          </>
        )}
        <span style={{ fontSize: 10, color: C.dim2, marginLeft: 'auto' }}>
          {view === 'list' ? `${filteredWallets.length} / ${enrichedWallets.length} wallets` : `${stats.leaders ?? 0} leaders · ${stats.edges_total ?? 0} edges`}
        </span>
      </div>

      {view === 'list' ? (
        <div style={{ flex: 1, overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.panel, zIndex: 1 }}>
              <tr>
                <TH>Wallet</TH>
                <TH>Phase</TH>
                {!allSameStrategy && <TH>Strategy</TH>}
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('falcon_score')}>Falcon{sortIndicator('falcon_score')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_24h')}>24h{sortIndicator('trades_24h')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('trades_observed')}>Trades{sortIndicator('trades_observed')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('positions_resolved')}>Resolved{sortIndicator('positions_resolved')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('win_rate')}>Win%{sortIndicator('win_rate')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('pnl_total')}>PnL{sortIndicator('pnl_total')}</th>
                <th style={{ ...S.label, fontWeight: 700, padding: '5px 10px', cursor: 'pointer', borderBottom: `1px solid ${C.border2}` }} onClick={() => setSort('readiness')}>Readiness{sortIndicator('readiness')}</th>
                <TH>Top Categories (30d)</TH>
                <TH>Last Action</TH>
              </tr>
            </thead>
            <tbody>
              {filteredWallets.length === 0 && (
                <tr><td colSpan={allSameStrategy ? 11 : 12} style={{ padding: '24px', color: C.dim2, textAlign: 'center', fontSize: 11 }}>{snapshot ? (activeOnly ? 'No active leaders — toggle "Show all" to see the full registry.' : 'No leader wallets profiled yet.') : 'Waiting for data…'}</td></tr>
              )}
              {filteredWallets.map((w) => {
                const cats = (w.top_categories || []).slice(0, 3);
                const catTooltip = cats.length
                  ? cats.map(c => `${c.category}: ${(c.pct * 100).toFixed(0)}% (${c.trades})`).join('\n')
                  : 'No category data in last 30d';
                const catLabel = cats.length === 0 ? '—'
                  : cats.slice(0, 2).map(c => `${c.category} ${(c.pct * 100).toFixed(0)}%`).join(' · ')
                    + (cats.length > 2 ? ` +${cats.length - 2}` : '');
                return (
                <tr key={w.id} style={{ cursor: 'pointer' }} onClick={() => { setSelected(w.id); setView('graph'); }}>
                  <TD style={{ color: C.purple, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{w.label}</TD>
                  <TD><Badge type={w.phase >= 3 ? 'green' : w.phase === 2 ? 'amber' : 'blue'}>P{w.phase}</Badge></TD>
                  {!allSameStrategy && <TD style={{ color: C.dim2 }}>{w.classification || '—'}</TD>}
                  <TD style={{ color: C.amber, fontFamily: 'monospace' }}>{(w.falcon_score || 0).toFixed(2)}</TD>
                  <TD style={{ color: w.trades_24h > 0 ? C.green : C.dim2, fontWeight: 600 }}>{w.trades_24h || 0}</TD>
                  <TD style={{ color: C.text }}>{w.trades_observed || 0}</TD>
                  <TD style={{ color: C.text }}>{w.positions_resolved || 0}</TD>
                  <TD style={{ color: w.win_rate != null ? (w.win_rate >= 0.5 ? C.green : C.red) : C.dim2 }}>
                    {w.win_rate != null ? `${(w.win_rate * 100).toFixed(0)}%` : '—'}
                  </TD>
                  <TD style={{ color: pnlColor(w.pnl_total), fontWeight: 600 }}>{fmtPnl(w.pnl_total)}</TD>
                  <TD style={{ minWidth: 120 }}>
                    <span title={`trades (target ${followMin}): +${w._rb.trades.toFixed(2)} / 0.40\nresolved (target ${fadeMin}): +${w._rb.resolved.toFixed(2)} / 0.40\nmaturity: +${w._rb.maturity.toFixed(2)} / 0.20`}>
                      <ScoreBar value={w.readiness || 0} />
                    </span>
                  </TD>
                  <TD style={{ color: cats.length ? C.text : C.dim2, whiteSpace: 'nowrap', fontSize: 10, maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    <span title={catTooltip}>{catLabel}</span>
                  </TD>
                  <TD>{w.last_action ? <Badge type={actionType(w.last_action)}>{w.last_action}</Badge> : <span style={{ color: C.dim2 }}>—</span>}</TD>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: sel ? 'minmax(0, 2fr) minmax(280px, 1fr)' : '1fr', overflow: 'hidden' }}>

        {/* Graph SVG with zoom/pan/filter/search/hover */}
        <div style={{ overflow: 'hidden', borderRight: sel ? `1px solid ${C.border}` : 'none', padding: 0, display: 'flex', flexDirection: 'column', position: 'relative' }}>
          {wg.nodes.length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 11, padding: 40, textAlign: 'center' }}>
              {snapshot ? 'No leaders profiled yet — graph will populate as leaders accumulate trades.' : 'Waiting for data…'}
            </div>
          ) : (
            <>
              {/* Toolbar */}
              <div style={{ position: 'absolute', top: 8, left: 8, right: 8, zIndex: 5, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', pointerEvents: 'none' }}>
                <div style={{ display: 'flex', gap: 4, pointerEvents: 'auto' }}>
                  <button onClick={() => setZoom(z => clampZoom(z * 1.25))} title="Zoom in"
                    style={{ background: C.panel, border: `1px solid ${C.border2}`, color: C.text, padding: '3px 9px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace' }}>+</button>
                  <button onClick={() => setZoom(z => clampZoom(z / 1.25))} title="Zoom out"
                    style={{ background: C.panel, border: `1px solid ${C.border2}`, color: C.text, padding: '3px 9px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace' }}>−</button>
                  <button onClick={resetView} title="Reset view (1:1)"
                    style={{ background: C.panel, border: `1px solid ${C.border2}`, color: C.dim2, padding: '3px 9px', fontSize: 10, cursor: 'pointer' }}>⟳</button>
                  <span style={{ color: C.dim2, fontSize: 10, alignSelf: 'center', marginLeft: 4, fontFamily: 'monospace' }}>{(zoom * 100).toFixed(0)}%</span>
                </div>
                <div style={{ display: 'flex', gap: 4, pointerEvents: 'auto' }}>
                  {[1, 2, 3].map(p => (
                    <button key={p} onClick={() => setPhaseMin(p)} title={`Show only Phase ≥ ${p}`}
                      style={{
                        background: phaseMin === p ? 'rgba(120,85,192,0.18)' : C.panel,
                        border: `1px solid ${phaseMin === p ? C.purple : C.border2}`,
                        color: phaseMin === p ? C.purple : C.dim2,
                        padding: '3px 8px', fontSize: 10, cursor: 'pointer',
                      }}>P≥{p}</button>
                  ))}
                  <button onClick={() => setConfirmedOnly(!confirmedOnly)} title="Show only confirmed edges"
                    style={{
                      background: confirmedOnly ? 'rgba(40,168,78,0.15)' : C.panel,
                      border: `1px solid ${confirmedOnly ? C.green : C.border2}`,
                      color: confirmedOnly ? C.green : C.dim2,
                      padding: '3px 8px', fontSize: 10, cursor: 'pointer',
                    }}>{confirmedOnly ? '✓ confirmed' : '○ all edges'}</button>
                </div>
                {availableCategories.length > 0 && (
                  <div style={{ display: 'flex', gap: 3, pointerEvents: 'auto', flexWrap: 'wrap', maxWidth: 360 }}>
                    {availableCategories.map(cat => {
                      const active = categoryFilter.includes(cat);
                      return (
                        <button key={cat} onClick={() => toggleCategory(cat)}
                          title={`Toggle ${cat} filter`}
                          style={{
                            background: active ? 'rgba(61,125,200,0.18)' : C.panel,
                            border: `1px solid ${active ? C.blue : C.border2}`,
                            color: active ? C.blue : C.dim2,
                            padding: '3px 7px', fontSize: 9, cursor: 'pointer',
                            textTransform: 'lowercase',
                          }}>{cat}</button>
                      );
                    })}
                    {categoryFilter.length > 0 && (
                      <button onClick={() => setCategoryFilter([])} title="Clear category filter"
                        style={{ background: 'transparent', border: 'none', color: C.dim2, fontSize: 9, cursor: 'pointer', padding: '3px 4px' }}>
                        ✕ clear
                      </button>
                    )}
                  </div>
                )}
                <input value={graphSearch} onChange={e => setGraphSearch(e.target.value)} placeholder="Search wallet…"
                  style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '3px 8px', fontSize: 10, width: 150, outline: 'none', pointerEvents: 'auto', fontFamily: 'monospace' }} />
                <span style={{ color: C.dim2, fontSize: 9, marginLeft: 'auto', alignSelf: 'center', pointerEvents: 'none' }}>
                  scroll = zoom · drag = pan · click = inspect
                </span>
              </div>

              <svg
                ref={svgRef}
                viewBox={`0 0 ${layout.W} ${layout.H}`}
                width="100%"
                preserveAspectRatio="xMidYMid meet"
                style={{ display: 'block', flex: 1, minHeight: 0, cursor: isPanning.current ? 'grabbing' : 'grab', touchAction: 'none' }}
                onWheel={(e) => {
                  e.preventDefault();
                  const delta = e.deltaY < 0 ? 1.12 : 1 / 1.12;
                  setZoom(z => clampZoom(z * delta));
                }}
                onMouseDown={(e) => {
                  if (e.button !== 0) return;
                  // Don't pan when clicking on a node (handled in node onClick).
                  if (e.target && e.target.tagName === 'circle') return;
                  isPanning.current = true;
                  panStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
                }}
                onMouseMove={(e) => {
                  if (!isPanning.current || !panStart.current || !svgRef.current) return;
                  const rect = svgRef.current.getBoundingClientRect();
                  const scale = layout.W / rect.width;  // viewBox units per CSS pixel
                  setPan({
                    x: panStart.current.panX + (e.clientX - panStart.current.x) * scale / zoom,
                    y: panStart.current.panY + (e.clientY - panStart.current.y) * scale / zoom,
                  });
                }}
                onMouseUp={() => { isPanning.current = false; panStart.current = null; }}
                onMouseLeave={() => { isPanning.current = false; panStart.current = null; setHover(null); }}
              >
                <g transform={`translate(${layout.W / 2} ${layout.H / 2}) scale(${zoom}) translate(${-layout.W / 2 + pan.x} ${-layout.H / 2 + pan.y})`}>
                  {/* Edges (filtered) */}
                  {wg.edges.map((e, i) => {
                    if (confirmedOnly && !e.confirmed) return null;
                    const a = layout.positions[e.source];
                    const b = layout.positions[e.target];
                    if (!a || !b) return null;
                    // Apply phase filter via the source-leader (target may be follower).
                    const sourceNode = wg.nodes.find(n => n.id === e.source);
                    if (sourceNode?.role === 'leader' && (sourceNode.phase || 1) < phaseMin) return null;
                    // Search highlight: dim non-matching edges.
                    const matchesSearch = !graphSearch || (e.source + e.target).toLowerCase().includes(graphSearch.toLowerCase());
                    const stroke = e.confirmed ? C.green : 'rgba(140,140,180,0.55)';
                    return (
                      <line
                        key={i}
                        x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                        stroke={stroke}
                        strokeWidth={(e.confirmed ? 2 : 0.9) / Math.max(0.5, zoom)}
                        opacity={(matchesSearch ? (e.confirmed ? 0.9 : 0.55) : 0.1)}
                        strokeDasharray={e.confirmed ? '' : `${3 / zoom} ${2 / zoom}`}
                      />
                    );
                  })}
                  {/* Nodes (filtered) */}
                  {wg.nodes.map(n => {
                    const p = layout.positions[n.id];
                    if (!p) return null;
                    // Phase filter — only filters leaders, followers stay visible.
                    if (n.role === 'leader' && (n.phase || 1) < phaseMin) return null;
                    // Category filter — leaders whose top categories don't intersect get dimmed/hidden.
                    if (categoryFilter.length && n.role === 'leader') {
                      const cats = (n.top_categories || []).map(c => c.category);
                      if (!cats.some(c => categoryFilter.includes(c))) return null;
                    }
                    const activitySize = (n.trades_24h || 0) > 0
                      ? Math.sqrt(n.trades_24h) * 1.7
                      : (n.maturity || 0) * 8;
                    const r = n.role === 'leader' ? 7 + Math.min(11, activitySize) : 4;
                    const phaseColor = n.phase === 3 ? C.green : n.phase === 2 ? C.amber : C.blue;
                    const fill = n.role === 'leader' ? phaseColor : C.purple;
                    const isSel = selected === n.id;
                    // Search match: dim non-matching nodes; keep selected always full opacity.
                    const matchesSearch = !graphSearch || n.id.toLowerCase().includes(graphSearch.toLowerCase());
                    const opacity = isSel || matchesSearch ? 0.95 : 0.18;
                    // Auto-hide labels at low zoom unless leader is interesting (Phase ≥2 / active / selected / hover / search match).
                    const isInteresting = n.role === 'leader' && (
                      (n.phase || 1) >= 2 || (n.trades_24h || 0) > 0 || isSel || hover?.node?.id === n.id || (graphSearch && matchesSearch)
                    );
                    const showLabel = isInteresting && zoom > 0.55;
                    const ang = layout.leaderAngle[n.id];
                    let lx = p.x, ly = p.y - r - 5, anchor = 'middle';
                    if (showLabel && ang !== undefined) {
                      lx = p.x + Math.cos(ang) * (r + 12);
                      ly = p.y + Math.sin(ang) * (r + 12) + 3;
                      anchor = Math.cos(ang) < -0.3 ? 'end' : Math.cos(ang) > 0.3 ? 'start' : 'middle';
                    }
                    return (
                      <g key={n.id} style={{ cursor: 'pointer' }}
                         onClick={(e) => { e.stopPropagation(); setSelected(isSel ? null : n.id); }}
                         onMouseEnter={(e) => {
                           if (!svgRef.current) return;
                           const rect = svgRef.current.getBoundingClientRect();
                           const scale = rect.width / layout.W;
                           // Convert SVG-space (p.x,p.y) → CSS px relative to container.
                           const tx = (p.x + pan.x - layout.W / 2) * zoom + layout.W / 2;
                           const ty = (p.y + pan.y - layout.H / 2) * zoom + layout.H / 2;
                           setHover({ node: n, x: tx * scale, y: ty * scale });
                         }}
                         onMouseLeave={() => setHover(null)}
                      >
                        <circle cx={p.x} cy={p.y} r={(r + (isSel ? 4 : 0)) / Math.max(0.7, zoom * 0.85)}
                          fill={fill}
                          stroke={isSel ? C.white : (hover?.node?.id === n.id ? C.text : 'transparent')}
                          strokeWidth={2 / Math.max(0.5, zoom)}
                          opacity={opacity} />
                        {showLabel && (
                          <text x={lx} y={ly} fill={isSel ? C.white : C.dim2}
                            fontSize={9 / Math.max(0.6, zoom * 0.7)}
                            textAnchor={anchor} fontFamily="monospace" opacity={opacity}>
                            {n.label}
                          </text>
                        )}
                      </g>
                    );
                  })}
                </g>
              </svg>

              {/* Hover tooltip — positioned in CSS-px overlay */}
              {hover && hover.node && (
                <div style={{
                  position: 'absolute',
                  left: Math.min(Math.max(hover.x + 14, 8), 300),
                  top: Math.min(Math.max(hover.y - 10, 8), 600),
                  background: C.panel,
                  border: `1px solid ${C.border2}`,
                  padding: '6px 10px',
                  fontSize: 10,
                  pointerEvents: 'none',
                  zIndex: 10,
                  minWidth: 180,
                  fontFamily: 'monospace',
                }}>
                  <div style={{ color: C.text, fontWeight: 600 }}>{hover.node.label}</div>
                  <div style={{ color: C.dim2, marginTop: 3, display: 'grid', gap: 2 }}>
                    <div>{hover.node.role === 'leader' ? `Leader · P${hover.node.phase}` : 'Follower'}</div>
                    {hover.node.role === 'leader' && (
                      <>
                        <div>Falcon <span style={{ color: C.amber }}>{(hover.node.falcon_score || 0).toFixed(2)}</span> · Mat <span style={{ color: C.purple }}>{(hover.node.maturity || 0).toFixed(2)}</span></div>
                        <div>{hover.node.trades_observed || 0} trades · {hover.node.positions_resolved || 0} resolved</div>
                        <div style={{ color: hover.node.trades_24h > 0 ? C.green : C.dim2 }}>{hover.node.trades_24h || 0} trades 24h</div>
                        {hover.node.top_categories && hover.node.top_categories.length > 0 && (
                          <div style={{ color: C.blue, fontSize: 9 }}>
                            {hover.node.top_categories.slice(0, 2).map(c => `${c.category} ${(c.pct * 100).toFixed(0)}%`).join(' · ')}
                          </div>
                        )}
                      </>
                    )}
                    <div style={{ color: C.dim2, fontSize: 9, marginTop: 2 }}>click to inspect</div>
                  </div>
                </div>
              )}
            </>
          )}

          <div style={{ padding: '6px 10px', fontSize: 10, color: C.dim2, display: 'flex', gap: 14, flexWrap: 'wrap', flexShrink: 0, borderTop: `1px solid ${C.border}`, background: C.panel }}>
            <span><span style={{ color: C.blue }}>●</span> phase 1</span>
            <span><span style={{ color: C.amber }}>●</span> phase 2</span>
            <span><span style={{ color: C.green }}>●</span> phase 3</span>
            <span><span style={{ color: C.purple }}>●</span> follower</span>
            <span style={{ color: C.dim2, fontStyle: 'italic' }}>node size = 24h activity</span>
            <span style={{ marginLeft: 'auto' }}>━ confirmed edge   ┄ pending</span>
          </div>
        </div>

        {/* Inspector — only render when a node is selected (otherwise the graph
            takes the full width). */}
        {sel && (
          <div style={{ overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <SectionLabel>{sel.role === 'leader' ? 'Leader' : 'Follower'} · {sel.label}</SectionLabel>
              <button onClick={() => setSelected(null)} title="Close inspector"
                style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 11, padding: '1px 8px', cursor: 'pointer', lineHeight: 1.4 }}>✕</button>
            </div>
            <div style={{ fontFamily: 'monospace', fontSize: 10, color: C.dim2, wordBreak: 'break-all' }}>{sel.id}</div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginTop: 8 }}>
              <Stat l="Falcon Score" v={(sel.falcon_score || 0).toFixed(3)} c={C.amber} />
              <Stat l="Phase" v={`P${sel.phase}`} c={sel.phase === 3 ? C.green : sel.phase === 2 ? C.amber : C.blue} />
              <Stat l="Maturity" v={(sel.maturity || 0).toFixed(3)} c={C.purple} />
              <Stat l="Trades" v={(sel.trades_observed || 0).toLocaleString()} c={C.text} />
              <Stat l="Resolved" v={(sel.positions_resolved || 0)} c={C.green} />
              <Stat l="Strategy" v={sel.classification || '—'} c={C.blue} />
            </div>

            {/* Profile drilldown — Dirichlet categories, Beta accuracy, sizing,
                wallet360 highlights. Lazy-loaded; collapsible to keep the
                drawer scannable. */}
            <WalletProfileSection
              loading={walletProfileLoading}
              profile={walletProfile}
            />

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

            {/* Markets traded by this wallet (last 30 days). Lazily fetched
                from /api/wallet/{addr}/markets on selection. */}
            <SectionLabel mb={6}>
              Markets traded (30d)
              {walletMarkets && (
                <span style={{ color: C.dim2, fontWeight: 400, marginLeft: 6, textTransform: 'none', letterSpacing: 0 }}>
                  · {walletMarkets.distinct_markets || 0} distinct · {walletMarkets.total_trades || 0} trades
                </span>
              )}
            </SectionLabel>
            {walletMarketsLoading && !walletMarkets ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>Loading…</div>
            ) : !walletMarkets || walletMarkets._error ? (
              <div style={{ color: walletMarkets?._error ? C.red : C.dim2, fontSize: 11 }}>
                {walletMarkets?._error ? 'Failed to load markets.' : 'No data.'}
              </div>
            ) : (walletMarkets.markets || []).length === 0 ? (
              <div style={{ color: C.dim2, fontSize: 11 }}>No markets traded in the last 30 days.</div>
            ) : (
              <>
                {/* Category breakdown summary */}
                {(walletMarkets.category_breakdown || []).length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 6 }}>
                    {walletMarkets.category_breakdown.slice(0, 5).map(cb => (
                      <span key={cb.category}
                        title={`${cb.trades} trades · $${(cb.volume_usdc || 0).toFixed(0)} volume`}
                        style={{ background: C.panel, padding: '2px 6px', fontSize: 9, color: C.dim2, border: `1px solid ${C.border2}` }}>
                        {cb.category} <span style={{ color: C.amber, fontWeight: 600 }}>{(cb.pct * 100).toFixed(0)}%</span>
                      </span>
                    ))}
                  </div>
                )}
                {/* Top markets table */}
                <div style={{ display: 'grid', gap: 3, fontSize: 10, maxHeight: 280, overflowY: 'auto' }}>
                  {walletMarkets.markets.map(m => {
                    const isExpired = m.end_date_iso && new Date(m.end_date_iso) < new Date();
                    return (
                      <div key={m.market_id}
                        style={{ background: C.panel, padding: '5px 7px', borderLeft: `2px solid ${m.pnl_usdc > 0 ? C.green : m.pnl_usdc < 0 ? C.red : C.border2}` }}
                        title={m.market_id}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{m.question}</span>
                          {isExpired && <span style={{ fontSize: 8, color: C.dim2, fontStyle: 'italic' }}>resolved</span>}
                        </div>
                        <div style={{ display: 'flex', gap: 8, marginTop: 2, color: C.dim2, fontSize: 9 }}>
                          <span style={{ color: C.blue }}>{m.category}</span>
                          <span>{m.n_trades} trades ({m.n_buys}b / {m.n_sells}s)</span>
                          <span>${(m.volume_usdc || 0).toFixed(0)}</span>
                          {m.resolved_positions > 0 && (
                            <span style={{ marginLeft: 'auto', color: pnlColor(m.pnl_usdc), fontWeight: 600 }}>{fmtPnl(m.pnl_usdc)}</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        )}
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

// ── WalletProfileSection ─────────────────────────────────────────────────
// Renders the rich profile drilldown returned by /api/wallet/{addr}/profile
// inside the Wallet Graph inspector drawer. Three collapsible sub-sections:
// behavioural (categories + sizing + entry), accuracy (overall + by_category),
// wallet360 (Falcon highlights). Designed for narrow ~360px drawer.
const WalletProfileSection = ({ loading, profile }) => {
  const [open, setOpen] = useStateT({ behaviour: true, accuracy: true, w360: false });
  const toggle = (k) => setOpen(o => ({ ...o, [k]: !o[k] }));

  if (loading && !profile) {
    return (
      <>
        <SectionLabel mb={6}>Profile</SectionLabel>
        <div style={{ color: C.dim2, fontSize: 11 }}>Loading profile…</div>
      </>
    );
  }
  if (!profile || profile._error) {
    return (
      <>
        <SectionLabel mb={6}>Profile</SectionLabel>
        <div style={{ color: profile?._error ? C.red : C.dim2, fontSize: 11 }}>
          {profile?._error ? 'Profile unavailable for this wallet.' : '—'}
        </div>
      </>
    );
  }

  const cats = profile.preferred_categories || [];
  const acc = profile.accuracy || { by_category: [] };
  const sizing = profile.sizing || {};
  const entry = profile.entry_patterns || {};
  const w360 = profile.wallet360 || {};
  const dec = profile.decisions_30d || {};
  const edges = profile.edges || {};

  const SubHeader = ({ k, title, count }) => (
    <div onClick={() => toggle(k)} style={{
      cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
      padding: '4px 6px', background: C.panel, marginBottom: 3,
      transition: 'background 100ms',
    }}
      onMouseEnter={e => e.currentTarget.style.background = C.panel2}
      onMouseLeave={e => e.currentTarget.style.background = C.panel}
    >
      <span style={{ color: C.dim2, fontSize: 9, width: 8 }}>{open[k] ? '▾' : '▸'}</span>
      <span style={{ ...S.label, fontSize: 9, flex: 1 }}>{title}</span>
      {count != null && <span style={{ color: C.dim2, fontSize: 9, fontFamily: 'monospace' }}>{count}</span>}
    </div>
  );

  return (
    <>
      <SectionLabel mb={6}>
        Profile
        <span style={{ color: C.dim2, fontWeight: 400, marginLeft: 6, textTransform: 'none', letterSpacing: 0, fontSize: 9 }}>
          · {edges.confirmed_followers || 0} confirmed followers · {dec.total || 0} decisions 30d
        </span>
      </SectionLabel>

      {/* Behavioural — categories, sizing, entry */}
      <SubHeader k="behaviour" title="Behavioural" count={cats.length ? `${cats.length} cats` : null} />
      {open.behaviour && (
        <div style={{ display: 'grid', gap: 8, fontSize: 10, marginBottom: 8 }}>
          {/* Preferred categories — Dirichlet */}
          {cats.length > 0 ? (
            <div style={{ display: 'grid', gap: 3 }}>
              {cats.map(c => (
                <div key={c.category} style={{ display: 'grid', gridTemplateColumns: '70px 1fr 36px', gap: 6, alignItems: 'center' }}>
                  <span style={{ color: C.blue, fontSize: 9, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.category}</span>
                  <div style={{ height: 4, background: 'rgba(255,255,255,0.05)' }}>
                    <div style={{ width: `${c.pct * 100}%`, height: '100%', background: C.blue }} />
                  </div>
                  <span style={{ color: C.text, fontSize: 9, fontFamily: 'monospace', textAlign: 'right' }}>{(c.pct * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: C.dim2, fontSize: 10 }}>No category data yet (Dirichlet uninformed).</div>
          )}
          {/* Sizing + entry */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, paddingTop: 6, borderTop: `1px solid ${C.border}` }}>
            <div>
              <div style={{ ...S.label, fontSize: 8 }}>Avg size</div>
              <div style={{ color: C.text, fontFamily: 'monospace', fontSize: 11 }}>{sizing.avg_size_usdc != null ? `$${sizing.avg_size_usdc.toFixed(0)}` : '—'}</div>
            </div>
            <div>
              <div style={{ ...S.label, fontSize: 8 }}>EWMA size</div>
              <div style={{ color: C.text, fontFamily: 'monospace', fontSize: 11 }}>{sizing.ewma_size_usdc != null ? `$${sizing.ewma_size_usdc.toFixed(0)}` : '—'}</div>
            </div>
            <div>
              <div style={{ ...S.label, fontSize: 8 }}>Contrarian rate</div>
              <div style={{ color: C.amber, fontFamily: 'monospace', fontSize: 11 }}>{entry.contrarian_rate != null ? `${(entry.contrarian_rate * 100).toFixed(0)}%` : '—'}</div>
            </div>
            <div>
              <div style={{ ...S.label, fontSize: 8 }}>Momentum rate</div>
              <div style={{ color: C.blue, fontFamily: 'monospace', fontSize: 11 }}>{entry.momentum_rate != null ? `${(entry.momentum_rate * 100).toFixed(0)}%` : '—'}</div>
            </div>
          </div>
        </div>
      )}

      {/* Accuracy — overall + by_category Beta posteriors */}
      <SubHeader k="accuracy" title="Accuracy" count={acc.resolved_count ? `${acc.resolved_count} resolved` : null} />
      {open.accuracy && (
        <div style={{ marginBottom: 8 }}>
          {acc.overall != null && (
            <div style={{ marginBottom: 6, display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <span style={{ ...S.label, fontSize: 9, flex: 1 }}>Overall</span>
              <span style={{ color: acc.overall >= 0.55 ? C.green : acc.overall >= 0.45 ? C.amber : C.red, fontSize: 13, fontWeight: 700, fontFamily: 'monospace' }}>
                {(acc.overall * 100).toFixed(1)}%
              </span>
            </div>
          )}
          {acc.by_category && acc.by_category.length > 0 ? (
            <div style={{ display: 'grid', gap: 3, fontSize: 10 }}>
              {acc.by_category.slice(0, 6).map(a => (
                <div key={a.category}
                  title={`Beta(${a.beta_a.toFixed(1)}, ${a.beta_b.toFixed(1)}) · ${a.wins}W / ${a.losses}L`}
                  style={{ display: 'grid', gridTemplateColumns: '70px 1fr 50px', gap: 6, alignItems: 'center' }}>
                  <span style={{ color: C.dim2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 9 }}>{a.category}</span>
                  <div style={{ height: 4, background: 'rgba(255,255,255,0.05)' }}>
                    <div style={{ width: `${(a.win_rate || 0) * 100}%`, height: '100%', background: a.win_rate >= 0.55 ? C.green : a.win_rate >= 0.45 ? C.amber : C.red }} />
                  </div>
                  <span style={{ color: C.text, fontFamily: 'monospace', textAlign: 'right', fontSize: 9 }}>
                    {a.win_rate != null ? `${(a.win_rate * 100).toFixed(0)}%` : '—'} <span style={{ color: C.dim2 }}>({a.n})</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: C.dim2, fontSize: 10 }}>No resolved positions in any category yet.</div>
          )}
        </div>
      )}

      {/* Wallet 360 — Falcon highlights */}
      <SubHeader k="w360" title="Wallet 360 (Falcon)" count={Object.keys(w360).length ? `${Object.keys(w360).length} fields` : null} />
      {open.w360 && (
        <div style={{ marginBottom: 8 }}>
          {Object.keys(w360).length === 0 ? (
            <div style={{ color: C.dim2, fontSize: 10 }}>Falcon wallet360 not populated for this wallet.</div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, fontSize: 10 }}>
              {Object.entries(w360).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 4, background: C.panel, padding: '3px 6px' }}>
                  <span style={{ color: C.dim2, fontSize: 9, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{k}</span>
                  <span style={{ color: C.text, fontFamily: 'monospace', fontSize: 9, whiteSpace: 'nowrap' }}>
                    {typeof v === 'number' ? (Math.abs(v) < 1 ? v.toFixed(3) : v.toFixed(2)) : (typeof v === 'boolean' ? (v ? '✓' : '✗') : String(v).slice(0, 18))}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
};

// ─── INSPECTOR ────────────────────────────────────────────────────────────────
// Pipeline observability tab — surfaces what the server is actually
// receiving and what it's deciding, so operators can debug attribution
// + latency + decision-pipeline issues without SSH.
const Inspector = () => {
  const { connectionState } = useLiveStore();
  const [snap, setSnap] = useStateT(null);
  const [filter, setFilter] = usePersistedState('insp.filter', 'all');     // all | leader | non-leader
  const [sourceFilter, setSourceFilter] = usePersistedState('insp.source', 'all');
  const [autoRefresh, setAutoRefresh] = usePersistedState('insp.autoRefresh', true);
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
                    <TD style={{ color: t.is_leader ? C.amber : C.dim2 }}>
                      <span
                        onClick={t.wallet_address ? (e) => { e.stopPropagation(); window.PoybotNav?.selectWallet(t.wallet_address); } : undefined}
                        title={t.wallet_address ? `Open ${short(t.wallet_address)} in Wallet Graph` : undefined}
                        style={{ cursor: t.wallet_address ? 'pointer' : 'default', textDecoration: t.wallet_address ? 'underline dotted rgba(255,255,255,0.15)' : 'none', textUnderlineOffset: 3 }}
                      >{short(t.wallet_address)}</span>
                    </TD>
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
                      <span
                        onClick={d.leader_wallet ? () => window.PoybotNav?.selectWallet(d.leader_wallet) : undefined}
                        title={d.leader_wallet ? `Open ${short(d.leader_wallet)} in Wallet Graph` : undefined}
                        style={{ color: C.purple, fontFamily: 'monospace', cursor: d.leader_wallet ? 'pointer' : 'default', textDecoration: d.leader_wallet ? 'underline dotted rgba(120,85,192,0.3)' : 'none', textUnderlineOffset: 3 }}
                      >{short(d.leader_wallet)}</span>
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
