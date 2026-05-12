// ============================================================================
// Polymarket Bot — Dashboard v2 tabs
//
// 8 top-level tabs implementing the R6→R13 surface per
// docs/UI_REDESIGN_PHASE3.md.
//
// Each tab:
//   • Receives { tab, setTab, tabDef } from the app shell
//   • Renders a BreadcrumbHeader + (optional) SubTabNav + KPI strip + content
//   • Fetches its data via useApi() — components handle their own loading
//     skeletons + empty states gracefully
//   • Honors all 12 pre-delivery accessibility checklist items
// ============================================================================

const { useState, useEffect, useMemo, useCallback } = React;
const {
  T, Icon, StatusPill, Dot, KpiStrip, KpiCell, Panel, BentoCard,
  Banner, GatedBanner, MissingHookBanner, MethodologyAuditBanner,
  PauseToggle, LiveStreamFeed, SubTabNav, BreadcrumbHeader,
  HeatmapMatrix, ScatterPlot, Sparkline, LineWithBand,
  StrategyFingerprintBar, EventTimelineTrack, NotebookTile,
  WalletCell, MarketCell, AuditLogTable, DataTable, GateToggle,
  CrossMarketOperatorCard, SkeletonLine, ChartSkeleton, SectionLabel,
  truncateAddr, fmtPnl, fmtMs, fmtPct, fmtAge, useApi, STRATEGY_COLORS,
} = window;

// Shared scope chips builder
const useScopeChips = () => {
  const { data: overview } = useApi('/api/overview', { interval: 3000 });
  const bot = overview?.bot || {};
  return [
    { status: bot.status === 'running' ? 'running' : 'stopped', label: (bot.status || '—').toUpperCase() },
    { status: bot.execution_enabled ? 'running' : 'gated', label: bot.execution_enabled ? 'LIVE' : 'DRY RUN' },
  ];
};

// ============================================================================
// 1. OVERVIEW — Bento Grid (brain/eyes/hands/mirror) + What-Changed
// ============================================================================

const OverviewTab = ({ tab, setTab }) => {
  const chips = useScopeChips();
  const { data: overview, loading } = useApi('/api/overview', { interval: 3000 });
  const { data: ml } = useApi('/api/ml', { interval: 5000 });
  const { data: calib } = useApi('/api/calibration/summary', { interval: 10000 });
  const { data: timeline } = useApi('/api/overview/timeline', { interval: 5000 });
  const { data: mempool } = useApi('/api/mempool/summary', { interval: 5000 });

  const bot = overview?.bot || {};
  const ingestion = overview?.ingestion || {};
  const stats = overview?.stats || {};

  const kpis = [
    { label: 'NET PNL',       value: fmtPnl(stats.net_pnl ?? 0), color: (stats.net_pnl ?? 0) >= 0 ? T.status.ok : T.status.err, loading },
    { label: 'WIN RATE',      value: fmtPct(stats.win_rate ?? 0, 1), loading },
    { label: 'POSITIONS',     value: `${stats.positions_open ?? 0}/${stats.max_positions ?? 10}`, loading },
    { label: 'DECISIONS 24H', value: stats.decisions_24h ?? 0, color: T.status.info, loading },
    { label: 'INTENT/HR',     value: mempool?.intents_per_hour ?? 0, loading },
    { label: 'BOT UPTIME',    value: bot.uptime_human || '—', loading },
    { label: 'INGESTION',     value: ingestion.total_markets ? `${ingestion.live_markets || 0}/${ingestion.total_markets}` : '—', loading },
    { label: 'AUTO-DISABLED', value: calib?.disabled_count ?? 0, color: (calib?.disabled_count ?? 0) > 0 ? T.status.warn : T.status.ok, loading },
  ];

  return (
    <>
      <BreadcrumbHeader tab="OVERVIEW" chips={chips} />
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--section-padding)' }}>
        {/* 2x2 Bento Grid */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(2, 1fr)',
          gap: 'var(--grid-gap)',
          marginBottom: 'var(--section-padding)',
        }}>
          <BentoCard title="BRAIN" icon="cpu" onClick={() => setTab({ id: 'intelligence', subTab: 'maturity' })}>
            <OverviewBrain ml={ml} calib={calib} />
          </BentoCard>
          <BentoCard title="EYES" icon="eye" onClick={() => setTab({ id: 'wallet', subTab: 'universe' })}>
            <OverviewEyes overview={overview} />
          </BentoCard>
          <BentoCard title="HANDS" icon="target" onClick={() => setTab({ id: 'mempool', subTab: 'live' })}>
            <OverviewHands mempool={mempool} bot={bot} />
          </BentoCard>
          <BentoCard title="MIRROR" icon="activity" onClick={() => setTab({ id: 'operations', subTab: 'calibration' })}>
            <OverviewMirror calib={calib} />
          </BentoCard>
        </div>

        <Panel title="WHAT CHANGED · LAST 5 EVENTS">
          {timeline?.events?.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {timeline.events.slice(0, 5).map((e, i) => (
                <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'center', fontSize: 'var(--font-size-sm)' }}>
                  <span style={{ color: T.text.tertiary, fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums', minWidth: 100 }}>
                    {e.time}
                  </span>
                  <Dot status={e.severity || 'info'} />
                  <span style={{ color: T.text.primary, flex: 1 }}>{e.message}</span>
                  {e.deepLink && (
                    <button onClick={() => setTab(e.deepLink)} style={{ color: T.accent.violet, padding: '2px 8px', fontSize: 'var(--font-size-xs)' }}>
                      View →
                    </button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>
              No recent events. Operator actions, gate toggles, drift alerts, and auto-disable events will appear here.
            </div>
          )}
        </Panel>
      </div>
    </>
  );
};

const OverviewBrain = ({ ml, calib }) => {
  const phases = ml?.phase_distribution || { p1: 0, p2: 0, p3: 0 };
  const total = phases.p1 + phases.p2 + phases.p3;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6, fontSize: 'var(--font-size-sm)' }}>
        <div><span style={{ color: T.text.tertiary }}>P1</span> <strong style={{ color: T.chart.c1 }}>{phases.p1}</strong></div>
        <div><span style={{ color: T.text.tertiary }}>P2</span> <strong style={{ color: T.chart.c2 }}>{phases.p2}</strong></div>
        <div><span style={{ color: T.text.tertiary }}>P3</span> <strong style={{ color: T.chart.c3 }}>{phases.p3}</strong></div>
      </div>
      <div style={{ fontSize: 'var(--font-size-xs)', color: T.text.tertiary }}>
        Maturity {fmtPct((ml?.maturity_pct ?? 0) * 100, 1)} · Total profiles {total}
      </div>
      <hr style={{ border: 0, borderTop: `1px solid ${T.border.subtle}` }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--font-size-xs)' }}>
        <ModelRow name="follow_confidence" status="protected" detail="Brier 0.043" />
        <ModelRow name="strategy_class"    status={calib?.strategy_class_enabled ? 'running' : 'gated'} detail={ml?.lens_trained ? 'log 0.51' : 'not trained'} />
        <ModelRow name="volume_forecast"   status={calib?.volume_forecast_enabled ? 'running' : 'gated'} detail="pending" />
        <ModelRow name="causal_ate"        status={calib?.causal_enabled ? 'running' : 'gated'} detail={calib?.causal_enabled ? '—' : 'gated OFF'} />
      </div>
    </div>
  );
};

const ModelRow = ({ name, status, detail }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
    <Dot status={status} />
    <span style={{ color: T.text.secondary, flex: 1, fontFamily: 'var(--font-mono)' }}>{name}</span>
    <span style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>{detail}</span>
  </div>
);

const OverviewEyes = ({ overview }) => {
  const layers = [
    { name: 'R6 onchain CLOB',  status: overview?.layers?.onchain ?? 'gated' },
    { name: 'R6 cold tier',     status: overview?.layers?.cold_tier ?? 'gated' },
    { name: 'R11 L3 book',      status: overview?.layers?.book_l3 ?? 'gated' },
    { name: 'R12 social',       status: overview?.layers?.social ?? 'gated' },
    { name: 'R12 cross-market', status: overview?.layers?.crossmarket ?? 'gated' },
  ];
  const coverage = overview?.coverage_pct;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--font-size-sm)' }}>
      {layers.map(l => (
        <div key={l.name} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Dot status={l.status} />
          <span style={{ color: T.text.secondary, flex: 1 }}>{l.name}</span>
          <span style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>
            {l.status === 'running' ? 'live' : l.status === 'gated' ? 'idle' : 'down'}
          </span>
        </div>
      ))}
      <hr style={{ border: 0, borderTop: `1px solid ${T.border.subtle}`, margin: '4px 0' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Coverage</span>
        <strong style={{ color: coverage > 95 ? T.status.ok : coverage > 80 ? T.status.warn : T.status.err }}>
          {coverage != null ? fmtPct(coverage, 1) : '—'}
        </strong>
      </div>
    </div>
  );
};

const OverviewHands = ({ mempool, bot }) => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--font-size-sm)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Mempool</span>
        <StatusPill status={mempool?.connected ? 'running' : 'off'} label={mempool?.connected ? 'LIVE' : 'OFF'} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Prefill pool</span>
        <span style={{ color: T.text.primary }}>{mempool?.pool_size ?? 0}/{mempool?.pool_capacity ?? 40}</span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Shadow fires 24h</span>
        <span style={{ color: T.text.primary }}>{mempool?.shadow_fires_24h ?? 0}</span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Live fires 24h</span>
        <span style={{ color: T.text.primary }}>{mempool?.live_fires_24h ?? 0}</span>
      </div>
      <hr style={{ border: 0, borderTop: `1px solid ${T.border.subtle}`, margin: '4px 0' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Killswitch</span>
        <StatusPill status={bot.killswitch_active ? 'err' : 'ok'} label={bot.killswitch_active ? 'TRIPPED' : 'ARMED'} />
      </div>
    </div>
  );
};

