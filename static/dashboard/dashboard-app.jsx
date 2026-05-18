// dashboard-app.jsx — Shell, sidebar, topbar — wired to Poybot LiveStore

const { useState: useStateA, useEffect: useEffectA } = React;
const {
  C, S, useLiveStore, useLiveStoreSlice, useConnectionState,
  Badge, Dot, SectionLabel,
  fmtAge, fmtPnl, fmtMs, pnlColor,
} = window;
// PLAN-UIA-001 (2026-05-18): MarketScanner removed from nav + destructure
// (folded into WalletGraph 2026-05-17, dead code since). See CLAUDE.md §17.
const { AlphaTerminal, LivePortfolio, DecisionEngine, RiskConfig, BotHealth, WalletGraph, MLProgression } = window;

const { Inspector, LabGates } = window;

// A12 — BootstrapBanner: surfaces the bot's learning-progress maturity
// so the operator never has to wonder whether "0" / "—" everywhere
// means bootstrap (algo learning, low trading expected) or bug (a
// daemon crashed). Reads snapshot.bot.maturity via the systemStatus
// slice. Auto-hides at overall_pct >= 70% (bot is "ready"). Sits
// between the topbar and the tab container so it never gets masked by
// the A11 keep-alive display:none toggle.
const BootstrapBanner = () => {
  const systemStatus = useLiveStoreSlice('systemStatus');
  const maturity = systemStatus?.maturity;
  if (!maturity || typeof maturity.overall_pct !== 'number') return null;
  if (maturity.overall_pct >= 0.7) return null;

  const tier = maturity.tier || 'bootstrap';
  const label = tier === 'bootstrap'
    ? 'BOOTSTRAP MODE · algo learning, low trading expected'
    : 'RAMPING UP · profiles maturing';

  // Color palette: amber for bootstrap (warn), cyan for ramping_up
  // (informative). Both readable on the dark dashboard background.
  const isBootstrap = tier === 'bootstrap';
  const fg     = isBootstrap ? '#ffc800' : '#00c8ff';
  const bg     = isBootstrap ? 'rgba(255,200,0,0.12)' : 'rgba(0,200,255,0.08)';
  const border = isBootstrap ? 'rgba(255,200,0,0.4)' : 'rgba(0,200,255,0.3)';

  const pct = Math.round((maturity.overall_pct || 0) * 100);
  const fmtSig = v => Math.round(((v || 0) * 100)) + '%';

  // Tooltip breakdown — surfaced on hover so the operator can see why
  // the bot is still learning instead of guessing.
  const counts = maturity.counts || {};
  const targets = maturity.targets || {};
  const tt = [
    `Maturity composition (mean of 4 signals):`,
    `• Profiles: ${fmtSig(maturity.profiles_pct)} (${(counts.positions_resolved || 0).toLocaleString()} / ${(targets.positions_resolved || 0).toLocaleString()} resolved positions)`,
    `• Sample: ${fmtSig(maturity.sample_eff_pct)} (${counts.leaders_with_sample || 0} / ${targets.leaders_with_sample || 0} leaders with ≥20 trades)`,
    `• Coverage: ${fmtSig(maturity.cat_coverage_pct)} (${counts.categories_covered || 0} / ${targets.categories_covered || 0} categories)`,
    `• Decisions: ${fmtSig(maturity.decision_health_pct)} (24h decision log activity)`,
    `Tier: ${tier} · auto-hides at ≥70%.`,
  ].join('\n');

  return (
    <div
      title={tt}
      style={{
        padding: '6px 16px',
        background: bg,
        borderBottom: `1px solid ${border}`,
        fontSize: 11,
        color: fg,
        letterSpacing: '0.5px',
        fontFamily: "'JetBrains Mono', monospace",
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        cursor: 'help',
      }}
    >
      <span style={{ fontWeight: 700 }}>⚠</span>
      <span>{label}</span>
      <span style={{ color: C.dim2, fontWeight: 400 }}>·</span>
      <span style={{ color: fg, fontWeight: 700 }}>maturity {pct}%</span>
      <span style={{ color: C.dim2, fontSize: 10, marginLeft: 'auto' }}>
        P {fmtSig(maturity.profiles_pct)} · S {fmtSig(maturity.sample_eff_pct)} · C {fmtSig(maturity.cat_coverage_pct)} · D {fmtSig(maturity.decision_health_pct)}
      </span>
    </div>
  );
};
window.BootstrapBanner = BootstrapBanner;

