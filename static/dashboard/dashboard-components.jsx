// dashboard-components.jsx — shared atomic components + live store hook

const { useState, useEffect, useRef, useMemo } = React;

const C = {
  amber: '#e8a020', green: '#28a84e', red: '#c93545', blue: '#3d7dc8', purple: '#7855c0',
  text: '#c4ccd8', dim: '#3a4558', dim2: '#6b7a94', white: '#eef2f8',
  bg: '#070809', panel: '#0c0e12', panel2: '#101318', border: '#1a2030', border2: '#252d3e',
};

const S = {
  label: { fontSize: 10, color: C.dim2, textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 600 },
  mono:  { fontFamily: "'JetBrains Mono', monospace" },
};

// ── Live store hook ────────────────────────────────────────────────────────────
const useLiveStore = () => {
  const [state, setState] = useState(() => ({
    snapshot:        window.LiveStore?.snapshot        || null,
    connectionState: window.LiveStore?.connectionState || 'connecting',
    lastUpdate:      window.LiveStore?.lastUpdate      || null,
  }));
  useEffect(() => {
    if (!window.LiveStore) return;
    return window.LiveStore.subscribe(s =>
      setState({ snapshot: s.snapshot, connectionState: s.connectionState, lastUpdate: s.lastUpdate })
    );
  }, []);
  return state;
};

// ── Connection banner ──────────────────────────────────────────────────────────
const ConnBanner = ({ state }) => {
  if (state === 'connected') return null;
  const map = {
    connecting:   { c: C.amber, msg: 'CONNECTING TO BACKEND…' },
    reconnecting: { c: C.amber, msg: 'RECONNECTING…' },
    disconnected: { c: C.red,   msg: 'BACKEND OFFLINE — showing stale data' },
  };
  const { c, msg } = map[state] || map.disconnected;
  const base = window.PoybotAPI?.getSettings?.()?.API_BASE || 'http://localhost:8000';
  return (
    <div style={{ background: `${c}18`, borderBottom: `1px solid ${c}44`, padding: '5px 16px', display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
      <Dot status={state === 'disconnected' ? 'err' : 'warn'} />
      <span style={{ fontSize: 10, color: c, fontWeight: 700, letterSpacing: '0.08em' }}>{msg}</span>
      <span style={{ fontSize: 10, color: C.dim2, marginLeft: 6 }}>{base}</span>
    </div>
  );
};

// ── Badge ──────────────────────────────────────────────────────────────────────
const Badge = ({ type = 'default', size = 'sm', children }) => {
  const map = {
    green:   { bg: 'rgba(40,168,78,0.13)',   color: C.green,  border: 'rgba(40,168,78,0.3)' },
    red:     { bg: 'rgba(201,53,69,0.13)',   color: C.red,    border: 'rgba(201,53,69,0.3)' },
    amber:   { bg: 'rgba(232,160,32,0.13)',  color: C.amber,  border: 'rgba(232,160,32,0.3)' },
    blue:    { bg: 'rgba(61,125,200,0.13)',  color: C.blue,   border: 'rgba(61,125,200,0.3)' },
    purple:  { bg: 'rgba(120,85,192,0.13)', color: C.purple, border: 'rgba(120,85,192,0.3)' },
    default: { bg: 'rgba(255,255,255,0.05)', color: C.dim2,   border: 'rgba(255,255,255,0.1)' },
  };
  const { bg, color, border } = map[type] || map.default;
  return (
    <span style={{
      display: 'inline-block',
      padding: size === 'xs' ? '1px 4px' : '2px 6px',
      fontSize: size === 'xs' ? 9 : 10,
      fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase',
      background: bg, color, border: `1px solid ${border}`, borderRadius: 2,
      whiteSpace: 'nowrap', lineHeight: 1.4,
    }}>{children}</span>
  );
};

const MiniBar = ({ value, max = 100, color = C.amber, height = 3 }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
    <div style={{ flex: 1, height, background: 'rgba(255,255,255,0.06)' }}>
      <div style={{ width: `${Math.min(100, Math.max(0, (value / max) * 100))}%`, height: '100%', background: color, transition: 'width 0.4s' }} />
    </div>
    <span style={{ fontSize: 10, color: C.dim2, minWidth: 28, textAlign: 'right' }}>{Math.round(value)}%</span>
  </div>
);

const ScoreBar = ({ value }) => {
  const pct   = Math.round((value || 0) * 100);
  const color = value > 0.7 ? C.green : value > 0.45 ? C.amber : C.red;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ width: 56, height: 3, background: 'rgba(255,255,255,0.06)' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color }} />
      </div>
      <span style={{ fontSize: 11, color, fontWeight: 600, minWidth: 34 }}>{(value || 0).toFixed(3)}</span>
    </div>
  );
};