const OverviewMirror = ({ calib }) => {
  if (!calib) {
    return <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>
      R13 calibration daemon not yet active. Engine hooks deferred (audit § 4.A) — operator must wire.
    </div>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--font-size-sm)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Auto-disabled</span>
        <span style={{ color: (calib.auto_disabled_count ?? 0) > 0 ? T.status.warn : T.status.ok }}>
          {calib.auto_disabled_count ?? 0}
        </span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Manual disabled</span>
        <span style={{ color: T.text.primary }}>{calib.manual_disabled_count ?? 0}</span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Drift alerts 24h</span>
        <span style={{ color: (calib.drift_alerts_24h ?? 0) > 0 ? T.status.warn : T.status.ok }}>
          {calib.drift_alerts_24h ?? 0}
        </span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Last batch</span>
        <span style={{ color: T.text.primary, fontFamily: 'var(--font-mono)' }}>{calib.last_batch_at || '—'}</span>
      </div>
      <hr style={{ border: 0, borderTop: `1px solid ${T.border.subtle}`, margin: '4px 0' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.text.tertiary }}>Predictions logged 24h</span>
        <span style={{ color: T.text.primary }}>{calib.predictions_logged_24h ?? 0}</span>
      </div>
    </div>
  );
};

window.OverviewTab = OverviewTab;

// ============================================================================
// 2. INTELLIGENCE — Maturity / Lens / Web / Causal
// ============================================================================

const IntelligenceTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  return (
    <>
      <BreadcrumbHeader
        tab="INTELLIGENCE"
        subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label}
        chips={chips}
      />
      <SubTabNav
        tabs={tabDef.subTabs}
        value={tab.subTab}
        onChange={(s) => setTab({ ...tab, subTab: s })}
      />
      <div style={{ flex: 1, overflow: 'auto' }}>
        {tab.subTab === 'maturity' && <IntelligenceMaturity />}
        {tab.subTab === 'lens'     && <IntelligenceLens />}
        {tab.subTab === 'web'      && <IntelligenceWeb />}
        {tab.subTab === 'causal'   && <IntelligenceCausal />}
      </div>
    </>
  );
};

const IntelligenceMaturity = () => {
  const { data: ml, loading } = useApi('/api/ml', { interval: 10000 });
  const phases = ml?.phase_distribution || {};
  const kpis = [
    { label: 'TOTAL PROFILES', value: ml?.total_profiles ?? 0, loading },
    { label: 'MATURITY',       value: fmtPct((ml?.maturity_pct ?? 0) * 100, 1), loading },
    { label: 'PHASE 1',        value: phases.p1 ?? 0, color: T.chart.c1, loading },
    { label: 'PHASE 2',        value: phases.p2 ?? 0, color: T.chart.c2, loading },
    { label: 'PHASE 3',        value: phases.p3 ?? 0, color: T.chart.c3, loading },
    { label: 'DECISIONS 24H',  value: ml?.decisions_24h ?? 0, loading },
  ];
  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)', display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 'var(--grid-gap)' }}>
        <Panel title="TRAINING PIPELINE">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
            <PhaseCard tag="P1" name="Beta-Binomial" count={phases.p1 ?? 0} range="0–99 resolved" />
            <PhaseCard tag="P2" name="Bayesian LogReg" count={phases.p2 ?? 0} range="100–499 resolved" />
            <PhaseCard tag="P3" name="LightGBM + Platt" count={phases.p3 ?? 0} range="500+ resolved" />
          </div>
        </Panel>
        <Panel title="LEARNING TRAJECTORY · 24H">
          <LineWithBand
            series={[
              { label: 'Trades observed',  color: T.chart.c1, values: (ml?.trajectory?.trades || []).map((y, i) => ({ x: i, y })) },
              { label: 'Positions resolved', color: T.chart.c3, values: (ml?.trajectory?.resolved || []).map((y, i) => ({ x: i, y })) },
              { label: 'Active edges',     color: T.chart.c2, values: (ml?.trajectory?.edges || []).map((y, i) => ({ x: i, y })) },
            ]}
            xLabel="hour"
            yLabel="count"
            loading={loading}
          />
        </Panel>
      </div>
    </>
  );
};

const PhaseCard = ({ tag, name, count, range }) => (
  <div style={{ background: T.bg.input, padding: 12, borderRadius: 'var(--radius-md)', display: 'flex', flexDirection: 'column', gap: 6 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{
        padding: '2px 6px',
        background: T.accent.amber,
        color: T.text.inverse,
        fontSize: 'var(--font-size-xs)',
        fontWeight: 700,
        letterSpacing: 'var(--letter-spacing-wide)',
        borderRadius: 'var(--radius-sm)',
      }}>{tag}</span>
      <span style={{ color: T.text.primary, fontSize: 'var(--font-size-sm)' }}>{name}</span>
    </div>
    <div style={{ color: T.chart.c1, fontSize: 24, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{count}</div>
    <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>{range}</div>
  </div>
);

const IntelligenceLens = () => {
  const { data: distrib, loading: dLoading } = useApi('/api/intelligence/lens/distribution', { interval: 30000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });

  const total = distrib?.total ?? 0;
  const kpis = [
    { label: 'PROFILES CLASSIFIED', value: total, loading: dLoading },
    { label: 'TRAINED CLASSES',     value: distrib?.trained_classes ?? '—', loading: dLoading },
    { label: 'COHEN κ',             value: distrib?.cohens_kappa ? distrib.cohens_kappa.toFixed(2) : '—', color: distrib?.cohens_kappa >= 0.7 ? T.status.ok : T.status.warn, loading: dLoading },
    { label: 'DRIFT ALERTS 24H',    value: distrib?.drift_alerts_24h ?? 0, color: (distrib?.drift_alerts_24h ?? 0) > 0 ? T.status.warn : T.status.ok, loading: dLoading },
  ];

  const classCounts = distrib?.by_class || {};
  const sortedClasses = Object.entries(classCounts).sort((a, b) => b[1] - a[1]);
  const maxCount = Math.max(1, ...Object.values(classCounts));

  return (
    <>
      <KpiStrip items={kpis} loading={dLoading} />
      <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
        {!gateState?.strategy_conditional_confidence_enabled && (
          <GatedBanner
            flag="strategy_conditional_confidence_enabled"
            route="OPERATIONS → Risk"
            description="The R8 classifier output is computed but NOT applied as a FOLLOW/FADE multiplier yet. Flip to enable STRATEGY_WEIGHTS in confidence_engine."
          />
        )}

        <Panel title="STRATEGY CLASS DISTRIBUTION · LAST 30D">
          {sortedClasses.length === 0 ? (
            <div style={{ color: T.text.tertiary, padding: 16, textAlign: 'center', fontSize: 'var(--font-size-sm)' }}>
              No classifications yet. Train LightGBM after labelling sprint completes (≥ 100 wallets across 9 classes).
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {sortedClasses.map(([cls, count]) => (
                <div key={cls} style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 'var(--font-size-sm)' }}>
                  <span style={{ width: 110, color: T.text.secondary }}>{cls}</span>
                  <div style={{ flex: 1, height: 12, background: T.bg.input, borderRadius: 'var(--radius-sm)', overflow: 'hidden' }}>
                    <div style={{ width: `${(count / maxCount) * 100}%`, height: '100%', background: STRATEGY_COLORS[cls] || T.text.tertiary }} />
                  </div>
                  <span style={{ width: 40, textAlign: 'right', color: T.text.primary, fontVariantNumeric: 'tabular-nums' }}>{count}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr', gap: 'var(--grid-gap)' }}>
          <Panel title="DRIFT HEATMAP · LEADER × 7D">
            <HeatmapMatrix
              rowLabels={(distrib?.drift_rows || []).map(r => r.wallet ? truncateAddr(r.wallet) : '—')}
              colLabels={distrib?.drift_cols || ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']}
              values={distrib?.drift_values || []}
              emptyMessage="No drift data — daemon hasn't run yet."
              loading={dLoading}
            />
          </Panel>
          <Panel title="LABELLING WORKSPACE">
            <LabellingForm />
          </Panel>
        </div>
      </div>
    </>
  );
};

const LabellingForm = () => {
  const { data: pending } = useApi('/api/intelligence/lens/labels/pending', { interval: 60000 });
  const [selected, setSelected] = useState({});
  const [bulkLabel, setBulkLabel] = useState('');
  const allClasses = ['directional','momentum','contrarian','arb_2way','arb_3way','market_maker','structural_bot','info_leak','social_driven'];
  const rows = pending?.wallets || [];

  if (rows.length === 0) {
    return (
      <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>
        No wallets pending label. Operator labelling sprint produces a backlog
        in <code>strategy_labels</code>; daemon surfaces high-confidence
        candidates here.
      </div>
    );
  }

  const selCount = Object.values(selected).filter(Boolean).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--font-size-sm)' }}>
        <span style={{ color: T.text.tertiary }}>{selCount} selected</span>
        <select value={bulkLabel} onChange={e => setBulkLabel(e.target.value)} style={{ marginLeft: 'auto' }}>
          <option value="">— bulk label as —</option>
          {allClasses.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <button disabled={!selCount || !bulkLabel}
                style={{ padding: '4px 10px', color: T.accent.amber, border: `1px solid ${T.accent.amber}`, fontSize: 'var(--font-size-sm)' }}>
          Apply
        </button>
      </div>
      <DataTable
        dense
        columns={[
          { key: 'select', label: '', render: (_v, row) => (
            <input type="checkbox" checked={!!selected[row.wallet]}
                   onChange={e => setSelected(s => ({ ...s, [row.wallet]: e.target.checked }))} />
          )},
          { key: 'wallet', label: 'WALLET', render: (v) => <WalletCell wallet={v} /> },
          { key: 'suggested', label: 'SUGGESTED', color: T.accent.amber },
          { key: 'confidence', label: 'CONF.', align: 'right',
            render: (v) => v != null ? v.toFixed(2) : '—' },
        ]}
        rows={rows}
        rowKey={r => r.wallet}
      />
    </div>
  );
};

const IntelligenceWeb = () => {
  const { data: web, loading } = useApi('/api/intelligence/web/summary', { interval: 30000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });

  const kpis = [
    { label: 'ACTIVE FITS',         value: web?.active_fits ?? 0, loading },
    { label: 'ACCEPTED COUPLINGS',  value: web?.accepted_couplings ?? 0, loading },
    { label: 'KALMAN UPDATES 24H',  value: web?.kalman_updates_24h ?? 0, loading },
    { label: 'FORECASTS 24H',       value: web?.forecasts_24h ?? 0, loading },
  ];

  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
        {!gateState?.volume_anticipation_enabled && (
          <GatedBanner
            flag="volume_anticipation_enabled"
            route="OPERATIONS → Risk"
            description="R9 volume forecasts are computed but NOT routed as new decision policy. Flip to enable the volume_anticipation branch in decision_router."
          />
        )}

        <Panel title="α-MATRIX HEATMAP">
          <HeatmapMatrix
            rowLabels={web?.alpha?.row_labels || []}
            colLabels={web?.alpha?.col_labels || []}
            values={web?.alpha?.matrix || []}
            emptyMessage="No fits yet. Run polymarket-follower-volume.service or wait for nightly 03:30 UTC cron."
            loading={loading}
          />
        </Panel>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--grid-gap)' }}>
          <Panel title="FOLLOWER POOL KALMAN">
            {web?.kalman?.length ? (
              <DataTable
                dense
                columns={[
                  { key: 'pool',    label: 'POOL' },
                  { key: 'pool_size_usdc', label: 'SIZE $', align: 'right', render: v => v != null ? `$${Number(v).toLocaleString()}` : '—' },
                  { key: 'recent_response_pct', label: 'RESP %', align: 'right', render: v => v != null ? fmtPct(v * 100, 1) : '—' },
                  { key: 'decay_rate', label: 'DECAY', align: 'right', render: v => v != null ? Number(v).toFixed(3) : '—' },
                  { key: 'n_observations', label: 'N OBS', align: 'right' },
                ]}
                rows={web.kalman}
              />
            ) : (
              <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>
                No Kalman state — depends on observed follow trades. Coverage TBD.
              </div>
            )}
          </Panel>
          <Panel title="VOLUME FORECAST">
            {web?.forecast ? (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                  <span style={{ color: T.text.tertiary }}>Total expected</span>
                  <strong style={{ color: T.text.primary }}>${Number(web.forecast.total_volume_usdc).toLocaleString()}</strong>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, fontSize: 'var(--font-size-xs)' }}>
                  <span style={{ color: T.text.tertiary }}>95% CI</span>
                  <span style={{ color: T.text.secondary }}>
                    [${Number(web.forecast.ci_low).toLocaleString()} – ${Number(web.forecast.ci_high).toLocaleString()}]
                  </span>
                </div>
                <hr style={{ border: 0, borderTop: `1px solid ${T.border.subtle}`, margin: '8px 0' }} />
                <SectionLabel>BY POOL</SectionLabel>
                {Object.entries(web.forecast.by_pool || {}).map(([pool, v]) => (
                  <div key={pool} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--font-size-sm)' }}>
                    <span style={{ color: T.text.secondary }}>{pool}</span>
                    <span style={{ color: T.text.primary }}>${Number(v).toLocaleString()}</span>
                  </div>
                ))}
              </>
            ) : (
              <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>
                Select a leader to forecast. Forecast = mvHawkes intensity × Kalman pool size × R8 prior.
              </div>
            )}
          </Panel>
        </div>
      </div>
    </>
  );
};

const IntelligenceCausal = () => {
  const { data: causal, loading } = useApi('/api/intelligence/causal/scatter', { interval: 60000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });

  const kpis = [
    { label: 'ESTIMATES 24H',           value: causal?.estimates_24h ?? 0, loading },
    { label: 'WU-H p<0.05 RATE',        value: causal ? fmtPct((causal.wu_hausman_pass_rate ?? 0) * 100, 0) : '—', loading },
    { label: 'F-STAT > 10 RATE',        value: causal ? fmtPct((causal.first_stage_pass_rate ?? 0) * 100, 0) : '—', loading },
    { label: 'DISAGREEMENT %',          value: causal ? fmtPct((causal.disagreement_pct ?? 0) * 100, 0) : '—', color: T.status.warn, loading },
  ];

  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
        <MethodologyAuditBanner />
        {!gateState?.causal_gating_enabled && (
          <GatedBanner
            flag="causal_gating_enabled"
            route="OPERATIONS → Risk"
            description="R10 IV estimates are computed but the confidence gate is OFF — pure-Hawkes confidence applies. Methodology audit required before flip."
          />
        )}

        <Panel title="KEYSTONE PLOT · IV ATE vs HAWKES α/μ" subtitle="Points in the top-left = high statistical α but near-zero causal effect = news confounding (skip these).">
          <ScatterPlot
            xLabel="Hawkes α/μ"
            yLabel="Causal ATE"
            points={(causal?.points || []).map(p => ({
              x: p.hawkes_alpha_mu_ratio,
              y: p.causal_ate,
              label: `${truncateAddr(p.leader_wallet)} · ${p.pool_class}`,
              color: p.causal_ate <= 0 && p.hawkes_alpha_mu_ratio > 1 ? T.status.err : T.chart.c1,
            }))}
            loading={loading}
          />
        </Panel>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--grid-gap)' }}>
          <Panel title="Wu-HAUSMAN p DISTRIBUTION">
            {causal?.wu_hausman_histogram?.length ? (
              <LineWithBand
                series={[{ label: 'count', color: T.chart.c1, values: causal.wu_hausman_histogram.map((y, i) => ({ x: i, y })) }]}
                xLabel="p bin (0 → 1)" yLabel="count" loading={loading}
              />
            ) : (
              <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>No data yet.</div>
            )}
          </Panel>
          <Panel title="FIRST-STAGE F DISTRIBUTION">
            {causal?.first_stage_f_histogram?.length ? (
              <LineWithBand
                series={[{ label: 'count', color: T.chart.c2, values: causal.first_stage_f_histogram.map((y, i) => ({ x: i, y })) }]}
                xLabel="F (log scale)" yLabel="count" loading={loading}
              />
            ) : (
              <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>No data yet.</div>
            )}
          </Panel>
        </div>
      </div>
    </>
  );
};