// PLAN-UIA-001 — ModeChip: 3-state mode badge (paper/live/dual) with
// reconciliation suffix so the operator can always tell whether the
// displayed numbers match Gamma ground truth. Replaces the binary
// PAPER/LIVE chip that hid the drift case.
const ModeChip = ({ mode, reconVerdict, size = 'xs' }) => {
  const cfg = {
    paper: { type: 'blue', label: 'PAPER' },
    live:  { type: 'red',  label: 'LIVE'  },
    dual:  { type: 'amber',label: 'DUAL'  },
  }[mode] || { type: 'default', label: (mode || '?').toUpperCase() };
  const showRecon = mode === 'paper' || mode === 'dual';
  const suffix = !showRecon ? ''
    : reconVerdict === 'critical' ? ' · DRIFT'
    : reconVerdict === 'warn'     ? ' · WARN'
    : reconVerdict === 'ok'       ? ' · OK'
    : '';
  // When critical, override the chip color to red regardless of mode.
  const finalType = (showRecon && reconVerdict === 'critical') ? 'red' : cfg.type;
  return <Badge type={finalType} size={size}>{cfg.label}{suffix}</Badge>;
};
window.ModeChip = ModeChip;
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
  // LAB tab — single cockpit for R7/R8/R9/R10 runtime gates. Added 2026-05-17
  // to surface the V2 features that have backend daemons running but whose
  // output is gated OFF until shadow-soaks pass + operator validates per gate.
  { id: 'lab',        label: 'LAB',             icon: '⚗', component: LabGates || (() => null) },
];

