// ============================================================================
// Polymarket Bot — Dashboard v2 app shell
//
// 8 top-level tabs with sub-tab routing. Sidebar fixed left, breadcrumb
// header per tab, KPI strip below header, content fills remaining space.
//
// Tab structure per docs/UI_REDESIGN_PHASE3.md § 4.1-4.2:
//   1. OVERVIEW       — bento brain/eyes/hands/mirror + What Changed timeline
//   2. INTELLIGENCE   — sub: Maturity / Lens / Web / Causal
//   3. WALLET LAB     — sub: Universe / Graph / Scanner / Profile
//   4. MEMPOOL        — sub: Live / Pool / Decisions  (R7)
//   5. MICROSCOPE     — sub: Firehose / Microstructure / Signatures  (R11)
//   6. PERIPHERY      — sub: Social / Crossmarket / Resolution / Instruments  (R12+R10)
//   7. EXECUTION      — sub: Portfolio / Decisions / Inspector  (merged)
//   8. OPERATIONS     — sub: Risk / Health / Calibration / Research  (R13)
// ============================================================================

const { useState, useEffect, useMemo } = React;
const { T, Icon, StatusPill, useApi, fmtMs, fmtPnl } = window;

// Tab + sub-tab manifest. Components resolved at render time via window.
const TABS = [
  {
    id: 'overview', label: 'OVERVIEW', icon: 'home',
    component: () => window.OverviewTab,
  },
  {
    id: 'intelligence', label: 'INTELLIGENCE', icon: 'cpu',
    component: () => window.IntelligenceTab,
    subTabs: [
      { id: 'maturity', label: 'Maturity' },
      { id: 'lens',     label: 'Lens (R8)' },
      { id: 'web',      label: 'Web (R9)' },
      { id: 'causal',   label: 'Causal (R10)' },
    ],
  },
  {
    id: 'wallet', label: 'WALLET LAB', icon: 'users',
    component: () => window.WalletLabTab,
    subTabs: [
      { id: 'universe', label: 'Universe (R6)' },
      { id: 'graph',    label: 'Graph' },
      { id: 'scanner',  label: 'Scanner' },
      { id: 'profile',  label: 'Profile' },
    ],
  },
  {
    id: 'mempool', label: 'MEMPOOL', icon: 'zap',
    component: () => window.MempoolTab,
    subTabs: [
      { id: 'live',      label: 'Live feed' },
      { id: 'pool',      label: 'Prefill pool' },
      { id: 'decisions', label: 'Router decisions' },
    ],
  },
  {
    id: 'microscope', label: 'MICROSCOPE', icon: 'microscope',
    component: () => window.MicroscopeTab,
    subTabs: [
      { id: 'firehose',       label: 'Firehose' },
      { id: 'microstructure', label: 'Microstructure' },
      { id: 'signatures',     label: 'Wallet signatures' },
    ],
  },
  {
    id: 'periphery', label: 'PERIPHERY', icon: 'radar',
    component: () => window.PeripheryTab,
    subTabs: [
      { id: 'social',      label: 'Social feed' },
      { id: 'crossmarket', label: 'Cross-market' },
      { id: 'resolution',  label: 'Resolution' },
      { id: 'instruments', label: 'Instruments (R10)' },
    ],
  },
  {
    id: 'execution', label: 'EXECUTION', icon: 'target',
    component: () => window.ExecutionTab,
    subTabs: [
      { id: 'portfolio', label: 'Portfolio' },
      { id: 'decisions', label: 'Decisions' },
      { id: 'inspector', label: 'Inspector' },
    ],
  },
  {
    id: 'operations', label: 'OPERATIONS', icon: 'settings',
    component: () => window.OperationsTab,
    subTabs: [
      { id: 'risk',        label: 'Risk & Config' },
      { id: 'health',      label: 'Health' },
      { id: 'calibration', label: 'Calibration (R13)' },
      { id: 'research',    label: 'Research' },
    ],
  },
];