window.IntelligenceTab = IntelligenceTab;

// ============================================================================
// 3. WALLET LAB — Universe / Graph / Scanner / Profile
// ============================================================================

const WalletLabTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  return (
    <>
      <BreadcrumbHeader
        tab="WALLET LAB"
        subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label}
        chips={chips}
      />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <div style={{ flex: 1, overflow: 'auto' }}>
        {tab.subTab === 'universe' && <WalletUniverse setTab={setTab} />}
        {tab.subTab === 'graph'    && <WalletGraphFull />}
        {tab.subTab === 'scanner'  && <WalletScanner setTab={setTab} />}
        {tab.subTab === 'profile'  && <WalletProfileSelect />}
      </div>
    </>
  );
};

const WalletUniverse = ({ setTab }) => {
  const [tierFilter, setTierFilter] = useState('all');
  const [stratFilter, setStratFilter] = useState('all');
  const [sortKey, setSortKey] = useState('volume_30d_usdc');
  const [sortDir, setSortDir] = useState('desc');
  const { data: universe, loading } = useApi('/api/wallet/universe?limit=500', { interval: 30000 });

  const kpis = [
    { label: 'UNIVERSE SIZE',  value: universe?.total ?? 0, loading },
    { label: 'TIER-0 WHALES',  value: universe?.tier_0 ?? 0, color: T.accent.amber, loading },
    { label: 'TIER-1 TOP',     value: universe?.tier_1 ?? 0, loading },
    { label: 'TIER-2 DEPTH',   value: universe?.tier_2 ?? 0, color: T.text.tertiary, loading },
    { label: 'LAST CRAWL',     value: universe?.last_crawl_at || '—', loading },
  ];

  const allClasses = ['directional','momentum','contrarian','arb_2way','arb_3way','market_maker','structural_bot','info_leak','social_driven'];

  const filtered = useMemo(() => {
    const wallets = universe?.wallets || [];
    return wallets
      .filter(w => tierFilter === 'all' || String(w.depth_tier) === tierFilter)
      .filter(w => stratFilter === 'all' || w.strategy_class === stratFilter)
      .slice()
      .sort((a, b) => {
        const av = a[sortKey], bv = b[sortKey];
        if (av == null) return 1;
        if (bv == null) return -1;
        const cmp = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv));
        return sortDir === 'asc' ? cmp : -cmp;
      });
  }, [universe, tierFilter, stratFilter, sortKey, sortDir]);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  };

  const sortableHeader = (key, label, align = 'left') => (
    <button
      onClick={() => toggleSort(key)}
      style={{
        padding: 0, background: 'transparent', border: 'none',
        color: sortKey === key ? T.accent.amber : T.text.tertiary,
        textAlign: align,
        fontFamily: 'inherit', fontSize: 'inherit', fontWeight: 'inherit',
        cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 4,
      }}
    >
      {label}
      {sortKey === key && <Icon name={sortDir === 'asc' ? 'chevron-up' : 'chevron-down'} size={10} />}
    </button>
  );

  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)' }}>
        <Panel
          title={`UNIVERSE BROWSER · ${filtered.length} of ${universe?.wallets?.length ?? 0}`}
          action={
            <div style={{ display: 'flex', gap: 8 }}>
              <select value={tierFilter} onChange={e => setTierFilter(e.target.value)}>
                <option value="all">All tiers</option>
                <option value="0">Tier 0 (whales)</option>
                <option value="1">Tier 1 (top)</option>
                <option value="2">Tier 2 (depth)</option>
              </select>
              <select value={stratFilter} onChange={e => setStratFilter(e.target.value)}>
                <option value="all">All strategies</option>
                {allClasses.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          }
        >
          <DataTable
            loading={loading}
            emptyMessage="No wallets match. Adjust filter or run polymarket-crawler.service."
            columns={[
              { key: 'wallet_address', label: 'WALLET',
                render: (v) => <WalletCell wallet={v} onClick={() => {
                  window.location.hash = `wallet/profile?w=${v}`;
                  setTab({ id: 'wallet', subTab: 'profile' });
                }} /> },
              { key: 'depth_tier',     label: sortableHeader('depth_tier', 'TIER', 'right'), align: 'right' },
              { key: 'strategy_class', label: 'STRATEGY', color: T.accent.amber,
                render: v => v ? <StatusPill status="info" label={v} /> : <span style={{ color: T.text.tertiary }}>—</span> },
              { key: 'volume_30d_usdc',label: sortableHeader('volume_30d_usdc', 'VOL 30D $', 'right'), align: 'right',
                render: v => v != null ? `$${Number(v).toLocaleString()}` : '—' },
              { key: 'trades_30d',     label: sortableHeader('trades_30d', 'TRADES', 'right'), align: 'right' },
              { key: 'last_seen',      label: 'LAST SEEN' },
            ]}
            rows={filtered}
            rowKey={r => r.wallet_address}
          />
        </Panel>
      </div>
    </>
  );
};

