// ============================================================================
// Polymarket Bot — Dashboard v2 shared components
//
// Synthesised from the ui-ux-pro-max skill audit
// (see docs/UI_REDESIGN_PHASE3.md § 12). Every component:
//   • Uses design tokens via CSS var(--*) — no hard-coded colors
//   • Has a loading prop that renders a Skeleton (never blank)
//   • Has cursor-pointer on interactive surfaces
//   • Uses ease-out for enter / ease-in for exit transitions
//   • Honors prefers-reduced-motion via tokens.css global query
//   • Has visible focus rings (handled in tokens.css *:focus-visible)
//
// Convention: all exports attached to `window` so the other JSX
// modules (dashboard-app, dashboard-tabs) can pick them up via
// `const { X } = window`.
// ============================================================================

const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ── Design token shortcuts ────────────────────────────────────────────────
// We use CSS variables everywhere but keep a JS object for places that
// need string interpolation (computed styles, charts).
const T = {
  bg: {
    page:      'var(--bg-page)',
    panel:     'var(--bg-panel)',
    elevated:  'var(--bg-panel-elevated)',
    input:     'var(--bg-input)',
  },
  border: {
    subtle:    'var(--border-subtle)',
    strong:    'var(--border-strong)',
    focus:     'var(--border-focus)',
  },
  text: {
    primary:   'var(--text-primary)',
    secondary: 'var(--text-secondary)',
    tertiary:  'var(--text-tertiary)',
    disabled:  'var(--text-disabled)',
  },
  accent: {
    amber:     'var(--accent-amber)',
    amberSoft: 'var(--accent-amber-soft)',
    violet:    'var(--accent-violet)',
    violetSoft:'var(--accent-violet-soft)',
  },
  status: {
    ok:        'var(--status-ok)',
    warn:      'var(--status-warn)',
    err:       'var(--status-err)',
    info:      'var(--status-info)',
    gated:     'var(--status-gated)',
    buy:       'var(--status-buy)',
    sell:      'var(--status-sell)',
  },
  chart: {
    c1: 'var(--chart-c1)', c2: 'var(--chart-c2)', c3: 'var(--chart-c3)',
    c4: 'var(--chart-c4)', c5: 'var(--chart-c5)', c6: 'var(--chart-c6)',
    actual:   'var(--chart-actual)',
    forecast: 'var(--chart-forecast)',
    ciBand:   'var(--chart-ci-band)',
    pulse:    'var(--chart-pulse)',
  },
  motion: {
    fast:  'var(--duration-fast)',
    base:  'var(--duration-base)',
    slow:  'var(--duration-slow)',
    enter: 'var(--easing-enter)',
    exit:  'var(--easing-exit)',
    move:  'var(--easing-move)',
  },
};

// ── Icon library (Lucide-style inline SVGs) ──────────────────────────────
// 24x24 viewBox. Use w-5 h-5 equivalents via the size prop.
// Pattern: stroke-only paths, stroke-width 2, round caps/joins.
const ICONS = {
  // Status / generic
  'check':       'M5 12l5 5L20 7',
  'x':           'M6 6l12 12M18 6L6 18',
  'alert-triangle': 'M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z',
  'circle':      'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0',
  'circle-dot':  'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'pause':       'M6 4h4v16H6zM14 4h4v16h-4z',
  'play':        'M5 3l14 9-14 9V3z',
  'refresh':     'M21 12a9 9 0 11-9-9c2.52 0 4.93 1 6.74 2.74L21 8M21 3v5h-5',
  'chevron-up':  'M18 15l-6-6-6 6',
  'chevron-down':'M6 9l6 6 6-6',
  'chevron-right':'M9 18l6-6-6-6',
  'chevron-left':'M15 18l-9-6 9-6',
  'arrow-up':    'M12 19V5M5 12l7-7 7 7',
  'arrow-down':  'M12 5v14M5 12l7 7 7-7',
  'arrow-right': 'M5 12h14M13 5l7 7-7 7',
  // Domain
  'activity':    'M22 12h-4l-3 9L9 3l-3 9H2',
  'database':    'M4 6c0-1.66 3.58-3 8-3s8 1.34 8 3v12c0 1.66-3.58 3-8 3s-8-1.34-8-3V6zM4 12c0 1.66 3.58 3 8 3s8-1.34 8-3M4 6c0 1.66 3.58 3 8 3s8-1.34 8-3',
  'cpu':         'M9 9h6v6H9zM4 9h2M4 15h2M18 9h2M18 15h2M9 4v2M15 4v2M9 18v2M15 18v2',
  'eye':         'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
  'globe':       'M12 12m-10 0a10 10 0 1 0 20 0a10 10 0 1 0-20 0M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z',
  'layers':      'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  'list':        'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  'lock':        'M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2zM7 11V7a5 5 0 0110 0v4',
  'settings':    'M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z',
  'shield':      'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  'target':      'M12 12m-10 0a10 10 0 1 0 20 0a10 10 0 1 0-20 0M12 12m-6 0a6 6 0 1 0 12 0a6 6 0 1 0-12 0M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'trending-up': 'M23 6l-9.5 9.5-5-5L1 18M17 6h6v6',
  'trending-down':'M23 18l-9.5-9.5-5 5L1 6M17 18h6v-6',
  'users':       'M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75',
  'wallet':      'M21 12V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2h14a2 2 0 002-2v-5zM16 12h.01M3 7h18',
  'zap':         'M13 2L3 14h9l-1 8 10-12h-9l1-8z',
  'mempool':     'M3 12l4-8h10l4 8M3 12v8h18v-8M3 12h18M7 16h2M11 16h2M15 16h2',
  'microscope':  'M6 18h8M3 22h18M14 22a7 7 0 100-14h-1M9 14h2M9 12a2 2 0 012-2h0a2 2 0 012 2v2H9zM12 6l-2 2-3-3 2-2zM7 5l3 3',
  'radar':       'M19.07 4.93a10 10 0 010 14.14M16.93 7.07a6 6 0 010 9.86M14.79 9.21a2 2 0 010 5.58M12 12L2 22',
  'compass':     'M12 12m-10 0a10 10 0 1 0 20 0a10 10 0 1 0-20 0M16.24 7.76l-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z',
  'book-open':   'M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2zM22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z',
  'beaker':      'M9 3h6M10 3v6.5L4 18a2 2 0 002 3h12a2 2 0 002-3l-6-8.5V3',
  // Sidebar / nav
  'home':        'M3 12l9-9 9 9v9a2 2 0 01-2 2h-5v-7H10v7H5a2 2 0 01-2-2v-9zM9 22v-7h6v7',
  // Misc
  'filter':      'M22 3H2l8 9.46V19l4 2v-8.54L22 3z',
  'search':      'M11 11m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0M21 21l-4.35-4.35',
  'sliders':     'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6',
};

