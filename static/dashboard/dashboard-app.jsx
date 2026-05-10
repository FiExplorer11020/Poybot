// dashboard-app.jsx — Shell, sidebar, topbar — wired to Poybot LiveStore

const { useState: useStateA, useEffect: useEffectA } = React;
const {
  C, S, useLiveStore, Badge, Dot, SectionLabel,
  fmtAge, fmtPnl, fmtMs, pnlColor,
} = window;
const { AlphaTerminal, MarketScanner, LivePortfolio, DecisionEngine, RiskConfig, BotHealth, WalletGraph, MLProgression } = window;

const { Inspector } = window;
const NAV = [
  { id: 'alpha',      label: 'ALPHA TERMINAL',  icon: '◈', component: AlphaTerminal },
  { id: 'mlprog',     label: 'ML PROGRESSION',  icon: '◍', component: MLProgression },
  // The legacy MARKET SCANNER tab has been folded into WALLET GRAPH —
  // the bot's edge is wallet-centric, so a market-only view is misleading.
  // The legacy market table is kept as a sub-tab inside WALLET GRAPH for
  // debugging until it's safe to remove the backend support.
  { id: 'graph',      label: 'WALLET GRAPH',    icon: '⬢', component: WalletGraph },
  { id: 'portfolio',  label: 'LIVE PORTFOLIO',  icon: '◎', component: LivePortfolio },
  { id: 'decisions',  label: 'DECISION ENGINE', icon: '◇', component: DecisionEngine },
  { id: 'inspector',  label: 'INSPECTOR',       icon: '✦', component: Inspector || (() => null) },
  { id: 'risk',       label: 'RISK & CONFIG',   icon: '◆', component: RiskConfig    },
  { id: 'health',     label: 'BOT HEALTH',      icon: '◐', component: BotHealth     },
];