// ── Wallet Graph (full SVG view) ─────────────────────────────────────────
const WalletGraphFull = () => {
  const { data: graph, loading } = useApi('/api/graph/top-edges?limit=80', { interval: 30000 });
  const [confirmedOnly, setConfirmedOnly] = useState(true);
  const [hoveredEdge, setHoveredEdge] = useState(null);

  const edges = (graph?.edges || [])
    .filter(e => !confirmedOnly || e.is_confirmed)
    .slice(0, 80);

  // Build node positions: leaders on left, followers on right, deterministic layout
  const nodes = useMemo(() => {
    const leaderSet = new Set();
    const followerSet = new Set();
    edges.forEach(e => {
      leaderSet.add(e.leader_wallet);
      followerSet.add(e.follower_wallet);
    });
    const leaders = Array.from(leaderSet);
    const followers = Array.from(followerSet);
    const W = 720, H = 480, padding = 40;
    const colLeftX = padding + 60;
    const colRightX = W - padding - 60;
    const positions = {};
    leaders.forEach((w, i) => {
      positions[w] = {
        x: colLeftX,
        y: padding + (i + 0.5) * ((H - 2 * padding) / Math.max(leaders.length, 1)),
        role: 'leader',
      };
    });
    followers.forEach((w, i) => {
      positions[w] = {
        x: colRightX,
        y: padding + (i + 0.5) * ((H - 2 * padding) / Math.max(followers.length, 1)),
        role: 'follower',
      };
    });
    return { positions, W, H, leaderCount: leaders.length, followerCount: followers.length };
  }, [edges]);

  const kpis = [
    { label: 'TOTAL EDGES',     value: graph?.edges?.length ?? 0, loading },
    { label: 'CONFIRMED EDGES', value: (graph?.edges || []).filter(e => e.is_confirmed).length, color: T.status.ok, loading },
    { label: 'UNIQUE LEADERS',  value: nodes.leaderCount, loading },
    { label: 'UNIQUE FOLLOWERS',value: nodes.followerCount, loading },
  ];

  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
        <Panel
          title="LEADER → FOLLOWER GRAPH"
          subtitle="Edges from follower_edges table. Confirmed = α/μ > 1 AND BIC accepted (R5/R9)."
          action={
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: T.text.secondary, fontSize: 'var(--font-size-sm)', cursor: 'pointer' }}>
              <input type="checkbox" checked={confirmedOnly} onChange={e => setConfirmedOnly(e.target.checked)} />
              Confirmed only
            </label>
          }
        >
          {loading ? (
            <ChartSkeleton height={480} />
          ) : edges.length === 0 ? (
            <div style={{ padding: 32, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>
              No edges in <code>follower_edges</code>. Run R5 graph engine + nightly Hawkes batch.
            </div>
          ) : (
            <div style={{ overflow: 'auto' }}>
              <svg width={nodes.W} height={nodes.H} role="img" aria-label="Leader to follower graph">
                {/* edges */}
                {edges.map((e, i) => {
                  const a = nodes.positions[e.leader_wallet];
                  const b = nodes.positions[e.follower_wallet];
                  if (!a || !b) return null;
                  const isHovered = hoveredEdge === i;
                  const stroke = e.is_confirmed ? T.status.ok : T.text.tertiary;
                  const opacity = isHovered ? 1 : 0.25 + (e.follow_probability || 0.5) * 0.6;
                  return (
                    <g key={i} onMouseEnter={() => setHoveredEdge(i)} onMouseLeave={() => setHoveredEdge(null)}>
                      <path
                        d={`M ${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${(a.y + b.y) / 2 - 40} ${b.x} ${b.y}`}
                        fill="none"
                        stroke={stroke}
                        strokeWidth={isHovered ? 2.5 : 1}
                        opacity={opacity}
                      >
                        <title>{`${truncateAddr(e.leader_wallet)} → ${truncateAddr(e.follower_wallet)}\nα/μ=${(e.alpha_mu || 0).toFixed(2)} · p=${(e.follow_probability || 0).toFixed(2)}${e.is_confirmed ? ' · CONFIRMED' : ''}`}</title>
                      </path>
                    </g>
                  );
                })}
                {/* nodes */}
                {Object.entries(nodes.positions).map(([w, p]) => (
                  <g key={w}>
                    <circle
                      cx={p.x} cy={p.y} r={p.role === 'leader' ? 8 : 5}
                      fill={p.role === 'leader' ? T.accent.amber : T.accent.violet}
                      stroke={T.bg.page} strokeWidth="2"
                    />
                    <text
                      x={p.role === 'leader' ? p.x - 14 : p.x + 14}
                      y={p.y + 3}
                      textAnchor={p.role === 'leader' ? 'end' : 'start'}
                      fill={T.text.secondary}
                      fontSize="9"
                      fontFamily="var(--font-mono)"
                    >
                      {truncateAddr(w)}
                    </text>
                  </g>
                ))}
                {/* legend */}
                <g transform="translate(20, 20)">
                  <circle cx="6" cy="0" r="6" fill={T.accent.amber} />
                  <text x="18" y="3" fontSize="10" fill={T.text.secondary}>LEADER</text>
                  <circle cx="6" cy="16" r="4" fill={T.accent.violet} />
                  <text x="18" y="19" fontSize="10" fill={T.text.secondary}>FOLLOWER</text>
                </g>
              </svg>
            </div>
          )}
        </Panel>

        <Panel title="TOP CONFIRMED EDGES · BY α/μ">
          <DataTable
            loading={loading}
            emptyMessage="No confirmed edges yet."
            dense
            columns={[
              { key: 'leader_wallet',   label: 'LEADER',   render: v => <WalletCell wallet={v} /> },
              { key: 'follower_wallet', label: 'FOLLOWER', render: v => <WalletCell wallet={v} /> },
              { key: 'alpha_mu',        label: 'α/μ', align: 'right',
                render: v => v != null ? Number(v).toFixed(2) : '—' },
              { key: 'follow_probability', label: 'P(follow)', align: 'right',
                render: v => v != null ? Number(v).toFixed(2) : '—' },
              { key: 'co_occurrences',  label: 'CO-OCC', align: 'right' },
              { key: 'is_confirmed', label: 'STATUS',
                render: v => <StatusPill status={v ? 'ok' : 'pending'} label={v ? 'CONFIRMED' : 'PENDING'} /> },
            ]}
            rows={(graph?.edges || []).slice().sort((a, b) => (b.alpha_mu || 0) - (a.alpha_mu || 0)).slice(0, 30)}
            rowKey={(r, i) => `${r.leader_wallet}-${r.follower_wallet}`}
          />
        </Panel>
      </div>
    </>
  );
};

const WalletScanner = ({ setTab }) => {
  const [search, setSearch] = useState('');
  const [phaseFilter, setPhaseFilter] = useState('all');
  const [sortKey, setSortKey] = useState('readiness');
  const [sortDir, setSortDir] = useState('desc');
  const { data: scanner, loading } = useApi('/api/leaders?limit=200', { interval: 30000 });

  const leaders = (scanner?.leaders || [])
    .filter(l => phaseFilter === 'all' || String(l.phase) === phaseFilter)
    .filter(l => !search || l.wallet_address.toLowerCase().includes(search.toLowerCase()))
    .slice()
    .sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv));
      return sortDir === 'asc' ? cmp : -cmp;
    });

  const kpis = [
    { label: 'LEADERS',          value: scanner?.leaders?.length ?? 0, loading },
    { label: 'PHASE 1',          value: (scanner?.leaders || []).filter(l => l.phase === 1).length, color: T.chart.c1, loading },
    { label: 'PHASE 2',          value: (scanner?.leaders || []).filter(l => l.phase === 2).length, color: T.chart.c2, loading },
    { label: 'PHASE 3',          value: (scanner?.leaders || []).filter(l => l.phase === 3).length, color: T.chart.c3, loading },
    { label: 'WIN RATE MEDIAN',  value: scanner?.win_rate_median ? fmtPct(scanner.win_rate_median * 100, 1) : '—', loading },
    { label: 'FILTERED RESULTS', value: leaders.length, loading },
  ];

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  };

  const sortableHeader = (key, label, align = 'left') => (
    <button onClick={() => toggleSort(key)} style={{
      padding: 0, background: 'transparent', border: 'none',
      color: sortKey === key ? T.accent.amber : T.text.tertiary,
      textAlign: align, fontFamily: 'inherit', fontSize: 'inherit', fontWeight: 'inherit',
      cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 4,
    }}>
      {label}
      {sortKey === key && <Icon name={sortDir === 'asc' ? 'chevron-up' : 'chevron-down'} size={10} />}
    </button>
  );

  return (
    <>
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ padding: 'var(--section-padding)' }}>
        <Panel
          title="WALLET SCANNER"
          action={
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                type="search"
                placeholder="Search wallet…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                style={{ width: 180 }}
              />
              <select value={phaseFilter} onChange={e => setPhaseFilter(e.target.value)}>
                <option value="all">All phases</option>
                <option value="1">Phase 1</option>
                <option value="2">Phase 2</option>
                <option value="3">Phase 3</option>
              </select>
            </div>
          }
        >
          <DataTable
            loading={loading}
            emptyMessage="No leaders match. Try clearing filters."
            columns={[
              { key: 'wallet_address', label: 'WALLET',
                render: (v) => <WalletCell wallet={v} onClick={() => {
                  window.location.hash = `wallet/profile?w=${v}`;
                  setTab && setTab({ id: 'wallet', subTab: 'profile' });
                }} /> },
              { key: 'phase',          label: sortableHeader('phase', 'PHASE', 'center'), align: 'center', color: T.accent.amber },
              { key: 'falcon_score',   label: sortableHeader('falcon_score', 'FALCON', 'right'), align: 'right',
                render: v => v != null ? Number(v).toFixed(2) : '—' },
              { key: 'trades_24h',     label: sortableHeader('trades_24h', 'TRADES 24H', 'right'), align: 'right' },
              { key: 'win_rate',       label: sortableHeader('win_rate', 'WIN %', 'right'), align: 'right',
                render: v => v != null ? fmtPct(v * 100, 1) : '—' },
              { key: 'pnl_30d',        label: sortableHeader('pnl_30d', 'PNL 30D', 'right'), align: 'right',
                render: v => fmtPnl(v) },
              { key: 'readiness',      label: sortableHeader('readiness', 'READINESS', 'right'), align: 'right',
                render: v => v != null ? Number(v).toFixed(2) : '—' },
            ]}
            rows={leaders}
            rowKey={r => r.wallet_address}
          />
        </Panel>
      </div>
    </>
  );
};

