// api-client.js — Poybot live data client
// Exposes window.LiveStore (reactive pub/sub) and window.PoybotAPI (actions)
//
// A9 (2026-05-18) — LiveStore is now a slice-based reactive store, not a
// single-snapshot cache. Each named slice (systemStatus, paperPnL, trades,
// decisions, positions, reconciliation) has its own subscriber set so a
// trade arriving on the WS only re-renders the components that consume the
// `trades` slice — not the whole dashboard.
//
// The legacy single-callback API (store.subscribe(fn) → fn(state)) is
// preserved so non-migrated components keep working: they receive the
// full snapshot exactly as before. The slice API adds:
//   store.subscribeSlice(name, cb) → () => unsubscribe
//   store.getSlice(name)
//
// HTTP polling is preserved as a safety net (typed deltas remain the
// primary source). Frequency adapts: 60s in steady state, 10s if the WS
// has been silent for >60s OR is disconnected.
//
// See docs/ws_contract.md for the typed-delta envelope shape.

(function () {
  const API_BASE  = localStorage.getItem('poybot_base')  || window.location.origin;
  const API_TOKEN = localStorage.getItem('poybot_token') || '';
  const READ_ONLY_ERROR = 'Dashboard is wired in read-only display mode for this backend.';

  // ── Constants ─────────────────────────────────────────────────────────────
  const TRADE_BUFFER_MAX = 200;
  const DECISION_BUFFER_MAX = 200;
  const POSITION_CLOSED_BUFFER_MAX = 100;
  const POLL_INTERVAL_FAST_MS = 10_000;   // WS down / silent → tight fallback
  const POLL_INTERVAL_SLOW_MS = 60_000;   // WS healthy → relaxed safety net
  const WS_SILENCE_THRESHOLD_MS = 60_000; // No typed events for 60s → assume degraded
  const SLICES = [
    'systemStatus',
    'paperPnL',
    'trades',
    'decisions',
    'positions',
    'reconciliation',
  ];

  // ── Normalisation helpers ─────────────────────────────────────────────────
  // Typed-delta trade payloads use strings for Decimal precision; flatten to
  // numbers so the UI doesn't have to remember which fields are stringy.
  const normalizeTradeDelta = trade => {
    if (!trade) return null;
    const num = v => v == null ? null : Number(v);
    return {
      id: trade.id || [
        trade.market_id || 'market',
        trade.token_id || 'token',
        trade.wallet_address || 'wallet',
        trade.side || 'side',
        trade.price != null ? String(trade.price) : 'na',
        trade.size_usdc != null ? String(trade.size_usdc) : 'na',
        trade.time || Date.now(),
      ].join(':'),
      timestamp: trade.time || trade.timestamp || null,
      market_id: trade.market_id || null,
      market_title: trade.market_question || trade.market_title || trade.market_id || 'Unknown market',
      market_category: trade.market_category || null,
      wallet_address: trade.wallet_address || null,
      is_leader: !!trade.is_leader,
      side: trade.side || '—',
      price: num(trade.price),
      notional: num(trade.size_usdc != null ? trade.size_usdc : trade.notional),
      size_usdc: num(trade.size_usdc),
      fees: trade.fees != null ? num(trade.fees) : null,
      pnl_abs: trade.pnl_abs != null ? num(trade.pnl_abs) : null,
      pnl_pct: trade.pnl_pct != null ? num(trade.pnl_pct) : null,
      execution_mode: trade.execution_mode || 'observed',
      status: trade.status || 'observed',
      source: trade.source || null,
      _raw: trade,
    };
  };

  // ── Reactive store with named slices ──────────────────────────────────────
  const store = {
    // Legacy state (preserved verbatim for back-compat with useLiveStore()).
    snapshot: null,
    connectionState: 'connecting', // 'connected' | 'reconnecting' | 'disconnected' | 'connecting'
    reconnectAttempt: 0,
    lastUpdate: null,
    _ls: new Set(),

    // Named slices — each component subscribes to ONLY the slices it consumes.
    slices: {
      systemStatus: null,    // {bot, ws, ingestion, killswitch, reconciliation, execution_mode}
      paperPnL: null,        // {total, win_rate, portfolio_total, open_positions, exec_trades_24h, observed_trades_24h, ...}
      trades: { recent: [] },
      decisions: { recent: [], counters: {} },
      positions: { open: [], closed_recent: [] },
      reconciliation: null,  // {verdict, delta_abs, last_run_ts, sample_size}
    },
    _sliceSubs: new Map(), // Map<sliceName, Set<callback>>

    // Last typed-delta receive time — used to gate the polling adaptive switch.
    _lastTypedDeltaAt: 0,

    // ── Legacy single-callback API (don't break old useLiveStore) ───────────
    subscribe(fn) {
      this._ls.add(fn);
      return () => this._ls.delete(fn);
    },
    _emit() { this._ls.forEach(fn => fn(this)); },
    _set(p) { Object.assign(this, p); this._emit(); },

    // ── Slice API ──────────────────────────────────────────────────────────
    subscribeSlice(name, cb) {
      if (!SLICES.includes(name)) {
        console.warn(`[LiveStore] subscribeSlice: unknown slice "${name}"`);
        return () => {};
      }
      let set = this._sliceSubs.get(name);
      if (!set) { set = new Set(); this._sliceSubs.set(name, set); }
      set.add(cb);
      return () => set.delete(cb);
    },
    getSlice(name) { return this.slices[name]; },

    _notify(name) {
      if (typeof window !== 'undefined' && window.__LIVESTORE_DEBUG__) {
        const count = (this._sliceSubs.get(name)?.size) || 0;
        console.log(`[LiveStore] notify slice="${name}", subscribers=${count}`);
      }
      const set = this._sliceSubs.get(name);
      if (!set) return;
      set.forEach(cb => { try { cb(this.slices[name]); } catch (e) { console.warn('[LiveStore] subscriber threw', e); } });
    },

    // ── Bootstrap from /api/v1/live-summary snapshot ───────────────────────
    processBootstrap(data) {
      this.snapshot = data;
      this.lastUpdate = Date.now();
      this._hydrateSlicesFromSnapshot(data);
      this._emit();
    },

    processTick(p) {
      if (!this.snapshot) { this.processBootstrap(p); return; }
      ['analytics','bot','stats','positions','ingestion',
       'decision_engine','markets','price_history','clock',
       'recent_trades','risk_config','logs','meta',
       'reconciliation','observed_trades_24h','exec_trades_24h',
       'health_pillars','alpha_extras','data_quality_full'].forEach(k => {
        if (p[k] !== undefined) this.snapshot[k] = p[k];
      });
      this.lastUpdate = Date.now();
      this._hydrateSlicesFromSnapshot(this.snapshot);
      this._emit();
    },

    processTrade(trade) {
      // Legacy path — still keeps snapshot.recent_trades in sync for
      // non-migrated components that read it directly.
      if (!this.snapshot || !trade) return;
      this.snapshot.recent_trades = [trade, ...(this.snapshot.recent_trades || [])].slice(0, 100);
      this.lastUpdate = Date.now();
      this._emit();
    },

    // Pull the relevant pieces of the HTTP snapshot into the slice cache.
    // Called on bootstrap and after every tick so the slice API is always
    // in sync with what the polling fallback returned.
    _hydrateSlicesFromSnapshot(snap) {
      if (!snap) return;
      const bot = snap.bot || {};
      const ingestion = snap.ingestion || {};
      const stats = snap.stats || {};
      const recon = snap.reconciliation || null;

      // systemStatus — everything an operator-facing chip/health card needs
      // to render. The shape mirrors the system_status WS payload + the bot
      // and ingestion blocks the HTTP snapshot carries.
      this.slices.systemStatus = {
        bot_status: (bot.bot_status || bot.status || '—').toString().toUpperCase(),
        ws_status: (bot.ws_status || (this.connectionState === 'connected' ? 'LIVE' : this.connectionState === 'disconnected' ? 'DOWN' : 'DEGRADED')).toString().toUpperCase(),
        execution_mode: (bot.execution_mode || 'paper').toLowerCase(),
        execution_enabled: !!bot.execution_enabled,
        killswitch: bot.killswitch_enabled === true || bot.killswitch === true,
        uptime_seconds: bot.uptime_seconds ?? null,
        ws_last_message_age_s: ingestion.ws_last_message_age_s ?? null,
        live_markets: ingestion.live_markets ?? null,
        total_markets: ingestion.total_markets ?? null,
        reconciliation: bot.reconciliation || recon || null,
        pillars: snap.health_pillars || null,
        // A12 — bootstrap maturity. Surfaced by /api/v1/live-summary at
        // `snapshot.bot.maturity` (see _build_bot_payload). The dashboard
        // BootstrapBanner subscribes to systemStatus and reads this field.
        maturity: bot.maturity || null,
      };

      // paperPnL — KPI strip in Alpha Terminal + Sidebar Win Rate / Net PnL.
      // Use the new top-level mirrors when present, fall back to old paths.
      const obs24 = snap.observed_trades_24h ?? stats.observed_trades_24h ?? null;
      const exec24 = snap.exec_trades_24h ?? stats.exec_trades_24h ?? null;
      this.slices.paperPnL = {
        total: stats.total_pnl ?? null,
        win_rate: stats.win_rate ?? null,
        portfolio_total: stats.portfolio_total ?? null,
        pnl_percent: stats.pnl_percent ?? null,
        open_positions: stats.open_positions ?? null,
        capital_in_trade: stats.capital_in_trade ?? null,
        active_markets: stats.active_markets ?? null,
        exec_trades_24h: exec24,
        observed_trades_24h: obs24,
      };

      this.slices.reconciliation = recon || null;

      // Trades / positions are kept hot via WS deltas; on a cold start we
      // seed them from the HTTP snapshot so the UI has something to show.
      if (Array.isArray(snap.recent_trades) && this.slices.trades.recent.length === 0) {
        this.slices.trades = { recent: snap.recent_trades.slice(0, TRADE_BUFFER_MAX) };
      }
      if (Array.isArray(snap.positions) && this.slices.positions.open.length === 0) {
        this.slices.positions = {
          ...this.slices.positions,
          open: snap.positions,
        };
      }

      // Notify every slice on bootstrap — coarse but right (the snapshot
      // touches all of them). Subsequent WS deltas notify only the slice
      // they actually mutate.
      SLICES.forEach(name => this._notify(name));
    },

    // ── WS typed-delta dispatcher ──────────────────────────────────────────
    // Maps the 5 typed event classes (+ legacy snapshot_updated trigger) to
    // their target slice(s) and emits granular notifications. See
    // docs/ws_contract.md § "Type ↔ channel ↔ schema map".
    _dispatchTyped(payload) {
      if (!payload || !payload.type) return;
      this._lastTypedDeltaAt = Date.now();
      const { type, data, ts } = payload;
      switch (type) {
        case 'trade':
          if (!data) break;
          {
            const norm = normalizeTradeDelta(data);
            this.slices.trades = {
              recent: [norm, ...(this.slices.trades.recent || [])].slice(0, TRADE_BUFFER_MAX),
            };
            this._notify('trades');
            // Optimistic firehose counter — sidebar / KPI strip can show
            // movement before the next HTTP poll lands.
            if (this.slices.paperPnL) {
              this.slices.paperPnL = {
                ...this.slices.paperPnL,
                observed_trades_24h: (this.slices.paperPnL.observed_trades_24h || 0) + 1,
              };
              this._notify('paperPnL');
            }
            // Mirror into the legacy snapshot so non-migrated components see
            // the new trade too. No emit — slice notify is the new fast path
            // and we don't want every legacy subscriber to rerun for a tick.
            if (this.snapshot) {
              this.snapshot.recent_trades = [norm, ...(this.snapshot.recent_trades || [])].slice(0, 100);
              this.lastUpdate = Date.now();
            }
          }
          break;

        case 'decision':
          if (!data) break;
          {
            const recent = [data, ...(this.slices.decisions.recent || [])].slice(0, DECISION_BUFFER_MAX);
            const action = (data.action || '').toString().toLowerCase();
            const counters = { ...(this.slices.decisions.counters || {}) };
            if (action) counters[action] = (counters[action] || 0) + 1;
            this.slices.decisions = { recent, counters };
            this._notify('decisions');
          }
          break;

        case 'position_closed':
          if (!data) break;
          {
            this.slices.positions = {
              ...this.slices.positions,
              closed_recent: [data, ...(this.slices.positions.closed_recent || [])].slice(0, POSITION_CLOSED_BUFFER_MAX),
            };
            this._notify('positions');
            // Optimistic PnL update — the next HTTP poll will reconcile.
            // exec_trades_24h is a paper-bot counter; only bump it for paper
            // closes. The strategy field on the payload disambiguates.
            const pnl = Number(data.pnl_usdc || 0);
            if (this.slices.paperPnL) {
              this.slices.paperPnL = {
                ...this.slices.paperPnL,
                total: (this.slices.paperPnL.total || 0) + pnl,
                exec_trades_24h: (this.slices.paperPnL.exec_trades_24h || 0) + 1,
              };
              this._notify('paperPnL');
            }
          }
          break;

        case 'system_status':
          if (!data) break;
          {
            // Merge — the WS payload may not carry the snapshot-only fields
            // (uptime, pillars, etc.). Keep what we already know.
            this.slices.systemStatus = {
              ...(this.slices.systemStatus || {}),
              bot_status: (data.bot || data.bot_status || this.slices.systemStatus?.bot_status || '—').toString().toUpperCase(),
              ws_status: (data.ws || data.ws_status || this.slices.systemStatus?.ws_status || '—').toString().toUpperCase(),
              killswitch: data.killswitch ?? this.slices.systemStatus?.killswitch ?? false,
            };
            this._notify('systemStatus');
          }
          break;

        case 'reconciliation':
          if (!data) break;
          {
            this.slices.reconciliation = data;
            // Mirror into systemStatus so the sidebar RECON chip refreshes
            // without needing a separate subscription.
            if (this.slices.systemStatus) {
              this.slices.systemStatus = {
                ...this.slices.systemStatus,
                reconciliation: data,
              };
              this._notify('systemStatus');
            }
            this._notify('reconciliation');
          }
          break;

        case 'snapshot_updated':
          // Legacy fallback — backend wants the front-end to refetch.
          // Debounced so a noisy producer can't pummel the HTTP path.
          _scheduleHTTPRefresh();
          break;

        default:
          // Unknown typed event — log once for the dev console, don't crash.
          if (typeof window !== 'undefined' && window.__LIVESTORE_DEBUG__) {
            console.warn(`[LiveStore] unhandled WS type="${type}"`, payload);
          }
      }
    },
  };

  // ── HTTP helpers ───────────────────────────────────────────────────────────
  const hdrs = (x = {}) => {
    const h = { 'Content-Type': 'application/json', ...x };
    if (API_TOKEN) h['x-api-token'] = API_TOKEN;
    return h;
  };

  // ETag cache for /api/v1/live-summary so the poll can return 304 when
  // nothing changed — saves a 50-200 KB payload + JSON parse on the hot path.
  let _liveSummaryEtag = null;

  // Snapshot poll. Uses HTTP conditional-GET: send If-None-Match with the
  // previous ETag. On 304, no body is parsed — just refresh lastUpdate. On
  // 200, store the new ETag for the next request.
  const loadSnap = async () => {
    try {
      const headers = hdrs();
      if (_liveSummaryEtag) headers['If-None-Match'] = _liveSummaryEtag;
      const r = await fetch(`${API_BASE}/api/v1/live-summary`, { headers });
      if (r.status === 304) {
        store._set({ lastUpdate: Date.now() });
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const newEtag = r.headers.get('etag');
      if (newEtag) _liveSummaryEtag = newEtag;
      const body = await r.json();
      if (body && body.data) store.processBootstrap(body.data);
    } catch (e) {
      console.warn('[Poybot] poll failed:', e.message);
    }
  };

  // Debounced refresh for the snapshot_updated trigger — never refetch more
  // often than once every 30s, otherwise a burst of typed deltas would also
  // produce a burst of snapshot_updated triggers and we'd thrash the API.
  let _refreshTimer = null;
  let _lastHTTPRefreshAt = 0;
  const _scheduleHTTPRefresh = () => {
    const now = Date.now();
    if (_refreshTimer) return;
    const sinceLast = now - _lastHTTPRefreshAt;
    const wait = sinceLast >= 30_000 ? 0 : 30_000 - sinceLast;
    _refreshTimer = setTimeout(() => {
      _refreshTimer = null;
      _lastHTTPRefreshAt = Date.now();
      loadSnap();
    }, wait);
  };

  // ── WebSocket ──────────────────────────────────────────────────────────────
  let ws = null, rtimer = null, attempts = 0, alive = true;

  const clearT = () => { clearTimeout(rtimer); rtimer = null; };

  const reconn = () => {
    attempts++;
    store._set({ connectionState: 'reconnecting', reconnectAttempt: attempts });
    clearT();
    rtimer = setTimeout(connect, Math.min(1000 * Math.pow(2, attempts - 1), 15000));
  };

  // onEvent splits between the typed-delta path (A8+, type ∈ {trade,
  // decision, position_closed, system_status, reconciliation,
  // snapshot_updated}) and the legacy bootstrap/tick path that the bridge
  // still emits in parallel during the migration window.
  const TYPED_DELTA_TYPES = new Set([
    'trade', 'decision', 'position_closed',
    'system_status', 'reconciliation', 'snapshot_updated',
  ]);

  const onEvent = (msg = {}) => {
    const { type, payload, snapshot, data } = msg;
    // Typed-delta envelopes have type + data and route via the dispatcher.
    if (type && TYPED_DELTA_TYPES.has(type)) {
      store._dispatchTyped(msg);
      return;
    }
    // Legacy envelopes — bootstrap / tick / snapshot-as-payload. Kept so
    // the dashboard doesn't go dark if A8 is rolled back.
    const body = payload !== undefined ? payload : data;
    if (snapshot) store.processBootstrap(snapshot);
    if      (type === 'bootstrap' || type === 'control') { if (body) store.processBootstrap(body); }
    else if (type === 'tick' || type === 'stats')        { if (body) store.processTick(body); }
    else if ((type === 'trade_closed') && !snapshot && body) store.processTrade(body);
  };

  function connect() {
    if (!alive) return;
    try { ws = new WebSocket(`${API_BASE.replace(/^http/, 'ws')}/ws/live`); }
    catch (e) { reconn(); return; }
    ws.onopen    = () => {
      attempts = 0;
      store._lastTypedDeltaAt = Date.now(); // pretend we heard something
      store._set({ connectionState: 'connected', reconnectAttempt: 0 });
    };
    ws.onmessage = e  => { try { onEvent(JSON.parse(e.data)); } catch (_) {} };
    ws.onerror   = ()  => {};
    ws.onclose   = ()  => { if (alive) reconn(); else store._set({ connectionState: 'disconnected' }); };
  }

  // ── Adaptive polling ───────────────────────────────────────────────────────
  // The polling fallback isn't the source of truth anymore — typed deltas
  // are. Slow down to 60s when the WS is healthy; speed up to 10s if the
  // WS has been silent for >60s or is disconnected, so the dashboard still
  // catches up via HTTP if the typed pipeline stalls.
  const pickInterval = () => {
    if (store.connectionState !== 'connected') return POLL_INTERVAL_FAST_MS;
    const silentFor = Date.now() - (store._lastTypedDeltaAt || 0);
    if (silentFor > WS_SILENCE_THRESHOLD_MS) return POLL_INTERVAL_FAST_MS;
    return POLL_INTERVAL_SLOW_MS;
  };

  const poll = () => {
    if (!alive) return;
    loadSnap().finally(() => alive && setTimeout(poll, pickInterval()));
  };

  // ── Public API actions ─────────────────────────────────────────────────────
  // PLAN-UIA-001: honest controls. Three distinct verbs that map to two
  // distinct backend endpoints:
  //   enable  → POST /api/control/killswitch {enabled: true}
  //   disable → POST /api/control/killswitch {enabled: false}
  //   halt    → POST /api/control/halt  (killswitch + force_close_all_positions)
  window.PoybotAPI = {
    botControl: async (cmd) => {
      if (cmd === 'halt') {
        const r = await fetch(`${API_BASE}/api/control/halt`, {
          method: 'POST',
          headers: hdrs(),
          body: JSON.stringify({ reason: 'dashboard:halt', actor: 'dashboard' }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        loadSnap();
        return r.json();
      }
      const enabledByCmd = { enable: true, disable: false };
      if (!(cmd in enabledByCmd)) throw new Error(`Unknown control command: ${cmd}`);
      const r = await fetch(`${API_BASE}/api/control/killswitch`, {
        method: 'POST',
        headers: hdrs(),
        body: JSON.stringify({ enabled: enabledByCmd[cmd], reason: `dashboard:${cmd}`, actor: 'dashboard' }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      loadSnap();
      return r.json();
    },
    updateConfig: async (edits) => {
      const r = await fetch(`${API_BASE}/api/risk/update`, {
        method: 'POST',
        headers: hdrs(),
        body: JSON.stringify({ edits, actor: 'dashboard' }),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const body = await r.json();
          if (body.detail) msg += `: ${body.detail}`;
        } catch (_) {}
        throw new Error(msg);
      }
      loadSnap();
      return r.json();
    },
    closePosition: async () => { throw new Error(READ_ONLY_ERROR); },
    pnlTimeframe: async () => { throw new Error(READ_ONLY_ERROR); },
    runBacktest: async () => { throw new Error(READ_ONLY_ERROR); },
    getSettings:    () => ({ API_BASE, API_TOKEN }),
    setSettings: (base, token) => {
      localStorage.setItem('poybot_base',  base);
      if (token != null) localStorage.setItem('poybot_token', token);
      location.reload();
    },
  };

  // ── Init ──────────────────────────────────────────────────────────────────
  store._set({ connectionState: 'connecting' });
  loadSnap();
  connect();
  setTimeout(poll, POLL_INTERVAL_SLOW_MS);

  // Test hook — expose the dispatcher so tests/dev tools can simulate a WS
  // event without standing up a server. Production code reaches it only via
  // window.__LIVESTORE_DEBUG__.
  store.__test = {
    dispatchTyped: (payload) => store._dispatchTyped(payload),
    normalizeTradeDelta,
    SLICES,
  };

  window.LiveStore = store;
})();