const Icon = ({ name, size = 16, color, strokeWidth = 2, style = {}, ...rest }) => {
  const path = ICONS[name];
  if (!path) {
    return <span aria-hidden="true" style={{ width: size, height: size, display: 'inline-block', ...style }} />;
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color || 'currentColor'}
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      style={{ flexShrink: 0, ...style }}
      {...rest}
    >
      <path d={path} />
    </svg>
  );
};

// ── Status helpers (color + icon — never color alone, UX rule A1) ────────
const STATUS_VOCAB = {
  ok:      { color: T.status.ok,    icon: 'check',           label: 'OK' },
  running: { color: T.status.ok,    icon: 'circle-dot',      label: 'RUNNING' },
  warn:    { color: T.status.warn,  icon: 'alert-triangle',  label: 'WARN' },
  degraded:{ color: T.status.warn,  icon: 'alert-triangle',  label: 'DEGRADED' },
  err:     { color: T.status.err,   icon: 'x',               label: 'ERROR' },
  stopped: { color: T.status.err,   icon: 'x',               label: 'STOPPED' },
  off:     { color: T.status.gated, icon: 'circle',          label: 'OFF' },
  gated:   { color: T.status.gated, icon: 'lock',            label: 'GATED' },
  pending: { color: T.status.gated, icon: 'circle',          label: 'PENDING' },
  info:    { color: T.status.info,  icon: 'circle-dot',      label: 'INFO' },
  rising:  { color: T.status.ok,    icon: 'arrow-up',        label: 'RISING' },
  falling: { color: T.status.err,   icon: 'arrow-down',      label: 'FALLING' },
  protected:{color: T.accent.violet,icon: 'shield',          label: 'PROTECTED' },
};

const StatusPill = ({ status, label, size = 'sm', style = {} }) => {
  const v = STATUS_VOCAB[status] || STATUS_VOCAB.info;
  const sz = size === 'lg' ? { pad: '4px 10px', font: 11, icon: 14 }
           : size === 'md' ? { pad: '3px 8px',  font: 10, icon: 12 }
           : { pad: '2px 6px', font: 9,  icon: 10 };
  return (
    <span
      role="status"
      aria-label={label || v.label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: sz.pad,
        background: 'transparent',
        border: `1px solid ${v.color}`,
        color: v.color,
        fontSize: sz.font,
        fontWeight: 600,
        letterSpacing: 'var(--letter-spacing-wide)',
        borderRadius: 'var(--radius-md)',
        lineHeight: 1,
        ...style,
      }}
    >
      <Icon name={v.icon} size={sz.icon} />
      <span>{label || v.label}</span>
    </span>
  );
};

// Inline dot — used in dense tables / lists where a pill is too heavy.
const Dot = ({ status = 'info', size = 6 }) => {
  const v = STATUS_VOCAB[status] || STATUS_VOCAB.info;
  return (
    <span
      role="img"
      aria-label={v.label}
      style={{
        width: size, height: size, borderRadius: '50%',
        background: v.color, flexShrink: 0, display: 'inline-block',
      }}
    />
  );
};

// ── Skeleton primitives ───────────────────────────────────────────────────
const SkeletonLine = ({ width = '100%', height = 12, style = {} }) => (
  <div className="skeleton" style={{ width, height, ...style }} />
);

const SkeletonBlock = ({ width = '100%', height = 80, style = {} }) => (
  <div className="skeleton" style={{ width, height, borderRadius: 'var(--radius-md)', ...style }} />
);

const KpiStripSkeleton = ({ count = 6 }) => (
  <div style={kpiStripStyle}>
    {Array.from({ length: count }).map((_, i) => (
      <div key={i} style={kpiCellStyle}>
        <SkeletonLine width="60%" height={9} style={{ marginBottom: 6 }} />
        <SkeletonLine width="40%" height={18} />
      </div>
    ))}
  </div>
);

const TableSkeleton = ({ rows = 8, cols = 6 }) => (
  <div style={{ padding: 'var(--card-padding)' }}>
    {Array.from({ length: rows }).map((_, r) => (
      <div key={r} style={{ display: 'flex', gap: 8, padding: '6px 0' }}>
        {Array.from({ length: cols }).map((_, c) => (
          <SkeletonLine key={c} width={`${100 / cols}%`} height={10} />
        ))}
      </div>
    ))}
  </div>
);

const ChartSkeleton = ({ height = 240 }) => (
  <SkeletonBlock height={height} />
);

// ── KPI primitives ────────────────────────────────────────────────────────
const kpiStripStyle = {
  display: 'grid',
  gridAutoFlow: 'column',
  gridAutoColumns: 'minmax(140px, 1fr)',
  gap: 0,
  background: T.bg.panel,
  borderBottom: `1px solid ${T.border.subtle}`,
  minHeight: 'var(--kpi-strip-height)',
};

const kpiCellStyle = {
  padding: '14px 16px',
  borderRight: `1px solid ${T.border.subtle}`,
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
  minHeight: 'var(--kpi-strip-height)',
};

const KpiCell = ({ label, value, hint, color = T.text.primary, loading = false }) => (
  <div style={kpiCellStyle}>
    <div style={{
      fontSize: 'var(--font-size-xs)',
      color: T.text.tertiary,
      letterSpacing: 'var(--letter-spacing-wide)',
      textTransform: 'uppercase',
    }}>{label}</div>
    {loading ? (
      <SkeletonLine width="55%" height={20} />
    ) : (
      <div
        aria-live="polite"
        style={{
          fontSize: 'var(--font-size-lg)',
          fontWeight: 600,
          color,
          lineHeight: 'var(--line-height-tight)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
    )}
    {hint && (
      <div style={{ fontSize: 'var(--font-size-xs)', color: T.text.tertiary }}>{hint}</div>
    )}
  </div>
);

const KpiStrip = ({ items, loading = false }) => {
  if (loading) return <KpiStripSkeleton count={items?.length || 6} />;
  return (
    <div style={kpiStripStyle} role="region" aria-label="Key metrics">
      {items.map((item, i) => <KpiCell key={item.label || i} {...item} />)}
    </div>
  );
};

// ── Section + panel primitives ────────────────────────────────────────────
const SectionLabel = ({ children, action, style = {} }) => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '12px 16px 8px',
    fontSize: 'var(--font-size-xs)',
    color: T.text.tertiary,
    letterSpacing: 'var(--letter-spacing-wide)',
    textTransform: 'uppercase',
    ...style,
  }}>
    <span>{children}</span>
    {action && <span>{action}</span>}
  </div>
);