const WalletProfileSelect = () => {
  const [wallet, setWallet] = useState('');
  return (
    <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      <Panel title="SELECT WALLET">
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            value={wallet}
            onChange={e => setWallet(e.target.value)}
            placeholder="0x..."
            style={{ flex: 1 }}
          />
          <button
            onClick={() => wallet && (window.location.hash = `wallet/profile?w=${wallet}`)}
            style={{ padding: '6px 14px', color: T.accent.amber, border: `1px solid ${T.accent.amber}` }}
          >
            Inspect
          </button>
        </div>
      </Panel>
      {wallet && <WalletProfile wallet={wallet} />}
    </div>
  );
};

const WalletProfile = ({ wallet }) => {
  const { data: profile, loading } = useApi(`/api/wallet/${wallet}/profile`);
  const { data: strategy }         = useApi(`/api/wallet/${wallet}/strategy`);
  const { data: micro }            = useApi(`/api/wallet/${wallet}/microstructure`);
  return (
    <Panel title={`PROFILE · ${truncateAddr(wallet)}`}>
      {loading && <SkeletonLine height={120} />}
      {!loading && (
        <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr 1fr', gap: 'var(--grid-gap)' }}>
          <div>
            <SectionLabel>STRATEGY FINGERPRINT (R8)</SectionLabel>
            <StrategyFingerprintBar probs={strategy?.probs || {}} loading={!strategy} />
          </div>
          <div>
            <SectionLabel>MICROSTRUCTURE (R11)</SectionLabel>
            {micro ? (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '4px 8px', fontSize: 'var(--font-size-sm)' }}>
                <span style={{ color: T.text.tertiary }}>cancel_to_fill_30d</span><span>{micro.cancel_to_fill_ratio_30d ?? '—'}</span>
                <span style={{ color: T.text.tertiary }}>place_to_fill_p50_s</span><span>{micro.place_to_fill_seconds_p50 ?? '—'}</span>
                <span style={{ color: T.text.tertiary }}>iceberg_score_30d</span><span>{micro.iceberg_score_30d ?? '—'}</span>
                <span style={{ color: T.text.tertiary }}>spoof_score_30d</span><span>{micro.spoof_score_30d ?? '—'}</span>
                <span style={{ color: T.text.tertiary }}>n_orders_30d</span><span>{micro.n_orders_30d ?? '—'}</span>
              </div>
            ) : <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>No microstructure signature yet.</div>}
          </div>
          <div>
            <SectionLabel>PROFILE</SectionLabel>
            <div style={{ fontSize: 'var(--font-size-sm)', display: 'grid', gridTemplateColumns: '1fr auto', gap: '4px 8px' }}>
              <span style={{ color: T.text.tertiary }}>Tier</span><span>{profile?.depth_tier ?? '—'}</span>
              <span style={{ color: T.text.tertiary }}>Falcon</span><span>{profile?.falcon_score ? Number(profile.falcon_score).toFixed(2) : '—'}</span>
              <span style={{ color: T.text.tertiary }}>Trades 30d</span><span>{profile?.trades_30d ?? '—'}</span>
              <span style={{ color: T.text.tertiary }}>Win rate</span><span>{profile?.win_rate ? fmtPct(profile.win_rate * 100, 1) : '—'}</span>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
};

window.WalletLabTab = WalletLabTab;

// ============================================================================
// 4. MEMPOOL — Live / Pool / Decisions (R7)
// ============================================================================

const MempoolTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  const { data: summary, loading } = useApi('/api/mempool/summary', { interval: 3000 });
  const kpis = [
    { label: 'INTENT/MIN',      value: summary?.intents_per_min ?? 0, loading },
    { label: 'DECODE HIT %',    value: summary ? fmtPct((summary.decode_hit_rate ?? 0) * 100, 1) : '—', loading },
    { label: 'POOL SIZE',       value: `${summary?.pool_size ?? 0}/${summary?.pool_capacity ?? 40}`, loading },
    { label: 'POOL FRESH %',    value: summary ? fmtPct((summary.pool_freshness_pct ?? 0) * 100, 0) : '—', loading },
    { label: 'INTENT→FIRE p50', value: summary ? fmtMs(summary.intent_to_fire_p50_ms) : '—', loading },
    { label: 'SHADOW FIRES',    value: summary?.shadow_fires_24h ?? 0, loading },
    { label: 'LIVE FIRES',      value: summary?.live_fires_24h ?? 0, loading },
    { label: 'NONCE CHAINS',    value: summary?.active_nonce_chains ?? 0, loading },
  ];
  return (
    <>
      <BreadcrumbHeader
        tab="MEMPOOL"
        subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label}
        chips={chips}
      />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--section-padding)' }}>
        {tab.subTab === 'live'      && <MempoolLive />}
        {tab.subTab === 'pool'      && <MempoolPool />}
        {tab.subTab === 'decisions' && <MempoolDecisions />}
      </div>
    </>
  );
};

const MempoolLive = () => {
  const { data: live, loading } = useApi('/api/mempool/live', { interval: 2000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });
  const events = live?.intents || [];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      {!gateState?.prefill_live_enabled && (
        <GatedBanner
          flag="prefill_live_enabled"
          route="OPERATIONS → Risk"
          description="Mempool watcher is active but pool firing is in SHADOW mode (paper-only). Live firing requires the 30-day shadow soak + CLOBClientWrapper.sign+submit split."
        />
      )}
      <Panel title={`LIVE INTENT FEED · ${events.length} intent${events.length === 1 ? '' : 's'}`}>
        <LiveStreamFeed
          events={events}
          loading={loading}
          emptyMessage="No leader intents in flight. Polygon mempool subscription idle — verify R7 daemon is running."
          rowKey={(e, i) => e.intent_id || i}
          renderRow={(e) => (
            <>
              <td style={{ padding: '6px 8px', color: T.text.tertiary, fontFamily: 'var(--font-mono)' }}>T-{fmtAge(e.age_s)}</td>
              <td style={{ padding: '6px 8px' }}><WalletCell wallet={e.wallet} /></td>
              <td style={{ padding: '6px 8px', color: e.side === 'buy' ? T.status.buy : T.status.sell, fontWeight: 600 }}>{(e.side || '').toUpperCase()}</td>
              <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{e.size_usdc != null ? `$${Number(e.size_usdc).toLocaleString()}` : '—'}</td>
              <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{e.price != null ? Number(e.price).toFixed(3) : '—'}</td>
              <td style={{ padding: '6px 8px' }}><MarketCell title={e.market_title || '—'} /></td>
              <td style={{ padding: '6px 8px' }}>
                <StatusPill status={e.decoded ? 'ok' : 'warn'} label={e.decoded ? 'DECODED' : (e.skip_reason || 'NOT_CLOB')} />
              </td>
            </>
          )}
        />
      </Panel>

      <Panel title="NONCE REPLACEMENT CHAINS (gas-price wars)">
        {live?.nonce_chains?.length ? (
          <DataTable
            dense
            columns={[
              { key: 'wallet', label: 'WALLET', render: v => <WalletCell wallet={v} /> },
              { key: 'nonce',  label: 'NONCE' },
              { key: 'chain_summary', label: 'CHAIN' },
              { key: 'state',  label: 'STATE' },
            ]}
            rows={live.nonce_chains}
            rowKey={r => `${r.wallet}-${r.nonce}`}
          />
        ) : (
          <div style={{ color: T.text.tertiary, padding: 12, fontSize: 'var(--font-size-sm)' }}>
            No active replacement chains in window.
          </div>
        )}
      </Panel>
    </div>
  );
};

const MempoolPool = () => {
  const { data: pool, loading } = useApi('/api/mempool/pool', { interval: 2000 });
  return (
    <Panel title="PRE-SIGNED POOL INVENTORY" subtitle="Buckets warm-fired and held in memory. Stale items rotate opportunistically.">
      <DataTable
        loading={loading}
        emptyMessage="Pool empty. PreSignedPool.warm() runs hourly + on new intents."
        columns={[
          { key: 'market_title', label: 'MARKET', render: v => <MarketCell title={v} /> },
          { key: 'token_side',   label: 'TOKEN · SIDE' },
          { key: 'bucket_usdc',  label: 'BUCKET $', align: 'right',
            render: v => v != null ? `$${Number(v).toLocaleString()}` : '—' },
          { key: 'age_s',        label: 'AGE', align: 'right',
            render: v => v != null ? fmtAge(v) : '—' },
          { key: 'status',       label: 'STATUS', render: v => <StatusPill status={v === 'ready' ? 'ok' : v === 'expired' ? 'warn' : 'pending'} label={(v || '').toUpperCase()} /> },
        ]}
        rows={pool?.entries || []}
        rowKey={r => r.entry_id}
      />
      {pool?.miss_reasons_last_hour && (
        <div style={{ marginTop: 12, fontSize: 'var(--font-size-xs)', color: T.text.tertiary }}>
          Miss reasons last hour: {Object.entries(pool.miss_reasons_last_hour).map(([k,v]) => `${k} ${v}`).join(' · ')}
        </div>
      )}
    </Panel>
  );
};