// ── Sidebar ────────────────────────────────────────────────────────────────────
const Sidebar = ({ tab, setTab }) => {
  const [time, setTime] = useStateA(new Date().toLocaleTimeString('en-GB'));
  const { snapshot, connectionState } = useLiveStore();

  useEffectA(() => {
    const id = setInterval(() => setTime(new Date().toLocaleTimeString('en-GB')), 1000);
    return () => clearInterval(id);
  }, []);

  const bot       = snapshot?.bot       || {};
  const ingestion = snapshot?.ingestion || {};
  const stats     = snapshot?.stats     || {};

  const connColor = { connected: C.green, reconnecting: C.amber, connecting: C.amber, disconnected: C.red }[connectionState] || C.dim2;
  const connLabel = { connected: 'LIVE', reconnecting: 'RECON…', connecting: 'CONN…', disconnected: 'OFFLINE' }[connectionState] || '—';
  const botColor  = bot.status === 'running' ? C.green : bot.status === 'stopped' ? C.amber : C.red;

  const sysRows = [
    { label: 'BOT',       color: botColor,  value: (bot.status || '—').toUpperCase() },
    { label: 'WS',        color: connColor, value: connLabel },
    {
      label: 'INGESTION',
      color: (ingestion.live_markets || 0) > 0 ? C.green : C.red,
      value: ingestion.total_markets ? `${ingestion.live_markets || 0}/${ingestion.total_markets}` : '—',
    },
    {
      label: 'EXEC',
      color: bot.execution_enabled ? C.green : C.amber,
      value: bot.execution_enabled ? 'LIVE' : 'DRY RUN',
    },
  ];

  return (
    <div style={{ width: 196, flexShrink: 0, background: C.panel, borderRight: `1px solid ${C.border}`, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

      {/* Brand */}
      <div style={{ padding: '14px 14px 12px', borderBottom: `1px solid ${C.border}` }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: C.amber, letterSpacing: '0.08em' }}>POLYMARKET</div>
        <div style={{ fontSize: 9, color: C.dim2, letterSpacing: '0.14em', marginTop: 1 }}>TRADING BOT</div>
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Dot status={connectionState === 'connected' ? 'live' : connectionState === 'disconnected' ? 'err' : 'warn'} />
          <span style={{ fontSize: 10, color: connColor, fontWeight: 700 }}>{connLabel}</span>
          <span style={{ fontSize: 10, color: C.dim2, marginLeft: 'auto', fontFamily: 'monospace' }}>{time}</span>
        </div>
      </div>

      {/* Nav */}
      <nav style={{ flex: 1, paddingTop: 6, overflow: 'auto' }}>
        {NAV.map(item => {
          const active = tab === item.id;
          return (
            <button key={item.id} onClick={() => setTab(item.id)} style={{
              width: '100%', textAlign: 'left',
              background: active ? 'rgba(232,160,32,0.09)' : 'transparent',
              border: 'none', borderLeft: `2px solid ${active ? C.amber : 'transparent'}`,
              color: active ? C.amber : C.dim2,
              padding: '8px 12px', cursor: 'pointer',
              fontSize: 10, fontWeight: active ? 700 : 400,
              letterSpacing: '0.07em', display: 'flex', alignItems: 'center', gap: 8,
              transition: 'color 0.1s, background 0.1s',
            }}
              onMouseEnter={e => { if (!active) { e.currentTarget.style.color = C.text; e.currentTarget.style.background = 'rgba(255,255,255,0.02)'; } }}
              onMouseLeave={e => { if (!active) { e.currentTarget.style.color = C.dim2;  e.currentTarget.style.background = 'transparent'; } }}
            >
              <span style={{ opacity: 0.6, fontSize: 10, flexShrink: 0 }}>{item.icon}</span>
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* System status footer */}
      <div style={{ borderTop: `1px solid ${C.border}`, padding: '10px 12px', fontSize: 10 }}>
        <div style={{ ...S.label, marginBottom: 8 }}>System</div>
        {sysRows.map(r => (
          <div key={r.label} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <Dot status={r.color === C.green ? 'ok' : r.color === C.amber ? 'warn' : 'err'} />
            <span style={{ color: C.dim2, minWidth: 58 }}>{r.label}</span>
            <span style={{ color: r.color, marginLeft: 'auto', fontWeight: 600, fontSize: 9, letterSpacing: '0.04em' }}>{r.value}</span>
          </div>
        ))}

        {/* PnL / win rate mini */}
        <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
            <span style={{ color: C.dim2 }}>NET PNL</span>
            <span style={{ color: pnlColor(stats.total_pnl), fontWeight: 700 }}>
              {stats.total_pnl != null ? `${stats.total_pnl >= 0 ? '+' : ''}$${Math.abs(stats.total_pnl).toFixed(2)}` : '—'}
            </span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ color: C.dim2 }}>WIN RATE</span>
            <span style={{ color: C.amber, fontWeight: 700 }}>
              {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—'}
            </span>
          </div>
        </div>

        <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${C.border}`, display: 'flex', gap: 6, alignItems: 'center' }}>
          <Badge type={bot.execution_enabled ? 'green' : 'default'}>{bot.execution_enabled ? 'LIVE' : 'PAPER'}</Badge>
          <span style={{ fontSize: 9, color: C.dim2 }}>Poybot</span>
          {bot.uptime_seconds != null && (
            <span style={{ fontSize: 9, color: C.dim2, marginLeft: 'auto' }}>↑{fmtAge(bot.uptime_seconds)}</span>
          )}
        </div>
      </div>
    </div>
  );
};

// ── App ────────────────────────────────────────────────────────────────────────
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accentColor": "#e8a020",
  "apiBase": "http://localhost:8000"
}/*EDITMODE-END*/;

const App = () => {
  const [tab, setTab]           = useStateA(() => localStorage.getItem('pmi_tab') || 'alpha');
  const [showTweaks, setShowTweaks] = useStateA(false);
  const [tweaks, setTweaks]     = useStateA(TWEAK_DEFAULTS);
  const { snapshot }            = useLiveStore();
  const bot = snapshot?.bot || {};

  useEffectA(() => { localStorage.setItem('pmi_tab', tab); }, [tab]);

  useEffectA(() => {
    const handler = e => {
      if (e.data?.type === '__activate_edit_mode')   setShowTweaks(true);
      if (e.data?.type === '__deactivate_edit_mode') setShowTweaks(false);
    };
    window.addEventListener('message', handler);
    window.parent.postMessage({ type: '__edit_mode_available' }, '*');
    return () => window.removeEventListener('message', handler);
  }, []);

  // UTC clock tick
  const [utc, setUtc] = useStateA(() => new Date().toISOString().slice(11, 19));
  useEffectA(() => {
    const id = setInterval(() => setUtc(new Date().toISOString().slice(11, 19)), 1000);
    return () => clearInterval(id);
  }, []);

  const ActiveTab = NAV.find(n => n.id === tab)?.component || AlphaTerminal;
  const isRunning = bot.status === 'running';

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden', fontFamily: "'JetBrains Mono', monospace", background: C.bg }}>
      <Sidebar tab={tab} setTab={setTab} />

      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>

        {/* Topbar */}
        <div style={{ padding: '6px 16px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 10, background: C.panel, flexShrink: 0 }}>
          <span style={{ fontSize: 10, color: C.amber, fontWeight: 700, letterSpacing: '0.09em' }}>
            {NAV.find(n => n.id === tab)?.label}
          </span>
          <span style={{ color: C.border2 }}>│</span>
          <Badge type={isRunning ? 'green' : bot.status === 'stopped' ? 'amber' : 'red'} size="xs">
            {(bot.status || 'offline').toUpperCase()}
          </Badge>
          <span style={{ color: C.border2 }}>│</span>
          <Badge type={bot.execution_enabled ? 'green' : 'default'} size="xs">
            {bot.execution_enabled ? 'LIVE EXECUTION' : 'DRY RUN'}
          </Badge>
          {bot.latency_ms != null && (
            <>
              <span style={{ color: C.border2 }}>│</span>
              <span style={{ fontSize: 10, color: C.dim2 }}>latency <span style={{ color: C.blue }}>{fmtMs(bot.latency_ms)}</span></span>
            </>
          )}
          <span style={{ marginLeft: 'auto', fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>UTC {utc}</span>
        </div>

        {/* Active tab — absolutely-positioned inner container guarantees the
            tab content fills the parent regardless of its own layout (flex,
            grid, block). Using `flex: 1` alone would let some tabs shrink to
            content width when they don't have a forcing grid template. */}
        <div style={{ flex: 1, overflow: 'hidden', position: 'relative', minWidth: 0 }}>
          <div style={{
            position: 'absolute',
            top: 0, left: 0, right: 0, bottom: 0,
            display: 'flex',
            flexDirection: 'column',
          }}>
            <ActiveTab />
          </div>
        </div>
      </div>

      {/* Tweaks panel */}
      {showTweaks && (
        <div style={{ position: 'fixed', bottom: 20, right: 20, background: C.panel, border: `1px solid ${C.border2}`, padding: 16, zIndex: 9999, minWidth: 270 }}>
          <div style={{ ...S.label, marginBottom: 14 }}>Tweaks</div>

          <div style={{ marginBottom: 12 }}>
            <div style={{ ...S.label, marginBottom: 6 }}>Accent Color</div>
            <input type="color" value={tweaks.accentColor}
              onChange={e => {
                const v = { ...tweaks, accentColor: e.target.value };
                setTweaks(v);
                window.parent.postMessage({ type: '__edit_mode_set_keys', edits: v }, '*');
              }}
              style={{ width: '100%', height: 28, background: 'transparent', border: `1px solid ${C.border2}`, cursor: 'pointer' }}
            />
          </div>

          <div style={{ marginBottom: 12 }}>
            <div style={{ ...S.label, marginBottom: 6 }}>Backend URL</div>
            <input
              type="text"
              defaultValue={window.PoybotAPI?.getSettings?.()?.API_BASE || 'http://localhost:8000'}
              style={{ background: C.panel2, border: `1px solid ${C.border2}`, color: C.text, padding: '4px 8px', fontSize: 11, width: '100%', outline: 'none' }}
              onBlur={e => {
                const v = e.target.value.trim();
                if (v) window.PoybotAPI?.setSettings?.(v);
              }}
            />
            <div style={{ fontSize: 9, color: C.dim2, marginTop: 3 }}>Blur to apply — reloads page</div>
          </div>

          <button
            onClick={() => { window.parent.postMessage({ type: '__edit_mode_dismissed' }, '*'); setShowTweaks(false); }}
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, padding: '4px 12px', cursor: 'pointer', fontSize: 11, width: '100%' }}
          >CLOSE</button>
        </div>
      )}
    </div>
  );
};

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