// ── Sidebar ──────────────────────────────────────────────────────────────
const Sidebar = ({ tab, onTabChange }) => {
  const [time, setTime] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const { data: systemData } = useApi('/api/system', { interval: 5000 });
  const { data: overview } = useApi('/api/overview', { interval: 3000 });

  const bot = overview?.bot || {};
  const ingestion = overview?.ingestion || {};
  const ws = systemData?.ws || {};

  const wsStatus = ws.connected ? 'running' : 'stopped';
  const botStatus = bot.status === 'running' ? 'running'
                  : bot.status === 'stopped' ? 'stopped'
                  : 'warn';
  const execStatus = bot.execution_enabled ? 'running' : 'gated';

  const sysRows = [
    { label: 'BOT',       status: botStatus, value: (bot.status || '—').toUpperCase() },
    { label: 'WS',        status: wsStatus, value: ws.connected ? 'LIVE' : 'OFFLINE' },
    { label: 'INGESTION', status: ingestion.live_markets > 0 ? 'running' : 'warn',
      value: ingestion.total_markets ? `${ingestion.live_markets || 0}/${ingestion.total_markets}` : '—' },
    { label: 'EXEC',      status: execStatus, value: bot.execution_enabled ? 'LIVE' : 'DRY RUN' },
  ];

  return (
    <aside style={{
      width: 'var(--sidebar-width)',
      flexShrink: 0,
      background: T.bg.panel,
      borderRight: `1px solid ${T.border.subtle}`,
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {/* Brand */}
      <header style={{ padding: '14px 14px 12px', borderBottom: `1px solid ${T.border.subtle}` }}>
        <div style={{
          fontSize: 13, fontWeight: 700, color: T.accent.amber,
          letterSpacing: 'var(--letter-spacing-wide)',
        }}>POLYMARKET</div>
        <div style={{
          fontSize: 9, color: T.text.tertiary,
          letterSpacing: 'var(--letter-spacing-wide)', marginTop: 2,
        }}>TRADING BOT · v2</div>
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <StatusPill status={wsStatus === 'running' ? 'running' : 'err'} label={ws.connected ? 'LIVE' : 'OFFLINE'} />
          <span style={{
            fontSize: 10, color: T.text.tertiary,
            marginLeft: 'auto', fontFamily: 'var(--font-mono)',
            fontVariantNumeric: 'tabular-nums',
          }}>{time.toLocaleTimeString('en-GB')}</span>
        </div>
      </header>

      {/* Nav */}
      <nav style={{ flex: 1, paddingTop: 6, overflow: 'auto' }} aria-label="Primary navigation">
        {TABS.map(item => {
          const active = tab.id === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onTabChange({ id: item.id, subTab: item.subTabs?.[0]?.id })}
              aria-current={active ? 'page' : undefined}
              style={{
                width: '100%',
                textAlign: 'left',
                background: active ? 'rgba(232,160,32,0.10)' : 'transparent',
                color: active ? T.accent.amber : T.text.secondary,
                border: 'none',
                borderLeft: `2px solid ${active ? T.accent.amber : 'transparent'}`,
                padding: '8px 14px',
                fontSize: 11,
                letterSpacing: 'var(--letter-spacing-wide)',
                fontWeight: active ? 700 : 500,
                display: 'flex',
                alignItems: 'center',
                gap: 10,
              }}
            >
              <Icon name={item.icon} size={14} color={active ? T.accent.amber : T.text.tertiary} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      {/* System block */}
      <footer style={{ borderTop: `1px solid ${T.border.subtle}`, padding: '10px 14px' }}>
        <div style={{
          fontSize: 9, color: T.text.tertiary,
          letterSpacing: 'var(--letter-spacing-wide)', marginBottom: 8,
        }}>SYSTEM</div>
        {sysRows.map(r => (
          <div key={r.label} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0' }}>
            <window.Dot status={r.status} size={6} />
            <span style={{ fontSize: 9, color: T.text.tertiary, flex: 1, letterSpacing: 'var(--letter-spacing-wide)' }}>{r.label}</span>
            <span style={{ fontSize: 10, color: T.text.primary, fontWeight: 600 }}>{r.value}</span>
          </div>
        ))}
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${T.border.subtle}` }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10 }}>
            <span style={{ color: T.text.tertiary, letterSpacing: 'var(--letter-spacing-wide)' }}>NET PNL</span>
            <span style={{ color: T.status.ok, fontWeight: 600 }}>{fmtPnl(0)}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10 }}>
            <span style={{ color: T.text.tertiary, letterSpacing: 'var(--letter-spacing-wide)' }}>WIN RATE</span>
            <span style={{ color: T.text.primary, fontWeight: 600 }}>0.0%</span>
          </div>
        </div>
      </footer>
    </aside>
  );
};

// ── Main shell ───────────────────────────────────────────────────────────
const App = () => {
  // tab state is { id, subTab }
  const [tab, setTab] = useState(() => {
    const hash = window.location.hash.slice(1);
    const [id, subTab] = hash.split('/');
    const found = TABS.find(t => t.id === id);
    return found
      ? { id: found.id, subTab: subTab || found.subTabs?.[0]?.id }
      : { id: 'overview', subTab: undefined };
  });

  // sync hash on tab change
  useEffect(() => {
    const hash = tab.subTab ? `${tab.id}/${tab.subTab}` : tab.id;
    window.location.hash = hash;
  }, [tab]);

  const tabDef = TABS.find(t => t.id === tab.id) || TABS[0];
  const TabComponent = tabDef.component();

  return (
    <>
      <Sidebar tab={tab} onTabChange={setTab} />
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', background: T.bg.page }}>
        {TabComponent
          ? <TabComponent tab={tab} setTab={setTab} tabDef={tabDef} />
          : <div style={{ padding: 32, color: T.text.tertiary }}>
              Loading tab <strong>{tabDef.label}</strong>… (component missing — likely a JSX load order error)
            </div>}
      </main>
    </>
  );
};

window.PolymarketApp = App;
window.PolymarketTabs = TABS;

// Mount
(function () {
  const rootEl = document.getElementById('root');
  if (!rootEl) return;
  // Defer the first render until tabs.jsx has attached its exports.
  // We schedule via setTimeout so the next script (dashboard-tabs.jsx
  // which runs in the SAME synchronous .new Function call but AFTER
  // this file) can complete first.
  setTimeout(() => {
    rootEl.innerHTML = '';
    ReactDOM.createRoot(rootEl).render(React.createElement(App));
  }, 0);
})();