const MempoolDecisions = () => {
  const [filter, setFilter] = useState('all');
  const { data: dec, loading } = useApi(`/api/mempool/decisions?filter=${filter}`, { interval: 5000 });
  const filters = ['all', 'killswitch_off', 'confidence_skip', 'size_cap', 'cooldown', 'shadow', 'pool_miss', 'filled', 'error'];
  return (
    <Panel
      title="INTENT ROUTER DECISIONS"
      action={
        <select value={filter} onChange={e => setFilter(e.target.value)}>
          {filters.map(f => <option key={f} value={f}>{f}</option>)}
        </select>
      }
    >
      <DataTable
        loading={loading}
        emptyMessage="No router decisions match this filter."
        columns={[
          { key: 'time',   label: 'WHEN' },
          { key: 'wallet', label: 'LEADER', render: v => <WalletCell wallet={v} /> },
          { key: 'market', label: 'MARKET', render: v => <MarketCell title={v} /> },
          { key: 'result', label: 'RESULT',
            render: v => <StatusPill
              status={v === 'filled' ? 'ok' : v === 'error' ? 'err' : v === 'shadow' ? 'info' : 'warn'}
              label={(v || '').toUpperCase()}
            /> },
          { key: 'detail', label: 'DETAIL', color: T.text.tertiary },
        ]}
        rows={dec?.decisions || []}
        rowKey={r => r.decision_id}
      />
    </Panel>
  );
};

window.MempoolTab = MempoolTab;

// ============================================================================
// 5. MICROSCOPE — Firehose / Microstructure / Signatures (R11)
// ============================================================================

const MicroscopeTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  const { data: summary, loading } = useApi('/api/microscope/summary', { interval: 3000 });
  const kpis = [
    { label: 'EVENTS/SEC',       value: summary?.events_per_sec ?? 0, loading },
    { label: 'QUEUE DEPTH',      value: `${summary?.queue_depth ?? 0}/${summary?.queue_capacity ?? 50000}`, loading },
    { label: 'DROPPED 24H',      value: summary?.dropped_24h ?? 0, color: (summary?.dropped_24h ?? 0) > 0 ? T.status.warn : T.status.ok, loading },
    { label: 'ICEBERG/HR',       value: summary?.iceberg_per_hour ?? 0, loading },
    { label: 'SPOOF/HR',         value: summary?.spoof_per_hour ?? 0, loading },
    { label: 'OFI MEAN',         value: summary?.ofi_mean != null ? Number(summary.ofi_mean).toFixed(2) : '—', loading },
    { label: 'PLACE→FILL p50',   value: summary?.place_to_fill_p50_ms ? fmtMs(summary.place_to_fill_p50_ms) : '—', loading },
    { label: 'STORAGE',          value: summary?.storage_gb != null ? `${summary.storage_gb.toFixed(0)} GB` : '—', loading },
  ];
  return (
    <>
      <BreadcrumbHeader tab="MICROSCOPE" subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label} chips={chips} />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--section-padding)' }}>
        {tab.subTab === 'firehose'       && <MicroscopeFirehose />}
        {tab.subTab === 'microstructure' && <MicroscopeMicrostructure />}
        {tab.subTab === 'signatures'     && <MicroscopeSignatures />}
      </div>
    </>
  );
};

const MicroscopeFirehose = () => {
  const { data: live, loading } = useApi('/api/microscope/firehose', { interval: 2000 });
  return (
    <Panel title="L3 BOOK EVENT FIREHOSE">
      <LiveStreamFeed
        events={live?.events || []}
        loading={loading}
        emptyMessage="No L3 events. polymarket-book-l3.service must be running."
        renderRow={(e) => (
          <>
            <td style={{ padding: '6px 8px', color: T.text.tertiary, fontFamily: 'var(--font-mono)' }}>T-{fmtAge(e.age_s)}</td>
            <td style={{ padding: '6px 8px' }}><MarketCell title={e.market_title || '—'} /></td>
            <td style={{ padding: '6px 8px', color: e.side === 'buy' ? T.status.buy : T.status.sell }}>{(e.side || '').toUpperCase()}</td>
            <td style={{ padding: '6px 8px' }}>
              <StatusPill
                status={e.event_type === 'cancelled' ? 'warn' : e.event_type === 'filled' ? 'ok' : 'info'}
                label={(e.event_type || '').toUpperCase()}
              />
            </td>
            <td style={{ padding: '6px 8px', textAlign: 'right' }}>{e.size_delta != null ? Number(e.size_delta).toLocaleString() : '—'}</td>
            <td style={{ padding: '6px 8px' }}>{e.wallet ? <WalletCell wallet={e.wallet} /> : <span style={{ color: T.text.tertiary }}>—</span>}</td>
          </>
        )}
        rowKey={e => e.event_id}
      />
    </Panel>
  );
};

const MicroscopeMicrostructure = () => {
  const { data: rollup, loading } = useApi('/api/microscope/microstructure?limit=50', { interval: 30000 });
  return (
    <Panel title="MICROSTRUCTURE PER MARKET · TOP 50 BY VOLUME">
      <DataTable
        loading={loading}
        emptyMessage="No microstructure rollups yet."
        columns={[
          { key: 'market_title', label: 'MARKET', render: v => <MarketCell title={v} /> },
          { key: 'iceberg_orders_count', label: 'ICEBERG', align: 'right' },
          { key: 'spoof_orders_count',   label: 'SPOOF',   align: 'right' },
          { key: 'ofi_mean', label: 'OFI MEAN', align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
          { key: 'ofi_max',  label: 'OFI MAX',  align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
        ]}
        rows={rollup?.rows || []}
        rowKey={r => r.market_id}
      />
    </Panel>
  );
};

const MicroscopeSignatures = () => {
  const { data: sig, loading } = useApi('/api/microscope/signatures?limit=100', { interval: 60000 });
  return (
    <Panel title="WALLET MICROSTRUCTURE SIGNATURES · TIER-0/1">
      <DataTable
        loading={loading}
        emptyMessage="No wallet signatures yet — nightly batch hasn't run."
        columns={[
          { key: 'wallet_address',          label: 'WALLET', render: v => <WalletCell wallet={v} /> },
          { key: 'cancel_to_fill_ratio_30d',label: 'CANCEL/FILL', align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
          { key: 'iceberg_score_30d',       label: 'ICEBERG',   align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
          { key: 'spoof_score_30d',         label: 'SPOOF',     align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
          { key: 'place_to_fill_seconds_p50', label: 'P→F p50', align: 'right',
            render: v => v != null ? `${Number(v).toFixed(1)}s` : '—' },
          { key: 'n_fills_30d',             label: 'N FILLS', align: 'right' },
        ]}
        rows={sig?.signatures || []}
        rowKey={r => r.wallet_address}
      />
    </Panel>
  );
};

window.MicroscopeTab = MicroscopeTab;

// ============================================================================
// 6. PERIPHERY — Social / Cross-market / Resolution / Instruments (R12 + R10)
// ============================================================================

const PeripheryTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  const { data: summary, loading } = useApi('/api/periphery/summary', { interval: 10000 });
  const kpis = [
    { label: 'TWEETS 24H',         value: summary?.tweets_24h ?? 0, loading },
    { label: 'ENTRY %',            value: summary ? fmtPct((summary.entry_pct ?? 0) * 100, 0) : '—', loading },
    { label: 'EXIT %',             value: summary ? fmtPct((summary.exit_pct ?? 0) * 100, 0) : '—', loading },
    { label: 'X QUOTA %',          value: summary ? fmtPct((summary.x_quota_pct ?? 0) * 100, 0) : '—', color: (summary?.x_quota_pct ?? 1) < 0.1 ? T.status.err : T.status.ok, loading },
    { label: 'OPERATORS RESOLVED', value: summary?.operators_resolved ?? 0, loading },
    { label: 'PENDING REVIEW',     value: summary?.operators_pending ?? 0, color: (summary?.operators_pending ?? 0) > 0 ? T.status.warn : T.status.ok, loading },
    { label: 'INSTRUMENT EVENTS 24H', value: summary?.instrument_events_24h ?? 0, loading },
  ];
  return (
    <>
      <BreadcrumbHeader tab="PERIPHERY" subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label} chips={chips} />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--section-padding)' }}>
        {tab.subTab === 'social'      && <PeripherySocial />}
        {tab.subTab === 'crossmarket' && <PeripheryCrossMarket />}
        {tab.subTab === 'resolution'  && <PeripheryResolution />}
        {tab.subTab === 'instruments' && <PeripheryInstruments />}
      </div>
    </>
  );
};

const PeripherySocial = () => {
  const { data: feed, loading } = useApi('/api/periphery/social/feed', { interval: 5000 });
  return (
    <Panel title="SOCIAL SIGNAL FEED · X / Telegram / Discord">
      <LiveStreamFeed
        events={feed?.signals || []}
        loading={loading}
        emptyMessage="No social signals captured. Configure X API key + Telegram/Discord channels."
        renderRow={(e) => (
          <>
            <td style={{ padding: '6px 8px', color: T.text.tertiary }}>T-{fmtAge(e.age_s)}</td>
            <td style={{ padding: '6px 8px' }}><StatusPill status="info" label={(e.source || '').toUpperCase()} /></td>
            <td style={{ padding: '6px 8px', color: T.accent.violet, fontFamily: 'var(--font-mono)' }}>@{e.author_handle}</td>
            <td style={{ padding: '6px 8px' }}>
              <StatusPill
                status={e.intent === 'entry_signal' ? 'ok' : e.intent === 'exit_signal' ? 'warn' : 'gated'}
                label={(e.intent || '').toUpperCase()}
              />
            </td>
            <td style={{ padding: '6px 8px', textAlign: 'right', color: T.text.tertiary }}>{e.intent_confidence != null ? e.intent_confidence.toFixed(2) : '—'}</td>
            <td style={{ padding: '6px 8px', color: T.text.primary, maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.text}</td>
          </>
        )}
        rowKey={e => e.signal_id}
      />
    </Panel>
  );
};

const PeripheryCrossMarket = () => {
  const { data: status } = useApi('/api/periphery/crossmarket/status', { interval: 30000 });
  const venues = [
    { name: 'Kalshi',    icon: 'database', status: status?.kalshi },
    { name: 'Manifold',  icon: 'database', status: status?.manifold },
    { name: 'PredictIt', icon: 'database', status: status?.predictit },
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 'var(--grid-gap)' }}>
      {venues.map(v => (
        <Panel key={v.name} title={v.name.toUpperCase()}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Icon name={v.icon} size={20} color={v.status?.reachable ? T.status.ok : T.status.err} />
            <StatusPill
              status={v.status?.reachable ? 'running' : 'err'}
              label={v.status?.reachable ? 'REACHABLE' : 'DOWN'}
            />
          </div>
          <div style={{ marginTop: 12, fontSize: 'var(--font-size-sm)', color: T.text.secondary, display: 'grid', gridTemplateColumns: '1fr auto', gap: '4px 8px' }}>
            <span>Latency p50</span><span>{v.status?.latency_p50_ms ? fmtMs(v.status.latency_p50_ms) : '—'}</span>
            <span>API calls 24h</span><span>{v.status?.api_calls_24h ?? '—'}</span>
            <span>Positions obs.</span><span>{v.status?.positions_observed ?? '—'}</span>
          </div>
        </Panel>
      ))}
    </div>
  );
};

const PeripheryResolution = () => {
  const { data: ops, loading } = useApi('/api/periphery/crossmarket/operators', { interval: 30000 });
  const handleConfirm = useCallback((id) => {
    fetch(`/api/periphery/crossmarket/confirm/${id}`, { method: 'POST' });
  }, []);
  return (
    <Panel title="CROSS-MARKET OPERATORS · Manual-in-the-loop resolution">
      {loading ? (
        <SkeletonLine height={120} />
      ) : !ops?.operators?.length ? (
        <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>
          No operators resolved yet. Operator manual-seed phase: insert into <code>cross_market_operators</code> with confidence ≥ 0.8.
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 'var(--grid-gap)' }}>
          {ops.operators.map(op => (
            <CrossMarketOperatorCard key={op.operator_id} operator={op} onConfirm={handleConfirm} />
          ))}
        </div>
      )}
    </Panel>
  );
};