// ── Sidebar ────────────────────────────────────────────────────────────────────
// A9 migration: subscribes to slices instead of the full snapshot. Re-renders
// only when systemStatus, paperPnL, or the connection state change — a
// trade landing on the WS no longer reruns this tree.
const Sidebar = ({ tab, setTab }) => {
  const [time, setTime] = useStateA(new Date().toLocaleTimeString('en-GB'));
  const systemStatus = useLiveStoreSlice('systemStatus');
  const paperPnL = useLiveStoreSlice('paperPnL');
  const connectionState = useConnectionState();

  if (typeof window !== 'undefined' && window.__LIVESTORE_DEBUG__) {
    console.log('[re-render] Sidebar', { systemStatus: !!systemStatus, paperPnL: !!paperPnL, connectionState });
  }

  useEffectA(() => {
    const id = setInterval(() => setTime(new Date().toLocaleTimeString('en-GB')), 1000);
    return () => clearInterval(id);
  }, []);

  const ss = systemStatus || {};
  const stats = paperPnL || {};
  // PLAN-UIA-001 — paper truth surface. Reconciliation rides in systemStatus
  // because the sidebar chip reads it alongside bot/ws status; the dedicated
  // `reconciliation` slice exists for components that ONLY care about recon
  // and don't want to rerun when bot uptime ticks.
  const recon = ss.reconciliation || { verdict: 'unknown', pnl_delta_abs: 0, age_s: null };
  const reconVerdict = recon.verdict || 'unknown';
  const reconColor = { ok: C.green, warn: C.amber, critical: C.red, unknown: C.dim2 }[reconVerdict] || C.dim2;
  const execMode = ss.execution_mode || 'paper';

  const connColor = { connected: C.green, reconnecting: C.amber, connecting: C.amber, disconnected: C.red }[connectionState] || C.dim2;
  const connLabel = { connected: 'LIVE', reconnecting: 'RECON…', connecting: 'CONN…', disconnected: 'OFFLINE' }[connectionState] || '—';
  const botStatusUpper = (ss.bot_status || '—').toUpperCase();
  const botColor = botStatusUpper === 'RUNNING' ? C.green : botStatusUpper === 'STOPPED' ? C.amber : C.red;

  const reconClickHandler = () => {
    if (window.PoybotNav?.setActiveTab) window.PoybotNav.setActiveTab('inspector');
    else if (typeof setTab === 'function') setTab('inspector');
  };

  // Recon delta has two shapes depending on the path: HTTP snapshot uses
  // `pnl_delta_abs`, the typed WS payload uses `delta_abs`. Accept both.
  const reconDelta = recon.pnl_delta_abs ?? recon.delta_abs ?? 0;

  const sysRows = [
    { label: 'BOT',       color: botColor,  value: botStatusUpper },
    { label: 'WS',        color: connColor, value: connLabel },
    {
      label: 'INGESTION',
      color: (ss.live_markets || 0) > 0 ? C.green : C.red,
      value: ss.total_markets ? `${ss.live_markets || 0}/${ss.total_markets}` : '—',
    },
    // PLAN-UIA-001 — always-rendered RECON chip per ADR-PMK-014.10.
    // "Silence implies trustable. Explicit UNKNOWN teaches operator the question exists."
    {
      label: 'RECON',
      color: reconColor,
      value: reconVerdict === 'unknown' ? 'UNKNOWN'
           : reconVerdict === 'ok'      ? 'OK'
           : `Δ ${fmtPnl(reconDelta)}`,
      onClick: reconClickHandler,
      title: 'Paper-trade reconciliation vs Gamma. Click to open Inspector.',
    },
    {
      label: 'EXEC',
      color: ss.execution_enabled ? C.green : C.amber,
      value: ss.execution_enabled ? 'LIVE' : 'DRY RUN',
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
          <div
            key={r.label}
            onClick={r.onClick}
            title={r.title}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4,
              cursor: r.onClick ? 'pointer' : 'default',
            }}
            onMouseEnter={r.onClick ? e => e.currentTarget.style.background = 'rgba(255,255,255,0.03)' : undefined}
            onMouseLeave={r.onClick ? e => e.currentTarget.style.background = 'transparent' : undefined}
          >
            <Dot status={r.color === C.green ? 'ok' : r.color === C.amber ? 'warn' : r.color === C.red ? 'err' : 'off'} />
            <span style={{ color: C.dim2, minWidth: 58 }}>{r.label}</span>
            <span style={{ color: r.color, marginLeft: 'auto', fontWeight: 600, fontSize: 9, letterSpacing: '0.04em' }}>{r.value}</span>
          </div>
        ))}

        {/* PLAN-UIA-001: KPI hierarchy — Win Rate FIRST (mission KPI, 28% → 70%+
            per memory project_polymarket_bot.md), Net PnL second (currently
            unreliable per project_paper_trading_truth.md; carries a warning
            marker when reconciliation is critical). */}
        <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
          {/* Win Rate vs 70% target */}
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
            <span style={{ color: C.dim2 }}>WIN RATE</span>
            <span style={{ color: stats.win_rate >= 0.7 ? C.green : stats.win_rate >= 0.5 ? C.amber : stats.win_rate != null ? C.red : C.dim2, fontWeight: 700 }}>
              {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—'}
              <span style={{ color: C.dim2, fontSize: 9, marginLeft: 4, fontWeight: 400 }}>/ 70%</span>
            </span>
          </div>
          {/* Target progress bar */}
          <div style={{ height: 2, background: 'rgba(255,255,255,0.05)', marginBottom: 8 }}>
            <div style={{
              width: `${Math.min(100, ((stats.win_rate || 0) / 0.70) * 100)}%`,
              height: '100%',
              background: stats.win_rate >= 0.7 ? C.green : C.amber,
              transition: 'width 0.4s',
            }} />
          </div>
          {/* Net PnL with reconciliation warning marker */}
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ color: C.dim2 }}>NET PNL</span>
            <span style={{ color: pnlColor(stats.total_pnl), fontWeight: 600 }}>
              {stats.total_pnl != null ? `${stats.total_pnl >= 0 ? '+' : ''}$${Math.abs(stats.total_pnl).toFixed(2)}` : '—'}
              {reconVerdict === 'critical' && (
                <span title="Reconciliation critical — displayed PnL diverges from Gamma" style={{ color: C.red, fontSize: 10, marginLeft: 4 }}>⚠</span>
              )}
              {reconVerdict === 'warn' && (
                <span title="Reconciliation warning — displayed PnL diverges from Gamma" style={{ color: C.amber, fontSize: 10, marginLeft: 4 }}>·</span>
              )}
            </span>
          </div>
        </div>

        {/* PLAN-UIA-001: ModeChip replaces binary PAPER/LIVE badge. */}
        <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${C.border}`, display: 'flex', gap: 6, alignItems: 'center' }}>
          <ModeChip mode={execMode} reconVerdict={reconVerdict} size="xs" />
          <span style={{ fontSize: 9, color: C.dim2 }}>Poybot</span>
          {ss.uptime_seconds != null && (
            <span style={{ fontSize: 9, color: C.dim2, marginLeft: 'auto' }}>↑{fmtAge(ss.uptime_seconds)}</span>
          )}
        </div>
      </div>
    </div>
  );
};

// ── App ────────────────────────────────────────────────────────────────────────
// PLAN-UIA-001: removed the Tweaks panel (CMS-style editor mode toggled via
// postMessage). It had no place in an operator trading terminal.

const App = () => {
  const [tab, setTab]           = useStateA(() => localStorage.getItem('pmi_tab') || 'alpha');
  const { snapshot }            = useLiveStore();
  const bot = snapshot?.bot || {};

  useEffectA(() => { localStorage.setItem('pmi_tab', tab); }, [tab]);

  // A11 — Tabs keep-alive. Each tab is mounted once on first visit and then
  // hidden via display:none on subsequent switches. State (filters, scroll,
  // pagination, intervals, fetched data) is preserved, switch is instant.
  //
  // Lazy-mount: a tab isn't rendered until the user activates it once. We
  // compute the visited set DURING render (not in an effect) so the first
  // render after a tab switch already includes the new tab — no flash of
  // empty content. The Set is held as a ref so it survives rerenders and
  // additions are O(1).
  const visitedTabsRef = React.useRef(new Set([tab]));
  if (!visitedTabsRef.current.has(tab)) visitedTabsRef.current.add(tab);
  const visitedTabs = visitedTabsRef.current;

  // ── Cross-module navigation bus ──────────────────────────────────────────
  // Each tab is otherwise siloed; this bus lets one tab dispatch a request to
  // open another tab pre-filtered to a specific entity (wallet, decision,
  // market). Listeners are added per-component via window.addEventListener.
  // Kept on `window` so tabs that re-mount don't lose subscribers, and so
  // PoybotNav.* can be called from anywhere (vanilla JS, console, future
  // keyboard shortcuts).
  // Active context (selected wallet / decision / etc) for the topbar
  // breadcrumb. Tabs publish their selection via PoybotNav.setContext —
  // App reads it directly to render the breadcrumb without prop drilling.
  const [navContext, setNavContext] = useStateA(null); // {type, id, label} or null

  useEffectA(() => {
    if (window.PoybotNav) return;
    const dispatch = (type, detail) => setTimeout(
      () => window.dispatchEvent(new CustomEvent(type, { detail })),
      30,
    );
    window.PoybotNav = {
      setActiveTab: (id) => setTab(id),
      selectWallet: (wallet, opts = {}) => {
        if (!wallet) return;
        setTab(opts.tabHint || 'graph');
        dispatch('pmi:select-wallet', { wallet, view: opts.view || 'graph', ...opts });
        setNavContext({ type: 'wallet', id: wallet, label: wallet.slice(0, 6) + '…' + wallet.slice(-4) });
      },
      selectDecision: (decision) => {
        if (!decision) return;
        setTab('decisions');
        dispatch('pmi:select-decision', { decision });
        const id = typeof decision === 'object' ? decision.id : decision;
        setNavContext({ type: 'decision', id, label: `#${id}` });
      },
      showDataQualityIssue: (issueKey) => {
        dispatch('pmi:dq-issue', { issueKey });
      },
      setContext: (ctx) => setNavContext(ctx),
      clearContext: () => setNavContext(null),
    };
  }, []);

  // ── Global keyboard shortcuts ────────────────────────────────────────────
  // Vim-style "g <letter>" sequences for tab switching, plus "?" for help.
  // Disabled when the focus is in a text input/textarea so typing isn't
  // hijacked. Sequences time out after 1.2s.
  const [showShortcuts, setShowShortcuts] = useStateA(false);
  const seqStateRef = React.useRef({ leader: null, expiresAt: 0 });
  useEffectA(() => {
    const TAB_BY_KEY = {
      a: 'alpha', m: 'mlprog', w: 'graph', p: 'portfolio',
      d: 'decisions', i: 'inspector', r: 'risk', h: 'health',
      l: 'lab',  // PLAN-UIA-001
    };
    const onKey = (e) => {
      // Ignore when typing in an editable field.
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      // ? toggles the help overlay (no leader needed).
      if (e.key === '?') { e.preventDefault(); setShowShortcuts(s => !s); return; }
      if (e.key === 'Escape' && showShortcuts) { setShowShortcuts(false); return; }

      const now = Date.now();
      const st = seqStateRef.current;

      // Leader key — start a "g …" sequence.
      if (e.key === 'g' && (!st.leader || st.expiresAt < now)) {
        st.leader = 'g';
        st.expiresAt = now + 1200;
        return;
      }

      // Second key of a sequence.
      if (st.leader === 'g' && st.expiresAt > now) {
        st.leader = null;
        const target = TAB_BY_KEY[e.key.toLowerCase()];
        if (target) {
          e.preventDefault();
          setTab(target);
        }
        return;
      }
      // Sequence stale — reset.
      if (st.expiresAt && st.expiresAt < now) { st.leader = null; st.expiresAt = 0; }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showShortcuts]);


  // UTC clock tick
  const [utc, setUtc] = useStateA(() => new Date().toISOString().slice(11, 19));
  useEffectA(() => {
    const id = setInterval(() => setUtc(new Date().toISOString().slice(11, 19)), 1000);
    return () => clearInterval(id);
  }, []);

  const isRunning = bot.status === 'running';

  return (
    <div style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', fontFamily: "'JetBrains Mono', monospace", background: C.bg }}>
      <Sidebar tab={tab} setTab={setTab} />

      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>

        {/* Topbar */}
        <div style={{ padding: '6px 16px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 10, background: C.panel, flexShrink: 0 }}>
          <span style={{ fontSize: 10, color: C.amber, fontWeight: 700, letterSpacing: '0.09em' }}>
            {NAV.find(n => n.id === tab)?.label}
          </span>

          {/* Breadcrumb context — shows the active selection (wallet/decision). */}
          {navContext && (
            <>
              <span style={{ color: C.dim2, fontSize: 11 }}>›</span>
              <span style={{
                fontSize: 10, fontFamily: 'monospace',
                color: navContext.type === 'wallet' ? C.purple : navContext.type === 'decision' ? C.blue : C.text,
                background: 'rgba(255,255,255,0.03)',
                padding: '2px 6px',
                border: `1px solid ${C.border2}`,
              }}>
                {navContext.type}: <b>{navContext.label}</b>
              </span>
              <button onClick={() => window.PoybotNav?.clearContext()} title="Clear context"
                style={{ background: 'transparent', border: 'none', color: C.dim2, fontSize: 10, cursor: 'pointer', padding: '0 2px' }}>
                ✕
              </button>
            </>
          )}

          <span style={{ color: C.border2 }}>│</span>
          <Badge type={isRunning ? 'green' : bot.status === 'stopped' ? 'amber' : 'red'} size="xs">
            {(bot.status || 'offline').toUpperCase()}
          </Badge>
          <span style={{ color: C.border2 }}>│</span>
          {/* PLAN-UIA-001 — ModeChip replaces the old binary LIVE/DRY-RUN badge. */}
          <ModeChip mode={(snapshot?.bot?.execution_mode || 'paper').toLowerCase()} reconVerdict={snapshot?.reconciliation?.verdict || 'unknown'} size="xs" />
          {/* PLAN-UIA-001 — WS lag replaces bot.latency_ms. The actually-important
              latency for a leader-intel bot is "how stale is the most recent trade". */}
          {(() => {
            const lag = snapshot?.ingestion?.ws_last_message_age_s ?? snapshot?.ingestion?.avg_freshness_ms;
            if (lag == null) return null;
            // ws_last_message_age_s is seconds; avg_freshness_ms is ms.
            const lagMs = snapshot?.ingestion?.ws_last_message_age_s != null
              ? snapshot.ingestion.ws_last_message_age_s * 1000
              : lag;
            const color = lagMs > 30_000 ? C.red : lagMs > 5_000 ? C.amber : C.green;
            return (
              <>
                <span style={{ color: C.border2 }}>│</span>
                <span style={{ fontSize: 10, color: C.dim2 }}>ws lag <span style={{ color }}>{fmtMs(lagMs)}</span></span>
              </>
            );
          })()}

          <button onClick={() => setShowShortcuts(true)} title="Keyboard shortcuts (?)"
            style={{ marginLeft: 'auto', background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 10, padding: '2px 7px', cursor: 'pointer', fontFamily: 'monospace' }}>
            ?
          </button>
          <span style={{ fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>UTC {utc}</span>
        </div>

        {/* A12 — Bootstrap banner. Sits OUTSIDE the tab keep-alive
            container so it's visible on every tab. Auto-hides at
            maturity ≥ 70%. Reads systemStatus slice — null-safe. */}
        <BootstrapBanner />

        {/* A11 — Keep-alive tab container. Every visited tab is mounted once
            and then masked via display:none. The active tab gets the full
            absolutely-positioned slot (flex/grid layouts inside it work as
            before); inactive tabs are removed from the layout flow but
            keep their internal state, polling intervals, and scroll
            position. First-visit tabs render under their slot too — the
            lazy-mount Set above guarantees we never instantiate a tab the
            user hasn't selected. */}
        <div style={{ flex: 1, overflow: 'hidden', position: 'relative', minWidth: 0 }}>
          {NAV.map(({ id, component: Component }) => {
            if (!visitedTabs.has(id)) return null;
            const active = id === tab;
            return (
              <div key={id} style={{
                position: 'absolute',
                top: 0, left: 0, right: 0, bottom: 0,
                display: active ? 'flex' : 'none',
                flexDirection: 'column',
              }}>
                <Component />
              </div>
            );
          })}
        </div>
      </div>

      {/* Data Quality drill-down modal — global so any tab can summon it */}
      <DqIssueModal />

      {/* Decision reasoning side panel — opened via PoybotNav.selectDecision */}
      <DecisionDetailPanel />

      {/* Keyboard shortcuts help — toggle with `?` */}
      {showShortcuts && (
        <ShortcutsOverlay nav={NAV} onClose={() => setShowShortcuts(false)} />
      )}
    </div>
  );
};

// ── Data Quality drill-down modal ───────────────────────────────────────────
// Triggered by `window.PoybotNav.showDataQualityIssue(key)`. Fetches the
// affected markets from /api/data-quality/markets and shows them in a
// scrollable list. Built as a separate component so any tab can summon it
// without prop drilling.
const DqIssueModal = () => {
  const [open, setOpen] = useStateA(false);
  const [issueKey, setIssueKey] = useStateA(null);
  const [data, setData] = useStateA(null);
  const [loading, setLoading] = useStateA(false);

  useEffectA(() => {
    const handler = (e) => {
      const key = e.detail?.issueKey;
      if (!key) return;
      setIssueKey(key);
      setOpen(true);
      setLoading(true);
      setData(null);
      const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
      fetch(`${base}/api/data-quality/markets?issue=${encodeURIComponent(key)}&limit=100`)
        .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
        .then(d => setData(d))
        .catch(err => { console.warn('[DqIssueModal] fetch failed', err); setData({ markets: [], _error: true }); })
        .finally(() => setLoading(false));
    };
    window.addEventListener('pmi:dq-issue', handler);
    return () => window.removeEventListener('pmi:dq-issue', handler);
  }, []);

  // ESC closes
  useEffectA(() => {
    if (!open) return;
    const onKey = e => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  if (!open) return null;

  const titleByKey = {
    unmapped_tokens: 'Markets without token mapping',
    expired_still_active: 'Expired markets still marked active',
    orphan_market_ids: 'Orphan trades (no market metadata)',
    stale_leaders: 'Leaders with stale Falcon refresh',
    stale_profiles: 'Profiles not updated in 24h',
  };
  const title = titleByKey[issueKey] || issueKey;
  const markets = data?.markets || [];

  return (
    <div onClick={() => setOpen(false)} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 10000, backdropFilter: 'blur(2px)',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: C.panel, border: `1px solid ${C.border2}`,
        width: 'min(900px, 92vw)', maxHeight: '82vh',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          <span style={{ ...S.label, color: C.amber, fontSize: 11, flex: 1 }}>{title}</span>
          {data && !data._error && (
            <span style={{ fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>
              {data.total != null ? `${markets.length} of ${data.total}` : `${markets.length} markets`}
            </span>
          )}
          <button onClick={() => setOpen(false)} title="Close (Esc)"
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 12, padding: '2px 9px', cursor: 'pointer', lineHeight: 1.4 }}>
            ✕
          </button>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflow: 'auto' }}>
          {loading ? (
            <div style={{ padding: 30, color: C.dim2, textAlign: 'center', fontSize: 11 }}>Loading…</div>
          ) : data?._error ? (
            <div style={{ padding: 30, color: C.red, textAlign: 'center', fontSize: 11 }}>Failed to fetch issue details.</div>
          ) : markets.length === 0 ? (
            <div style={{ padding: 30, color: C.dim2, textAlign: 'center', fontSize: 11 }}>No items to show.</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
              <thead style={{ position: 'sticky', top: 0, background: C.panel2, zIndex: 1 }}>
                <tr>
                  <th style={{ ...S.label, padding: '6px 10px', textAlign: 'left', fontWeight: 700 }}>Market</th>
                  <th style={{ ...S.label, padding: '6px 10px', textAlign: 'left', fontWeight: 700 }}>Category</th>
                  <th style={{ ...S.label, padding: '6px 10px', textAlign: 'right', fontWeight: 700 }}>Trades 7d</th>
                  <th style={{ ...S.label, padding: '6px 10px', textAlign: 'left', fontWeight: 700 }}>Token YES / NO</th>
                  <th style={{ ...S.label, padding: '6px 10px', textAlign: 'left', fontWeight: 700 }}>Last seen</th>
                </tr>
              </thead>
              <tbody>
                {markets.map(m => (
                  <tr key={m.market_id} style={{ borderBottom: `1px solid ${C.border}` }}>
                    <td style={{ padding: '5px 10px', maxWidth: 380 }}>
                      <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.text }}>{m.question || '—'}</div>
                      <div style={{ fontFamily: 'monospace', fontSize: 9, color: C.dim2 }}>{m.market_id?.slice(0, 16)}…</div>
                    </td>
                    <td style={{ padding: '5px 10px', color: C.blue, fontSize: 10 }}>{m.category || 'unknown'}</td>
                    <td style={{ padding: '5px 10px', color: m.trades_7d > 0 ? C.green : C.dim2, fontFamily: 'monospace', textAlign: 'right' }}>{m.trades_7d ?? 0}</td>
                    <td style={{ padding: '5px 10px', fontSize: 10, fontFamily: 'monospace' }}>
                      <span style={{ color: m.has_token_yes ? C.green : C.red }}>{m.has_token_yes ? 'YES' : 'no'}</span>
                      {' / '}
                      <span style={{ color: m.has_token_no ? C.green : C.red }}>{m.has_token_no ? 'NO' : 'no'}</span>
                    </td>
                    <td style={{ padding: '5px 10px', color: C.dim2, fontSize: 10 }}>{m.last_seen_iso ? new Date(m.last_seen_iso).toLocaleString('en-GB', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer hint */}
        {data?.hint && (
          <div style={{ padding: '8px 16px', borderTop: `1px solid ${C.border}`, fontSize: 10, color: C.dim2, fontStyle: 'italic', flexShrink: 0 }}>
            ↳ {data.hint}
          </div>
        )}
      </div>
    </div>
  );
};

// ── Decision reasoning side panel ──────────────────────────────────────────
// Slides in from the right when PoybotNav.selectDecision({id} | {decision})
// is called. Fetches /api/decision/{id} for the full reasoning + scores +
// market + leader context + sibling decisions on the same market.
const DecisionDetailPanel = () => {
  const [open, setOpen] = useStateA(false);
  const [data, setData] = useStateA(null);
  const [loading, setLoading] = useStateA(false);

  useEffectA(() => {
    const handler = (e) => {
      const dec = e.detail?.decision;
      if (!dec) return;
      const id = (typeof dec === 'object' ? dec.id : dec);
      if (!id) return;
      setOpen(true);
      setLoading(true);
      setData(null);
      const base = window.PoybotAPI?.getSettings?.()?.API_BASE || '';
      fetch(`${base}/api/decision/${id}`)
        .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
        .then(d => setData(d))
        .catch(err => { console.warn('[DecisionDetailPanel] fetch failed', err); setData({ _error: true }); })
        .finally(() => setLoading(false));
    };
    window.addEventListener('pmi:select-decision', handler);
    return () => window.removeEventListener('pmi:select-decision', handler);
  }, []);

  useEffectA(() => {
    if (!open) return;
    const onKey = e => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  if (!open) return null;

  const sc = data?.scores || {};
  const lead = data?.leader || {};
  const mkt = data?.market || {};
  const audit = data?.signal_audit || {};
  const auditEntries = Object.entries(audit || {});

  const actionColor = data?.action === 'follow' ? C.green : data?.action === 'fade' ? C.amber : C.dim2;

  return (
    <div onClick={() => setOpen(false)} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
      zIndex: 10000, display: 'flex', justifyContent: 'flex-end',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: 'min(560px, 92vw)', height: '100%',
        background: C.panel, borderLeft: `1px solid ${C.border2}`,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          <span style={{ ...S.label, color: actionColor, fontSize: 11, flex: 1 }}>
            {data?.action ? `Decision · ${data.action.toUpperCase()}` : 'Decision'}
            {data?.id ? <span style={{ color: C.dim2, fontFamily: 'monospace', marginLeft: 6, fontWeight: 400 }}>#{data.id}</span> : null}
          </span>
          {data?.outcome && <Badge type={data.outcome === 'win' ? 'green' : data.outcome === 'loss' ? 'red' : 'default'} size="xs">{data.outcome}</Badge>}
          <button onClick={() => setOpen(false)} title="Close (Esc)"
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 12, padding: '2px 9px', cursor: 'pointer', lineHeight: 1.4 }}>✕</button>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
          {loading ? (
            <div style={{ color: C.dim2, fontSize: 11 }}>Loading…</div>
          ) : data?._error ? (
            <div style={{ color: C.red, fontSize: 11 }}>Failed to load decision details.</div>
          ) : !data ? null : (
            <>
              {/* Scores card */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6 }}>
                <div style={{ background: C.panel2, padding: '8px 10px' }}>
                  <div style={{ ...S.label, fontSize: 9 }}>Confidence</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: actionColor, fontFamily: 'monospace', marginTop: 2 }}>{sc.confidence != null ? sc.confidence.toFixed(2) : '—'}</div>
                </div>
                <div style={{ background: C.panel2, padding: '8px 10px' }}>
                  <div style={{ ...S.label, fontSize: 9 }}>T(follow)</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: C.green, fontFamily: 'monospace', marginTop: 2 }}>{sc.thompson_follow != null ? sc.thompson_follow.toFixed(2) : '—'}</div>
                </div>
                <div style={{ background: C.panel2, padding: '8px 10px' }}>
                  <div style={{ ...S.label, fontSize: 9 }}>T(fade)</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: C.amber, fontFamily: 'monospace', marginTop: 2 }}>{sc.thompson_fade != null ? sc.thompson_fade.toFixed(2) : '—'}</div>
                </div>
                <div style={{ background: C.panel2, padding: '8px 10px' }}>
                  <div style={{ ...S.label, fontSize: 9 }}>Kelly</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: C.purple, fontFamily: 'monospace', marginTop: 2 }}>{sc.kelly_fraction != null ? sc.kelly_fraction.toFixed(3) : '—'}</div>
                </div>
              </div>

              {/* Time + invalidation */}
              <div style={{ fontSize: 10, color: C.dim2, fontFamily: 'monospace' }}>
                {data.time_iso ? new Date(data.time_iso).toLocaleString('en-GB') : '—'}
                {data.invalidated_at_iso && (
                  <span style={{ color: C.red, marginLeft: 10 }}>
                    ✗ invalidated {new Date(data.invalidated_at_iso).toLocaleString('en-GB')} — {data.invalidated_reason || '(no reason)'}
                  </span>
                )}
              </div>

              {/* Leader */}
              <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                <div style={{ ...S.label, marginBottom: 6 }}>Leader</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <span
                    onClick={() => lead.wallet && window.PoybotNav?.selectWallet(lead.wallet)}
                    title={lead.wallet ? `Open ${lead.wallet} in Wallet Graph` : undefined}
                    style={{ color: C.purple, fontFamily: 'monospace', fontSize: 12, fontWeight: 600, cursor: lead.wallet ? 'pointer' : 'default', textDecoration: lead.wallet ? 'underline dotted rgba(120,85,192,0.3)' : 'none', textUnderlineOffset: 3 }}>
                    {lead.wallet || '—'}
                  </span>
                  <Badge type={lead.phase === 3 ? 'green' : lead.phase === 2 ? 'amber' : 'blue'} size="xs">P{lead.phase || 1}</Badge>
                  {lead.classification?.strategy && <Badge type="default" size="xs">{lead.classification.strategy}</Badge>}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, fontSize: 10 }}>
                  <div><div style={{ ...S.label, fontSize: 8 }}>Falcon</div><div style={{ color: C.amber, fontFamily: 'monospace', fontSize: 12, fontWeight: 600 }}>{(lead.falcon_score || 0).toFixed(2)}</div></div>
                  <div><div style={{ ...S.label, fontSize: 8 }}>Maturity</div><div style={{ color: C.purple, fontFamily: 'monospace', fontSize: 12, fontWeight: 600 }}>{(lead.maturity || 0).toFixed(2)}</div></div>
                  <div><div style={{ ...S.label, fontSize: 8 }}>Trades</div><div style={{ color: C.text, fontFamily: 'monospace', fontSize: 12, fontWeight: 600 }}>{(lead.trades_observed || 0).toLocaleString()}</div></div>
                  <div><div style={{ ...S.label, fontSize: 8 }}>Resolved</div><div style={{ color: C.green, fontFamily: 'monospace', fontSize: 12, fontWeight: 600 }}>{lead.positions_resolved || 0}</div></div>
                </div>
              </div>

              {/* Market */}
              <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                <div style={{ ...S.label, marginBottom: 6 }}>Market</div>
                <div style={{ color: C.text, fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{mkt.question || '—'}</div>
                <div style={{ display: 'flex', gap: 12, fontSize: 10, color: C.dim2 }}>
                  <span style={{ color: C.blue }}>{mkt.category}</span>
                  <span>vol 24h: <span style={{ color: C.text, fontFamily: 'monospace' }}>${(mkt.volume_24h || 0).toFixed(0)}</span></span>
                  {mkt.end_date_iso && <span>ends: <span style={{ color: C.text }}>{new Date(mkt.end_date_iso).toLocaleDateString('en-GB')}</span></span>}
                </div>
                <div style={{ marginTop: 4, fontSize: 9, color: C.dim2, fontFamily: 'monospace' }}>{mkt.id}</div>
              </div>

              {/* Reason */}
              {data.reason && (
                <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                  <div style={{ ...S.label, marginBottom: 6 }}>Reason</div>
                  <div style={{ color: C.text, fontSize: 11, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{data.reason}</div>
                </div>
              )}

              {/* Signal audit (if populated) */}
              {auditEntries.length > 0 && (
                <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                  <div style={{ ...S.label, marginBottom: 6 }}>Signal audit</div>
                  <div style={{ display: 'grid', gap: 3, fontSize: 10 }}>
                    {auditEntries.map(([k, v]) => (
                      <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 6, background: C.panel2, padding: '3px 8px' }}>
                        <span style={{ color: C.dim2 }}>{k}</span>
                        <span style={{ color: C.text, fontFamily: 'monospace', textAlign: 'right', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Siblings */}
              {data.siblings && data.siblings.length > 0 && (
                <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
                  <div style={{ ...S.label, marginBottom: 6 }}>Other decisions on this market (±30 min)</div>
                  <div style={{ display: 'grid', gap: 3, fontSize: 10 }}>
                    {data.siblings.map(s => (
                      <div key={s.id}
                        onClick={() => window.PoybotNav?.selectDecision(s)}
                        title="Open this decision"
                        style={{ display: 'grid', gridTemplateColumns: '90px 70px 1fr 50px', gap: 6, alignItems: 'center', padding: '4px 6px', background: C.panel2, cursor: 'pointer', transition: 'background 100ms' }}
                        onMouseEnter={e => e.currentTarget.style.background = 'rgba(232,160,32,0.06)'}
                        onMouseLeave={e => e.currentTarget.style.background = C.panel2}>
                        <span style={{ color: C.dim2, fontFamily: 'monospace' }}>{s.time_iso ? new Date(s.time_iso).toLocaleTimeString('en-GB') : '—'}</span>
                        <span style={{ color: C.purple, fontFamily: 'monospace' }}>{s.leader_wallet ? s.leader_wallet.slice(0, 6) + '…' : '—'}</span>
                        <Badge type={s.action === 'follow' ? 'green' : s.action === 'fade' ? 'amber' : 'default'} size="xs">{s.action}</Badge>
                        <span style={{ color: C.text, textAlign: 'right', fontFamily: 'monospace' }}>{s.confidence != null ? s.confidence.toFixed(2) : '—'}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

// ── Keyboard shortcuts overlay ────────────────────────────────────────────
const ShortcutsOverlay = ({ nav, onClose }) => {
  const KEYS = [
    { keys: ['g', 'a'], desc: 'Go to Alpha Terminal' },
    { keys: ['g', 'm'], desc: 'Go to ML Progression' },
    { keys: ['g', 'w'], desc: 'Go to Wallet Graph' },
    { keys: ['g', 'p'], desc: 'Go to Live Portfolio' },
    { keys: ['g', 'd'], desc: 'Go to Decision Engine' },
    { keys: ['g', 'i'], desc: 'Go to Inspector' },
    { keys: ['g', 'r'], desc: 'Go to Risk & Config' },
    { keys: ['g', 'h'], desc: 'Go to Bot Health' },
    { keys: ['g', 'l'], desc: 'Go to LAB' },
    { keys: ['?'], desc: 'Toggle this help' },
    { keys: ['Esc'], desc: 'Close any modal/panel' },
  ];
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 10001, backdropFilter: 'blur(2px)',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: C.panel, border: `1px solid ${C.border2}`,
        width: 'min(420px, 90vw)', padding: '20px 22px',
        display: 'flex', flexDirection: 'column', gap: 14,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ ...S.label, color: C.amber, flex: 1 }}>Keyboard shortcuts</span>
          <button onClick={onClose} title="Close (Esc)"
            style={{ background: 'transparent', border: `1px solid ${C.border2}`, color: C.dim2, fontSize: 12, padding: '2px 9px', cursor: 'pointer', lineHeight: 1.4 }}>✕</button>
        </div>
        <div style={{ display: 'grid', gap: 8 }}>
          {KEYS.map((row, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ display: 'flex', gap: 4, minWidth: 90 }}>
                {row.keys.map((k, j) => (
                  <kbd key={j} style={{
                    background: C.panel2, border: `1px solid ${C.border2}`,
                    color: C.amber, fontFamily: 'monospace',
                    padding: '2px 7px', fontSize: 11, fontWeight: 700,
                    borderRadius: 2, minWidth: 14, textAlign: 'center', display: 'inline-block',
                  }}>{k}</kbd>
                ))}
              </div>
              <span style={{ color: C.text, fontSize: 11 }}>{row.desc}</span>
            </div>
          ))}
        </div>
        <div style={{ fontSize: 10, color: C.dim2, fontStyle: 'italic', borderTop: `1px solid ${C.border}`, paddingTop: 10 }}>
          Sequences time out after 1.2s. Disabled when typing in an input.
        </div>
      </div>
    </div>
  );
};

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