const Dot = ({ status }) => {
  const colors = { ok: C.green, warn: C.amber, err: C.red, live: C.green, off: C.dim2 };
  const c = colors[status] || C.dim2;
  return (
    <span style={{
      display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
      background: c, flexShrink: 0,
      boxShadow: status === 'live' ? `0 0 5px ${c}` : 'none',
    }} />
  );
};

const KpiStrip = ({ items }) => (
  <div style={{ display: 'grid', gridTemplateColumns: `repeat(${items.length}, 1fr)`, borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
    {items.map((k, i) => (
      <div key={i} style={{ padding: '10px 14px', borderRight: i < items.length - 1 ? `1px solid ${C.border}` : 'none' }}>
        <div style={S.label}>{k.label}</div>
        <div style={{ fontSize: k.large ? 24 : 18, fontWeight: 700, color: k.color || C.white, marginTop: 4, letterSpacing: '-0.02em' }}>{k.value}</div>
        {k.sub && <div style={{ fontSize: 10, color: C.dim2, marginTop: 2 }}>{k.sub}</div>}
      </div>
    ))}
  </div>
);

const TH = ({ children }) => (
  <th style={{ padding: '5px 10px', textAlign: 'left', ...S.label, fontWeight: 700, borderBottom: `1px solid ${C.border2}`, whiteSpace: 'nowrap' }}>
    {children}
  </th>
);

const TD = ({ children, style }) => (
  <td style={{ padding: '5px 10px', borderBottom: `1px solid ${C.border}`, verticalAlign: 'middle', ...style }}>
    {children}
  </td>
);

const SectionLabel = ({ children, mb = 10 }) => (
  <div style={{ ...S.label, marginBottom: mb, paddingBottom: 6, borderBottom: `1px solid ${C.border}` }}>{children}</div>
);

// ── Helpers ────────────────────────────────────────────────────────────────────
const short      = w  => w ? w.slice(0, 6) + '…' + w.slice(-4) : '—';
const fmtAge     = s  => { if (s == null) return '—'; if (s < 60) return s + 's'; if (s < 3600) return Math.floor(s / 60) + 'm'; if (s < 86400) return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'; return Math.floor(s / 86400) + 'd'; };
const fmtPnl     = v  => v == null ? '—' : (v >= 0 ? '+' : '') + ' $' + Math.abs(v).toFixed(2);
const fmtPct     = (v, d = 1) => ((v || 0) * 100).toFixed(d) + '%';
const fmtMs      = ms => ms == null ? '—' : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
const fmtNum     = (v, d = 2) => v == null ? '—' : Number(v).toFixed(d);
const pnlColor   = v  => v == null ? C.text : v >= 0 ? C.green : C.red;
const sideColor  = s  => s === 'BUY' || s === 'YES' ? C.green : s === 'SELL' || s === 'NO' ? C.red : C.dim2;
const actionType = a  => a === 'open' ? 'green' : a === 'close' ? 'red' : a === 'reduce' ? 'amber' : 'default';
const stratType  = s  => s === 'directional' ? 'blue' : s === 'structural' ? 'amber' : 'purple';
const phaseLabel = { 0: 'WARM', 1: 'BETA', 2: 'LOGREG', 3: 'LGBM' };
const phaseType  = { 0: 'default', 1: 'blue', 2: 'amber', 3: 'green' };

Object.assign(window, {
  C, S, useLiveStore, ConnBanner,
  Badge, MiniBar, ScoreBar, Dot, KpiStrip, TH, TD, SectionLabel,
  short, fmtAge, fmtPnl, fmtPct, fmtMs, fmtNum,
  pnlColor, sideColor, actionType, stratType, phaseLabel, phaseType,
});