const PeripheryInstruments = () => {
  const { data: instr, loading } = useApi('/api/intelligence/causal/instruments', { interval: 30000 });
  return (
    <Panel title="INSTRUMENT EVENT TIMELINE · LAST 24H (R10)">
      <EventTimelineTrack events={instr?.events || []} loading={loading} window={24} />
    </Panel>
  );
};

window.PeripheryTab = PeripheryTab;

// ============================================================================
// 7. EXECUTION — Portfolio / Decisions / Inspector (merged from v1)
// ============================================================================

const ExecutionTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  const { data: summary, loading } = useApi('/api/execution/summary', { interval: 3000 });
  const kpis = [
    { label: 'OPEN',            value: `${summary?.positions_open ?? 0}/${summary?.max_positions ?? 10}`, loading },
    { label: 'FILLED 24H',      value: summary?.filled_24h ?? 0, loading },
    { label: 'SHADOW 24H',      value: summary?.shadow_24h ?? 0, color: T.status.info, loading },
    { label: 'DECISIONS/HR',    value: summary?.decisions_per_hour ?? 0, loading },
    { label: 'ACTIONABLE',      value: summary?.actionable ?? 0, color: T.accent.amber, loading },
    { label: 'NET PNL',         value: fmtPnl(summary?.net_pnl ?? 0), color: (summary?.net_pnl ?? 0) >= 0 ? T.status.ok : T.status.err, loading },
  ];
  return (
    <>
      <BreadcrumbHeader tab="EXECUTION" subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label} chips={chips} />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <KpiStrip items={kpis} loading={loading} />
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--section-padding)' }}>
        {tab.subTab === 'portfolio' && <ExecutionPortfolio />}
        {tab.subTab === 'decisions' && <ExecutionDecisions />}
        {tab.subTab === 'inspector' && <ExecutionInspector />}
      </div>
    </>
  );
};

const ExecutionPortfolio = () => {
  const { data: portfolio, loading } = useApi('/api/positions/live', { interval: 3000 });
  return (
    <Panel title="OPEN POSITIONS">
      <DataTable
        loading={loading}
        emptyMessage="No open positions."
        columns={[
          { key: 'opened_at',    label: 'OPENED' },
          { key: 'market_title', label: 'MARKET', render: v => <MarketCell title={v} /> },
          { key: 'direction',    label: 'DIR', render: v => (
            <span style={{ color: v === 'yes' ? T.status.buy : T.status.sell, fontWeight: 600 }}>{(v || '').toUpperCase()}</span>
          )},
          { key: 'entry_price',  label: 'ENTRY', align: 'right',
            render: v => v != null ? Number(v).toFixed(3) : '—' },
          { key: 'size_usdc',    label: 'SIZE $', align: 'right',
            render: v => v != null ? `$${Number(v).toLocaleString()}` : '—' },
          { key: 'unrealized_pnl', label: 'UNREAL P&L', align: 'right',
            render: v => fmtPnl(v),
            color: T.text.primary },
          { key: 'leader_wallet', label: 'LEADER', render: v => <WalletCell wallet={v} /> },
        ]}
        rows={portfolio?.positions || []}
        rowKey={r => r.id}
      />
    </Panel>
  );
};

const ExecutionDecisions = () => {
  const { data: dec, loading } = useApi('/api/decisions?limit=200', { interval: 5000 });
  return (
    <Panel title="RECENT DECISIONS · ALL SOURCES (confidence_engine + intent_router)">
      <DataTable
        loading={loading}
        emptyMessage="No decisions logged."
        columns={[
          { key: 'time',          label: 'WHEN' },
          { key: 'leader_wallet', label: 'LEADER', render: v => <WalletCell wallet={v} /> },
          { key: 'market_title',  label: 'MARKET', render: v => <MarketCell title={v} /> },
          { key: 'action',        label: 'ACTION',
            render: v => <StatusPill
              status={v === 'follow' ? 'ok' : v === 'fade' ? 'warn' : 'gated'}
              label={(v || '').toUpperCase()}
            /> },
          { key: 'confidence',    label: 'CONF.', align: 'right',
            render: v => v != null ? Number(v).toFixed(2) : '—' },
          { key: 'reason',        label: 'REASON', color: T.text.tertiary },
          { key: 'outcome',       label: 'OUTCOME',
            render: v => v ? <StatusPill status={v === 'win' ? 'ok' : 'err'} label={v.toUpperCase()} /> : <span style={{ color: T.text.tertiary }}>pending</span> },
        ]}
        rows={dec?.decisions || []}
        rowKey={r => r.id}
      />
    </Panel>
  );
};

const ExecutionInspector = () => {
  const { data: insp, loading } = useApi('/api/inspector/snapshot', { interval: 3000 });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      <Panel title="PIPELINE HEALTH">
        {insp?.pipeline_health ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, fontSize: 'var(--font-size-sm)' }}>
            <div><Dot status={insp.pipeline_health.redis_reachable ? 'ok' : 'err'} /> Redis reachable</div>
            <div><Dot status={insp.pipeline_health.ws_age_s < 30 ? 'ok' : 'warn'} /> WS age {fmtAge(insp.pipeline_health.ws_age_s)}</div>
            <div><Dot status="info" /> Msgs/min {insp.pipeline_health.msgs_per_minute}</div>
            <div><Dot status="info" /> Subs {insp.pipeline_health.subscribers}</div>
          </div>
        ) : <SkeletonLine height={32} />}
      </Panel>
      <Panel title="RAW TRADES · LAST 5 MIN">
        <DataTable
          loading={loading}
          emptyMessage="No raw trades."
          dense
          columns={[
            { key: 'time',   label: 'TIME' },
            { key: 'wallet', label: 'WALLET', render: v => <WalletCell wallet={v} /> },
            { key: 'side',   label: 'SIDE', render: v => (
              <span style={{ color: v === 'buy' ? T.status.buy : T.status.sell, fontWeight: 600 }}>{(v || '').toUpperCase()}</span>
            )},
            { key: 'price',  label: 'PRICE', align: 'right',
              render: v => v != null ? Number(v).toFixed(3) : '—' },
            { key: 'size_usdc', label: 'SIZE $', align: 'right',
              render: v => v != null ? `$${Number(v).toLocaleString()}` : '—' },
            { key: 'source', label: 'SOURCE',
              render: v => <StatusPill status="info" label={v?.toUpperCase()} /> },
            { key: 'market', label: 'MARKET' },
          ]}
          rows={insp?.trades || []}
          rowKey={r => `${r.time}-${r.wallet}-${r.market}`}
        />
      </Panel>
    </div>
  );
};

window.ExecutionTab = ExecutionTab;

// ============================================================================
// 8. OPERATIONS — Risk / Health / Calibration / Research
// ============================================================================

const OperationsTab = ({ tab, setTab, tabDef }) => {
  const chips = useScopeChips();
  return (
    <>
      <BreadcrumbHeader tab="OPERATIONS" subTab={tabDef.subTabs.find(s => s.id === tab.subTab)?.label} chips={chips} />
      <SubTabNav tabs={tabDef.subTabs} value={tab.subTab} onChange={s => setTab({ ...tab, subTab: s })} />
      <div style={{ flex: 1, overflow: 'auto' }}>
        {tab.subTab === 'risk'        && <OperationsRisk />}
        {tab.subTab === 'health'      && <OperationsHealth />}
        {tab.subTab === 'calibration' && <OperationsCalibration />}
        {tab.subTab === 'research'    && <OperationsResearch />}
      </div>
    </>
  );
};

const OperationsRisk = () => {
  const { data: risk, loading } = useApi('/api/risk', { interval: 5000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });
  const { data: history } = useApi('/api/risk/history?limit=20', { interval: 30000 });
  const toggleFlag = useCallback((flag, next) => {
    fetch('/api/risk/update', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [flag]: next }) });
  }, []);
  return (
    <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      <Panel title="RUNTIME GATE FLAGS">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <GateToggle
            value={!!gateState?.strategy_conditional_confidence_enabled}
            onChange={(v) => toggleFlag('strategy_conditional_confidence_enabled', v)}
            label="strategy_conditional_confidence_enabled (R8)"
            description="Apply STRATEGY_WEIGHTS multiplier to FOLLOW/FADE per leader's R8 strategy class."
          />
          <GateToggle
            value={!!gateState?.volume_anticipation_enabled}
            onChange={(v) => toggleFlag('volume_anticipation_enabled', v)}
            label="volume_anticipation_enabled (R9)"
            description="Activate the volume_anticipation decision policy on R9 forecast threshold breaches."
          />
          <GateToggle
            value={!!gateState?.causal_gating_enabled}
            onChange={(v) => toggleFlag('causal_gating_enabled', v)}
            label="causal_gating_enabled (R10) — methodology audit required"
            description="Use R10 IV-corrected ATE to gate follow/fade confidence. Pure-Hawkes applies when OFF."
          />
          <GateToggle
            value={!!gateState?.prefill_live_enabled}
            onChange={(v) => toggleFlag('prefill_live_enabled', v)}
            label="prefill_live_enabled (R7) — operator gates required"
            description="Allow the IntentRouter to fire pre-signed orders LIVE. Shadow only when OFF."
          />
        </div>
      </Panel>

      <Panel title="RISK COCKPIT">
        {risk ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--grid-gap)' }}>
            <RiskKnob label="RISK / TRADE %" value={risk.risk_per_trade_pct} />
            <RiskKnob label="MAX EXPOSURE %" value={risk.max_total_exposure_pct} />
            <RiskKnob label="KELLY FRACTION" value={risk.kelly_fraction} />
            <RiskKnob label="FADE SIZE RATIO" value={risk.fade_size_ratio} />
            <RiskKnob label="MAX DRAWDOWN %" value={risk.max_drawdown_stop_pct} />
            <RiskKnob label="MAX CONS. LOSSES" value={risk.max_consecutive_losses} />
            <RiskKnob label="COOLDOWN (S)" value={risk.cooldown_seconds} />
            <RiskKnob label="MAX POSITIONS" value={risk.max_concurrent_positions} />
          </div>
        ) : <SkeletonLine height={80} />}
      </Panel>

      <Panel title="AUDIT LOG · RISK CONFIG CHANGES">
        <AuditLogTable rows={history?.rows || []} loading={!history} />
      </Panel>
    </div>
  );
};