const Panel = ({ title, subtitle, children, action, style = {}, bodyStyle = {} }) => (
  <section style={{
    background: T.bg.panel,
    border: `1px solid ${T.border.subtle}`,
    borderRadius: 'var(--radius-md)',
    overflow: 'hidden',
    ...style,
  }}>
    {(title || action) && (
      <header style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '10px 12px',
        borderBottom: `1px solid ${T.border.subtle}`,
        gap: 8,
      }}>
        <div>
          <div style={{
            fontSize: 'var(--font-size-xs)',
            color: T.text.tertiary,
            letterSpacing: 'var(--letter-spacing-wide)',
            textTransform: 'uppercase',
          }}>{title}</div>
          {subtitle && (
            <div style={{ fontSize: 'var(--font-size-xs)', color: T.text.tertiary, marginTop: 2 }}>
              {subtitle}
            </div>
          )}
        </div>
        {action}
      </header>
    )}
    <div style={{ padding: 'var(--card-padding)', ...bodyStyle }}>
      {children}
    </div>
  </section>
);

// ── Bento card (clickable + deep-link to a tab) ──────────────────────────
const BentoCard = ({ title, icon, onClick, children, accent = T.accent.amber, style = {} }) => (
  <button
    type="button"
    onClick={onClick}
    className={onClick ? 'bento-card cursor-pointer' : 'bento-card'}
    aria-label={typeof title === 'string' ? `Open ${title}` : undefined}
    style={{
      background: T.bg.panel,
      border: `1px solid ${T.border.subtle}`,
      borderRadius: 'var(--radius-md)',
      padding: 'var(--section-padding)',
      textAlign: 'left',
      cursor: onClick ? 'pointer' : 'default',
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      ...style,
    }}
  >
    <header style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {icon && <Icon name={icon} size={16} color={accent} />}
      <h3 style={{
        fontSize: 'var(--font-size-xs)',
        color: T.text.tertiary,
        letterSpacing: 'var(--letter-spacing-wide)',
        textTransform: 'uppercase',
        margin: 0,
        fontWeight: 600,
      }}>{title}</h3>
      {onClick && (
        <Icon name="chevron-right" size={12} color={T.text.tertiary} style={{ marginLeft: 'auto' }} />
      )}
    </header>
    <div style={{ flex: 1 }}>{children}</div>
  </button>
);

// ── Banners (gated / missing-hook / methodology-audit) ───────────────────
const Banner = ({ tone = 'info', icon = 'alert-triangle', title, children, action, style = {} }) => {
  const colorMap = {
    info:   T.status.info,
    warn:   T.status.warn,
    err:    T.status.err,
    gated:  T.status.gated,
    audit:  T.accent.violet,
  };
  const c = colorMap[tone] || T.status.info;
  return (
    <div
      role="status"
      style={{
        display: 'flex',
        gap: 12,
        padding: '10px 12px',
        background: 'transparent',
        border: `1px solid ${c}`,
        borderRadius: 'var(--radius-md)',
        ...style,
      }}
    >
      <Icon name={icon} size={16} color={c} style={{ marginTop: 2, flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        {title && (
          <div style={{ fontWeight: 600, color: c, fontSize: 'var(--font-size-sm)', marginBottom: 4 }}>
            {title}
          </div>
        )}
        <div style={{ fontSize: 'var(--font-size-sm)', color: T.text.secondary, lineHeight: 'var(--line-height-body)' }}>
          {children}
        </div>
      </div>
      {action && <div style={{ flexShrink: 0 }}>{action}</div>}
    </div>
  );
};

const GatedBanner = ({ flag, route, description, onOpen }) => (
  <Banner
    tone="gated"
    icon="lock"
    title={`GATED — runtime flag '${flag}' = OFF`}
    action={
      onOpen && (
        <button
          onClick={onOpen}
          style={{
            color: T.accent.amber,
            border: `1px solid ${T.accent.amber}`,
            padding: '4px 10px',
            fontSize: 'var(--font-size-sm)',
          }}
        >
          Open {route}
        </button>
      )
    }
  >
    {description || `Flip ${flag} in OPERATIONS → Risk & Config to enable.`}
  </Banner>
);

const MissingHookBanner = ({ title, children }) => (
  <Banner tone="warn" icon="alert-triangle" title={title || 'DEFERRED HOOK'}>
    {children}
  </Banner>
);

const MethodologyAuditBanner = () => (
  <Banner tone="audit" icon="book-open" title="METHODOLOGY AUDIT PENDING">
    Causal-inference math is validated against synthetic data
    (Monte Carlo rel_err 3.5 %, F = 1355, Wu-Hausman p = 3e-13).
    Per spec § 6, an external causal-inference expert must review the
    application surface (instruments, controls, multiple-testing) before
    flipping <code>causal_gating_enabled</code> = ON. See
    <code> docs/audit/phase3/round10_wave3_review.md § 11</code>.
  </Banner>
);

// ── Pause toggle for streaming feeds (UX rule A4) ────────────────────────
const PauseToggle = ({ paused, queuedCount = 0, onToggle }) => (
  <button
    type="button"
    onClick={onToggle}
    aria-pressed={paused}
    aria-label={paused ? 'Resume live feed' : 'Pause live feed'}
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      padding: '4px 10px',
      border: `1px solid ${paused ? T.accent.amber : T.border.strong}`,
      color: paused ? T.accent.amber : T.text.secondary,
      fontSize: 'var(--font-size-sm)',
      fontWeight: 600,
    }}
  >
    <Icon name={paused ? 'play' : 'pause'} size={12} />
    {paused
      ? <>RESUME {queuedCount > 0 && <span style={{ color: T.status.ok }}>(+{queuedCount} new)</span>}</>
      : <>PAUSE</>}
  </button>
);