const RiskKnob = ({ label, value }) => (
  <div style={{ background: T.bg.input, padding: 12, borderRadius: 'var(--radius-md)' }}>
    <div style={{ fontSize: 'var(--font-size-xs)', color: T.text.tertiary, letterSpacing: 'var(--letter-spacing-wide)', textTransform: 'uppercase' }}>{label}</div>
    <div style={{ fontSize: 'var(--font-size-lg)', color: T.text.primary, fontWeight: 600, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>
      {value != null ? value : '—'}
    </div>
  </div>
);

const OperationsHealth = () => {
  const { data: health, loading } = useApi('/api/data-quality', { interval: 10000 });
  const { data: daemons } = useApi('/api/system', { interval: 5000 });
  return (
    <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      <Panel title="DAEMON REGISTRY">
        {daemons?.daemons?.length ? (
          <DataTable
            dense
            columns={[
              { key: 'name', label: 'DAEMON' },
              { key: 'status', label: 'STATUS', render: v => <StatusPill status={v === 'running' ? 'running' : v === 'stopped' ? 'stopped' : 'warn'} label={(v || '').toUpperCase()} /> },
              { key: 'last_heartbeat_at', label: 'LAST HEARTBEAT' },
              { key: 'restart_count', label: 'RESTARTS', align: 'right' },
              { key: 'memory_mb', label: 'MEM MB', align: 'right' },
            ]}
            rows={daemons.daemons}
            rowKey={r => r.name}
          />
        ) : <div style={{ color: T.text.tertiary, padding: 16, fontSize: 'var(--font-size-sm)' }}>
          Daemon registry not exposed by the API yet. Currently 15 systemd units expected.
        </div>}
      </Panel>

      <Panel title="DATA QUALITY ISSUES">
        {loading ? <SkeletonLine height={60} /> : !health?.issues?.length ? (
          <div style={{ color: T.status.ok, padding: 16, fontSize: 'var(--font-size-sm)' }}>No active issues — all green.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {health.issues.map((iss, i) => (
              <Banner key={i} tone="warn" icon="alert-triangle" title={iss.code}>
                {iss.description}
              </Banner>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="INGESTION SOURCES">
        {health?.sources ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 'var(--grid-gap)' }}>
            {health.sources.map(s => (
              <div key={s.name} style={{ background: T.bg.input, padding: 12, borderRadius: 'var(--radius-md)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <strong style={{ color: T.text.primary }}>{s.name}</strong>
                  <StatusPill status={s.health === 'healthy' ? 'ok' : s.health === 'degraded' ? 'warn' : 'err'} label={(s.health || '').toUpperCase()} />
                </div>
                <div style={{ fontSize: 'var(--font-size-sm)', color: T.text.tertiary, marginTop: 8 }}>
                  Lag {fmtMs(s.lag_ms || 0)} · {s.msgs_per_minute || 0} msgs/min
                </div>
              </div>
            ))}
          </div>
        ) : <SkeletonLine height={120} />}
      </Panel>
    </div>
  );
};

const OperationsCalibration = () => {
  const { data: losses, loading } = useApi('/api/calibration/losses?days=30', { interval: 60000 });
  const { data: drift } = useApi('/api/calibration/drift', { interval: 60000 });
  const { data: disabled } = useApi('/api/calibration/disabled', { interval: 30000 });
  const { data: gateState } = useApi('/api/control/state', { interval: 5000 });

  return (
    <div style={{ padding: 'var(--section-padding)', display: 'flex', flexDirection: 'column', gap: 'var(--grid-gap)' }}>
      {!gateState?.calibration_replay_enabled && (
        <MissingHookBanner title="DEFERRED HOOK — engine + position_tracker not yet wired">
          The R13 calibration daemon runs but receives 0 predictions until
          <code> record_decision_predictions</code> is called from
          <code> confidence_engine.decide()</code> (same transaction as
          decision_log INSERT) and <code>fill_actual_outcomes</code> from
          position_tracker close path. Pseudo-diff in
          <code> docs/audit/phase3/round13_wave3_review.md § 9</code>.
        </MissingHookBanner>
      )}

      <Panel title="PER-MODEL LOSS TRAJECTORY · 30D">
        <LineWithBand
          series={(losses?.series || []).map((s, i) => ({
            label: s.model,
            color: T.chart[`c${(i % 6) + 1}`],
            values: (s.points || []).map((p) => ({ x: p.day, y: p.loss })),
          }))}
          xLabel="day" yLabel="loss"
          loading={loading}
          emptyMessage="No calibration loss history yet — daemon runs nightly at 04:30 UTC."
        />
      </Panel>

      <div style={{ display: 'grid', gridTemplateColumns: '1.3fr 1fr', gap: 'var(--grid-gap)' }}>
        <Panel title="DRIFT GAUGES">
          {drift?.models ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {drift.models.map(m => (
                <div key={m.model} style={{ display: 'grid', gridTemplateColumns: '160px 1fr 60px 100px', alignItems: 'center', gap: 8, fontSize: 'var(--font-size-sm)' }}>
                  <span style={{ color: T.text.secondary, fontFamily: 'var(--font-mono)' }}>{m.model}</span>
                  <DriftBar z={m.z_score} threshold={2} />
                  <span style={{ textAlign: 'right', color: T.text.primary, fontVariantNumeric: 'tabular-nums' }}>z={m.z_score != null ? m.z_score.toFixed(2) : '—'}</span>
                  <StatusPill
                    status={m.protected ? 'protected' : m.disabled ? 'err' : 'ok'}
                    label={m.protected ? 'PROTECTED' : m.disabled ? 'DISABLED' : 'OK'}
                  />
                </div>
              ))}
            </div>
          ) : <SkeletonLine height={120} />}
        </Panel>

        <Panel title="AUTO-DISABLED MODELS">
          {disabled?.rows?.length ? (
            <DataTable
              dense
              columns={[
                { key: 'model', label: 'MODEL' },
                { key: 'disabled_at', label: 'AT' },
                { key: 'disabled_reason', label: 'REASON', color: T.text.tertiary },
                { key: 'auto_or_manual', label: 'KIND' },
                { key: 'enable', label: '', render: (_v, row) => (
                  <button onClick={() => fetch(`/api/calibration/enable/${row.model}`, { method: 'POST' })}
                          style={{ padding: '2px 8px', color: T.status.ok, border: `1px solid ${T.status.ok}`, fontSize: 'var(--font-size-xs)' }}>
                    Re-enable
                  </button>
                )},
              ]}
              rows={disabled.rows}
              rowKey={r => r.model}
            />
          ) : <div style={{ color: T.status.ok, padding: 16, fontSize: 'var(--font-size-sm)' }}>No models currently disabled.</div>}
        </Panel>
      </div>
    </div>
  );
};

const DriftBar = ({ z, threshold = 2 }) => {
  const maxAbs = Math.max(threshold * 1.5, Math.abs(z || 0));
  const center = 50;
  const value = z == null ? 0 : (z / maxAbs) * 50;
  const breach = Math.abs(z || 0) > threshold;
  return (
    <div style={{ position: 'relative', height: 10, background: T.bg.input, borderRadius: 'var(--radius-sm)' }}>
      <span style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: T.border.strong }} />
      <span style={{
        position: 'absolute', top: 1, bottom: 1,
        left: value >= 0 ? `${center}%` : `${center + value}%`,
        width: `${Math.abs(value)}%`,
        background: breach ? T.status.warn : T.status.info,
        borderRadius: 'var(--radius-sm)',
      }} />
    </div>
  );
};

const OperationsResearch = () => {
  const notebooks = [
    { id: '00_data_loader',                name: '00_data_loader',                summary: 'DuckDB views over cold tier.' },
    { id: '01_strategy_classifier_validation', name: '01_strategy_validation',     summary: 'R8 vs hand labels disagreement surface.' },
    { id: '02_causal_analysis',            name: '02_causal_analysis',           summary: 'R10 IV vs R9 Hawkes disagreement scatter.' },
    { id: '03_counterfactual_replay',      name: '03_counterfactual_replay',     summary: 'Interactive what-if via R10 replayer.' },
    { id: '04_what_if_explorer',           name: '04_what_if_explorer',          summary: 'Per-hypothesis sandbox.' },
    { id: '05_calibration_review',         name: '05_calibration_review',        summary: 'Per-model drift trajectories.' },
  ];
  return (
    <div style={{ padding: 'var(--section-padding)' }}>
      <Panel title="RESEARCH NOTEBOOK SUBSTRATE">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 'var(--grid-gap)' }}>
          {notebooks.map(nb => (
            <NotebookTile
              key={nb.id}
              name={nb.name}
              summary={nb.summary}
              lastRun="never"
              onRun={() => fetch(`/api/research/notebook/${nb.id}/run`, { method: 'POST' })}
              onOpen={() => window.open(`/research/notebooks/${nb.id}.ipynb`, '_blank')}
            />
          ))}
        </div>
      </Panel>
    </div>
  );
};

window.OperationsTab = OperationsTab;