// ── Live stream feed (Mempool / Microscope / Social) ─────────────────────
const LiveStreamFeed = ({
  events = [],
  renderRow,
  emptyMessage = 'No events.',
  loading = false,
  maxRows = 200,
  height = 360,
  rowKey,
  pauseable = true,
}) => {
  const [paused, setPaused] = useState(false);
  const [queue, setQueue] = useState([]);
  const [snapshot, setSnapshot] = useState(events);

  // When paused, accumulate events into queue; on resume, flush.
  useEffect(() => {
    if (paused) {
      setQueue((q) => {
        // Only events not yet seen go to queue. Simple equality on length.
        const known = snapshot.length;
        return events.length > known ? events.slice(0, events.length - known) : [];
      });
    } else {
      setSnapshot(events);
      setQueue([]);
    }
  }, [events, paused, snapshot.length]);

  const rows = (paused ? snapshot : events).slice(0, maxRows);

  if (loading) return <ChartSkeleton height={height} />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {pauseable && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <PauseToggle paused={paused} queuedCount={queue.length} onToggle={() => setPaused(p => !p)} />
        </div>
      )}
      <div
        role="log"
        aria-live={paused ? 'off' : 'polite'}
        aria-busy={loading}
        style={{
          height,
          overflowY: 'auto',
          border: `1px solid ${T.border.subtle}`,
          borderRadius: 'var(--radius-md)',
          background: T.bg.panel,
        }}
      >
        {rows.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>
            {emptyMessage}
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--font-size-sm)', fontVariantNumeric: 'tabular-nums' }}>
            <tbody>
              {rows.map((row, i) => (
                <tr key={rowKey ? rowKey(row, i) : i}>
                  {renderRow(row, i)}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

// ── Sub-tab nav (used inside multi-pane tabs) ────────────────────────────
const SubTabNav = ({ tabs, value, onChange }) => (
  <div role="tablist" style={{
    display: 'flex',
    gap: 4,
    padding: '8px 12px',
    background: T.bg.panel,
    borderBottom: `1px solid ${T.border.subtle}`,
    overflowX: 'auto',
  }}>
    {tabs.map(t => {
      const active = value === t.id;
      return (
        <button
          key={t.id}
          role="tab"
          aria-selected={active}
          onClick={() => onChange(t.id)}
          style={{
            padding: '6px 12px',
            background: active ? 'rgba(232,160,32,0.10)' : 'transparent',
            border: `1px solid ${active ? T.accent.amber : 'transparent'}`,
            color: active ? T.accent.amber : T.text.secondary,
            fontSize: 'var(--font-size-sm)',
            letterSpacing: 'var(--letter-spacing-wide)',
            textTransform: 'uppercase',
            fontWeight: 600,
            whiteSpace: 'nowrap',
          }}
        >
          {t.label}
        </button>
      );
    })}
  </div>
);

// ── Breadcrumb header (tab > sub-tab > scope chips) ──────────────────────
const BreadcrumbHeader = ({ tab, subTab, chips = [], rightSlot }) => (
  <header style={{
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '12px 16px',
    background: T.bg.panel,
    borderBottom: `1px solid ${T.border.subtle}`,
    minHeight: 'var(--tab-header-height)',
  }}>
    <span style={{
      color: T.accent.amber,
      fontSize: 'var(--font-size-md)',
      fontWeight: 700,
      letterSpacing: 'var(--letter-spacing-wide)',
      textTransform: 'uppercase',
    }}>{tab}</span>
    {subTab && (
      <>
        <Icon name="chevron-right" size={12} color={T.text.tertiary} />
        <span style={{ color: T.text.primary, fontSize: 'var(--font-size-md)', letterSpacing: 'var(--letter-spacing-wide)' }}>
          {subTab}
        </span>
      </>
    )}
    {chips.length > 0 && (
      <div style={{ display: 'flex', gap: 6, marginLeft: 8 }}>
        {chips.map((c, i) => <StatusPill key={i} status={c.status} label={c.label} size="md" />)}
      </div>
    )}
    {rightSlot && (
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>{rightSlot}</div>
    )}
  </header>
);

// ── Heatmap matrix (R8 drift + R9 α-matrix) ──────────────────────────────
// Supports: pattern overlay for colorblind users + "Show as table" toggle.
const HeatmapMatrix = ({
  rowLabels = [],
  colLabels = [],
  values = [],     // 2D array; values[row][col] in [0, 1]
  scaleLow = '#0F172A',
  scaleHigh = T.accent.amber,
  cellSize = 32,
  emptyMessage = 'No matrix data.',
  loading = false,
}) => {
  const [showTable, setShowTable] = useState(false);

  if (loading) return <ChartSkeleton height={cellSize * Math.max(rowLabels.length, 4) + 40} />;
  if (!values?.length) {
    return <div style={{ padding: 24, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>{emptyMessage}</div>;
  }

  const lerp = (lo, hi, t) => {
    // Hex blend assumed both are #rrggbb hex strings or var. Caller should
    // pass real hex (var resolves at render). Fallback: opacity-based.
    return `linear-gradient(${scaleHigh}, ${scaleHigh})`;
  };

  const patternClass = (v) => {
    if (v == null) return '';
    if (v < 0.25) return 'heatmap-pattern-25';
    if (v < 0.5)  return 'heatmap-pattern-50';
    if (v < 0.75) return 'heatmap-pattern-75';
    return '';
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={() => setShowTable(s => !s)} style={{
          padding: '4px 10px',
          color: T.accent.amber,
          border: `1px solid ${T.accent.amber}`,
          fontSize: 'var(--font-size-sm)',
        }}>
          {showTable ? 'Show heatmap' : 'Show as table'}
        </button>
      </div>

      {showTable ? (
        <table style={{ width: '100%', fontSize: 'var(--font-size-sm)', fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', color: T.text.tertiary, padding: '6px 8px' }}></th>
              {colLabels.map((c, i) => <th key={i} style={{ textAlign: 'right', color: T.text.tertiary, padding: '6px 8px' }}>{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {rowLabels.map((r, ri) => (
              <tr key={ri}>
                <td style={{ padding: '6px 8px', color: T.text.secondary }}>{r}</td>
                {colLabels.map((c, ci) => (
                  <td key={ci} style={{ padding: '6px 8px', textAlign: 'right' }}>
                    {values[ri]?.[ci] != null ? values[ri][ci].toFixed(3) : '—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div role="img" aria-label="Heatmap matrix" style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'separate', borderSpacing: 2, fontSize: 'var(--font-size-xs)' }}>
            <thead>
              <tr>
                <th></th>
                {colLabels.map((c, i) => (
                  <th key={i} style={{ padding: '0 4px', color: T.text.tertiary, fontWeight: 500, textAlign: 'center' }}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rowLabels.map((r, ri) => (
                <tr key={ri}>
                  <td style={{ padding: '0 8px', color: T.text.secondary, whiteSpace: 'nowrap' }}>{r}</td>
                  {colLabels.map((c, ci) => {
                    const v = values[ri]?.[ci];
                    const intensity = v == null ? 0 : Math.max(0, Math.min(1, v));
                    return (
                      <td
                        key={ci}
                        className={patternClass(intensity)}
                        title={`${r} → ${c}: ${v == null ? '—' : v.toFixed(3)}`}
                        style={{
                          width: cellSize,
                          height: cellSize,
                          background: v == null ? T.bg.input : scaleHigh,
                          opacity: v == null ? 0.4 : 0.25 + intensity * 0.75,
                          textAlign: 'center',
                          verticalAlign: 'middle',
                          color: T.text.primary,
                          fontVariantNumeric: 'tabular-nums',
                        }}
                      >
                        {v != null && v >= 0.1 ? v.toFixed(2) : ''}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--font-size-xs)', color: T.text.tertiary }}>
        <span>Low</span>
        <div style={{ flex: 1, height: 8, background: `linear-gradient(to right, ${scaleLow}, ${scaleHigh})`, borderRadius: 'var(--radius-sm)' }} />
        <span>High</span>
      </div>
    </div>
  );
};

// ── Scatter plot (R10 IV vs Hawkes — the keystone) ───────────────────────
const ScatterPlot = ({
  points = [],          // [{ x, y, label, color }]
  xLabel = 'x',
  yLabel = 'y',
  width = 480,
  height = 360,
  identityLine = true,
  emptyMessage = 'No causal estimates yet — run the R10 nightly daemon.',
  loading = false,
}) => {
  const [showTable, setShowTable] = useState(false);
  if (loading) return <ChartSkeleton height={height} />;
  if (!points.length) {
    return <div style={{ padding: 24, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>{emptyMessage}</div>;
  }

  const xs = points.map(p => p.x).filter(v => Number.isFinite(v));
  const ys = points.map(p => p.y).filter(v => Number.isFinite(v));
  const xMin = Math.min(0, ...xs);
  const xMax = Math.max(...xs) * 1.1 || 1;
  const yMin = Math.min(0, ...ys);
  const yMax = Math.max(...ys) * 1.1 || 1;
  const padding = { top: 20, right: 20, bottom: 32, left: 40 };
  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;
  const sx = (x) => padding.left + ((x - xMin) / (xMax - xMin)) * innerW;
  const sy = (y) => padding.top + innerH - ((y - yMin) / (yMax - yMin)) * innerH;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={() => setShowTable(s => !s)} style={{
          padding: '4px 10px', color: T.accent.amber,
          border: `1px solid ${T.accent.amber}`,
          fontSize: 'var(--font-size-sm)',
        }}>
          {showTable ? 'Show scatter' : 'Show as table'}
        </button>
      </div>
      {showTable ? (
        <table style={{ width: '100%', fontSize: 'var(--font-size-sm)', fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', color: T.text.tertiary, padding: '6px 8px' }}>Label</th>
              <th style={{ textAlign: 'right', color: T.text.tertiary, padding: '6px 8px' }}>{xLabel}</th>
              <th style={{ textAlign: 'right', color: T.text.tertiary, padding: '6px 8px' }}>{yLabel}</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p, i) => (
              <tr key={i}>
                <td style={{ padding: '6px 8px', color: T.text.secondary }}>{p.label || `#${i}`}</td>
                <td style={{ padding: '6px 8px', textAlign: 'right' }}>{Number(p.x).toFixed(3)}</td>
                <td style={{ padding: '6px 8px', textAlign: 'right' }}>{Number(p.y).toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <svg width={width} height={height} role="img" aria-label={`${xLabel} vs ${yLabel} scatter plot`}>
          {/* axes */}
          <line x1={padding.left} y1={padding.top + innerH} x2={padding.left + innerW} y2={padding.top + innerH} stroke={T.border.strong} strokeWidth="1"/>
          <line x1={padding.left} y1={padding.top} x2={padding.left} y2={padding.top + innerH} stroke={T.border.strong} strokeWidth="1"/>
          {/* identity line y = x */}
          {identityLine && (
            <line
              x1={sx(Math.max(xMin, yMin))} y1={sy(Math.max(xMin, yMin))}
              x2={sx(Math.min(xMax, yMax))} y2={sy(Math.min(xMax, yMax))}
              stroke={T.status.err} strokeWidth="1" strokeDasharray="4,3" opacity="0.6"
            />
          )}
          {/* points */}
          {points.map((p, i) => (
            <circle
              key={i}
              cx={sx(p.x)} cy={sy(p.y)} r="4"
              fill={p.color || T.chart.c1}
              opacity="0.7"
            >
              <title>{`${p.label || ''} — ${xLabel}=${Number(p.x).toFixed(3)} ${yLabel}=${Number(p.y).toFixed(3)}`}</title>
            </circle>
          ))}
          {/* labels */}
          <text x={padding.left + innerW / 2} y={height - 8} textAnchor="middle" fill={T.text.tertiary} fontSize="10">{xLabel}</text>
          <text x={12} y={padding.top + innerH / 2} textAnchor="middle" fill={T.text.tertiary} fontSize="10" transform={`rotate(-90 12 ${padding.top + innerH / 2})`}>{yLabel}</text>
        </svg>
      )}
    </div>
  );
};

// ── Sparkline (compact line, used in tables) ─────────────────────────────
const Sparkline = ({ data, width = 100, height = 22, color = T.accent.amber, fillOpacity = 0.15 }) => {
  if (!data || data.length === 0) {
    return <div style={{ width, height, color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>—</div>;
  }
  const lo = Math.min(...data), hi = Math.max(...data);
  const range = hi - lo || 1;
  const stepX = data.length > 1 ? width / (data.length - 1) : 0;
  const points = data.map((v, i) => `${i * stepX},${height - ((v - lo) / range) * height}`).join(' ');
  const fillPath = `M0,${height} L${points.split(' ').join(' L')} L${width},${height} Z`;
  return (
    <svg width={width} height={height} aria-hidden="true">
      <path d={fillPath} fill={color} opacity={fillOpacity} />
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
};

// ── Line chart with confidence band (R9 forecast, R13 loss trajectory) ──
const LineWithBand = ({
  series = [],   // [{ label, color, values: [{x, y}], ciLow?: [{x, y}], ciHigh?: [{x, y}] }]
  xLabel,
  yLabel,
  width = 480,
  height = 240,
  loading = false,
  emptyMessage = 'No series data.',
}) => {
  const [showTable, setShowTable] = useState(false);
  if (loading) return <ChartSkeleton height={height} />;
  if (!series.length || series.every(s => !s.values?.length)) {
    return <div style={{ padding: 24, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>{emptyMessage}</div>;
  }
  const allX = series.flatMap(s => (s.values || []).map(p => p.x));
  const allY = series.flatMap(s => [
    ...(s.values || []).map(p => p.y),
    ...(s.ciLow || []).map(p => p.y),
    ...(s.ciHigh || []).map(p => p.y),
  ]);
  const xMin = Math.min(...allX), xMax = Math.max(...allX);
  const yMin = Math.min(...allY), yMax = Math.max(...allY);
  const pad = { top: 16, right: 16, bottom: 28, left: 40 };
  const inW = width - pad.left - pad.right;
  const inH = height - pad.top - pad.bottom;
  const sx = (x) => pad.left + ((x - xMin) / ((xMax - xMin) || 1)) * inW;
  const sy = (y) => pad.top + inH - ((y - yMin) / ((yMax - yMin) || 1)) * inH;

  const toPath = (pts) => pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(p.x)},${sy(p.y)}`).join(' ');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 12, fontSize: 'var(--font-size-xs)', color: T.text.tertiary }}>
          {series.map((s, i) => (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 10, height: 2, background: s.color || T.chart[`c${(i % 6) + 1}`], display: 'inline-block', borderStyle: s.dashed ? 'dashed' : 'solid', borderWidth: s.dashed ? '1px 0 0 0' : 0 }} />
              {s.label}
            </span>
          ))}
        </div>
        <button onClick={() => setShowTable(s => !s)} style={{ padding: '4px 10px', color: T.accent.amber, border: `1px solid ${T.accent.amber}`, fontSize: 'var(--font-size-sm)' }}>
          {showTable ? 'Show chart' : 'Show as table'}
        </button>
      </div>
      {showTable ? (
        <table style={{ width: '100%', fontSize: 'var(--font-size-sm)', fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', color: T.text.tertiary, padding: '6px 8px' }}>{xLabel || 'x'}</th>
              {series.map((s, i) => <th key={i} style={{ textAlign: 'right', color: T.text.tertiary, padding: '6px 8px' }}>{s.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {series[0].values.map((pt, i) => (
              <tr key={i}>
                <td style={{ padding: '6px 8px', color: T.text.secondary }}>{pt.x}</td>
                {series.map((s, j) => <td key={j} style={{ padding: '6px 8px', textAlign: 'right' }}>{s.values[i]?.y ?? '—'}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <svg width={width} height={height} role="img" aria-label={`${yLabel} over ${xLabel}`}>
          {/* axes */}
          <line x1={pad.left} y1={pad.top + inH} x2={pad.left + inW} y2={pad.top + inH} stroke={T.border.strong} strokeWidth="1"/>
          <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + inH} stroke={T.border.strong} strokeWidth="1"/>
          {series.map((s, i) => {
            const color = s.color || T.chart[`c${(i % 6) + 1}`];
            const dashed = s.dashed ? '4,3' : '0';
            return (
              <g key={i}>
                {s.ciLow && s.ciHigh && (
                  <path
                    d={
                      s.ciHigh.map((p, j) => `${j === 0 ? 'M' : 'L'}${sx(p.x)},${sy(p.y)}`).join(' ') +
                      ' ' +
                      s.ciLow.slice().reverse().map((p, j) => `L${sx(p.x)},${sy(p.y)}`).join(' ') +
                      ' Z'
                    }
                    fill={T.chart.ciBand}
                  />
                )}
                <path d={toPath(s.values)} fill="none" stroke={color} strokeWidth="1.5" strokeDasharray={dashed} strokeLinecap="round" strokeLinejoin="round" />
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
};

// ── Strategy fingerprint bar (R8) ────────────────────────────────────────
const STRATEGY_COLORS = {
  directional:     T.chart.c1,
  momentum:        T.chart.c2,
  contrarian:      T.chart.c3,
  arb_2way:        T.chart.c4,
  arb_3way:        T.chart.c5,
  market_maker:    T.chart.c6,
  structural_bot:  T.text.tertiary,
  info_leak:       T.accent.violet,
  social_driven:   T.accent.amber,
};

const StrategyFingerprintBar = ({ probs = {}, loading = false }) => {
  if (loading) return <SkeletonLine height={36} />;
  const entries = Object.entries(probs).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    return <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)' }}>Not classified yet — operator must train R8 model.</div>;
  }
  return (
    <div role="img" aria-label="Strategy probability distribution">
      <div style={{ display: 'flex', height: 12, borderRadius: 'var(--radius-sm)', overflow: 'hidden', border: `1px solid ${T.border.subtle}` }}>
        {entries.map(([cls, p]) => (
          <div
            key={cls}
            title={`${cls}: ${(p * 100).toFixed(1)}%`}
            style={{ width: `${p * 100}%`, background: STRATEGY_COLORS[cls] || T.text.tertiary }}
          />
        ))}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 6, fontSize: 'var(--font-size-xs)' }}>
        {entries.slice(0, 4).map(([cls, p]) => (
          <span key={cls} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 8, height: 8, background: STRATEGY_COLORS[cls] || T.text.tertiary, borderRadius: 'var(--radius-sm)' }} />
            <span style={{ color: T.text.secondary }}>{cls}</span>
            <span style={{ color: T.text.primary, fontVariantNumeric: 'tabular-nums' }}>{(p * 100).toFixed(0)}%</span>
          </span>
        ))}
      </div>
    </div>
  );
};

// ── Event timeline track (R10 instruments, R12 social) ───────────────────
const EventTimelineTrack = ({ events = [], window: w = 24, height = 28, loading = false }) => {
  if (loading) return <SkeletonLine height={height} />;
  const types = [...new Set(events.map(e => e.type))];
  const now = Date.now();
  const windowMs = w * 3600 * 1000;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {types.map(t => {
        const subset = events.filter(e => e.type === t);
        return (
          <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{ width: 100, color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>{t}</div>
            <div style={{ position: 'relative', flex: 1, height, background: T.bg.input, borderRadius: 'var(--radius-sm)' }}>
              {subset.map((e, i) => {
                const left = Math.max(0, Math.min(100, 100 - ((now - new Date(e.time).getTime()) / windowMs) * 100));
                return (
                  <span
                    key={i}
                    title={`${t} @ ${e.time}`}
                    style={{
                      position: 'absolute', top: '50%', left: `${left}%`,
                      transform: 'translate(-50%, -50%)',
                      width: 6, height: 6, borderRadius: '50%',
                      background: T.chart.c2,
                    }}
                  />
                );
              })}
            </div>
          </div>
        );
      })}
      {events.length === 0 && (
        <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-sm)', padding: 8 }}>No events in window.</div>
      )}
    </div>
  );
};

// ── Notebook tile (R13 research substrate launcher) ──────────────────────
const NotebookTile = ({ name, summary, lastRun, onRun, onOpen }) => (
  <div style={{
    background: T.bg.panel,
    border: `1px solid ${T.border.subtle}`,
    borderRadius: 'var(--radius-md)',
    padding: 'var(--card-padding)',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <Icon name="beaker" size={14} color={T.accent.violet} />
      <strong style={{ color: T.text.primary, fontSize: 'var(--font-size-sm)' }}>{name}</strong>
    </div>
    <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)', lineHeight: 'var(--line-height-body)' }}>
      {summary}
    </div>
    <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>
      Last run: {lastRun || 'never'}
    </div>
    <div style={{ display: 'flex', gap: 6, marginTop: 'auto' }}>
      <button onClick={onRun} style={{ flex: 1, padding: '6px 10px', color: T.accent.amber, border: `1px solid ${T.accent.amber}`, fontSize: 'var(--font-size-sm)' }}>
        Run
      </button>
      <button onClick={onOpen} style={{ flex: 1, padding: '6px 10px', color: T.text.secondary, border: `1px solid ${T.border.strong}`, fontSize: 'var(--font-size-sm)' }}>
        Open
      </button>
    </div>
  </div>
);

// ── Wallet / Market cells (sortable, deep-linkable) ──────────────────────
const truncateAddr = (a) => (a && a.length > 10 ? `${a.slice(0, 6)}…${a.slice(-4)}` : (a || '—'));

const WalletCell = ({ wallet, onClick }) => (
  <button
    onClick={onClick}
    style={{
      color: T.accent.violet,
      fontFamily: 'var(--font-mono)',
      fontSize: 'var(--font-size-sm)',
      background: 'transparent',
      border: 'none',
      cursor: onClick ? 'pointer' : 'default',
      padding: 0,
    }}
  >
    {truncateAddr(wallet)}
  </button>
);

const MarketCell = ({ title }) => (
  <span style={{ color: T.text.primary, fontSize: 'var(--font-size-sm)' }}>{title}</span>
);

// ── Audit log row (Risk & Config history, Resolution log) ────────────────
const AuditLogTable = ({ rows = [], emptyMessage = 'No audit events.', loading = false }) => {
  if (loading) return <TableSkeleton rows={4} cols={5} />;
  if (!rows.length) {
    return <div style={{ padding: 16, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>{emptyMessage}</div>;
  }
  return (
    <table style={{ width: '100%', fontSize: 'var(--font-size-sm)', fontVariantNumeric: 'tabular-nums', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          {['WHEN', 'KEY', 'OLD → NEW', 'ACTOR', 'SOURCE'].map(h => (
            <th key={h} style={{ padding: '6px 8px', textAlign: 'left', color: T.text.tertiary, borderBottom: `1px solid ${T.border.subtle}`, fontWeight: 500, letterSpacing: 'var(--letter-spacing-wide)' }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td style={{ padding: '6px 8px', color: T.text.secondary, whiteSpace: 'nowrap' }}>{r.when}</td>
            <td style={{ padding: '6px 8px', color: T.accent.amber, whiteSpace: 'nowrap' }}>{r.key}</td>
            <td style={{ padding: '6px 8px', color: T.text.secondary, whiteSpace: 'nowrap' }}>
              <span style={{ textDecoration: 'line-through', color: T.text.tertiary }}>{r.old}</span>
              {' → '}
              <span style={{ color: T.status.ok }}>{r.new}</span>
            </td>
            <td style={{ padding: '6px 8px', color: T.accent.violet }}>{r.actor}</td>
            <td style={{ padding: '6px 8px' }}>
              <StatusPill status="info" label={r.source} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
};

// ── Generic data table with skeleton + empty states ──────────────────────
const DataTable = ({ columns = [], rows = [], loading = false, emptyMessage = 'No data.', rowKey, dense = false }) => {
  if (loading) return <TableSkeleton rows={8} cols={columns.length || 4} />;
  if (!rows.length) {
    return <div style={{ padding: 16, color: T.text.tertiary, fontSize: 'var(--font-size-sm)', textAlign: 'center' }}>{emptyMessage}</div>;
  }
  return (
    <table style={{ width: '100%', fontSize: 'var(--font-size-sm)', borderCollapse: 'collapse', fontVariantNumeric: 'tabular-nums' }}>
      <thead>
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              style={{
                padding: dense ? '4px 6px' : '8px 8px',
                textAlign: col.align || 'left',
                color: T.text.tertiary,
                borderBottom: `1px solid ${T.border.subtle}`,
                fontWeight: 500,
                letterSpacing: 'var(--letter-spacing-wide)',
                whiteSpace: 'nowrap',
              }}
            >
              {col.label || col.key}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={rowKey ? rowKey(row, i) : i} style={{ height: 'var(--table-row-height)' }}>
            {columns.map((col) => (
              <td
                key={col.key}
                style={{
                  padding: dense ? '4px 6px' : '8px 8px',
                  textAlign: col.align || 'left',
                  color: col.color || T.text.primary,
                }}
              >
                {col.render ? col.render(row[col.key], row, i) : (row[col.key] ?? '—')}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
};

// ── Toggle (gated runtime config flag) ───────────────────────────────────
const GateToggle = ({ value, onChange, label, description, disabled = false }) => (
  <div style={{
    display: 'flex',
    alignItems: 'flex-start',
    gap: 12,
    padding: 'var(--card-padding)',
    background: T.bg.panel,
    border: `1px solid ${value ? T.status.ok : T.border.subtle}`,
    borderRadius: 'var(--radius-md)',
  }}>
    <button
      role="switch"
      aria-checked={value}
      aria-label={label}
      disabled={disabled}
      onClick={() => !disabled && onChange(!value)}
      style={{
        width: 40,
        height: 22,
        background: value ? T.status.ok : T.border.strong,
        border: `1px solid ${value ? T.status.ok : T.border.strong}`,
        borderRadius: 999,
        padding: 0,
        position: 'relative',
        flexShrink: 0,
        cursor: disabled ? 'not-allowed' : 'pointer',
      }}
    >
      <span style={{
        display: 'inline-block',
        width: 16,
        height: 16,
        background: T.bg.panel,
        borderRadius: '50%',
        position: 'absolute',
        top: 2,
        left: value ? 20 : 2,
        transition: `left var(--duration-base) var(--easing-move)`,
      }} />
    </button>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <strong style={{ color: T.text.primary, fontSize: 'var(--font-size-sm)' }}>{label}</strong>
        <StatusPill status={value ? 'ok' : 'gated'} label={value ? 'ON' : 'OFF'} />
      </div>
      {description && (
        <div style={{ color: T.text.tertiary, fontSize: 'var(--font-size-xs)', marginTop: 4 }}>
          {description}
        </div>
      )}
    </div>
  </div>
);

// ── Cross-market operator card (R12) ─────────────────────────────────────
const CrossMarketOperatorCard = ({ operator, onConfirm }) => (
  <div style={{
    background: T.bg.panel,
    border: `1px solid ${operator.is_pending_review ? T.status.warn : T.border.subtle}`,
    borderRadius: 'var(--radius-md)',
    padding: 'var(--card-padding)',
  }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <Icon name="users" size={14} color={T.accent.violet} />
      <strong style={{ color: T.text.primary, fontSize: 'var(--font-size-sm)' }}>
        Operator #{operator.operator_id}
      </strong>
      <StatusPill
        status={operator.is_pending_review ? 'pending' : 'ok'}
        label={operator.is_pending_review ? 'PENDING REVIEW' : 'CONFIRMED'}
      />
      <span style={{ marginLeft: 'auto', color: T.text.tertiary, fontSize: 'var(--font-size-xs)' }}>
        conf {Number(operator.confidence || 0).toFixed(2)}
      </span>
    </div>
    <div style={{ marginTop: 8, fontSize: 'var(--font-size-sm)', color: T.text.secondary, display: 'grid', gridTemplateColumns: '80px 1fr', gap: '4px 8px' }}>
      <span>Polymarket</span><span style={{ color: T.accent.violet, fontFamily: 'var(--font-mono)' }}>{truncateAddr(operator.polymarket_wallet)}</span>
      {operator.kalshi_account && (<><span>Kalshi</span><span>{operator.kalshi_account}</span></>)}
      {operator.manifold_handle && (<><span>Manifold</span><span>{operator.manifold_handle}</span></>)}
      {operator.x_handle && (<><span>X handle</span><span>@{operator.x_handle}</span></>)}
    </div>
    {operator.is_pending_review && onConfirm && (
      <button
        onClick={() => onConfirm(operator.operator_id)}
        style={{
          marginTop: 8,
          padding: '4px 12px',
          color: T.status.ok,
          border: `1px solid ${T.status.ok}`,
          fontSize: 'var(--font-size-sm)',
        }}
      >
        Confirm resolution
      </button>
    )}
  </div>
);

// ── Format helpers ───────────────────────────────────────────────────────
const fmtPnl = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${n.toFixed(2)}`;
};

const fmtMs = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${n.toFixed(0)} ms`;
};

const fmtPct = (v, d = 1) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${n.toFixed(d)}%`;
};

const fmtAge = (sec) => {
  const n = Number(sec);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 60) return `${n.toFixed(0)}s`;
  if (n < 3600) return `${(n / 60).toFixed(0)}m`;
  return `${(n / 3600).toFixed(1)}h`;
};

// ── useApi — module-level cache + ETag conditional GET ───────────────────
//
// Architectural notes:
//   * V1 uses ONE `/api/v1/live-summary` snapshot stored in window.LiveStore
//     so every tab reads from cache — sub-tab navigation is instantaneous.
//   * V2 has 49 specialised endpoints. The original useApi held data in
//     local component state, so each sub-tab mount triggered a fresh fetch
//     even if the same endpoint had just resolved in another tab. Switching
//     tabs flashed skeletons for 1-15s while waiting for the network.
//
// This rewrite addresses three problems at once:
//
//   1. Module-level `_apiCache` (path → {data, fetchedAt, inflight}) so
//      navigating away and back to a sub-tab finds the data already in
//      memory. Fresh values are served synchronously on mount; stale
//      values are returned while a background revalidation runs (SWR).
//
//   2. `If-None-Match` / ETag — the backend already emits ETag headers
//      on the heavy endpoints (e.g. /api/v1/live-summary). We honour it
//      here so a 304 Not Modified avoids re-parsing 80-100 KB of JSON
//      on every poll.
//
//   3. Single-flight: if two components mount with the same path at the
//      same time, they share one in-flight fetch promise.
//
// Backward-compatible: same call signature `useApi(path, { interval, deps })`,
// same return shape `{ data, loading, error }`.

const _apiCache = new Map(); // path -> { data, etag, fetchedAt, inflight }

const _apiFetch = (path) => {
  const entry = _apiCache.get(path);
  // De-duplicate in-flight requests for the same path.
  if (entry?.inflight) return entry.inflight;

  const headers = {};
  if (entry?.etag) headers['If-None-Match'] = entry.etag;

  const promise = fetch(path, { headers })
    .then(async (r) => {
      if (r.status === 304) {
        // Backend says "nothing changed" — keep cached body, just refresh ts.
        const cur = _apiCache.get(path);
        if (cur) cur.fetchedAt = Date.now();
        return cur?.data ?? null;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const etag = r.headers.get('etag') || null;
      const data = await r.json();
      _apiCache.set(path, {
        data,
        etag,
        fetchedAt: Date.now(),
        inflight: null,
        listeners: _apiCache.get(path)?.listeners || new Set(),
      });
      const cur = _apiCache.get(path);
      cur.listeners.forEach((fn) => { try { fn(data, null); } catch (_) {} });
      return data;
    })
    .catch((err) => {
      const cur = _apiCache.get(path);
      if (cur) {
        cur.inflight = null;
        cur.listeners?.forEach((fn) => { try { fn(cur.data, err); } catch (_) {} });
      }
      throw err;
    });

  if (entry) entry.inflight = promise;
  else _apiCache.set(path, { data: null, etag: null, fetchedAt: 0, inflight: promise, listeners: new Set() });
  return promise;
};

const useApi = (path, { interval = 0, deps = [] } = {}) => {
  // Read the cached value synchronously on first render — if we already
  // have a value, the component mounts with data ready and `loading=false`.
  // This is the key fix for "every sub-tab change flashes a skeleton".
  const cached = _apiCache.get(path);
  const [data, setData] = useState(cached?.data ?? null);
  const [loading, setLoading] = useState(!cached?.data);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;

    // Subscribe to cache updates so OTHER components fetching the same
    // path also notify us. Cheap pub/sub keyed by path.
    if (!_apiCache.has(path)) {
      _apiCache.set(path, { data: null, etag: null, fetchedAt: 0, inflight: null, listeners: new Set() });
    }
    const entry = _apiCache.get(path);
    const listener = (next, err) => {
      if (!alive) return;
      if (err) {
        setError(err);
        setLoading(false);
        return;
      }
      setData(next);
      setError(null);
      setLoading(false);
    };
    entry.listeners.add(listener);

    // If we have NO cached data, kick off an immediate fetch (and the
    // listener above will receive the result). If we have data but it's
    // stale, refresh in the background (stale-while-revalidate). "Stale"
    // is defined as: older than the poll interval / 2 (so periodic polls
    // still hit the network as expected) OR older than 30s for endpoints
    // without an interval.
    const stalenessMs = interval > 0 ? Math.max(1000, Math.floor(interval / 2)) : 30000;
    const ageMs = Date.now() - (entry.fetchedAt || 0);
    const isStale = !entry.data || ageMs > stalenessMs;
    if (isStale) {
      _apiFetch(path).catch(() => { /* listener will surface the error */ });
    } else {
      // Fresh enough — keep the cached data, drop the spinner.
      setData(entry.data);
      setLoading(false);
    }

    // Interval polling — only the FIRST subscriber per path actually
    // schedules a timer (the rest piggyback on the cache pub/sub).
    let timerId = null;
    if (interval > 0) {
      timerId = setInterval(() => {
        _apiFetch(path).catch(() => { });
      }, interval);
    }

    return () => {
      alive = false;
      entry.listeners.delete(listener);
      if (timerId !== null) clearInterval(timerId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);

  return { data, loading, error };
};

// ── Expose everything to window ──────────────────────────────────────────
Object.assign(window, {
  T,
  ICONS,
  Icon,
  StatusPill,
  Dot,
  STATUS_VOCAB,
  SkeletonLine,
  SkeletonBlock,
  KpiStripSkeleton,
  TableSkeleton,
  ChartSkeleton,
  KpiCell,
  KpiStrip,
  SectionLabel,
  Panel,
  BentoCard,
  Banner,
  GatedBanner,
  MissingHookBanner,
  MethodologyAuditBanner,
  PauseToggle,
  LiveStreamFeed,
  SubTabNav,
  BreadcrumbHeader,
  HeatmapMatrix,
  ScatterPlot,
  Sparkline,
  LineWithBand,
  StrategyFingerprintBar,
  STRATEGY_COLORS,
  EventTimelineTrack,
  NotebookTile,
  WalletCell,
  MarketCell,
  AuditLogTable,
  DataTable,
  GateToggle,
  CrossMarketOperatorCard,
  truncateAddr,
  fmtPnl,
  fmtMs,
  fmtPct,
  fmtAge,
  useApi,
});
